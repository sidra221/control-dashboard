import json
import logging
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import connection as db_connection
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import ApiToken, Conversation, Product, WooCommerceConnection
from .services import woocommerce as woocommerce_service

logger = logging.getLogger(__name__)

# WooCommerce ما بيسمح بوسم السلة من المتصفح (Store API ما بيقبل بيانات مخصصة)، فما في
# معرّف محادثة على الطلب متل Shopify. بنربط الطلب بأحدث محادثة ضغط زبونها "أضف للسلة" أو
# "اشتري" على منتج من منتجات الطلب (من توصيات الشات أو من أي كرت ضغط عليه) خلال هالمدة قبل
# الطلب — ربط تقريبي، مش قطعي.
ATTRIBUTION_WINDOW = timedelta(hours=24)


@csrf_exempt
@require_http_methods(["POST"])
def webhook(request, token):
    merchant = ApiToken.objects.filter(token=token).first()
    connection = getattr(merchant, "woocommerce_connection", None) if merchant else None
    if connection is None:
        return HttpResponse(status=404)

    # WooCommerce بيبعت "ping" (بدون توقيع، جسمه webhook_id=N) أول ما ننشئ الـ webhook —
    # لازم نرد 200 وإلا بيعتبره فاشل. ما بيغيّر أي شي عنا.
    if request.body.startswith(b"webhook_id="):
        return HttpResponse(status=200)

    # ربط موقوف أو تاجر موقوف: نرد 200 بلا أي أثر — رد خطأ يجعل WooCommerce يعطّل الـ webhook.
    if not connection.is_active or not merchant.is_active:
        return HttpResponse(status=200)

    signature = request.headers.get("X-WC-Webhook-Signature", "")
    if not woocommerce_service.verify_webhook_signature(request.body, signature, connection.webhook_secret):
        return HttpResponse(status=401)

    topic = request.headers.get("X-WC-Webhook-Topic", "")
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if topic in ("order.created", "order.updated"):
        _record_order(merchant, payload)
        return HttpResponse(status=200)

    external_id = str(payload.get("id", ""))
    if topic == "product.deleted":
        Product.objects.filter(merchant=merchant, external_id=external_id).delete()
    elif topic in ("product.created", "product.updated"):
        _upsert_product(merchant, external_id, payload)

    return HttpResponse(status=200)


def _upsert_product(merchant, external_id, payload):
    """نحدّث المنتج من محتوى الإشعار فقط، دون أي طلب REST إضافي يحجز رد الإشعار
    (WooCommerce يعطّل الـ webhook بعد إخفاقات متكررة). إشعار المنتج المتغيّر لا يحمل
    إلا أرقام النسخ؛ لذا نُبقي الخيارات المتوفرة المعروفة من آخر مزامنة (ضمن خيارات
    المنتج الحالية)، والتحديث الكامل للنسخ تتولاه sync_woocommerce كل ساعة."""
    variations = payload.get("variations") or []
    if "_variations" not in payload and variations and all(isinstance(v, dict) for v in variations):
        payload["_variations"] = variations
    row = woocommerce_service.product_row_from_payload(payload)
    if row is None:
        # المنتج صار مسودة/مخفي/بدون صورة — منشيله من الكتالوج بدل ما المساعد
        # يعرض منتج الزبون ما بيقدر يشتريه.
        Product.objects.filter(merchant=merchant, external_id=external_id).delete()
        return
    row.pop("external_id")
    if payload.get("type") == "variable" and "_variations" not in payload:
        existing = Product.objects.filter(merchant=merchant, external_id=external_id).first()
        if existing is not None and existing.options:
            row["options"] = {
                name: [v for v in (existing.options.get(name) or values) if v in values]
                for name, values in row["options"].items()
            }
            row["options"] = {name: values for name, values in row["options"].items() if values}
    Product.objects.update_or_create(merchant=merchant, external_id=external_id, defaults=row)


def start_initial_sync(connection_pk, callback_url):
    """المزامنة الكاملة وتسجيل الـ webhooks في الخلفية، حتى لا ينتهي وقت طلب الموظف
    مع الكتالوجات الكبيرة (على نمط سلة وزد)."""
    threading.Thread(target=_initial_sync, args=(connection_pk, callback_url), daemon=True).start()


def _initial_sync(connection_pk, callback_url):
    try:
        from .services import catalog as catalog_service

        connection = WooCommerceConnection.objects.select_related("merchant").get(pk=connection_pk)
        # المزامنة أولاً: لو فشل تسجيل الـ webhooks (مفاتيح Read فقط مثلاً) يبقى الكتالوج جاهزاً.
        count = catalog_service.sync_woocommerce(connection.merchant, connection)
        try:
            orders_ok = woocommerce_service.register_webhooks(connection, callback_url)
        except woocommerce_service.WooCommerceError:
            logger.warning("woo_webhook_register_failed connection=%s", connection_pk, exc_info=True)
            orders_ok = None
        logger.info("woo_initial_sync connection=%s products=%s order_hooks=%s", connection_pk, count, orders_ok)
    except Exception:  # noqa: BLE001 — خيط بالخلفية، أي خطأ لازم ينسجّل بس
        logger.exception("woo_initial_sync_failed connection=%s", connection_pk)
    finally:
        db_connection.close()


def _order_created_at(payload):
    """date_created_gmt من WooCommerce بدون منطقة زمنية (مثلاً 2026-11-15T10:00:00) وهو GMT."""
    try:
        created = datetime.fromisoformat(str(payload.get("date_created_gmt") or ""))
    except ValueError:
        return timezone.now()
    return created.replace(tzinfo=dt_timezone.utc) if created.tzinfo is None else created


def _record_order(merchant, payload):
    """بيربط الطلب (لما يصير بيع فعلي) بالمحادثة اللي جابته — شوف ATTRIBUTION_WINDOW.
    الطلب بيوصل مرتين (created ثم updated) فكل شي هون آمن للتكرار."""
    if not woocommerce_service.order_is_counted(payload):
        return
    order_id = str(payload.get("id") or "")[:64]
    product_ids = woocommerce_service.order_product_ids(payload)
    if not order_id or not product_ids:
        return
    skus = set(
        Product.objects.filter(merchant=merchant, external_id__in=product_ids).values_list("sku", flat=True)
    )
    if not skus:
        return

    order_at = _order_created_at(payload)
    candidates = Conversation.objects.filter(
        Q(add_to_carts__gt=0) | Q(buy_clicks__gt=0),
        merchant=merchant, order_id="",
        started_at__lte=order_at, updated_at__gte=order_at - ATTRIBUTION_WINDOW,
    ).order_by("-updated_at")

    with transaction.atomic():
        locked = list(candidates.select_for_update())
        # بعد القفل: لو إشعار تاني لنفس الطلب سبقنا وربطه، منوقف.
        if Conversation.objects.filter(merchant=merchant, order_id=order_id).exists():
            return
        conversation = next(
            (c for c in locked if skus & {*(c.recommended_skus or []), *(c.acted_skus or [])}), None
        )
        if conversation is None:
            return
        try:
            total = Decimal(str(payload.get("total") or "0"))
        except InvalidOperation:
            total = None
        conversation.order_id = order_id
        conversation.order_total = total
        conversation.order_currency = (payload.get("currency") or "")[:8]
        conversation.order_at = order_at
        conversation.save(
            update_fields=["order_id", "order_total", "order_currency", "order_at", "updated_at"]
        )
