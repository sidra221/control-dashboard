"""تكامل سلة (salla.sa): حفظ الربط لما التاجر يركّب التطبيق، تجديد التوكن،
واستدعاء Merchant API.

المرجع: مستودع سلة الرسمي SallaApp/salla-partners-agent-kit (مواقع التوثيق
docs.salla.dev بتحظر القراءة الآلية).
"""

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import ApiToken, SallaConnection

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
