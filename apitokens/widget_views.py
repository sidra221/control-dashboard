import json
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ApiToken, Conversation, Product
from .services import catalog as catalog_service
from .services import kieai, llm

logger = logging.getLogger(__name__)

MAX_MESSAGES = 30
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_CHARS = 2000
MAX_RECOMMENDATIONS_PER_TURN = 3
MAX_SHOWN_SKUS = 50
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
WIDGET_EVENTS = {"buy_click": "buy_clicks", "add_to_cart": "add_to_carts"}

# يلتقط رقم بعد كلمة بتدل على ميزانية صريحة (مش أي رقم عشوائي قرب كلمة
# "دولار" — هيك بنتجنب نلتقط سعر منافس أو سعر منتج عادي عن طريق الخطأ
# كأنه ميزانية الزبون).
BUDGET_PATTERN = re.compile(
    r"(?:ميزانيتي|ميزانيه|ميزانيتى|مش\s*اكتر\s*من|مو\s*اكتر\s*من|ما\s*بدي\s*ادفع\s*اكتر\s*من|"
    r"اقل\s*من|أقل\s*من|لا\s*يتجاوز|ما\s*يتجاوز|ضمن|budget)\D{0,15}?(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

# علامات لغوية ضيّقة ومحدّدة لكلام الخصم — نطاق صغير ومقصود (خصم/كود/%)
# حتى ما نحجب رسائل طبيعية ما إلها علاقة بالخصومات أبداً.
DISCOUNT_FABRICATION_MARKERS = ("خصم", "كود", "%", "discount", "coupon", "promo code")

# الزبون بدو يشتري منتج سبق وانعرض — هون بس منسمح بإعادة عرض نفس الكرت
# (لأنه هو اللي فيه زر/رابط الشراء).
PURCHASE_INTENT_MARKERS = (
    "كيف بطلبه", "كيف اطلبه", "كيف أطلبه", "كيف بطلب", "الرابط", "بدي اشتريه", "بدي أشتريه",
    "كيف اشتريه", "كمل الدفع", "اكمل الدفع", "أكمل الدفع", "للسلة", "بدي اطلبه", "ابي اطلبه",
    "أبي أطلبه", "عايز اطلبه", "عايزة اطلبه", "كيف اطلب", "how do i buy", "how to buy",
    "how do i order", "how to order", "checkout", "add to cart", "add it to", "send me the link",
    "the link", "buy it", "order it", "i'll take it",
)

# نصوص بيولّدها الكود نفسه (مش الموديل) — منختار نسختها حسب لغة آخر رسالة
# من الزبون حتى ما يطلعله زر أو رد بالعربي وهو عم يحكي إنجليزي.
TRYON_BUTTON_LABELS = {
    "ar": {"self": "جرّب شكلك فيه 📸", "gift": "جرّب شكله على صاحب الهدية 📸"},
    "en": {"self": "Try it on with your photo 📸", "gift": "See it on the gift recipient 📸"},
}
FALLBACK_DISCOUNT_REPLY = {
    "ar": "خلينا نسهّلها عليك — استخدم كود {code} وخود {description}! 🎁",
    "en": "Let's make it easier for you — use code {code} for {description}! 🎁",
}
DISCOUNT_APPENDIX = {
    "ar": "وعشان نسهّلها عليك: استخدم كود {code} ({description}) 🎁",
    "en": "And to make it easier: use code {code} ({description}) 🎁",
}
FALLBACK_REPHRASE_REPLY = {
    "ar": "ممكن تعيد سؤالك بطريقة تانية؟",
    "en": "Could you rephrase that for me?",
}

ARABIC_CHAR_PATTERN = re.compile(r"[؀-ۿ]")
# عربي مكتوب بحروف لاتينية: أرقام بدل حروف (3=ع، 7=ح، 2=ء...) جوا الكلمة، أو كلمات شائعة.
ARABIZI_DIGIT_PATTERN = re.compile(r"[a-z][2356789]|[2356789][a-z]", re.IGNORECASE)
# 2nd، 3rd، 4k، 5pm... — أرقام إنجليزية عادية مش Arabizi
ENGLISH_NUMBER_WORD = re.compile(r"^\d+(st|nd|rd|th|s|k|m|x|pm|am|cm|kg|g|ml|l|gb|mb)?$")
ARABIZI_WORDS = {
    "baddi", "badde", "bade", "biddi", "shu", "shou", "kifak", "kifik", "marhaba", "mar7aba",
    "ahlan", "wesh", "wsh", "abi", "abgha", "3ayez", "3ayza", "ayez", "mesh", "msh", "keda",
    "ezayak", "habibi", "yalla", "inshallah", "mafi", "fi", "3ndkon", "3andkon", "3ndak",
    "fostan", "ma3", "kteer", "ktir", "halla2", "hala", "zay", "bkam", "addesh", "ade",
}


def _is_arabic_text(text):
    """عربي بحروف عربية، أو Arabizi (عربي بحروف لاتينية)."""
    if ARABIC_CHAR_PATTERN.search(text):
        return True
    words = re.findall(r"[a-z0-9']+", text.lower())
    score = 0
    for w in words:
        if ARABIZI_DIGIT_PATTERN.search(w) and not ENGLISH_NUMBER_WORD.match(w):
            score += 2  # رقم مكان حرف (3ndkon، a7mar) — إشارة قوية
        elif w in ARABIZI_WORDS:
            score += 1
    return score >= 2 or (score == 1 and len(words) <= 2)


def _customer_lang(messages):
    """عربي إذا آخر رسالة من الزبون عربية (بأي حروف)، وإلا إنجليزي."""
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    return "ar" if _is_arabic_text(last_user) else "en"

RECOMMEND_TOOL = {
    "name": "recommend_product",
    "description": (
        "Show the customer ONE specific product from the catalog. Call this the moment you decide to "
        "name or describe a specific product to the customer — including when it's only the closest "
        "alternative, not an exact match. Never describe a specific product by name or detail in your "
        "plain text without calling this in the very same turn. Forbidden: ending your reply with a "
        "yes/no permission question instead of calling this — e.g. \"Want to see it?\", \"Shall I show "
        "it to you?\", \"Would you like to take a look?\", or the equivalent in any language. If you find "
        "yourself about to write a question like that, call this function instead of writing it. "
        "Calling this is low-commitment: it only displays a product card for the customer to look at, "
        "it does not finalize anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sku": {
                "type": "string",
                "description": "The exact sku of the recommended product, copied exactly from the catalog.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "A short, persuasive, benefit-led reason this product fits, written strictly in "
                    "the customer's own language and dialect (the same ones as your chat reply), "
                    "with no markdown formatting (no bold, headings, or lists — plain text only). "
                    "The first time you show a product, also invite them to upload a photo to try it "
                    "on — don't repeat that same invitation on every card. If the conversation "
                    "already established this is for someone else (a gift, etc.), any try-on invitation MUST "
                    "refer to that person specifically, not to the customer — never say 'your photo' or "
                    "'how it looks on you' when it's not for them."
                ),
            },
            "for_someone_else": {
                "type": "boolean",
                "description": (
                    "True if this product is for someone other than the customer (a gift, a family "
                    "member, etc.), false if it's for the customer themselves. Defaults to false. Once "
                    "the conversation establishes this is for someone else, keep passing true on EVERY "
                    "later call too (even when recommending a different/alternative product) — don't "
                    "reset it to false just because this is a later message."
                ),
            },
            "tryon_button_label": {
                "type": "string",
                "description": (
                    "Very short label (2-6 words, may end with 📸) for the button that lets the customer "
                    "upload a photo to try this product on, written strictly in the customer's own "
                    "language and dialect. If it's for someone else, word it for trying it on that "
                    "person, not the customer."
                ),
            },
        },
        "required": ["sku", "reason"],
    },
}

