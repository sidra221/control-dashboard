import base64
import hashlib
import hmac
import html
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests

REQUEST_TIMEOUT_SECONDS = 20
PRODUCTS_PER_PAGE = 100
PRODUCT_WEBHOOK_TOPICS = ["product.created", "product.updated", "product.deleted"]
# order.updated لازم نسمعه كمان: طلب الدفع بالبطاقة بينعمل "pending" وبيصير "processing"
# بعد ما الدفع ينجح — وقتها بس منعتبره طلب فعلي.
ORDER_WEBHOOK_TOPICS = ["order.created", "order.updated"]
# حالات الطلب اللي منعتبرها بيع فعلي (مش pending/failed/cancelled/refunded/draft).
COUNTED_ORDER_STATUSES = {"processing", "completed", "on-hold"}
# بس هالأنواع بتنضاف للسلة بضغطة وحدة — المجمّعة (grouped) والخارجية (external) لأ.
SUPPORTED_PRODUCT_TYPES = {"simple", "variable"}

PRODUCT_FIELDS = ",".join([
    "id", "name", "slug", "permalink", "type", "status", "catalog_visibility", "sku",
    "description", "short_description", "price", "regular_price", "sale_price", "on_sale",
    "stock_status", "purchasable", "images", "categories", "tags", "attributes", "variations",
])
VARIATION_FIELDS = "id,status,stock_status,purchasable,attributes"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class WooCommerceError(Exception):
    pass


# ---------------------------------------------------------------------------
# REST API (v3) — مصادقة Basic بمفاتيح المتجر، لازم https
# ---------------------------------------------------------------------------


