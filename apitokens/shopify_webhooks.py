import json

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ApiToken, Product
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

    external_id = str(payload.get("id", ""))

    if topic == "products/delete":
        Product.objects.filter(merchant=merchant, external_id=external_id).delete()
    elif topic in ("products/create", "products/update"):
        row = shopify_service.product_row_from_payload(connection.shop_domain, payload)
        if row is not None:
            row.pop("external_id")
            Product.objects.update_or_create(
                merchant=merchant, external_id=external_id, defaults=row,
            )
        else:
            # المنتج صار بدون صورة (أو اتشال آخر صورة) — موديلنا بيطلب صورة
            # دايماً، فمنشيل أي نسخة قديمة عنا منه بدل ما نعرض منتج ناقص.
            Product.objects.filter(merchant=merchant, external_id=external_id).delete()

    return HttpResponse(status=200)