DISCOUNT_TOOL = {
    "name": "offer_discount_code",
    "description": (
        "Offer one active, genuinely applicable discount code to the customer. Call this when you "
        "recognize a closing moment where a discount actually helps: a price objection, hesitation "
        "after seeing a specific product, a competitor-price comparison, or a customer close to "
        "buying who asks for a better price. Don't call it for a customer who has already decided "
        "to buy with no price concern — they don't need it. Don't reveal a code to every browsing customer "
        "up front — use it as a deliberate closing move, not a default greeting. If more than one "
        "code is active, pick only the one whose own description genuinely fits this product/customer "
        "(e.g. a code described as shirts-only should not be offered for a dress). Call it at most "
        "once per conversation, and only with a code+description that actually exist in the active "
        "codes list below — never invent a code, percentage, promotion, expiry date, or eligibility "
        "condition not present in that list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The exact discount code, copied exactly from the active codes list.",
            },
            "pitch": {
                "type": "string",
                "description": (
                    "Your full closing message that goes with the code, strictly in the customer's own "
                    "language and dialect, plain text: acknowledge their hesitation in a few words, one "
                    "concrete sentence on the product's real value, then present the code as the reason "
                    "to go ahead now. Never just the code alone."
                ),
            },
        },
        "required": ["code", "pitch"],
    },
}

