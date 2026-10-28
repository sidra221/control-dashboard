import base64
import hashlib
import hmac
import re
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import requests
from django.utils import timezone

# نسخ الـ API بتنطفي بعد ~12 شهر — 2026-07 مدعومة لمنتصف 2027.
API_VERSION = "2026-07"
REQUEST_TIMEOUT_SECONDS = 20
PRODUCT_WEBHOOK_TOPICS = ["PRODUCTS_CREATE", "PRODUCTS_UPDATE", "PRODUCTS_DELETE"]
# بيحتاج صلاحية read_orders — اختياري: لو مش متاح منكمّل بدون قياس الطلبات.
ORDER_WEBHOOK_TOPIC = "ORDERS_CREATE"
# الكود بيقرأ هالخاصية من الطلب حتى يعرف أي محادثة جابت هالطلب.
CART_ATTRIBUTE_KEY = "_sw_conversation"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class ShopifyError(Exception):
    pass


# ---------------------------------------------------------------------------
# المصادقة: تطبيقات Dev Dashboard (من 2026) بتعطي client_id + secret ومنبدّلهم
# بتوكن صالح 24 ساعة. التطبيقات القديمة (Custom App بالأدمن) عندها توكن ثابت.
# ---------------------------------------------------------------------------


def exchange_client_credentials(shop_domain, client_id, client_secret):
    try:
        resp = requests.post(
            f"https://{shop_domain}/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise ShopifyError(f"تعذر الاتصال بـ Shopify: {exc}") from exc
    if not resp.ok:
        detail = resp.text[:200]
        if "shop_not_permitted" in detail:
            raise ShopifyError(
                "Shopify رفض الطلب: التطبيق والمتجر لازم يكونوا بنفس المنظمة (organization) "
                "بالـ Dev Dashboard، والتطبيق لازم يكون منزّل على المتجر."
            )
        raise ShopifyError(f"فشل الحصول على توكن من Shopify ({resp.status_code}): {detail}")
    data = resp.json()
    return data["access_token"], int(data.get("expires_in") or 86399)


def get_access_token(connection):
    """برجّع توكن صالح للاتصال، وبيجدّده لحاله لو التطبيق من نوع client
    credentials وقرّب ينتهي (منجدّد قبلها بـ 10 دقايق)."""
    if not connection.client_id:
        return connection.access_token
    now = timezone.now()
    if (
        connection.access_token
        and connection.token_expires_at
        and connection.token_expires_at - timedelta(minutes=10) > now
    ):
        return connection.access_token
    token, expires_in = exchange_client_credentials(
        connection.shop_domain, connection.client_id, connection.api_secret
    )
    connection.access_token = token
    connection.token_expires_at = now + timedelta(seconds=expires_in)
    if connection.pk:
        connection.save(update_fields=["access_token", "token_expires_at"])
    return token


# ---------------------------------------------------------------------------
# GraphQL Admin API (الـ REST للمنتجات صار legacy)
# ---------------------------------------------------------------------------


def graphql(shop_domain, access_token, query, variables=None, max_throttle_retries=5):
    url = f"https://{shop_domain}/admin/api/{API_VERSION}/graphql.json"
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
    for attempt in range(max_throttle_retries + 1):
        try:
            resp = requests.post(
                url,
                json={"query": query, "variables": variables or {}},
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ShopifyError(f"تعذر الاتصال بـ Shopify: {exc}") from exc
        if resp.status_code == 401:
            raise ShopifyError("التوكن غير صحيح أو منتهي — تأكد من بيانات التطبيق")
        if resp.status_code == 403:
            raise ShopifyError("التطبيق ما عنده الصلاحيات المطلوبة (scopes) — راجع إعدادات التطبيق")
        if resp.status_code == 429:
            time.sleep(2)
            continue
        if not resp.ok:
            raise ShopifyError(f"رد غير متوقع من Shopify ({resp.status_code})")
        body = resp.json()
        errors = body.get("errors") or []
        if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
            time.sleep(2)
            continue
        if errors:
            raise ShopifyError(f"خطأ من Shopify: {errors[0].get('message', errors)}")
        return body.get("data") or {}
    raise ShopifyError("Shopify مضغوط حالياً (throttled) — جرّب بعد شوي")


SHOP_QUERY = """
query {
  shop { name currencyCode primaryDomain { url } }
}
"""

POLICIES_QUERY = """
query {
  shop { shopPolicies { type title body } }
}
"""


def get_shop_info(shop_domain, access_token):
    data = graphql(shop_domain, access_token, SHOP_QUERY)
    shop = data.get("shop") or {}
    primary_url = ((shop.get("primaryDomain") or {}).get("url") or "").rstrip("/")
    return {
        "name": shop.get("name", ""),
        "currency": shop.get("currencyCode", ""),
        "primary_domain": re.sub(r"^https?://", "", primary_url),
    }


def get_store_policies_text(shop_domain, access_token):
    """سياسات المتجر (شحن، إرجاع...) كنص عادي للمساعد. برجّع "" لو مش متاحة
    (مثلاً التطبيق ما عنده صلاحية) — هي معلومات إضافية، مش شرط للربط."""
    try:
        data = graphql(shop_domain, access_token, POLICIES_QUERY)
    except ShopifyError:
        return ""
    parts = []
    for policy in (data.get("shop") or {}).get("shopPolicies") or []:
        body = _clean_text(policy.get("body"))
        if body:
            parts.append(f"{policy.get('title') or policy.get('type')}:\n{body[:1500]}")
    return "\n\n".join(parts)


WEBHOOKS_QUERY = """
query { webhookSubscriptions(first: 100) { nodes { topic uri } } }
"""

WEBHOOK_CREATE_MUTATION = """
mutation($topic: WebhookSubscriptionTopic!, $uri: String!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: {uri: $uri, format: JSON}) {
    userErrors { field message }
  }
}
"""


def register_webhooks(shop_domain, access_token, callback_url):
    """بتسجّل webhooks تغييرات المنتجات (إجباري) والطلبات (اختياري). برجّع
    True لو webhook الطلبات تسجّل، False لو التطبيق ما عنده read_orders."""
    data = graphql(shop_domain, access_token, WEBHOOKS_QUERY)
    existing = {
        (n.get("topic"), n.get("uri"))
        for n in (data.get("webhookSubscriptions") or {}).get("nodes") or []
    }

    def create(topic):
        if (topic, callback_url) in existing:
            return
        result = graphql(
            shop_domain, access_token, WEBHOOK_CREATE_MUTATION,
            {"topic": topic, "uri": callback_url},
        )
        errors = (result.get("webhookSubscriptionCreate") or {}).get("userErrors") or []
        if errors:
            raise ShopifyError(f"فشل تسجيل webhook لـ {topic}: {errors[0].get('message')}")

    for topic in PRODUCT_WEBHOOK_TOPICS:
        create(topic)
    try:
        create(ORDER_WEBHOOK_TOPIC)
        return True
    except ShopifyError:
        return False


PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 20, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      legacyResourceId title handle descriptionHtml productType tags status onlineStoreUrl
      options { name position }
      media(first: 1) { nodes { preview { image { url } } } }
      variants(first: 40) {
        nodes {
          legacyResourceId sku price compareAtPrice availableForSale
          selectedOptions { name value }
        }
      }
    }
  }
}
"""


def fetch_all_products(shop_domain, access_token):
    """كل المنتجات الفعّالة، محوّلة لنفس شكل payload الـ webhook (REST) حتى
    نستخدم نفس product_row_from_payload للحالتين."""
    products = []
    cursor = None
    while True:
        data = graphql(shop_domain, access_token, PRODUCTS_QUERY, {"cursor": cursor})
        page = data.get("products") or {}
        products.extend(_graphql_product_to_payload(node) for node in page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return products
        cursor = info.get("endCursor")


def _graphql_product_to_payload(node):
    media = ((node.get("media") or {}).get("nodes") or [])
    image_url = (((media[0] if media else {}).get("preview") or {}).get("image") or {}).get("url")
    variants = []
    for v in (node.get("variants") or {}).get("nodes") or []:
        variants.append({
            "id": v.get("legacyResourceId"),
            "sku": v.get("sku") or "",
            "price": v.get("price"),
            "compare_at_price": v.get("compareAtPrice"),
            "available": bool(v.get("availableForSale")),
            "selected_options": {o["name"]: o["value"] for o in v.get("selectedOptions") or []},
        })
    return {
        "id": node.get("legacyResourceId"),
        "title": node.get("title"),
        "handle": node.get("handle"),
        "body_html": node.get("descriptionHtml"),
        "product_type": node.get("productType"),
        "tags": ", ".join(node.get("tags") or []),
        "status": (node.get("status") or "").lower(),
        "online_store_url": node.get("onlineStoreUrl"),
        "options": [{"name": o.get("name"), "position": o.get("position")} for o in node.get("options") or []],
        "images": [{"src": image_url}] if image_url else [],
        "variants": variants,
    }


# ---------------------------------------------------------------------------
# تحويل منتج (من GraphQL المحوّل أو من webhook REST) لصف Product
# ---------------------------------------------------------------------------


def _clean_text(html):
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html or "")).strip()


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _variant_available(variant):
    if "available" in variant:
        return bool(variant["available"])
    # شكل webhook الـ REST: ما في available، منستنتجها من إعدادات المخزون.
    if variant.get("inventory_policy") == "continue":
        return True
    if "inventory_management" in variant and not variant.get("inventory_management"):
        return True
    qty = variant.get("inventory_quantity")
    return True if qty is None else qty > 0


def _variant_options(payload, variant):
    if "selected_options" in variant:
        return variant["selected_options"]
    result = {}
    for opt in payload.get("options") or []:
        value = variant.get(f"option{opt.get('position')}")
        if opt.get("name") and value:
            result[opt["name"]] = value
    return result


def product_row_from_payload(shop_domain, payload, primary_domain=""):
    """بيحوّل منتج Shopify لصف جاهز لموديل Product. برجّع None إذا المنتج
    مش فعّال أو ما إلو صورة (موديلنا بيطلبها) — هيك بينشال من الكتالوج."""
    if payload.get("status") and payload["status"] != "active":
        return None
    images = payload.get("images") or []
    if not images or not images[0].get("src"):
        return None

    variants = payload.get("variants") or []
    available = [v for v in variants if _variant_available(v)]
    main = (available or variants or [{}])[0]

    options = {}
    for v in available:
        for name, value in _variant_options(payload, v).items():
            # Shopify بيحط "Title: Default Title" للمنتجات بدون خيارات
            if name == "Title" and value == "Default Title":
                continue
            options.setdefault(name, [])
            if value not in options[name]:
                options[name].append(value)

    price = _decimal(main.get("price")) or Decimal("0")
    compare_at = _decimal(main.get("compare_at_price"))
    if compare_at is not None and compare_at <= price:
        compare_at = None

    domain = primary_domain or shop_domain
    product_url = payload.get("online_store_url") or f"https://{domain}/products/{payload.get('handle', '')}"

    return {
        "external_id": str(payload.get("id")),
        "sku": (main.get("sku") or "").strip() or f"SHOPIFY-{payload.get('id')}",
        "title": (payload.get("title") or "").strip(),
        "description": _clean_text(payload.get("body_html"))[:2000],
        "price": price,
        "compare_at_price": compare_at,
        "image_url": images[0]["src"],
        "product_url": product_url,
        "category": (payload.get("product_type") or "").strip(),
        "tags": (payload.get("tags") or "").strip(),
        "options": options,
        "in_stock": bool(available),
        "variant_id": str(main.get("id") or "") if available else "",
    }


def conversation_id_from_order(payload):
    for attr in payload.get("note_attributes") or []:
        if attr.get("name") == CART_ATTRIBUTE_KEY:
            return str(attr.get("value") or "")
    return ""


def verify_webhook_hmac(body_bytes, hmac_header, api_secret):
    if not hmac_header:
        return False
    digest = hmac.new(api_secret.encode("utf-8"), body_bytes, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header)
