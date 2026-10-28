import json
from decimal import Decimal, InvalidOperation

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ApiToken, Conversation, Product
from .services import shopify as shopify_service


@csrf_exempt
@require_http_methods(["POST"])
def product_webhook(request, token):
    merchant = ApiToken.objects.filter(token=token).first()
    connection = getattr(merchant, "shopify_connection", None) if merchant else None
    if connection is None:
        return HttpResponse(status=404)

    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not shopify_service.verify_webhook_hmac(request.body, hmac_header, connection.api_secret):
        return HttpResponse(status=401)

    topic = request.headers.get("X-Shopify-Topic", "")
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if topic == "orders/create":
        _record_order(merchant, payload)
        return HttpResponse(status=200)

    external_id = str(payload.get("id", ""))

    if topic == "products/delete":
        Product.objects.filter(merchant=merchant, external_id=external_id).delete()
    elif topic in ("products/create", "products/update"):
        row = shopify_service.product_row_from_payload(
            connection.shop_domain, payload, connection.primary_domain
        )
        if row is not None:
            row.pop("external_id")
            Product.objects.update_or_create(
                merchant=merchant, external_id=external_id, defaults=row,
            )
        else:
            # المنتج صار بدون صورة أو مش فعّال (draft/archived) — منشيله من
            # الكتالوج بدل ما المساعد يعرض منتج الزبون ما بيقدر يشتريه.
            Product.objects.filter(merchant=merchant, external_id=external_id).delete()

    return HttpResponse(status=200)


def _record_order(merchant, payload):
    """بيربط الطلب بالمحادثة اللي جابته عبر خاصية السلة اللي حطها الودجت.
    طلب بدون هالخاصية ما إلو علاقة بالمساعد — منتجاهله."""
    session_id = shopify_service.conversation_id_from_order(payload)
    if not session_id:
        return
    conversation = Conversation.objects.filter(merchant=merchant, session_id=session_id).first()
    if conversation is None or conversation.order_id:
        return
    try:
        total = Decimal(str(payload.get("total_price") or "0"))
    except InvalidOperation:
        total = None
    conversation.order_id = str(payload.get("id") or "")
    conversation.order_total = total
    conversation.order_currency = (payload.get("currency") or "")[:8]
    conversation.save(update_fields=["order_id", "order_total", "order_currency", "updated_at"])