SYSTEM_PROMPT_TEMPLATE = """You are the top-performing salesperson of an online store, working inside \
the chat widget on its website. You are sharp, warm, confident and genuinely persuasive: you read the \
customer quickly, figure out what they really want, and guide them to a product they're excited to buy. \
You sell the way the best in-store stylist does — by understanding the person and making them feel \
good about the choice — never by pressure, tricks or invented facts.

LANGUAGE AND DIALECT (highest priority — this overrides every example in these instructions)
These instructions are written in English for you only. The customer never sees them, and their language \
says nothing about the language you reply in.
- Reply ONLY in the language the customer is using in their latest message. Every word you write — the \
chat reply AND the `reason` field of every tool call — must be in that language. Never mix in English \
or any other language unless the customer does so themselves.
- Match their dialect and register, not just the language. If they write Arabic, first identify their \
dialect from their own words, then write EVERYTHING (chat reply and every `reason`) in that same \
dialect with its everyday vocabulary — never drift into Modern Standard Arabic or into a different \
dialect. Typical cues: Gulf uses words like "abi", "wesh", "zain", "7eil"; Egyptian "3ayez", "eh", \
"keda", "awi", "ha-" for the future; Levantine "baddi", "shu", "halla2", "ktir", "b-" verbs; Iraqi \
"aku", "shaku", "hwaya"; Maghrebi "bghit", "wash", "bzaf". Use MSA only if the customer writes in \
MSA. Talk like a friendly local salesperson from their own region, not like a textbook. The same \
applies to other languages (casual vs. formal English, Brazilian vs. European Portuguese, etc.).
- Keep each reply in ONE script and never mix Arabic and Latin letters inside it. If they write Arabic \
in Latin letters and numbers (Arabizi, e.g. "kifak", "3ndkon", "baddi"), they are speaking Arabic: \
reply in their dialect written in normal Arabic script (every word in Arabic letters, no Latin words).
- If they switch language or dialect mid-conversation, switch with them from that message on.
- Any example phrase in these instructions only illustrates the intent. Never copy it literally — \
express the same idea naturally in the customer's own language and dialect.
- Grammatical gender (Arabic and other gendered languages): match the gender the customer reveals \
about themselves in their own words (e.g. a feminine vs. masculine form of "I'd like" or "I'm sure"), \
and keep addressing them that way. Never guess a gender with no signal; until one appears, use the \
gender-neutral wording (phrasing that avoids gendered verb forms, or polite plural/impersonal forms); \
if that's impossible in their language, use the standard generic masculine. What they're shopping for \
is NOT a signal (a man can buy a dress as a gift) — only how they refer to themselves counts.

HOW YOU SELL
1. Discover fast. Have a short, natural conversation to learn what they want: type of item, style, \
occasion, color, budget, size if relevant. Ask at most TWO short follow-up questions in total — never \
more, and never several questions in one message. Prioritize whichever you don't know yet: category, \
color, occasion. If they've already given you enough — or the catalog has one item that clearly fits \
what they said — recommend it right away and ask any follow-up AFTER showing it. Showing a good \
product beats asking a question.
2. Gifts and other people. If they say it's for someone else (a gift, a family member, anyone other \
than themselves), remember it for the REST of the conversation: every mention of trying it on or how \
it will look must refer to that person (by name or relation if given), never to the customer, until \
they say otherwise. Attending someone else's event (a friend's wedding, a colleague's party) does \
NOT make it a gift — the item is still for the customer to wear unless they say otherwise.
3. Show, don't just talk. As soon as you're reasonably confident about ONE product from the catalog \
below — or the moment the customer agrees to see, or asks about, a product you already named — call \
`recommend_product` in that same reply. The tool call is what actually shows the product card and the \
try-on button, so never describe a product in plain text while holding off on the call, and never end \
with a yes/no permission question like "Would you like to see it?" — just show it. The card appears \
together with your message, so never announce it ("let me show you", "one moment") — just talk about \
the product as if it's already in front of them.
4. Sell the benefit, not the spec. Every pitch has three beats, in one to three short sentences: \
(a) a hook tied to THEIR situation (the occasion, the person, the feeling they want), (b) one or two \
concrete real details from the product's description turned into what they get from them ("the wrap \
waist cinches you in, so it flatters without effort"), and (c) a confident next step. Sound like you \
genuinely love this pick for them. Vary the next step instead of repeating the same line every time: \
the first time you show a product, invite them to try it on with a photo; after that, prefer \
pointing them to the buy button, completing the look with one real matching item, or a real sale \
(`was_price`).
5. Read their reaction and keep momentum.
   - If they want something different, don't apologize — confidently present a DIFFERENT product that \
fits better, with enthusiasm ("How about this one?"). Never say "we don't have exactly what you want" \
when you're simply pivoting; that wording belongs only to rule 7.
   - If they react to, or ask about, a product you ALREADY showed (a question about its size, price, \
fabric, or "it's nice but..."), answer in your plain-text reply and do NOT call `recommend_product` \
for it — its card is already on screen. If they sound lukewarm or unsure, don't drop it: close the \
sale on that item — reassure, highlight the benefit that matters most to them, and use rule 10 if a \
discount applies.
   - The products already shown in this conversation are listed at the end of these instructions. \
Never call `recommend_product` for one of them again, except in the purchase case right below.
   - Exception to "never show the same sku twice": if they ask how to buy, order, check out, get the \
link, or add a product you already showed to the cart, call `recommend_product` again for that exact \
sku — this is how the system surfaces their purchase link/button. In the `reason`, point them straight \
to the button/link below instead of re-describing the product. Outside this case, never recommend the \
same sku twice in one conversation.
   - Keep offering strong alternatives until they're happy or the catalog genuinely has nothing close.
6. Close like a pro. When they show interest, make the next step obvious and easy — use an assumptive, \
friendly close (point them to the buy button, or to trying it on now) rather than asking whether \
they're interested. Once they've decided on an item, you may suggest ONE real complementary product \
from the catalog that completes the look — only if it genuinely fits, and never in a way that delays \
their purchase.
7. No exact match. Never just say the store doesn't have it and stop, and never ask permission to show \
something else. In that SAME reply, call `recommend_product` for the closest real alternative and \
present it as a confident suggestion stated as fact. Example — they ask for a green dress and there \
isn't one, but there's a sage-green dress: WRONG is "We don't have green, but we have sage green — want \
to see it?" (a question). RIGHT is calling `recommend_product` for the sage-green dress immediately, \
with a reason like "We don't have pure green right now, but this sage-green one is gorgeous and would \
look stunning for your occasion — have a look!" Only skip an alternative if nothing in the catalog is \
even loosely related.
8. Handle objections with value. On price, doubt or comparison with another store, your reply must: \
acknowledge it in a few words (empathy, not apology), then give the product's real value in one \
concrete sentence (quality, versatility, how many occasions it covers, a real sale), then the closer — \
a discount code if rule 10 allows one, otherwise a genuinely cheaper real alternative. Never answer an \
objection with only a code. An objection is never a reason to end the conversation or give up.
8b. If they ask about discounts or offers before choosing anything, don't reveal a code yet: tell them \
warmly you'll get them the best deal once they find the right piece, mention any real sale items \
(`was_price`), and ask what they're looking for.

HONESTY (never break these — a persuasive salesperson never lies)
9. Only recommend products that exist in the catalog below, using their exact sku. Never invent \
products, skus, prices, materials, features or other details that aren't in the catalog. Every \
product in the catalog is in stock right now. `available_options` lists the sizes/colors/etc. that are \
actually available — use it to answer size or color questions precisely, and never offer an option \
that isn't listed. `was_price` is the product's real original price: when present the item is genuinely \
on sale and you may use that real saving as a selling point. Use the `description` for real details \
(fabric, fit, features). Never claim an item is a bestseller, popular, trending, low in stock or ending \
soon — the store gives you no such data. Build urgency and desire only from real benefits and real \
sales, never from invented scarcity.
10. Discount codes. If the store has active discount codes listed below, reveal one deliberately as a \
closing move — not to every browsing customer. Use it only where a discount actually helps: a price \
objection ("too expensive"), lukewarm hesitation about a specific product already shown ("it's okay", \
"not sure"), a comparison with a competitor's price, or a customer who's close to buying but asks for a \
better price. A customer who has already decided to buy with no price concern ("I'll take it", "I want \
to order it now") does NOT need a discount — don't give away margin there; just help them complete the \
purchase (point them to the product's buy button). In the hesitation or \
objection case, use the code AS your argument to win them over on that same item rather than pivoting \
away. If several codes are active, offer only the one whose own description genuinely applies to this \
product or customer. Call `offer_discount_code` with the exact code, at most once per conversation, \
and never before the customer has engaged with a specific product (a generic "do you have discounts?" \
with nothing shown yet doesn't count). Never invent a code, percentage, promotion, expiry date or \
eligibility condition that isn't in the active codes list. If no code applies — including when none are \
listed at all — never call this tool or mention a discount; keep selling with value or a real \
alternative instead.
11. Shipping, delivery times, returns/exchanges, payment methods and size guidance: answer ONLY from \
the "Store information" section below (and each product's `available_options`). Summarize what it says \
briefly in the customer's language, then steer back to the product. If the answer isn't there (or there \
is no such section), say honestly in one short line that you don't have that detail right now — never \
invent a delivery date, fee, return guarantee or size measurement — and immediately pivot back to being \
useful. Don't sound robotic and don't treat it as the end of the conversation.
11b. Every informational answer (shipping, returns, sizes, fabric, how to buy) still ends with a short, \
confident nudge toward the sale, tied to what they asked — e.g. after sizes: the size that's available \
and that it's ready to order from the button; after returns: that this makes trying it risk-free; \
after "how do I buy": that it's one tap away and a word on why it's a great pick. One short sentence, \
never pushy.
12. When you recommend more than one product in the same reply (they asked for "options", "the best \
two", etc.), every one must satisfy every constraint already confirmed in the conversation — same \
color/style family, same category, and within any stated budget. Never add a second item just for \
variety if it breaks a confirmed constraint; one correct item beats two where one is wrong.

STYLE
13. Keep replies short, warm and natural — this is a live chat bubble, not an essay. Usually one to \
three short sentences. Write the way a friendly salesperson texts a customer. Never use markdown: no \
**bold**, no # headings, no bullet or numbered lists — plain sentences only. A well-placed emoji is \
fine; don't overdo it.

Store catalog — the in-stock products most relevant to this conversation (JSON array). Prices are \
in {currency}. If the customer asks for something that isn't here, recommend the closest item that is.
{catalog_json}
{store_info_section}{discount_codes_section}{shown_section}"""


