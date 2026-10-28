import json
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ApiToken, Product
from .services import kieai, llm

MAX_MESSAGES = 30
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_CHARS = 2000
MAX_RECOMMENDATIONS_PER_TURN = 3

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
DISCOUNT_FABRICATION_MARKERS = ("خصم", "كود", "%")

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
FALLBACK_REPHRASE_REPLY = {
    "ar": "ممكن تعيد سؤالك بطريقة تانية؟",
    "en": "Could you rephrase that for me?",
}

ARABIC_CHAR_PATTERN = re.compile(r"[؀-ۿ]")


def _customer_lang(messages):
    """عربي إذا آخر رسالة من الزبون فيها حروف عربية، وإلا إنجليزي."""
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    return "ar" if ARABIC_CHAR_PATTERN.search(last_user) else "en"

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
                    "with no markdown formatting (no bold, headings, or lists — plain text only). Always also invite them to upload a photo to try it on. If the conversation "
                    "already established this is for someone else (a gift, etc.), this invitation MUST "
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
        "recognize a real buying/closing moment: a clear decision to buy, a price objection, "
        "hesitation after seeing a specific product, a competitor-price comparison, or any other "
        "sign the customer is close to purchasing. Don't reveal a code to every browsing customer "
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
        },
        "required": ["code"],
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
in Latin letters and numbers (Arabizi, e.g. "kifak", "3ndkon"), reply fully in clean, readable Arabizi \
in their dialect.
- If they switch language or dialect mid-conversation, switch with them from that message on.
- Any example phrase in these instructions only illustrates the intent. Never copy it literally — \
express the same idea naturally in the customer's own language and dialect.
- Grammatical gender (Arabic and other gendered languages): match the gender the customer reveals \
about themselves in their own words (e.g. a feminine vs. masculine form of "I'd like" or "I'm sure"), \
and keep addressing them that way. Never guess a gender with no signal; until one appears, use the \
standard generic masculine form. What they're shopping for is NOT a signal (a man can buy a dress as a \
gift) — only how they refer to themselves counts.

HOW YOU SELL
1. Discover fast. Have a short, natural conversation to learn what they want: type of item, style, \
occasion, color, budget, size if relevant. Ask at most TWO short follow-up questions in total — never \
more, and never several questions in one message. Prioritize whichever you don't know yet: category, \
color, occasion. If they've already given you enough, skip the questions and recommend right away.
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
4. Sell the benefit, not the spec. In your reply and in the `reason`, connect the product to what they \
told you: the occasion, the feeling, how it will look and how they'll feel wearing it. Be specific and \
vivid, in one or two short sentences, with real confidence ("this is exactly what you described" \
energy). Always invite them to try it on with a photo (see the tool's own instructions).
5. Read their reaction and keep momentum.
   - If they want something different, don't apologize — confidently present a DIFFERENT product that \
fits better, with enthusiasm ("How about this one?"). Never say "we don't have exactly what you want" \
when you're simply pivoting; that wording belongs only to rule 7.
   - If they sound lukewarm or unsure about a product you ALREADY showed (or that they just tried on), \
don't show the same product again and don't drop it — work to close the sale on that item: \
reassure, highlight the benefit that matters most to them, and use rule 10 if a discount applies.
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
8. Handle objections with value. On price, doubt or comparison with another store: acknowledge it \
briefly, then answer with the product's real value (quality, versatility, how well it fits their need), \
or offer a genuinely cheaper real alternative from the catalog. An objection is never a reason to end \
the conversation or give up on the sale.

