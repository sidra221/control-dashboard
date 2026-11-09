import json
import logging
import threading

from django.conf import settings
from django.db import connection as db_connection
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Conversation, Product, SallaConnection
from .services import catalog as catalog_service
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
            connection = salla_service.save_authorization(salla_merchant_id, data)
        except salla_service.SallaError:
            logger.exception("salla_authorize_failed merchant=%s", salla_merchant_id)
            return HttpResponse(status=400)
        start_initial_sync(connection.pk)
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
        _record_order(merchant, data)
    return HttpResponse(status=200)


def start_initial_sync(connection_pk):
    """أول مزامنة بعد التركيب بالخلفية: سلة بتستنى رد الإشعار ~30 ثانية بس،
    ومتجر كبير بياخد أكتر. بعدها المنتجات بتتحدّث بالإشعارات + sync_salla."""
    threading.Thread(target=_initial_sync, args=(connection_pk,), daemon=True).start()


def _initial_sync(connection_pk):
    try:
        connection = SallaConnection.objects.select_related("merchant").get(pk=connection_pk)
        count = catalog_service.sync_salla(connection.merchant, connection)
        logger.info("salla_initial_sync merchant=%s products=%s", connection.salla_merchant_id, count)
    except Exception:  # noqa: BLE001 — خيط بالخلفية، أي خطأ لازم ينسجّل بس
        logger.exception("salla_initial_sync_failed connection=%s", connection_pk)
    finally:
        db_connection.close()


def _order_cart_id(data):
    """رقم السلة اللي انعمل منها الطلب — سلة ممكن تحطه بأكتر من مكان."""
    cart = data.get("cart") if isinstance(data.get("cart"), dict) else {}
    for value in (data.get("cart_id"), cart.get("id"), data.get("cart_reference_id")):
        if value:
            return str(value)
    return ""


def _record_order(merchant, data):
    """بيربط الطلب بالمحادثة يلي الودجت سجّل رقم سلّتها. طلب ما إله علاقة
    بالمساعد منتجاهله."""
    cart_id = _order_cart_id(data)
    if not cart_id:
        logger.info("salla_order_without_cart merchant=%s order=%s", merchant.pk, data.get("id"))
        return
    conversation = Conversation.objects.filter(merchant=merchant, platform_cart_id=cart_id).first()
    if conversation is None or conversation.order_id:
        return
    amounts = data.get("amounts") if isinstance(data.get("amounts"), dict) else {}
    total = amounts.get("total") or data.get("total") or {}
    if not isinstance(total, dict):
        total = {"amount": total}
    conversation.order_id = str(data.get("id") or "")[:64]
    conversation.order_total = salla_service._amount(total.get("amount"))
    conversation.order_currency = (total.get("currency") or data.get("currency") or "")[:8]
    conversation.order_at = timezone.now()
    conversation.save(
        update_fields=["order_id", "order_total", "order_currency", "order_at", "updated_at"]
    )