def _cors_response(data, status=200):
    response = JsonResponse(data, status=status)
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Token"
    response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response


def _extract_budget_ceiling(messages):
    """بتدور على آخر رقم ميزانية صريح ذكره الزبون (مش أي رقم عشوائي) —
    آخر وحد بالترتيب لأنه التفضيل الأحدث لازم يغلب القديم.
    """
    ceiling = None
    for m in messages:
        if m["role"] != "user":
            continue
        match = BUDGET_PATTERN.search(m["content"])
        if match:
            ceiling = float(match.group(1))
    return ceiling


def _process_tool_calls(
    tool_calls, products, discount_codes, lang="ar", blocked_skus=(), currency=""
):
    """بتعالج استدعاءات الأدوات يلي رجعت من الموديل لقائمة توصيات
    (بدون تكرار نفس الـ sku، وبنفس ترتيب الموديل) + عرض خصم واحد كحد أقصى.
    blocked_skus: منتجات سبق وانعرضت بالمحادثة — ما منعيد كرتها.
    """
    recommendations = []
    seen_skus = set(blocked_skus)
    discount_offer = None
    blocked_reason = ""
    for call in tool_calls:
        call_input = call.get("input") or {}
        if call["name"] == "recommend_product":
            sku = call_input.get("sku", "")
            if sku in seen_skus:
                # الموديل أحياناً بيحط جوابه كله بالـ reason تبع كرت مكرر —
                # منشيل الكرت بس منحتفظ بالنص حتى ما يضيع الرد.
                blocked_reason = blocked_reason or call_input.get("reason", "")
                continue
            product = next((p for p in products if p.sku == sku), None)
            if product is not None:
                seen_skus.add(sku)
                for_someone_else = bool(call_input.get("for_someone_else"))
                recommendations.append(
                    {
                        "sku": product.sku,
                        "title": product.title,
                        "price": str(product.price),
                        "compare_at_price": (
                            str(product.compare_at_price) if product.compare_at_price else None
                        ),
                        "currency": currency,
                        "image_url": product.image_url,
                        "product_url": product.product_url,
                        "variant_id": product.variant_id,
                        "reason": call_input.get("reason", ""),
                        "tryon_button_label": (
                            (call_input.get("tryon_button_label") or "").strip()[:40]
                            or TRYON_BUTTON_LABELS[lang]["gift" if for_someone_else else "self"]
                        ),
                    }
                )
        elif call["name"] == "offer_discount_code" and discount_offer is None:
            code_value = call_input.get("code", "")
            code_obj = next((d for d in discount_codes if d.code == code_value), None)
            if code_obj is not None:
                discount_offer = {"code": code_obj.code, "description": code_obj.description}
                blocked_reason = blocked_reason or (call_input.get("pitch") or "").strip()
    return recommendations[:MAX_RECOMMENDATIONS_PER_TURN], discount_offer, blocked_reason


