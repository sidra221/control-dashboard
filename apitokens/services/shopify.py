import base64
import hashlib
import hmac
import re
from decimal import Decimal, InvalidOperation

import requests

API_VERSION = "2024-10"
REQUEST_TIMEOUT_SECONDS = 20
WEBHOOK_TOPICS = ["products/create", "products/update", "products/delete"]

_TAG_RE = re.compile(r"<[^>]+>")


class ShopifyError(Exception):
    pass


def _headers(access_token):
    return {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}


def _base_url(shop_domain):
    return f"https://{shop_domain}/admin/api/{API_VERSION}"


def get_shop_info(shop_domain, access_token):
    try:
        resp = requests.get(
            f"{_base_url(shop_domain)}/shop.json",
            headers=_headers(access_token),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ShopifyError(f"تعذر الاتصال بـ Shopify: {exc}") from exc
    if resp.status_code == 401:
        raise ShopifyError("التوكن غير صحيح أو ما إلو صلاحية — تأكد من الـ Admin API access token")
    if not resp.ok:
        raise ShopifyError(f"رد غير متوقع من Shopify ({resp.status_code})")
    return resp.json().get("shop", {})


def register_webhooks(shop_domain, access_token, callback_url):
    """بتسجّل webhooks لتغييرات المنتجات إذا مش مسجّلة أصلاً على نفس العنوان."""
    try:
        existing_resp = requests.get(
            f"{_base_url(shop_domain)}/webhooks.json",
            headers=_headers(access_token),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        existing_resp.raise_for_status()
    except requests.RequestException as exc:
        raise ShopifyError(f"تعذر قراءة الـ webhooks الحالية: {exc}") from exc

    existing = existing_resp.json().get("webhooks", [])
    existing_keys = {(w["topic"], w["address"]) for w in existing}

    for topic in WEBHOOK_TOPICS:
        if (topic, callback_url) in existing_keys:
            continue
        try:
            resp = requests.post(
                f"{_base_url(shop_domain)}/webhooks.json",
                headers=_headers(access_token),
                json={"webhook": {"topic": topic, "address": callback_url, "format": "json"}},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ShopifyError(f"تعذر تسجيل webhook لـ {topic}: {exc}") from exc
        if not resp.ok:
            raise ShopifyError(f"فشل تسجيل webhook لـ {topic}: {resp.status_code} {resp.text[:200]}")


def fetch_all_products(shop_domain, access_token):
    products = []
    url = f"{_base_url(shop_domain)}/products.json?limit=250"
    headers = _headers(access_token)
    while url:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise ShopifyError(f"تعذر جلب المنتجات: {exc}") from exc
        if not resp.ok:
            raise ShopifyError(f"فشل جلب المنتجات: {resp.status_code}")
        data = resp.json()
        products.extend(data.get("products", []))
        url = _next_page_url(resp.headers.get("Link", ""))
    return products


def _next_page_url(link_header):
    if not link_header:
        return None
    for part in link_header.split(","):
        segment = part.strip()
        if segment.endswith('rel="next"'):
            start = segment.find("<") + 1
            end = segment.find(">")
            if start > 0 and end > start:
                return segment[start:end]
    return None


def _strip_html(html):
    return _TAG_RE.sub("", html or "").strip()


def product_row_from_payload(shop_domain, payload):
    """بيحوّل منتج Shopify خام (من الـ REST API أو من webhook) لصف جاهز
    لموديل Product. برجّع None إذا المنتج ما إلو صورة (موديلنا بيطلبها).
    """
    images = payload.get("images") or []
    if not images:
        return None

    variants = payload.get("variants") or []
    first_variant = variants[0] if variants else {}

    sku = (first_variant.get("sku") or "").strip() or f"SHOPIFY-{payload.get('id')}"
    try:
        price = Decimal(str(first_variant.get("price", "0")))
    except InvalidOperation:
        price = Decimal("0")

    return {
        "external_id": str(payload.get("id")),
        "sku": sku,
        "title": (payload.get("title") or "").strip(),
        "description": _strip_html(payload.get("body_html"))[:2000],
        "price": price,
        "image_url": images[0].get("src", ""),
        "product_url": f"https://{shop_domain}/products/{payload.get('handle', '')}",
        "category": (payload.get("product_type") or "").strip(),
        "tags": (payload.get("tags") or "").strip(),
    }


def verify_webhook_hmac(body_bytes, hmac_header, api_secret):
    if not hmac_header:
        return False
    digest = hmac.new(api_secret.encode("utf-8"), body_bytes, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header)
