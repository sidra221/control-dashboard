import json
import logging

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Product, SallaConnection
from .services import salla as salla_service

logger = logging.getLogger(__name__)

PRODUCT_UPSERT_EVENTS = {"product.created", "product.updated", "product.available", "product.quantity.low"}


@csrf_exempt
@require_http_methods(["POST"])
def webhook(request):
    """رابط واحد لكل إشعارات تطبيق سلة (أحداث التطبيق + أحداث المتاجر).
    سلة بتعيد الإرسال 3 مرات لو ما رجّعنا 200، فكل معالجة هون آمنة للتكرار."""
    if not settings.SALLA_WEBHOOK_SECRET:
        return HttpResponse(status=503)
    if not salla_service.verify_webhook(request.body, request.headers, settings.SALLA_WEBHOOK_SECRET):
        return HttpResponse(status=401)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    event = payload.get("event") or ""
    salla_merchant_id = str(payload.get("merchant") or "")
    data = payload.get("data") or {}

    if event == "app.store.authorize":
        try:
            salla_service.save_authorization(salla_merchant_id, data)
        except salla_service.SallaError:
            logger.exception("salla_authorize_failed merchant=%s", salla_merchant_id)
            return HttpResponse(status=400)
        return HttpResponse(status=200)
    if event == "app.uninstalled":
        salla_service.deactivate(salla_merchant_id)
        return HttpResponse(status=200)

    connection = SallaConnection.objects.filter(
        salla_merchant_id=salla_merchant_id, is_active=True,
    ).select_related("merchant").first()
    if connection is None:
        # متجر مش مركّب التطبيق (أو لغاه) — 200 حتى ما تضل سلة تعيد.
        return HttpResponse(status=200)
    merchant = connection.merchant

    if event in PRODUCT_UPSERT_EVENTS:
        salla_service.upsert_product(merchant, data)
    elif event == "product.deleted":
        Product.objects.filter(merchant=merchant, external_id=str(data.get("id") or "")).delete()
    elif event == "order.created":
        # ربط الطلب بالمحادثة بدّه علامة من الودجت على سلة الزبون — بيجي مع
        # تاسك الودجت. لهلأ منسجّل إنه وصل بس.
        logger.info("salla_order_received merchant=%s order=%s", salla_merchant_id, data.get("id"))
    return HttpResponse(status=200)