def _apply_budget(recommendations, budget_ceiling):
    # يحافظ على ميزانية الزبون لو ذكرها صراحة: بيشيل أي توصية فوق السقف،
    # إلا إذا هيك رح يصفّي كل التوصيات — ساعتها منفضّل نضل مع خيار وحد
    # (ولو فوق الميزانية شوي) عن ما نرجّع رد فاضي بالكامل للزبون.
    if budget_ceiling is not None and len(recommendations) > 1:
        within_budget = [r for r in recommendations if float(r["price"]) <= budget_ceiling]
        if within_budget:
            return within_budget
    return recommendations


def _script_mismatch(customer_text, reply_text):
    """True لو الزبون كتب بحروف عربية والرد ما فيه ولا حرف عربي، أو العكس."""
    customer_arabic = _is_arabic_text(customer_text)
    reply_arabic = bool(ARABIC_CHAR_PATTERN.search(reply_text))
    if not any(ch.isalpha() for ch in customer_text):
        return False  # إيموجي أو أرقام بس — ما في لغة نقارن فيها
    return customer_arabic != reply_arabic


def _has_purchase_intent(text):
    lowered = text.lower()
    return any(marker in lowered for marker in PURCHASE_INTENT_MARKERS)


def _clean_session_id(value):
    value = (value or "").strip() if isinstance(value, str) else ""
    return value if SESSION_ID_PATTERN.match(value) else ""


def _log_conversation(merchant, session_id, messages, reply_text, recommendations, discount_offer):
    """تسجيل المحادثة للقياس — فشله (مثلاً قاعدة بيانات مقفولة) ما لازم أبداً
    يمنع الزبون من استلام الرد."""
    if not session_id:
        return
    try:
        _save_conversation(merchant, session_id, messages, reply_text, recommendations, discount_offer)
    except DatabaseError:
        logger.exception("conversation_log_failed merchant=%s", merchant.pk)


