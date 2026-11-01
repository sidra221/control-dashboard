"""تكامل زد (zid.sa): تبادل كود OAuth بالمفاتيح، تجديدها، واستدعاء Merchant API.

المرجع: docs.zid.sa (صفحة Authorization ومتطلبات OAuth للتطبيقات العامة).
"""

import base64
import hmac
import logging
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import ApiToken, Product, ZidConnection
from .shopify import _clean_text as clean_html

logger = logging.getLogger(__name__)

OAUTH_BASE = "https://oauth.zid.sa"
AUTHORIZE_URL = f"{OAUTH_BASE}/oauth/authorize"
TOKEN_URL = f"{OAUTH_BASE}/oauth/token"
API_BASE = "https://api.zid.sa/v1"
REQUEST_TIMEOUT_SECONDS = 20
# الـ refresh بيعيش سنة — منجدّد كل ~10 شهور بغض النظر عن أي خطأ (توصية زد).
REFRESH_EVERY = timedelta(days=300)


class ZidError(Exception):
    pass


def callback_url():
    """لازم يطابق الـ Callback URL المسجّل ببوابة الشركاء حرفياً (حتى الـ /)."""
    return f"{settings.SITE_BASE_URL}/zid/callback/"


def authorize_url(state):
    from urllib.parse import urlencode

    return AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": settings.ZID_CLIENT_ID,
            "redirect_uri": callback_url(),
            "response_type": "code",
            "state": state,
        }
    )