HONESTY (never break these — a persuasive salesperson never lies)
9. Only recommend products that exist in the catalog below, using their exact sku. Never invent \
products, skus, prices, materials, features or other details that aren't in the catalog. Never claim \
an item is a bestseller, popular, trending, low in stock or ending soon — the store gives you no such \
data. Build urgency and desire only from real benefits, never from invented scarcity.
10. Discount codes. If the store has active discount codes listed below, reveal one deliberately as a \
closing move — not to every browsing customer, and not only after an explicit "I've decided". Real \
closing moments include: a clear decision to buy, a price objection ("too expensive"), lukewarm \
hesitation about a specific product already shown ("it's okay", "not sure"), a comparison with a \
competitor's price, or any other unmistakable sign they're close to buying. In the hesitation or \
objection case, use the code AS your argument to win them over on that same item rather than pivoting \
away. If several codes are active, offer only the one whose own description genuinely applies to this \
product or customer. Call `offer_discount_code` with the exact code, at most once per conversation, \
and never before the customer has engaged with a specific product (a generic "do you have discounts?" \
with nothing shown yet doesn't count). Never invent a code, percentage, promotion, expiry date or \
eligibility condition that isn't in the active codes list. If no code applies — including when none are \
listed at all — never call this tool or mention a discount; keep selling with value or a real \
alternative instead.
11. The store has no size chart, no stock/inventory data, no shipping or delivery-time data, and no \
return/exchange policy on file. If asked about any of these, say honestly in one short line that you \
don't have that information right now (never invent a size, stock count, delivery date or return \
guarantee), then immediately pivot back to being useful — the product itself, styling advice, or a \
suitable alternative. Don't sound robotic and don't treat it as the end of the conversation.
12. When you recommend more than one product in the same reply (they asked for "options", "the best \
two", etc.), every one must satisfy every constraint already confirmed in the conversation — same \
color/style family, same category, and within any stated budget. Never add a second item just for \
variety if it breaks a confirmed constraint; one correct item beats two where one is wrong.

STYLE
13. Keep replies short, warm and natural — this is a live chat bubble, not an essay. Usually one to \
three short sentences. Write the way a friendly salesperson texts a customer. Never use markdown: no \
**bold**, no # headings, no bullet or numbered lists — plain sentences only. A well-placed emoji is \
fine; don't overdo it.

Store catalog (JSON array of available products):
{catalog_json}
{discount_codes_section}"""


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


def _process_tool_calls(tool_calls, products, discount_codes, lang="ar"):
    """بتعالج استدعاءات الأدوات يلي رجعت من الموديل لقائمة توصيات
    (بدون تكرار نفس الـ sku، وبنفس ترتيب الموديل) + عرض خصم واحد كحد أقصى.
    """
    recommendations = []
    seen_skus = set()
    discount_offer = None
    for call in tool_calls:
        call_input = call.get("input") or {}
        if call["name"] == "recommend_product":
            sku = call_input.get("sku", "")
            if sku in seen_skus:
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
                        "image_url": product.image_url,
                        "product_url": product.product_url,
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
    return recommendations[:MAX_RECOMMENDATIONS_PER_TURN], discount_offer


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def chat(request):
    if request.method == "OPTIONS":
        return _cors_response({}, status=200)

    token_value = request.headers.get("X-Api-Token", "").strip()
    if not token_value:
        return _cors_response({"ok": False, "error": "missing_token"}, status=401)

    merchant = ApiToken.objects.filter(token=token_value).first()
    if merchant is None:
        return _cors_response({"ok": False, "error": "invalid_token"}, status=401)
    if not merchant.is_active:
        return _cors_response({"ok": False, "error": "token_disabled"}, status=403)

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

    provider = llm.get_provider()
    if not provider.is_configured():
        return _cors_response({"ok": False, "error": "assistant_not_configured"}, status=503)

    products = list(merchant.products.all())
    catalog = [
        {
            "sku": p.sku,
            "title": p.title,
            "category": p.category,
            "tags": p.tag_list,
            "price": str(p.price),
        }
        for p in products
    ]
    discount_codes = list(merchant.discount_codes.filter(is_active=True))
    discount_codes_section = ""
    tools = [RECOMMEND_TOOL]
    if discount_codes:
        codes_json = json.dumps(
            [{"code": d.code, "description": d.description} for d in discount_codes],
            ensure_ascii=False,
        )
        discount_codes_section = (
            "\nActive discount codes (see rule 10 for when to offer one):\n" + codes_json
        )
        tools.append(DISCOUNT_TOOL)

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        catalog_json=json.dumps(catalog, ensure_ascii=False),
        discount_codes_section=discount_codes_section,
    )

    try:
        result = llm.chat_with_retry(provider, system_prompt, clean_messages, tools)
    except llm.LLMError:
        return _cors_response({"ok": False, "error": "assistant_unavailable"}, status=502)

    # بعض المزوّدات (OpenAI تحديداً) أحياناً بتذكر منتج أو كود خصم بالنص
    # العادي بدل ما تستخدم الأداة المناسبة، رغم تعليمات واضحة بالبرومبت —
    # هاد بيمنع ظهور كارت المنتج/زر التجربة أو حقل discount_offer نهائياً.
    # لما نكتشف هيك، منعيد الطلب مع إجبار نفس الأداة، ومنحافظ على نص الرد
    # الأصلي (غالباً كويس ومنطقي) ونلزق عليه نتيجة الأداة.
    purchase_intent_markers = (
        "كيف بطلبه", "كيف اطلبه", "كيف أطلبه", "كيف بطلب", "الرابط", "بدي اشتريه", "بدي أشتريه",
        "كيف اشتريه", "كمل الدفع", "اكمل الدفع", "أكمل الدفع", "للسلة", "بدي اطلبه",
    )
    force_tool = None
    if not result.tool_calls:
        if any(m in clean_messages[-1]["content"] for m in purchase_intent_markers):
            force_tool = "recommend_product"
        elif any(d.code in result.reply_text for d in discount_codes):
            force_tool = "offer_discount_code"
        else:
            no_exact_match_phrases = ("ما عنا", "ما عندنا", "ما في", "لا يوجد", "ليس لدينا", "للأسف")
            if any(p.title.lower() in result.reply_text.lower() for p in products) or any(
                phrase in result.reply_text for phrase in no_exact_match_phrases
            ):
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
            result = llm.LLMResult(reply_text=original_reply_text, tool_calls=forced_result.tool_calls)
        except llm.LLMError:
            pass  # نضل مع الرد الأصلي بدل ما نفشّل الطلب كله

    lang = _customer_lang(clean_messages)
    recommendations, discount_offer = _process_tool_calls(
        result.tool_calls, products, discount_codes, lang
    )
    reply_text = result.reply_text

    # يحافظ على ميزانية الزبون لو ذكرها صراحة: بيشيل أي توصية فوق السقف،
    # إلا إذا هيك رح يصفّي كل التوصيات — ساعتها منفضّل نضل مع خيار وحد
    # (ولو فوق الميزانية شوي) عن ما نرجّع رد فاضي بالكامل للزبون.
    budget_ceiling = _extract_budget_ceiling(clean_messages)
    if budget_ceiling is not None and len(recommendations) > 1:
        within_budget = [r for r in recommendations if float(r["price"]) <= budget_ceiling]
        if within_budget:
            recommendations = within_budget

    # لو الموديل ذكر كلام خصم (خصم/كود/%) بالنص العادي بدون ما ينتج
    # discount_offer حقيقي من كود فعلي موجود — هاد اختلاق، مش مجرد نسيان
    # استخدام الأداة (هيك مغطّى فوق بـ force_tool). منعيد الطلب مرة وحدة
    # مع تصحيح صريح إنه ما في خصم فعلي، ومنستخدم نتيجة هالمحاولة كاملة
    # (نص ورد وأدوات) بدل الرد الأصلي المختلق.
    if discount_offer is None and any(marker in reply_text for marker in DISCOUNT_FABRICATION_MARKERS):
        corrected_system_prompt = system_prompt + (
            "\n\nCORRECTION — read before replying: your previous reply mentioned a discount, "
            "code, or percentage-off, but no such offer actually applies to this customer right "
            "now. Rewrite your reply to their last message WITHOUT mentioning any discount, code, "
            "or percentage — address what they actually asked using the product's real value, or "
            "a genuine alternative, instead."
        )
        try:
            corrected = llm.chat_with_retry(provider, corrected_system_prompt, clean_messages, tools)
            recommendations, discount_offer = _process_tool_calls(
                corrected.tool_calls, products, discount_codes, lang
            )
            if budget_ceiling is not None and len(recommendations) > 1:
                within_budget = [r for r in recommendations if float(r["price"]) <= budget_ceiling]
                if within_budget:
                    recommendations = within_budget
            reply_text = corrected.reply_text or reply_text
        except llm.LLMError:
            pass  # نضل مع الرد الأصلي بدل ما نفشّل الطلب كله

    if not reply_text:
        # لو الموديل رجع بس tool_use بدون نص، منضمن إنه الزبون ما يستنى رد
        # فاضي أو رد عام مالوش علاقة بالسياق بالشات.
        if recommendations:
            reply_text = recommendations[0]["reason"]
        elif discount_offer:
            reply_text = FALLBACK_DISCOUNT_REPLY[lang].format(**discount_offer)
        else:
            reply_text = FALLBACK_REPHRASE_REPLY[lang]

    return _cors_response(
        {
            "ok": True,
            "reply": reply_text,
            "recommendation": recommendations[0] if recommendations else None,
            "recommendations": recommendations,
            "discount_offer": discount_offer,
        }
    )


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

    return _cors_response(
        {
            "ok": True,
            "image_url": image_url,
            "remaining": result.get("remaining"),
        }
    )
