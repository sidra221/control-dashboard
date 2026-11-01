"""تكامل سلة (salla.sa): حفظ الربط لما التاجر يركّب التطبيق، تجديد التوكن،
واستدعاء Merchant API.

المرجع: مستودع سلة الرسمي SallaApp/salla-partners-agent-kit (مواقع التوثيق
docs.salla.dev بتحظر القراءة الآلية).
"""

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import ApiToken, Product, SallaConnection
from .shopify import _clean_text as clean_html

logger = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.salla.sa/oauth2/token"
USER_INFO_URL = "https://accounts.salla.sa/oauth2/user/info"
API_BASE = "https://api.salla.dev/admin/v2"
REQUEST_TIMEOUT_SECONDS = 20
# منجدّد قبل الانتهاء بيوم (توصية سلة) بدل ما نستنى 401 بنص مزامنة.
REFRESH_BEFORE = timedelta(days=1)


class SallaError(Exception):
    pass


def _expires_at(expires):
    """سلة بتبعت `expires` كـ Unix timestamp مطلق، مش مدة."""
    try:
        return datetime.fromtimestamp(int(expires), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def save_authorization(salla_merchant_id, data):
    """إشعار app.store.authorize: بيوصل عند التركيب وبعد كل تحديث للتطبيق،
    فدايماً upsert. أول مرة منعمل للتاجر حساب (ApiToken) لحاله."""
    salla_merchant_id = str(salla_merchant_id)
    if not salla_merchant_id or not data.get("access_token"):
        raise SallaError("authorize_payload_incomplete")
    with transaction.atomic():
        connection = (
            SallaConnection.objects.select_for_update()
            .filter(salla_merchant_id=salla_merchant_id)
            .first()
        )
        if connection is None:
            merchant = ApiToken.objects.create(
                label=f"Salla {salla_merchant_id}",
                image_quota=settings.SALLA_DEFAULT_IMAGE_QUOTA,
                currency="SAR",
            )
            connection = SallaConnection(merchant=merchant, salla_merchant_id=salla_merchant_id)
        connection.access_token = data["access_token"]
        connection.refresh_token = data.get("refresh_token") or connection.refresh_token
        connection.token_expires_at = _expires_at(data.get("expires"))
        connection.scope = (data.get("scope") or "")[:500]
        connection.is_active = True
        connection.save()
        if not connection.merchant.is_active:
            connection.merchant.is_active = True
            connection.merchant.save(update_fields=["is_active"])
    _fill_store_info(connection)
    return connection


def _fill_store_info(connection):
    """اسم المتجر ورابطه للداشبورد — فشلها ما بيوقف الربط."""
    try:
        resp = requests.get(
            USER_INFO_URL,
            headers={"Authorization": f"Bearer {connection.access_token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        store = resp.json().get("merchant") or {}
    except (requests.RequestException, ValueError):
        logger.warning("salla_user_info_failed merchant=%s", connection.salla_merchant_id)
        return
    connection.store_name = (store.get("name") or "")[:255]
    connection.store_domain = (store.get("domain") or "")[:255]
    connection.save(update_fields=["store_name", "store_domain", "updated_at"])
    merchant = connection.merchant
    if connection.store_name and merchant.label.startswith("Salla "):
        merchant.label = connection.store_name[:120]
        merchant.save(update_fields=["label"])


def deactivate(salla_merchant_id):
    """app.uninstalled: التوكنات صارت ميتة — منوقف الربط والودجت."""
    connection = SallaConnection.objects.filter(salla_merchant_id=str(salla_merchant_id)).first()
    if connection is None:
        return None
    connection.is_active = False
    connection.access_token = ""
    connection.refresh_token = ""
    connection.save(update_fields=["is_active", "access_token", "refresh_token", "updated_at"])
    connection.merchant.is_active = False
    connection.merchant.save(update_fields=["is_active"])
    return connection


def _needs_refresh(connection):
    return connection.token_expires_at is None or (
        connection.token_expires_at - timezone.now() < REFRESH_BEFORE
    )


def get_access_token(connection, force_refresh=False):
    """توكن صالح. التجديد جوا قفل على سطر الربط: refresh_token بينستخدم مرة
    وحدة، وطلبين بنفس الوقت (مزامنة + webhook) كانوا رح يلغوا الربط."""
    if not connection.is_active:
        raise SallaError("connection_inactive")
    if not force_refresh and not _needs_refresh(connection):
        return connection.access_token
    stale_token = connection.access_token
    with transaction.atomic():
        locked = SallaConnection.objects.select_for_update().get(pk=connection.pk)
        # عملية تانية جدّدت وإحنا ناطرين القفل — منستخدم توكنها بدل ما نحرق الـ refresh.
        if locked.access_token != stale_token or (not force_refresh and not _needs_refresh(locked)):
            _copy_tokens(locked, connection)
            return locked.access_token
        if not locked.refresh_token:
            raise SallaError("no_refresh_token")
        try:
            resp = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": locked.refresh_token,
                    "client_id": settings.SALLA_CLIENT_ID,
                    "client_secret": settings.SALLA_CLIENT_SECRET,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise SallaError(f"refresh_request_failed: {exc}") from exc
        if not resp.ok:
            logger.error(
                "salla_refresh_failed merchant=%s status=%s body=%s",
                locked.salla_merchant_id, resp.status_code, resp.text[:200],
            )
            raise SallaError(f"refresh_failed:{resp.status_code}")
        data = resp.json()
        # لازم نحفظ التنين: الـ refresh القديم صار ميت.
        locked.access_token = data["access_token"]
        locked.refresh_token = data.get("refresh_token") or ""
        locked.token_expires_at = (
            _expires_at(data.get("expires"))
            or timezone.now() + timedelta(seconds=int(data.get("expires_in") or 0))
        )
        locked.save(update_fields=["access_token", "refresh_token", "token_expires_at", "updated_at"])
    _copy_tokens(locked, connection)
    return connection.access_token


def _copy_tokens(source, target):
    target.access_token = source.access_token
    target.refresh_token = source.refresh_token
    target.token_expires_at = source.token_expires_at


def api_get(connection, path, params=None):
    """GET على Merchant API. على 401 منجدّد مرة وحدة ومنعيد."""
    for attempt in range(2):
        token = get_access_token(connection, force_refresh=attempt > 0)
        try:
            resp = requests.get(
                f"{API_BASE}{path}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params=params or {},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise SallaError(f"request_failed: {exc}") from exc
        if resp.status_code == 401 and attempt == 0:
            continue
        if not resp.ok:
            raise SallaError(f"api_error:{resp.status_code}:{resp.text[:200]}")
        return resp.json()
    raise SallaError("unauthorized")


# ---------------------------------------------------------------------------
# الإشعارات والمنتجات
# ---------------------------------------------------------------------------


def verify_webhook(body_bytes, headers, secret):
    """سلة بتدعم استراتيجيتين: signature (HMAC-SHA256 للجسم الخام بهيدر
    x-salla-signature) أو token (هيدر Authorization = السر نفسه)."""
    if not secret:
        return False
    signature = (headers.get("X-Salla-Signature") or "").strip().lower()
    if signature:
        expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    authorization = (headers.get("Authorization") or "").strip()
    return bool(authorization) and hmac.compare_digest(authorization, secret)


def _amount(value):
    """سلة عندها أكتر من شكل للسعر: رقم، نص، أو {"amount": .., "currency": ..}."""
    if isinstance(value, dict):
        value = value.get("amount")
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def _names(items):
    """قائمة أسماء من [{"name": ..}] أو ["..."]."""
    names = []
    for item in items or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name and str(name).strip():
            names.append(str(name).strip())
    return names


def _main_image(payload):
    images = payload.get("images") or []
    main = next((i for i in images if isinstance(i, dict) and i.get("main")), None)
    for candidate in (main, images[0] if images else None):
        if isinstance(candidate, dict) and candidate.get("url"):
            return candidate["url"]
    main_image = payload.get("main_image")
    if isinstance(main_image, dict):
        main_image = main_image.get("url")
    return main_image or payload.get("thumbnail") or ""


def _is_available(payload):
    if payload.get("is_available") is False or payload.get("status") in ("out", "hidden", "deleted"):
        return False
    if payload.get("unlimited_quantity"):
        return True
    quantity = payload.get("quantity")
    return quantity is None or (isinstance(quantity, (int, float)) and quantity > 0)


def product_row_from_payload(payload):
    """منتج سلة (من الـ API أو من إشعار) لصف جاهز لموديل Product. برجّع None
    لمنتج مخفي أو بلا صورة — هيك بينشال من الكتالوج."""
    product_id = str(payload.get("id") or "")
    image_url = _main_image(payload)
    if not product_id or not image_url or payload.get("status") in ("hidden", "deleted"):
        return None

    regular = _amount(payload.get("regular_price")) or _amount(payload.get("price"))
    sale = _amount(payload.get("sale_price"))
    if sale is not None and regular is not None and sale < regular:
        price, compare_at = sale, regular
    else:
        price, compare_at = (regular or sale or Decimal("0")), None

    urls = payload.get("urls") if isinstance(payload.get("urls"), dict) else {}
    options = {}
    for option in payload.get("options") or []:
        name = (option.get("name") or "").strip() if isinstance(option, dict) else ""
        values = _names(option.get("values")) if isinstance(option, dict) else []
        if name and values:
            options[name] = values

    return {
        "external_id": product_id,
        "sku": (payload.get("sku") or "").strip() or f"SALLA-{product_id}",
        "title": (payload.get("name") or "").strip()[:255],
        "description": clean_html(payload.get("description"))[:2000],
        "price": price,
        "compare_at_price": compare_at,
        "image_url": image_url[:500],
        "product_url": (urls.get("customer") or payload.get("url") or "")[:500],
        "category": ", ".join(_names(payload.get("categories")))[:120],
        "tags": ", ".join(_names(payload.get("tags")))[:500],
        "options": options,
        "in_stock": _is_available(payload),
        "variant_id": "",
    }


def upsert_product(merchant, payload):
    row = product_row_from_payload(payload)
    product_id = str(payload.get("id") or "")
    if row is None:
        Product.objects.filter(merchant=merchant, external_id=product_id).delete()
        return None
    external_id = row.pop("external_id")
    product, _ = Product.objects.update_or_create(
        merchant=merchant, external_id=external_id, defaults=row,
    )
    return product


PRODUCTS_PER_PAGE = 60  # أقصى عدد بتسمح فيه سلة بالصفحة
MAX_PRODUCT_PAGES = 200  # سقف أمان (12,000 منتج) ضد pagination خربان
RATE_LIMIT_RETRIES = 3


def fetch_all_products(connection):
    """كل منتجات المتجر صفحة صفحة. على 429 (حد الطلبات بالدقيقة) منستنى ومنعيد."""
    page = 1
    while page <= MAX_PRODUCT_PAGES:
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                body = api_get(connection, "/products", {"page": page, "per_page": PRODUCTS_PER_PAGE})
                break
            except SallaError as exc:
                if not str(exc).startswith("api_error:429") or attempt == RATE_LIMIT_RETRIES:
                    raise
                time.sleep(10 * (attempt + 1))
        yield from body.get("data") or []
        pagination = body.get("pagination") or {}
        if page >= int(pagination.get("totalPages") or 1):
            return
        page += 1
