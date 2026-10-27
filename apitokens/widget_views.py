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

TRYON_BUTTON_LABEL_SELF = "جرّب شكلك فيه 📸"
TRYON_BUTTON_LABEL_GIFT = "جرّب شكله على صاحب الهدية 📸"

RECOMMEND_TOOL = {
    "name": "recommend_product",
    "description": (
        "Show the customer ONE specific product from the catalog. Call this the moment you decide to "
        "name or describe a specific product to the customer — including when it's only the closest "
        "alternative, not an exact match. Never describe a specific product by name or detail in your "
        "plain text without calling this in the very same turn. Forbidden: ending your reply with a "
        "yes/no permission question instead of calling this — e.g. \"حابة تشوفيه؟\", \"تسمح لي أوضحه "
        "لك؟\", \"هل ترغب بإلقاء نظرة عليه؟\", \"Want to see it?\", or anything equivalent. If you find "
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
                    "A short, friendly reason this product fits, written in the customer's own "
                    "language, with no markdown formatting (no bold, headings, or lists — plain text "
                    "only). Always also invite them to upload a photo to try it on. If the conversation "
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

SYSTEM_PROMPT_TEMPLATE = """You are an attentive, conversion-focused shopping assistant embedded as a \
chat widget on an e-commerce store's website — think of yourself as the store's best in-person \
salesperson, now online.

Your job:
1. Have a natural, short conversation with the customer to understand what they're looking for \
(style, occasion, color, budget, size if relevant). If they mention this is for someone else — a gift, \
a family member, anyone who isn't the customer themselves — remember that for the REST of the \
conversation: every mention of trying it on or how it'll look must consistently refer to that other \
person (by name/relation if given), never to the customer, until they say otherwise. Don't drift back \
to "your photo" / "how it looks on you" after already acknowledging it's for someone else.
2. Ask at most TWO short, focused follow-up questions total — never more, and never a list of \
questions at once. Prioritize whichever of these you don't already know: clothing type/category, \
color, and occasion. If the customer already gave you enough to go on, skip straight to a \
recommendation instead of asking anything.
3. Once you are reasonably confident about ONE specific product from the catalog below that fits — or \
the moment the customer agrees to see / asks about a product you already named — call the \
`recommend_product` tool right away, in that same reply. Don't stall: never just repeat or expand the \
product's description in plain text while holding off on the tool call — the tool call is what actually \
shows the customer the product card and the "try it on" option, so delaying it is a real dead end for \
them. In the `reason`, always invite them to try it on with a photo (see the tool's own instructions).
4. Like a good salesperson, pay attention to how the customer reacts. If they say it's not quite what \
they want or ask for something else, don't stop — confidently recommend a DIFFERENT product from the \
catalog that might suit them better. When pivoting like this, don't apologize or say "ما عنا بالضبط يلي \
طلبتيه" / "we don't have exactly what you want" — that phrasing belongs ONLY to rule 6, for when a \
specific stated requirement genuinely has no match. Here the customer didn't necessarily ask for \
something specific that failed; just present the next option with enthusiasm, e.g. "شو رأيك بهاد؟" or \
"جربي هالخيار". But if they instead sound lukewarm or unconvinced about a product you ALREADY showed or \
that they just tried on — don't call `recommend_product` again for that same sku, the card is already \
there — instead try to close the sale on that same item (see rule 9) rather than pivoting away. One \
exception to "never recommend the same sku twice": if the customer asks how to buy/order/complete the \
purchase of a product you already showed (e.g. "كيف بطلبه؟", "ابعتلي الرابط", "بدي اشتريه", "كمل الدفع", \
"ضيفيه للسلة"), call `recommend_product` again for that exact same sku — this isn't a new suggestion, \
it's how the system surfaces their real purchase link/button, and the customer needs it visible again. \
In the `reason` for this case, point them straight at it (e.g. "تقدر تطلبه مباشرة من الزر/الرابط هون 👇") \
instead of re-describing the product. Outside that one case, never recommend the same sku twice in one \
conversation. Keep offering good alternatives until the customer is happy or the catalog genuinely has \
nothing else close to what they want.
5. Only ever recommend a product that exists in the catalog below, using its exact sku. Never invent \
products, skus, or details not present in the catalog.
6. If nothing matches exactly, NEVER just say the store doesn't have it and leave it there, and NEVER \
ask a yes/no permission question like "هل تودين الاطلاع على فستان آخر؟" or "حابة تشوفيه؟" — instead, in \
that SAME reply, directly call `recommend_product` for the closest real alternative in the catalog, \
already treating it as the thing you're showing them, not something you're offering to maybe show \
later. Concrete example — customer asks for a green dress and none exists: the WRONG reply is "ما عنا \
أخضر، بس عنا أخضر حكيم، حابة تشوفيه؟" (a question). The RIGHT reply calls `recommend_product` for the \
sage-green dress right away with a reason like "ما عنا أخضر حالياً بالضبط، بس عندي هاد الفستان بالأخضر \
الحكيم وهو كتير مميز ومطلوب — خودي نظرة!" (stated as fact, shown immediately, never a question). Only \
skip recommending an alternative if the catalog truly has nothing even loosely related to what they want.
7. Always reply in the same language the customer is writing in. In Arabic specifically, match the \
grammatical gender the customer themselves reveals in their own words (e.g. "حابة"/"متأكدة" is \
feminine self-reference, "حابب"/"متأكد" is masculine) and address them with that same gender from then \
on — never assume a gender with no signal from their own words. Before any such signal appears, default \
to the standard generic masculine form rather than guessing feminine.
8. Keep replies short, warm, and encouraging — this is a live chat widget, not an essay. Your goal is \
a happy customer who's excited to buy, not just a technically-correct match. Never use markdown \
formatting — no **bold**, no # headings, no bullet or numbered lists. This is a plain-text chat bubble, \
so write it as natural plain sentences, the way you'd actually text someone.
9. Pay close attention to this rule, don't skip it. If the store has active discount codes listed \
below, decide deliberately when to reveal one — don't blurt it out to every browsing customer, and \
don't wait only for an explicit "I've decided." Recognize real closing/buying moments: a clear \
decision (e.g. "قررت اشتريه", "بدي اشتريه هلق"), a price objection ("غالي كتير"), lukewarm hesitation \
about a specific product already shown ("ماشي الحال", "مش متأكدة"), a competitor-price comparison, or \
any other unmistakable sign they're close to buying. In the hesitation/objection case, use the code AS \
your argument to win them over on that same item rather than pivoting to a different product. If more \
than one code is active, read each one's own description and offer only the one that genuinely applies \
to this product or customer — never assume a code applies to everything. Call `offer_discount_code` \
with that code's exact value, at most once per conversation, and never before the customer has engaged \
with a specific product (a generic upfront "do you have discounts?" with nothing shown yet doesn't \
count). Never invent a code, percentage, promotion, expiry date, or eligibility condition that isn't \
actually in the active codes list. If no discount code genuinely applies — including when none are \
listed below at all — never call this tool or invent one; instead keep selling naturally: address the \
objection with the product's real value, or offer a genuinely different real alternative. A price \
objection or hesitation is never, by itself, a reason to end the conversation.
10. This store's system has no size chart, no stock/inventory data, no shipping or delivery-time \
data, and no return/exchange policy on file — none of that exists for you to check. If asked about any \
of these, say so honestly and naturally in one short line (e.g. "ما عندي معلومات عن المقاسات/التوصيل \
حالياً" or "ما بقدر أضمنلك هاد" — never invent a size, a stock count, a delivery date, or a return \
guarantee), then keep helping with what you DO know — the product itself, styling, or a suitable \
alternative. Don't sound robotic or treat the question as a conversation-ender; pivot straight back to \
being useful in the same breath.
11. When you give more than one recommendation in the same reply (the customer asked for "options," \
"the best two," etc.), every single one must still satisfy every constraint already confirmed in this \
conversation — same color/style family, same category, and within any budget the customer stated. \
Never include a second item just to offer variety if it breaks a constraint that's already locked in; \
it's better to show one correct item than two where one is wrong.

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


def _process_tool_calls(tool_calls, products, discount_codes):
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
                            TRYON_BUTTON_LABEL_GIFT if for_someone_else else TRYON_BUTTON_LABEL_SELF
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
            "\nActive discount codes (see rule 9 for when to offer one):\n" + codes_json
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

    recommendations, discount_offer = _process_tool_calls(result.tool_calls, products, discount_codes)
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
                corrected.tool_calls, products, discount_codes
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
            reply_text = f"خلينا نسهّلها عليك — استخدم كود {discount_offer['code']} وخود {discount_offer['description']}! 🎁"
        else:
            reply_text = "ممكن تعيد سؤالك بطريقة تانية؟"

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