def _save_conversation(merchant, session_id, messages, reply_text, recommendations, discount_offer):
    conversation, _ = Conversation.objects.get_or_create(merchant=merchant, session_id=session_id)
    conversation.messages = messages + [{"role": "assistant", "content": reply_text}]
    skus = list(conversation.recommended_skus)
    for rec in recommendations:
        if rec["sku"] not in skus:
            skus.append(rec["sku"])
    conversation.recommended_skus = skus
    if discount_offer and not conversation.discount_code:
        conversation.discount_code = discount_offer["code"]
    conversation.save()


def _authenticate(request):
    """برجّع (merchant, None) أو (None, response) جاهز للرجوع."""
    token_value = request.headers.get("X-Api-Token", "").strip()
    if not token_value:
        return None, _cors_response({"ok": False, "error": "missing_token"}, status=401)
    merchant = ApiToken.objects.filter(token=token_value).first()
    if merchant is None:
        return None, _cors_response({"ok": False, "error": "invalid_token"}, status=401)
    if not merchant.is_active:
        return None, _cors_response({"ok": False, "error": "token_disabled"}, status=403)
    return merchant, None


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def chat(request):
    if request.method == "OPTIONS":
        return _cors_response({}, status=200)

    merchant, error_response = _authenticate(request)
    if error_response:
        return error_response

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return _cors_response({"ok": False, "error": "invalid_json"}, status=400)

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _cors_response({"ok": False, "error": "messages_required"}, status=400)
    if len(messages) > MAX_MESSAGES:
        return _cors_response({"ok": False, "error": "too_many_messages"}, status=400)

    clean_messages = []
    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            return _cors_response({"ok": False, "error": "invalid_message"}, status=400)
        clean_messages.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})

    if clean_messages[-1]["role"] != "user":
        return _cors_response({"ok": False, "error": "last_message_must_be_user"}, status=400)

    session_id = _clean_session_id(body.get("session_id"))
    raw_shown = body.get("shown_skus") if isinstance(body.get("shown_skus"), list) else []
    shown_skus = [str(s)[:64] for s in raw_shown if isinstance(s, (str, int))][:MAX_SHOWN_SKUS]

    provider = llm.get_provider()
    if not provider.is_configured():
        return _cors_response({"ok": False, "error": "assistant_not_configured"}, status=503)

    products = catalog_service.select_products(merchant, clean_messages, shown_skus)
    discount_codes = list(merchant.discount_codes.filter(is_active=True))
    discount_codes_section = ""
    tools = [RECOMMEND_TOOL]
    if discount_codes:
        codes_json = json.dumps(
            [{"code": d.code, "description": d.description} for d in discount_codes],
            ensure_ascii=False,
        )
        discount_codes_section = (
            "\nActive discount codes (see rule 10 for when to offer one):\n" + codes_json + "\n"
        )
        tools.append(DISCOUNT_TOOL)

    store_info_section = ""
    if merchant.store_info.strip():
        store_info_section = (
            "\nStore information (shipping, returns, payments, sizing — written by the store; "
            "it may be in any language, always answer in the customer's):\n"
            + merchant.store_info.strip()[:6000] + "\n"
        )
    shown_section = ""
    if shown_skus:
        shown_section = (
            "\nProducts already shown in this conversation (don't show them again unless the "
            "customer wants to buy one): " + ", ".join(shown_skus) + "\n"
        )

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        catalog_json=json.dumps(
            catalog_service.catalog_for_prompt(products, shown_skus), ensure_ascii=False
        ),
        currency=merchant.currency or "the store's currency",
        store_info_section=store_info_section,
        discount_codes_section=discount_codes_section,
        shown_section=shown_section,
    )

    try:
        result = llm.chat_with_retry(provider, system_prompt, clean_messages, tools)
    except llm.LLMError:
        return _cors_response({"ok": False, "error": "assistant_unavailable"}, status=502)

    last_user_text = clean_messages[-1]["content"]
    purchase_intent = _has_purchase_intent(last_user_text)
    # منتج سبق وانعرض ما منعيد كرته — إلا لما الزبون بدو يشتريه (الكرت فيه زر الشراء).
    blocked_skus = () if purchase_intent else shown_skus

    # بعض الموديلات أحياناً بتذكر منتج أو كود خصم بالنص العادي بدل ما تستخدم
    # الأداة المناسبة، رغم تعليمات واضحة بالبرومبت — هاد بيمنع ظهور كارت
    # المنتج/زر التجربة أو حقل discount_offer نهائياً. لما نكتشف هيك، منعيد
    # الطلب مع إجبار نفس الأداة، ومنحافظ على نص الرد الأصلي ونلزق عليه نتيجة الأداة.
    has_recommendation = any(c["name"] == "recommend_product" for c in result.tool_calls)
    names_a_product = any(
        p.title.lower() in result.reply_text.lower() for p in products if p.sku not in blocked_skus
    )
    force_tool = None
    if result.tool_calls and not has_recommendation and names_a_product:
        # استدعى أداة تانية (غالباً الخصم) وسمّى منتج بالنص بدون كرت.
        force_tool = "recommend_product"
    elif not result.tool_calls:
        if purchase_intent:
            force_tool = "recommend_product"
        elif any(d.code in result.reply_text for d in discount_codes):
            force_tool = "offer_discount_code"
        else:
            no_exact_match_phrases = ("ما عنا", "ما عندنا", "ما في", "لا يوجد", "ليس لدينا", "للأسف")
            if any(
                p.title.lower() in result.reply_text.lower()
                for p in products
                if p.sku not in blocked_skus
            ) or any(phrase in result.reply_text for phrase in no_exact_match_phrases):
                force_tool = "recommend_product"

    if force_tool:
        original_reply_text = result.reply_text
        try:
            forced_result = llm.chat_with_retry(
                provider,
                system_prompt,
                clean_messages + [{"role": "assistant", "content": original_reply_text}],
                tools,
                force_tool=force_tool,
            )
            result = llm.LLMResult(
                reply_text=original_reply_text,
                tool_calls=result.tool_calls + forced_result.tool_calls,
            )
        except llm.LLMError:
            pass  # نضل مع الرد الأصلي بدل ما نفشّل الطلب كله

    lang = _customer_lang(clean_messages)
    recommendations, discount_offer, blocked_reason = _process_tool_calls(
        result.tool_calls, products, discount_codes, lang, blocked_skus, merchant.currency
    )
    reply_text = result.reply_text

    budget_ceiling = _extract_budget_ceiling(clean_messages)
    recommendations = _apply_budget(recommendations, budget_ceiling)

    # لو الموديل ذكر كلام خصم بالنص العادي بدون ما ينتج discount_offer حقيقي
    # من كود فعلي موجود — هاد اختلاق، مش مجرد نسيان استخدام الأداة (هيك مغطّى
    # فوق بـ force_tool). منعيد الطلب مرة وحدة مع تصحيح صريح إنه ما في خصم
    # فعلي، ومنستخدم نتيجة هالمحاولة كاملة بدل الرد الأصلي المختلق.
    # استثناء: تخفيض حقيقي على منتج (was_price) مش اختلاق.
    has_real_sale = any(r["compare_at_price"] for r in recommendations)
    if (
        discount_offer is None
        and not has_real_sale
        and any(marker in reply_text.lower() for marker in DISCOUNT_FABRICATION_MARKERS)
    ):
        corrected_system_prompt = system_prompt + (
            "\n\nCORRECTION — read before replying: your previous reply mentioned a discount, "
            "code, or percentage-off, but no such offer actually applies to this customer right "
            "now. Rewrite your reply to their last message WITHOUT mentioning any discount, code, "
            "or percentage — address what they actually asked using the product's real value, or "
            "a genuine alternative, instead."
        )
        try:
            corrected = llm.chat_with_retry(provider, corrected_system_prompt, clean_messages, tools)
            recommendations, discount_offer, blocked_reason = _process_tool_calls(
                corrected.tool_calls, products, discount_codes, lang, blocked_skus, merchant.currency
            )
            recommendations = _apply_budget(recommendations, budget_ceiling)
            reply_text = corrected.reply_text or reply_text
        except llm.LLMError:
            pass  # نضل مع الرد الأصلي بدل ما نفشّل الطلب كله

    # شبكة أمان للغة: زبون كاتب بحروف عربية لازم يجيه رد فيه حروف عربية، والعكس.
    # (الموديلات الرخيصة نادراً بتغلط وبترد بلغة تانية.) منعيد مرة وحدة بتصحيح صريح.
    if reply_text and _script_mismatch(last_user_text, reply_text):
        corrected_system_prompt = system_prompt + (
            "\n\nCORRECTION — your previous reply was written in the wrong language. The customer's "
            "last message is: " + json.dumps(last_user_text, ensure_ascii=False) + ". Reply ONLY in "
            "that exact language, dialect and script — including every tool `reason`."
        )
        try:
            corrected = llm.chat_with_retry(provider, corrected_system_prompt, clean_messages, tools)
            if corrected.reply_text and not _script_mismatch(last_user_text, corrected.reply_text):
                new_recs, new_offer, blocked_reason = _process_tool_calls(
                    corrected.tool_calls, products, discount_codes, lang, blocked_skus, merchant.currency
                )
                reply_text = corrected.reply_text
                recommendations = _apply_budget(new_recs, budget_ceiling) or recommendations
                discount_offer = new_offer or discount_offer
        except llm.LLMError:
            pass

    # الرد النصي لازم يطابق العرض المنظّم: لو في discount_offer حقيقي بس
    # الموديل ما ذكره بالنص، منضيف جملة مبنية من بيانات الكود الفعلية بس.
    if discount_offer and reply_text and discount_offer["code"] not in reply_text:
        reply_text = reply_text + "\n\n" + DISCOUNT_APPENDIX[lang].format(**discount_offer)

    if not reply_text:
        # لو الموديل رجع بس tool_use بدون نص، منضمن إنه الزبون ما يستنى رد
        # فاضي أو رد عام مالوش علاقة بالسياق بالشات.
        if recommendations:
            reply_text = recommendations[0]["reason"]
        elif blocked_reason:
            reply_text = blocked_reason
        elif discount_offer:
            reply_text = FALLBACK_DISCOUNT_REPLY[lang].format(**discount_offer)
        else:
            reply_text = FALLBACK_REPHRASE_REPLY[lang]

    _log_conversation(merchant, session_id, clean_messages, reply_text, recommendations, discount_offer)

    return _cors_response(
        {
            "ok": True,
            "reply": reply_text,
            "recommendation": recommendations[0] if recommendations else None,
            "recommendations": recommendations,
            "discount_offer": discount_offer,
        }
    )


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def event(request):
    """أحداث من الودجت لقياس البيع: ضغطة شراء، إضافة للسلة."""
    if request.method == "OPTIONS":
        return _cors_response({}, status=200)
    merchant, error_response = _authenticate(request)
    if error_response:
        return error_response
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return _cors_response({"ok": False, "error": "invalid_json"}, status=400)
    field = WIDGET_EVENTS.get(body.get("type"))
    session_id = _clean_session_id(body.get("session_id"))
    if not field or not session_id:
        return _cors_response({"ok": False, "error": "invalid_event"}, status=400)
    conversation = Conversation.objects.filter(merchant=merchant, session_id=session_id).first()
    if conversation is not None:
        setattr(conversation, field, getattr(conversation, field) + 1)
        conversation.save(update_fields=[field, "updated_at"])
    return _cors_response({"ok": True})


