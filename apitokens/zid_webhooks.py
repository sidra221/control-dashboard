import hmac
import json
import logging
import threading

from django.conf import settings
from django.db import connection as db_connection
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Conversation, Product, ZidConnection
from .services import zid as zid_service

logger = logging.getLogger(__name__)

# زد ما بتوقّع إشعارات التطبيق — منضيف هالهيدر بكلمة سر من تبويب Webhooks
# ببوابة الشركاء، ومنرفض أي طلب بدونه.
SECRET_HEADER = "X-Control-Secret"


def _secret_ok(request):
    secret = settings.ZID_WEBHOOK_SECRET
    provided = request.headers.get(SECRET_HEADER, "")
    return bool(secret) and bool(provided) and hmac.compare_digest(provided, secret)


@csrf_exempt
@require_http_methods(["POST"])
def app_webhook(request):
    """أحداث التطبيق والاشتراكات (تثبيت، إلغاء، تفعيل/تجديد/انتهاء باقة).
    زد ما بتعيد المحاولة للأحداث اللي ما اشتركنا فيها، والمعالجة آمنة للتكرار."""
    if not settings.ZID_WEBHOOK_SECRET:
        return HttpResponse(status=503)
    if not _secret_ok(request):
        return HttpResponse(status=401)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)
    event = payload.get("event_name") or ""
    if event == "app.market.application.uninstall":
        zid_service.deactivate(
            store_id=str(payload.get("store_id") or ""), store_uuid=str(payload.get("store_uuid") or ""),
        )
    elif event.startswith("app.market.subscription."):
        zid_service.record_subscription_event(payload)
    else:
        # authorized / install / rated / private.plan.request: التثبيت نفسه
        # بيصير بالـ callback، هون منسجّل بس.
        logger.info("zid_app_event event=%s store=%s", event, payload.get("store_id"))
    return HttpResponse(status=200)


def start_initial_sync(connection_pk):
    """أول مزامنة بعد التثبيت بالخلفية حتى ما يستنى التاجر."""
    threading.Thread(target=_initial_sync, args=(connection_pk,), daemon=True).start()


def _initial_sync(connection_pk):
    try:
        from .services import catalog as catalog_service

        connection = ZidConnection.objects.select_related("merchant").get(pk=connection_pk)
        events = zid_service.register_store_webhooks(connection)
        count = catalog_service.sync_zid(connection.merchant, connection)
        logger.info("zid_initial_sync store=%s products=%s webhooks=%s", connection.store_id, count, events)
    except Exception:  # noqa: BLE001 — خيط بالخلفية، أي خطأ لازم ينسجّل بس
        logger.exception("zid_initial_sync_failed connection=%s", connection_pk)
    finally:
        db_connection.close()


@csrf_exempt
@require_http_methods(["POST"])
def store_webhook(request, store_id, event):
    """إشعارات المنتجات والطلبات لمتجر واحد (منسجّلها بعد التثبيت). رقم المتجر
    واسم الحدث بالرابط، والتحقق بـ Basic Auth بكلمة سرنا."""
    if not zid_service.verify_store_webhook(request.headers.get("Authorization", "")):
        return HttpResponse(status=401)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)
    connection = (
        ZidConnection.objects.filter(store_id=str(store_id), is_active=True).select_related("merchant").first()
    )
    if connection is None:
        return HttpResponse(status=200)  # متجر لغى التطبيق — ما منعيد
    merchant = connection.merchant
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if event in ("product.create", "product.update", "product.publish"):
        zid_service.upsert_product(merchant, data)
    elif event == "product.delete":
        Product.objects.filter(merchant=merchant, external_id=str(data.get("id") or "")).delete()
    elif event == "order.create":
        _record_order(merchant, data)
    return HttpResponse(status=200)


def _order_cart_id(data):
    cart = data.get("cart") if isinstance(data.get("cart"), dict) else {}
    for value in (data.get("cart_id"), cart.get("id"), data.get("cart_session_id"), cart.get("session_id")):
        if value:
            return str(value)
    return ""


def _record_order(merchant, data):
    """بيربط الطلب بالمحادثة اللي الودجت سجّل رقم سلّتها (متل سلة)."""
    cart_id = _order_cart_id(data)
    if not cart_id:
        logger.info("zid_order_without_cart merchant=%s order=%s", merchant.pk, data.get("id"))
        return
    conversation = Conversation.objects.filter(merchant=merchant, platform_cart_id=cart_id).first()
    if conversation is None or conversation.order_id:
        return
    total = data.get("order_total") or data.get("total") or {}
    if not isinstance(total, dict):
        total = {"amount": total}
    conversation.order_id = str(data.get("id") or "")[:64]
    conversation.order_total = zid_service._amount(total.get("amount"))
    conversation.order_currency = str(data.get("currency_code") or total.get("currency") or "")[:8]
    conversation.save(update_fields=["order_id", "order_total", "order_currency", "updated_at"])
