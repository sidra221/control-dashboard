"""تكامل زد (zid.sa): تبادل كود OAuth بالمفاتيح، تجديدها، واستدعاء Merchant API.

المرجع: docs.zid.sa (صفحة Authorization ومتطلبات OAuth للتطبيقات العامة).
"""

import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import ApiToken, ZidConnection

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


def api_request(connection, method, path, params=None, json_body=None):
    """طلب على Merchant API بالمفتاحين مع بعض."""
    if not connection.is_active or not connection.authorization_token:
        raise ZidError("connection_inactive")
    headers = _headers(connection.authorization_token, connection.manager_token)
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
