import json
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

RECOMMEND_TOOL = {
    "name": "recommend_product",
    "description": (
        "Recommend exactly one specific product from the store catalog once you are "
        "reasonably confident it fits what the customer is looking for."
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
                "description": "A short, friendly reason this product fits, written in the customer's own language.",
            },
        },
        "required": ["sku", "reason"],
    },
}

DISCOUNT_TOOL = {
    "name": "offer_discount_code",
    "description": (
        "Offer an active discount code to the customer. Call this ONLY when the customer has just "
        "clearly signaled they've decided to buy right now (for example: \"قررت اشتريه\", \"بدي اشتريه "
        "هلق\", or an equally unmistakable, enthusiastic commitment after a recommendation or a "
        "try-on) — never for mere hesitation or price questions with no clear commitment. Call it at "
        "most once per conversation, and only with a code that actually exists in the active codes "
        "list."
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
(style, occasion, color, budget, size if relevant).
2. Ask at most TWO short, focused follow-up questions total — never more, and never a list of \
questions at once. Prioritize whichever of these you don't already know: clothing type/category, \
color, and occasion. If the customer already gave you enough to go on, skip straight to a \
recommendation instead of asking anything.
3. Once you are reasonably confident about ONE specific product from the catalog below that fits, \
call the `recommend_product` tool with its exact sku and a short, warm, persuasive reason.
4. Like a good salesperson, pay attention to how the customer reacts. If they seem unsure, say it's \
not quite what they want, or ask for something else, don't stop — confidently recommend a DIFFERENT \
product from the catalog that might suit them better. Never recommend the same sku twice in one \
conversation. Keep offering good alternatives until the customer is happy or the catalog genuinely \
has nothing else close to what they want.
5. Only ever recommend a product that exists in the catalog below, using its exact sku. Never invent \
products, skus, or details not present in the catalog.
6. If nothing matches exactly, NEVER just say the store doesn't have it and leave it there — be honest \
that there's no exact match, but ALWAYS follow up in the same breath by actively pointing to the \
closest real alternatives from the catalog (a different but related color, a similar style, a set or \
piece that shares part of what they asked for) — exactly like a good in-store salesperson would say "we \
don't have that exact one, but here's something close: a burgundy one, or this red-and-black set." Only \
skip suggesting an alternative if the catalog truly has nothing even loosely related to what they want.
7. Always reply in the same language the customer is writing in.
8. Keep replies short, warm, and encouraging — this is a live chat widget, not an essay. Your goal is \
a happy customer who's excited to buy, not just a technically-correct match.
9. Pay close attention to this rule, don't skip it: if the store has active discount codes listed \
below, and the customer clearly signals they've decided to buy right now — phrases like "قررت اشتريه", \
"بدي اشتريه هلق", "عجبني كتير، بدي اطلبه", or an equally clear, enthusiastic commitment after a \
recommendation or a try-on — then call the `offer_discount_code` tool with that code's exact value, \
once per conversation, as a nice final touch to help them commit. Never do this just because they \
hesitated or asked about price with no clear commitment — only when the buying signal is unmistakable. \
If no discount codes are listed below, never call this tool or invent a code.

Store catalog (JSON array of available products):
{catalog_json}
{discount_codes_section}"""


def _cors_response(data, status=200):
    response = JsonResponse(data, status=status)
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Token"
    response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response


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

    recommendation = None
    discount_offer = None
    for call in result.tool_calls:
        call_input = call.get("input") or {}
        if call["name"] == "recommend_product":
            sku = call_input.get("sku", "")
            reason = call_input.get("reason", "")
            product = next((p for p in products if p.sku == sku), None)
            if product is not None:
                recommendation = {
                    "sku": product.sku,
                    "title": product.title,
                    "price": str(product.price),
                    "image_url": product.image_url,
                    "product_url": product.product_url,
                    "reason": reason,
                }
        elif call["name"] == "offer_discount_code":
            code_value = call_input.get("code", "")
            code_obj = next((d for d in discount_codes if d.code == code_value), None)
            if code_obj is not None:
                discount_offer = {"code": code_obj.code, "description": code_obj.description}

    reply_text = result.reply_text
    if not reply_text:
        # لو الموديل رجع بس tool_use بدون نص (أو sku مش موجود بالكتالوج فطلع
        # recommendation فاضي)، منضمن إنه الزبون ما يستنى رد فاضي بالشات.
        reply_text = recommendation["reason"] if recommendation else "ممكن تعيد سؤالك بطريقة تانية؟"

    return _cors_response(
        {
            "ok": True,
            "reply": reply_text,
            "recommendation": recommendation,
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