def normalize_site_url(value):
    """بيقبل الرابط بأي شكل التاجر بينسخه (بدون https، مع /wp-admin أو /wp-json
    بالآخر) وبيرجّع https://my-store.com. بيرجّع "" لو مش https."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.netloc:
        return ""
    path = parts.path
    for marker in ("/wp-admin", "/wp-json", "/wp-login.php"):
        if marker in path:
            path = path.split(marker)[0]
    return f"https://{parts.netloc.lower()}{path.rstrip('/')}"


def _request(connection, method, path, params=None, json_body=None, max_throttle_retries=3):
    url = f"{connection.site_url}/wp-json/wc/v3/{path}"
    for attempt in range(max_throttle_retries + 1):
        try:
            resp = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                auth=(connection.consumer_key, connection.consumer_secret),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise WooCommerceError(f"تعذر الاتصال بالمتجر: {exc}") from exc
        if resp.status_code == 401:
            raise WooCommerceError("مفاتيح الـ API غير صحيحة — تأكد من الـ Consumer key والـ Secret")
        if resp.status_code == 403:
            raise WooCommerceError(
                "المفاتيح ما عندها الصلاحية المطلوبة — لازم تكون بصلاحية Read/Write"
            )
        if resp.status_code == 404:
            raise WooCommerceError(
                "ما لقينا WooCommerce REST API — تأكد من الرابط، ومن إن Permalinks "
                "بإعدادات ووردبريس مش على Plain"
            )
        if resp.status_code == 429:
            time.sleep(2)
            continue
        if not resp.ok:
            raise WooCommerceError(f"رد غير متوقع من المتجر ({resp.status_code})")
        try:
            return resp, resp.json()
        except ValueError as exc:
            # صفحة HTML بدل JSON = غالباً جدار حماية (Cloudflare/Wordfence) بيحجب الـ API
            raise WooCommerceError(
                "المتجر رجّع رد مش JSON — غالباً جدار حماية بيمنع سيرفرنا من الوصول للـ API"
            ) from exc
    raise WooCommerceError("المتجر مضغوط حالياً — جرّب بعد شوي")


def get_store_info(connection):
    """بيتحقق من المفاتيح (قراءة) وبيرجّع عملة المتجر."""
    _, data = _request(connection, "GET", "settings/general/woocommerce_currency")
    return {"currency": str((data or {}).get("value") or "")}


def _fetch_pages(connection, path, params):
    items = []
    page = 1
    while True:
        resp, data = _request(connection, "GET", path, {**params, "per_page": PRODUCTS_PER_PAGE, "page": page})
        items.extend(data or [])
        try:
            total_pages = int(resp.headers.get("X-WP-TotalPages") or 1)
        except ValueError:
            total_pages = 1
        if page >= total_pages:
            return items
        page += 1


def fetch_variations(connection, product_id):
    return _fetch_pages(
        connection, f"products/{product_id}/variations", {"_fields": VARIATION_FIELDS}
    )


def fetch_all_products(connection):
    """كل المنتجات المنشورة، ومنتجات الـ variable معها نسخها بمفتاح `_variations`
    (التوافر بيتحدد من النسخ، مش من المنتج الأب)."""
    products = _fetch_pages(
        connection, "products", {"status": "publish", "_fields": PRODUCT_FIELDS}
    )
    for product in products:
        if product.get("type") == "variable":
            product["_variations"] = fetch_variations(connection, product["id"])
    return products


# ---------------------------------------------------------------------------
# Webhooks: WooCommerce بيوقّع كل إشعار بـ HMAC-SHA256 (base64) بسر الـ webhook
# ---------------------------------------------------------------------------


def register_webhooks(connection, callback_url):
    """بتسجّل webhooks المنتجات (إجباري) والطلبات (اختياري) وبتثبّت سرّها. لو
    الـ webhook موجود أصلاً (إعادة ربط) منحدّث السر حتى ما تضل التواقيع بالقديم.
    برجّع True لو webhooks الطلبات تسجّلت."""
    _, existing_list = _request(connection, "GET", "webhooks", {"per_page": 100, "status": "all"})
    existing = {
        (hook.get("topic"), hook.get("delivery_url")): hook.get("id")
        for hook in existing_list or []
    }

    def ensure(topic):
        hook_id = existing.get((topic, callback_url))
        if hook_id:
            _request(
                connection, "PUT", f"webhooks/{hook_id}",
                json_body={"secret": connection.webhook_secret, "status": "active"},
            )
            return
        _request(
            connection, "POST", "webhooks",
            json_body={
                "name": f"Control — {topic}",
                "topic": topic,
                "delivery_url": callback_url,
                "secret": connection.webhook_secret,
                "status": "active",
            },
        )

    for topic in PRODUCT_WEBHOOK_TOPICS:
        ensure(topic)
    try:
        for topic in ORDER_WEBHOOK_TOPICS:
            ensure(topic)
        return True
    except WooCommerceError:
        return False


def verify_webhook_signature(body_bytes, signature_header, secret):
    if not signature_header:
        return False
    digest = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, signature_header)


# ---------------------------------------------------------------------------
# تحويل منتج WooCommerce (من الـ REST أو من webhook) لصف Product
# ---------------------------------------------------------------------------


def _clean_text(value):
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(value or ""))).strip()


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _variation_available(variation):
    if variation.get("status") not in (None, "publish"):
        return False
    if variation.get("purchasable") is False:
        return False
    return variation.get("stock_status") in ("instock", "onbackorder")


def _attribute_options(payload, variations):
    """{"Size": ["S", "M"]} — بس القيم اللي إلها نسخة متوفرة. نسخة بقيمة فاضية
    ("أي قيمة") بتعني كل خيارات الخاصية."""
    parent_options = {
        attr.get("name"): [html.unescape(str(o)) for o in attr.get("options") or []]
        for attr in payload.get("attributes") or []
        if attr.get("variation") and attr.get("name")
    }
    if variations is None:
        # ما قدرنا نجيب النسخ (مثلاً خطأ شبكة بوقت الـ webhook) — منعرض كل الخيارات.
        return {name: values for name, values in parent_options.items() if values}

    options = {}
    for variation in variations:
        if not _variation_available(variation):
            continue
        for attr in variation.get("attributes") or []:
            name = attr.get("name")
            if not name:
                continue
            values = [html.unescape(str(attr["option"]))] if attr.get("option") else parent_options.get(name, [])
            bucket = options.setdefault(name, [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)
    return options


def product_row_from_payload(payload):
    """بيحوّل منتج WooCommerce لصف جاهز لموديل Product. برجّع None إذا المنتج
    مش منشور، مخفي، نوعه مش مدعوم، أو بدون صورة — هيك بينشال من الكتالوج."""
    if payload.get("status") and payload["status"] != "publish":
        return None
    if payload.get("catalog_visibility") == "hidden":
        return None
    if payload.get("type") not in SUPPORTED_PRODUCT_TYPES:
        return None
    images = payload.get("images") or []
    if not images or not images[0].get("src"):
        return None

    variations = payload.get("_variations")
    if payload.get("type") == "variable":
        options = _attribute_options(payload, variations)
        if variations is None:
            in_stock = payload.get("stock_status") in ("instock", "onbackorder")
        else:
            in_stock = any(_variation_available(v) for v in variations)
    else:
        options = {}
        in_stock = payload.get("stock_status") in ("instock", "onbackorder")
        if payload.get("purchasable") is False:
            in_stock = False

    price = _decimal(payload.get("price")) or _decimal(payload.get("regular_price")) or Decimal("0")
    compare_at = None
    if payload.get("on_sale"):
        regular = _decimal(payload.get("regular_price"))
        if regular is not None and regular > price:
            compare_at = regular

    categories = payload.get("categories") or []
    description = _clean_text(f"{payload.get('short_description') or ''} {payload.get('description') or ''}")
    return {
        "external_id": str(payload.get("id")),
        "sku": (payload.get("sku") or "").strip() or f"WOO-{payload.get('id')}",
        "title": html.unescape(payload.get("name") or "").strip(),
        "description": description[:2000],
        "price": price,
        "compare_at_price": compare_at,
        "image_url": images[0]["src"],
        "product_url": payload.get("permalink") or "",
        "category": html.unescape((categories[0].get("name") if categories else "") or "").strip(),
        "tags": ", ".join(html.unescape(tag["name"]) for tag in payload.get("tags") or [] if tag.get("name")),
        "options": options,
        "in_stock": in_stock,
        # الإضافة للسلة بالـ Store API بتستخدم رقم المنتج نفسه (product_id) — مش نسخة.
        "variant_id": "",
    }


def order_is_counted(payload):
    return payload.get("status") in COUNTED_ORDER_STATUSES


def order_product_ids(payload):
    """أرقام المنتجات (الأب، مش النسخة) بالطلب — بنطابقها مع Product.external_id."""
    return {
        str(item.get("product_id"))
        for item in payload.get("line_items") or []
        if item.get("product_id")
    }