def _token_request(data):
    payload = {
        "client_id": settings.ZID_CLIENT_ID,
        "client_secret": settings.ZID_CLIENT_SECRET,
        "redirect_uri": callback_url(),
        **data,
    }
    try:
        resp = requests.post(TOKEN_URL, data=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise ZidError(f"token_request_failed: {exc}") from exc
    if not resp.ok:
        # جسم الخطأ ما فيه أسرار، بس الكود والتوكنات ما منسجّلهم أبداً.
        logger.error("zid_token_failed grant=%s status=%s body=%s",
                     data.get("grant_type"), resp.status_code, resp.text[:200])
        raise ZidError(f"token_failed:{resp.status_code}")
    tokens = resp.json()
    if not tokens.get("authorization") or not tokens.get("access_token"):
        raise ZidError("token_response_incomplete")
    return tokens


def _apply_tokens(connection, tokens):
    connection.authorization_token = tokens["authorization"]
    connection.manager_token = tokens["access_token"]
    connection.refresh_token = tokens.get("refresh_token") or ""
    expires_in = int(tokens.get("expires_in") or 0)
    connection.token_expires_at = timezone.now() + timedelta(seconds=expires_in) if expires_in else None
    connection.tokens_refreshed_at = timezone.now()


def _headers(authorization_token, manager_token):
    return {
        "Authorization": f"Bearer {authorization_token}",
        "X-Manager-Token": manager_token,
        # بدون هالهيدر زد بترد بالعربي حتى رسائل الخطأ.
        "Accept-Language": "en",
        "Accept": "application/json",
    }


def complete_installation(code):
    """الـ callback: الكود بيعيش 10 دقايق وبينستخدم مرة وحدة — منبدّله فوراً،
    منعرف المتجر من زد نفسها (ما منسأل التاجر)، ومنعمل/منحدّث الربط."""
    tokens = _token_request({"grant_type": "authorization_code", "code": code})
    try:
        resp = requests.get(
            f"{API_BASE}/managers/account/store",
            headers=_headers(tokens["authorization"], tokens["access_token"]),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        store = resp.json().get("store") or {}
    except (requests.RequestException, ValueError) as exc:
        raise ZidError(f"store_lookup_failed: {exc}") from exc
    store_id = str(store.get("id") or "")
    if not store_id:
        raise ZidError("store_lookup_incomplete")

    with transaction.atomic():
        connection = ZidConnection.objects.select_for_update().filter(store_id=store_id).first()
        if connection is None:
            merchant = ApiToken.objects.create(
                label=(store.get("title") or f"Zid {store_id}")[:120],
                image_quota=settings.ZID_DEFAULT_IMAGE_QUOTA,
                currency="SAR",
            )
            connection = ZidConnection(merchant=merchant, store_id=store_id)
        _apply_tokens(connection, tokens)
        connection.store_uuid = str(store.get("uuid") or "")[:64]
        connection.store_name = (store.get("title") or "")[:255]
        connection.store_url = (store.get("url") or "")[:255]
        connection.is_active = True
        connection.save()
        if not connection.merchant.is_active:
            connection.merchant.is_active = True
            connection.merchant.save(update_fields=["is_active"])
    # اشتراك وصل قبل ما التاجر يكمّل التفعيل (زد بتقبض قبل OAuth).
    apply_subscription(connection, new_period=True)
    return connection


def deactivate(store_id="", store_uuid=""):
    """إلغاء التثبيت: زد بتلغي المفاتيح فوراً — منوقف الربط والودجت."""
    lookup = {"store_id": str(store_id)} if store_id else {"store_uuid": str(store_uuid)}
    if not any(lookup.values()):
        return None
    connection = ZidConnection.objects.filter(**lookup).select_related("merchant").first()
    if connection is None:
        return None
    connection.is_active = False
    connection.authorization_token = ""
    connection.manager_token = ""
    connection.refresh_token = ""
    connection.save(update_fields=[
        "is_active", "authorization_token", "manager_token", "refresh_token", "updated_at",
    ])
    connection.merchant.is_active = False
    connection.merchant.save(update_fields=["is_active"])
    return connection


def needs_scheduled_refresh(connection):
    return connection.tokens_refreshed_at is None or (
        timezone.now() - connection.tokens_refreshed_at > REFRESH_EVERY
    )


def refresh_tokens(connection):
    """التجديد بيرجّع refresh جديد وبيلغي القديم — منحفظ الطقم كامل جوا قفل،
    وإذا عملية تانية جدّدت وإحنا ناطرين القفل منستخدم نتيجتها."""
    stale_refresh = connection.refresh_token
    with transaction.atomic():
        locked = ZidConnection.objects.select_for_update().get(pk=connection.pk)
        if not locked.is_active:
            raise ZidError("connection_inactive")
        if locked.refresh_token != stale_refresh:
            _copy_tokens(locked, connection)
            return connection
        if not locked.refresh_token:
            raise ZidError("no_refresh_token")
        tokens = _token_request({"grant_type": "refresh_token", "refresh_token": locked.refresh_token})
        _apply_tokens(locked, tokens)
        locked.save(update_fields=[
            "authorization_token", "manager_token", "refresh_token",
            "token_expires_at", "tokens_refreshed_at", "updated_at",
        ])
    _copy_tokens(locked, connection)
    return connection


def _copy_tokens(source, target):
    for field in ("authorization_token", "manager_token", "refresh_token",
                  "token_expires_at", "tokens_refreshed_at"):
        setattr(target, field, getattr(source, field))


def api_request(connection, method, path, params=None, json_body=None, language="en"):
    """طلب على Merchant API بالمفتاحين مع بعض."""
    if not connection.is_active or not connection.authorization_token:
        raise ZidError("connection_inactive")
    headers = _headers(connection.authorization_token, connection.manager_token)
    headers["Accept-Language"] = language
    # endpoints المنتجات بتقرا نفس القيمة باسم Access-Token (لأسباب تاريخية).
    headers["Access-Token"] = connection.manager_token
    headers["Store-Id"] = connection.store_id
    headers["Role"] = "Manager"
    try:
        resp = requests.request(
            method, f"{API_BASE}{path}", headers=headers, params=params or {},
            json=json_body, timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ZidError(f"request_failed: {exc}") from exc
    if not resp.ok:
        raise ZidError(f"api_error:{resp.status_code}:{resp.text[:200]}")
    return resp.json() if resp.content else {}


# ---------------------------------------------------------------------------
# الاشتراكات
# ---------------------------------------------------------------------------

ACTIVATING_EVENTS = {
    "app.market.subscription.active",
    "app.market.subscription.renew",
    "app.market.subscription.upgrade",
}
ENDING_EVENTS = {
    "app.market.subscription.suspended",
    "app.market.subscription.expired",
    "app.market.subscription.refunded",
}


def _parse_date(value):
    from django.utils.dateparse import parse_datetime

    try:
        return parse_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def plan_quota(subscription):
    """رصيد الصور الشهري للباقة، من ZID_PLAN_QUOTAS (بالرقم أو بالاسم)."""
    quotas = settings.ZID_PLAN_QUOTAS or {}
    for key in (subscription.plan_id, subscription.plan_name):
        if key and str(key) in quotas:
            return int(quotas[str(key)])
    return settings.ZID_DEFAULT_IMAGE_QUOTA


def record_subscription_event(payload):
    """بيسجّل حالة الاشتراك (حتى لو التاجر لسا ما كمّل OAuth) وبيطبّقها على
    حسابه إذا موجود. منفرّق بين الأحداث بـ event_name بس (توصية زد)."""
    from ..models import ZidSubscription

    event = payload.get("event_name") or ""
    store_id = str(payload.get("store_id") or "")
    if not store_id:
        return None
    subscription, _ = ZidSubscription.objects.get_or_create(store_id=store_id)
    subscription.store_uuid = str(payload.get("store_uuid") or subscription.store_uuid)[:64]
    subscription.last_event = event[:80]
    renewed = event in ACTIVATING_EVENTS
    if renewed:
        subscription.is_active = True
        subscription.plan_id = str(payload.get("plan_id") or "")[:32]
        subscription.plan_name = (payload.get("plan_name") or "")[:120]
        subscription.plan_type = (payload.get("plan_type") or "")[:32]
        subscription.end_date = _parse_date(payload.get("end_date"))
    elif event in ENDING_EVENTS:
        subscription.is_active = False
    subscription.save()

    connection = ZidConnection.objects.filter(store_id=store_id).select_related("merchant").first()
    if connection is not None:
        apply_subscription(connection, subscription, new_period=renewed)
    return subscription


def apply_subscription(connection, subscription=None, new_period=False):
    """بيطبّق الباقة على حساب التاجر: الرصيد الشهري، وتوقيف الودجت لما ينتهي
    الاشتراك. متجر ما إله اشتراك مسجّل (قبل ما نعمل باقات) بيضل متل ما هو."""
    from ..models import ZidSubscription

    if subscription is None:
        subscription = ZidSubscription.objects.filter(store_id=connection.store_id).first()
        if subscription is None:
            return
    merchant = connection.merchant
    fields = []
    if subscription.is_active:
        quota = plan_quota(subscription)
        if merchant.image_quota != quota:
            merchant.image_quota = quota
            fields.append("image_quota")
        if new_period:
            # فترة اشتراك جديدة (تفعيل/تجديد/ترقية) = رصيد جديد.
            merchant.images_used = 0
            merchant.period_start = timezone.now()
            fields += ["images_used", "period_start"]
    should_be_active = subscription.is_active and connection.is_active
    if merchant.is_active != should_be_active:
        merchant.is_active = should_be_active
        fields.append("is_active")
    if fields:
        merchant.save(update_fields=fields)


# ---------------------------------------------------------------------------
# المنتجات وإشعارات المتجر
# ---------------------------------------------------------------------------

PRODUCTS_PAGE_SIZE = 50
MAX_PRODUCT_PAGES = 300  # سقف أمان ضد pagination خربان
RATE_LIMIT_RETRIES = 3  # زد: 60 طلب بالدقيقة لكل متجر
STORE_WEBHOOK_EVENTS = ["product.create", "product.update", "product.publish", "product.delete", "order.create"]
STORE_WEBHOOK_USERNAME = "control"


def _localized(value):
    """زد بترجّع النصوص {ar, en} (مع all-languages) أو نص عادي. السوق
    سعودي فالعربي أولاً."""
    if isinstance(value, dict):
        return (value.get("ar") or value.get("en") or "").strip()
    return (value or "").strip() if isinstance(value, str) else ""


def _amount(value):
    if isinstance(value, dict):
        value = value.get("amount", value.get("value"))
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def _image_url(payload):
    for item in payload.get("images") or []:
        if isinstance(item, str) and item:
            return item
        if not isinstance(item, dict):
            continue
        image = item.get("image") if isinstance(item.get("image"), dict) else item
        for size in ("full_size", "large", "medium", "original", "url", "small", "thumbnail"):
            if image.get(size):
                return image[size]
    main = payload.get("main_image") or payload.get("thumbnail")
    if isinstance(main, dict):
        main = main.get("url") or main.get("full_size")
    return main or ""


def _options(payload):
    options = {}
    for option in payload.get("options") or []:
        if not isinstance(option, dict):
            continue
        name = _localized(option.get("name_i18n")) or _localized(option.get("name"))
        values = []
        for choice in option.get("choices") or option.get("values") or []:
            if isinstance(choice, dict):
                label = _localized(choice.get("value")) or _localized(choice.get("name"))
            else:
                label = str(choice).strip()
            if label and label not in values:
                values.append(label)
        if name and values:
            options[name] = values
    return options


def product_row_from_payload(payload):
    """منتج زد (API أو إشعار) لصف Product. برجّع None لمسودة/غير منشور/بلا صورة."""
    product_id = str(payload.get("id") or "")
    if not product_id or payload.get("is_draft") or payload.get("is_published") is False:
        return None
    image_url = _image_url(payload)
    if not image_url:
        return None
    regular = _amount(payload.get("price"))
    sale = _amount(payload.get("sale_price"))
    if sale is not None and regular is not None and sale < regular:
        price, compare_at = sale, regular
    else:
        price, compare_at = (regular or sale or Decimal("0")), None
    quantity = payload.get("quantity")
    in_stock = bool(payload.get("is_infinite")) or quantity is None or (
        isinstance(quantity, (int, float)) and quantity > 0
    )
    description = _localized(payload.get("description")) or _localized(payload.get("short_description"))
    categories = [_localized(c.get("name")) for c in payload.get("categories") or [] if isinstance(c, dict)]
    keywords = payload.get("keywords") or []
    return {
        "external_id": product_id,
        "sku": (payload.get("sku") or "").strip()[:64] or f"ZID-{product_id[:58]}",
        "title": _localized(payload.get("name"))[:255],
        "description": clean_html(description)[:2000],
        "price": price,
        "compare_at_price": compare_at,
        "image_url": image_url[:500],
        "product_url": (payload.get("html_url") or "")[:500],
        "category": ", ".join(c for c in categories if c)[:120],
        "tags": ", ".join(str(k) for k in keywords if k)[:500] if isinstance(keywords, list) else "",
        "options": _options(payload),
        "in_stock": in_stock,
        "variant_id": "",
    }


def upsert_product(merchant, payload):
    product_id = str(payload.get("id") or "")
    row = product_row_from_payload(payload)
    if row is None:
        Product.objects.filter(merchant=merchant, external_id=product_id).delete()
        return None
    external_id = row.pop("external_id")
    product, _ = Product.objects.update_or_create(merchant=merchant, external_id=external_id, defaults=row)
    return product


def fetch_all_products(connection):
    """كل منتجات المتجر بالعربي والإنجليزي، صفحة صفحة. على 429 منستنى ومنعيد."""
    page = 1
    while page <= MAX_PRODUCT_PAGES:
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                body = api_request(
                    connection, "GET", "/products/",
                    params={"page": page, "page_size": PRODUCTS_PAGE_SIZE}, language="all-languages",
                )
                break
            except ZidError as exc:
                if not str(exc).startswith("api_error:429") or attempt == RATE_LIMIT_RETRIES:
                    raise
                time.sleep(15 * (attempt + 1))
        yield from body.get("results") or []
        if not body.get("next"):
            return
        page += 1


def store_webhook_url(connection, event):
    # رقم المتجر واسم الحدث بالرابط نفسه — ما منعتمد على شكل جسم الإشعار.
    return f"{settings.SITE_BASE_URL}/webhooks/zid/store/{connection.store_id}/{event}/"


def register_store_webhooks(connection):
    """إشعارات المنتجات والطلبات بتنسجّل لكل متجر عبر الـ API (صلاحية
    Webhooks)، مع Basic Auth بكلمة سرنا حتى نتأكد إنها من زد."""
    registered = []
    for event in STORE_WEBHOOK_EVENTS:
        try:
            api_request(connection, "POST", "/managers/webhooks", json_body={
                "event": event,
                "target_url": store_webhook_url(connection, event),
                "original_id": settings.ZID_CLIENT_ID,
                "username": STORE_WEBHOOK_USERNAME,
                "password": settings.ZID_WEBHOOK_SECRET,
            })
            registered.append(event)
        except ZidError as exc:
            # order.create مثلاً بدّه صلاحية الطلبات — منكمّل بالباقي.
            logger.warning("zid_webhook_register_failed store=%s event=%s error=%s",
                           connection.store_id, event, exc)
    return registered


def verify_store_webhook(authorization_header):
    secret = settings.ZID_WEBHOOK_SECRET
    if not secret or not authorization_header.startswith("Basic "):
        return False
    expected = base64.b64encode(f"{STORE_WEBHOOK_USERNAME}:{secret}".encode()).decode()
    return hmac.compare_digest(authorization_header[6:].strip(), expected)
