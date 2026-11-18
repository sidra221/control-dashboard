import base64
import hashlib
import hmac
import html
import ipaddress
import re
import socket
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import requests

from .sync_guard import CatalogIncomplete

REQUEST_TIMEOUT_SECONDS = 20
# حذف الـ webhooks عند فصل الربط "أفضل محاولة": مهلة قصيرة حتى لا يتعطّل طلب الموظف.
CLEANUP_TIMEOUT_SECONDS = 5
# 429: نلتزم بـ Retry-After (بالثواني) ضمن هذا السقف، وإلا تراجع أُسّي 1، 2، 4...
MAX_RETRY_AFTER_SECONDS = 30
PRODUCTS_PER_PAGE = 100
MAX_PAGES = 500  # سقف أمان (50,000 منتج) ضد pagination خربان
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


def _ip_is_public(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified or not ip.is_global
    )


def assert_public_host(site_url):
    """حماية من SSRF: الرابط يُدخله الموظف ويطلبه سيرفرنا، فنرفض أي اسم يُترجَم
    (ولو لعنوان واحد) إلى عنوان داخلي/خاص/محلي/محجوز (IPv4 وIPv6)."""
    parts = urlsplit(site_url or "")
    host = parts.hostname
    if parts.scheme != "https" or not host:
        raise WooCommerceError("رابط المتجر غير صالح، ويجب أن يبدأ بـ https")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise WooCommerceError("رابط المتجر غير صالح") from exc
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise WooCommerceError("تعذّر العثور على عنوان المتجر، يُرجى التحقق من الرابط") from exc
    addresses = set()
    for info in infos:
        try:
            addresses.add(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError as exc:
            raise WooCommerceError("تعذّر التحقق من عنوان المتجر") from exc
    if not addresses or not all(_ip_is_public(ip) for ip in addresses):
        raise WooCommerceError("رابط المتجر يشير إلى عنوان داخلي أو غير عام، ولا يُسمح بالاتصال به")


def _retry_delay(resp, attempt):
    header = (resp.headers.get("Retry-After") or "").strip() if resp.headers else ""
    try:
        seconds = float(header)
    except ValueError:
        seconds = None
    if seconds is None or seconds < 0:
        seconds = 2 ** attempt
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _request(connection, method, path, params=None, json_body=None, max_throttle_retries=3,
             timeout=REQUEST_TIMEOUT_SECONDS):
    url = f"{connection.site_url}/wp-json/wc/v3/{path}"
    for attempt in range(max_throttle_retries + 1):
        assert_public_host(connection.site_url)
        try:
            resp = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                auth=(connection.consumer_key, connection.consumer_secret),
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise WooCommerceError(f"تعذر الاتصال بالمتجر: {exc}") from exc
        if 300 <= resp.status_code < 400:
            # لا نتبع التحويلات (قد تقود إلى عنوان داخلي). نطلب الرابط النهائي مباشرة.
            raise WooCommerceError(
                "المتجر أعاد توجيه الطلب إلى رابط آخر — يُرجى إدخال الرابط النهائي للمتجر "
                "بصيغة https كما يظهر في المتصفح (مع www أو بدونها)"
            )
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
            if attempt < max_throttle_retries:
                time.sleep(_retry_delay(resp, attempt))
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
    """صفحة صفحة. بنعتمد على X-WP-TotalPages؛ لو الهيدر ضاع (CDN/كاش بيشيله) منكمّل لحد صفحة ناقصة
    بدل ما نفترض صفحة وحدة. فوق MAX_PAGES: CatalogIncomplete مع يلي انجاب (ما منحذف شي)."""
    items = []
    page = 1
    while page <= MAX_PAGES:
        resp, data = _request(connection, "GET", path, {**params, "per_page": PRODUCTS_PER_PAGE, "page": page})
        data = data or []
        items.extend(data)
        header = resp.headers.get("X-WP-TotalPages")
        try:
            total_pages = int(header) if header else None
        except ValueError:
            total_pages = None
        if total_pages is not None:
            if page >= total_pages:
                return items
        elif len(data) < PRODUCTS_PER_PAGE:
            return items
        page += 1
    raise CatalogIncomplete("page_cap", items)


def fetch_variations(connection, product_id):
    return _fetch_pages(
        connection, f"products/{product_id}/variations", {"_fields": VARIATION_FIELDS}
    )


def fetch_all_products(connection):
    """كل المنتجات المنشورة، ومنتجات الـ variable معها نسخها بمفتاح `_variations`
    (التوافر بيتحدد من النسخ، مش من المنتج الأب)."""
    complete = True
    try:
        products = _fetch_pages(
            connection, "products", {"status": "publish", "_fields": PRODUCT_FIELDS}
        )
    except CatalogIncomplete as exc:
        products, complete = exc.partial, False
    for product in products:
        if product.get("type") == "variable":
            try:
                product["_variations"] = fetch_variations(connection, product["id"])
            except CatalogIncomplete as exc:
                product["_variations"] = exc.partial
    if not complete:
        raise CatalogIncomplete("page_cap", products)
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


def delete_webhooks(connection, callback_url):
    """عند فصل الربط: نحذف الـ webhooks التي سجّلناها (أفضل محاولة، مهلة قصيرة،
    والأخطاء تُتجاهل). برجّع عدد ما انحذف."""
    deleted = 0
    try:
        _, hooks = _request(
            connection, "GET", "webhooks", {"per_page": 100, "status": "all"},
            max_throttle_retries=0, timeout=CLEANUP_TIMEOUT_SECONDS,
        )
    except WooCommerceError:
        return 0
    for hook in hooks or []:
        if hook.get("delivery_url") != callback_url or not hook.get("id"):
            continue
        try:
            _request(
                connection, "DELETE", f"webhooks/{hook['id']}", {"force": "true"},
                max_throttle_retries=0, timeout=CLEANUP_TIMEOUT_SECONDS,
            )
            deleted += 1
        except WooCommerceError:
            continue
    return deleted


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