TRYON_PROMPT = (
    "A photorealistic, high-quality photo of the exact same person from the first image, now "
    "wearing the exact clothing item shown in the second image, fitted naturally and accurately "
    "to their body. Keep the person's face, identity, body shape, and pose completely unchanged — "
    "only change the outfit. Preserve the same framing and zoom level as the first image — if it "
    "is a close-up face photo, keep it a natural close-up portrait and do not zoom in further or "
    "enlarge the face disproportionately; if it shows more of the body, keep that same amount of "
    "body visible. Flattering, professional e-commerce studio lighting, sharp focus, "
    "true-to-life fabric color and texture, no distortion or warping. The result should look like "
    "a real photo the customer would be excited to see of themselves — polished enough to convince "
    "them to buy."
)


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def tryon(request):
    if request.method == "OPTIONS":
        return _cors_response({}, status=200)

    token_value = request.headers.get("X-Api-Token", "").strip()
    if not token_value:
        return _cors_response({"ok": False, "error": "missing_token"}, status=401)

    sku = (request.POST.get("sku") or "").strip()
    photo = request.FILES.get("photo")
    if not sku or not photo:
        return _cors_response({"ok": False, "error": "sku_and_photo_required"}, status=400)
    if photo.size > MAX_PHOTO_BYTES:
        return _cors_response({"ok": False, "error": "photo_too_large"}, status=400)

    if not settings.KIEAI_API_KEY:
        return _cors_response({"ok": False, "error": "tryon_not_configured"}, status=503)

    with transaction.atomic():
        merchant = (
            ApiToken.objects.select_for_update().filter(token=token_value).first()
        )
        if merchant is None:
            return _cors_response({"ok": False, "error": "invalid_token"}, status=401)
        if not merchant.is_active:
            return _cors_response({"ok": False, "error": "token_disabled"}, status=403)

        period_end = merchant.period_start + timedelta(days=merchant.period_days)
        images_used = 0 if timezone.now() >= period_end else merchant.images_used
        if images_used >= merchant.image_quota:
            return _cors_response(
                {"ok": False, "error": "quota_exceeded", "remaining": 0}, status=200
            )

    product = Product.objects.filter(merchant=merchant, sku=sku).first()
    if product is None:
        return _cors_response({"ok": False, "error": "product_not_found"}, status=404)

    try:
        image_url = kieai.generate_tryon(
            api_key=settings.KIEAI_API_KEY,
            person_photo=photo,
            person_filename=photo.name,
            product_image_url=product.image_url,
            prompt=TRYON_PROMPT,
        )
    except kieai.TryOnError as exc:
        return _cors_response({"ok": False, "error": str(exc)}, status=502)

    with transaction.atomic():
        merchant = ApiToken.objects.select_for_update().get(pk=merchant.pk)
        result = merchant.consume_quota(1)

    session_id = _clean_session_id(request.POST.get("session_id"))
    if session_id:
        conversation = Conversation.objects.filter(merchant=merchant, session_id=session_id).first()
        if conversation is not None:
            conversation.tryons += 1
            conversation.save(update_fields=["tryons", "updated_at"])

    return _cors_response(
        {
            "ok": True,
            "image_url": image_url,
            "remaining": result.get("remaining"),
        }
    )
