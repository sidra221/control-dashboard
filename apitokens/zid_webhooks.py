import hmac
import json
import logging
import threading

from django.conf import settings
from django.db import connection as db_connection
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ZidConnection
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
        sync = getattr(catalog_service, "sync_zid", None)
        if sync is None:
            return  # المزامنة بتنضاف بتاسك 3
        count = sync(connection.merchant, connection)
        logger.info("zid_initial_sync store=%s products=%s", connection.store_id, count)
    except Exception:  # noqa: BLE001 — خيط بالخلفية، أي خطأ لازم ينسجّل بس
        logger.exception("zid_initial_sync_failed connection=%s", connection_pk)
    finally:
        db_connection.close()
