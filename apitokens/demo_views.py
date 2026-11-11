"""متجر الديمو (/demo/) — متجر أزياء وهمي "متجر الأناقة" منعرض فيه ويدجت
الشات كأنه مركّب على متجر سعودي حقيقي. المنتجات بتجي من كتالوج التاجر يلي
توكنه = settings.DEMO_STORE_KEY؛ إذا مو مضبوط أو التاجر مو موجود ← 404.

السلة كلها بالمتصفح (localStorage)، ما في دفع حقيقي أبداً. "إتمام الطلب" بيسجّل طلب
تجريبي (DEMO-...) على المحادثة اللي إضافة السلة جاية منها، بنفس آلية الربط برقم السلة
عند سلة/زد، حتى تظهر المبيعات المنسوبة بالإحصائيات.
"""

import json
import re
import secrets

from decimal import Decimal
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import ApiToken, Conversation

DEMO_PRODUCTS_PREFIX = "/demo/products/"
STORE_NAME = "متجر الأناقة"

# أسماء عربية للفئات إذا الكتالوج جاي بالإنجليزي (شوبيفاي مثلاً)
CATEGORY_LABELS = {
    "dresses": "فساتين", "dress": "فساتين",
    "abayas": "عبايات", "abaya": "عبايات",
    "shirts": "قمصان", "shirt": "قمصان", "tops": "بلوزات", "blouses": "بلوزات",
    "pants": "بناطيل", "trousers": "بناطيل", "jeans": "جينز", "skirts": "تنانير",
    "jackets": "جاكيتات", "coats": "معاطف", "bags": "حقائب", "shoes": "أحذية",
    "accessories": "إكسسوارات", "scarves": "أوشحة", "hijabs": "طرح", "kaftans": "قفاطين",
}


# ---------------------------------------------------------------- مساعدات

def demo_merchant():
    key = settings.DEMO_STORE_KEY
    if not key:
        raise Http404
    merchant = ApiToken.objects.filter(token=key).first()
    if merchant is None:
        raise Http404
    return merchant


def product_handle(product):
    """الـ handle بالرابط: آخر مقطع من product_url إذا كان من /demo/products/،
    وإلا external_id أو sku بعد ما نعملهم slug."""
    if product.product_url:
        path = urlparse(product.product_url).path or ""
        if path.startswith(DEMO_PRODUCTS_PREFIX):
            last = path.rstrip("/").rsplit("/", 1)[-1]
            if last:
                return last
    return (
        slugify(product.external_id or "", allow_unicode=True)
        or slugify(product.sku or "", allow_unicode=True)
        or f"p-{product.pk}"
    )


def demo_product_url(product):
    return f"{DEMO_PRODUCTS_PREFIX}{product_handle(product)}/"


def category_label(category):
    return CATEGORY_LABELS.get(category.strip().lower(), category.strip())


def category_slug(category):
    return slugify(category.strip(), allow_unicode=True) or "other"


def size_options(product):
    """خيار المقاس: "المقاس" أو أي مفتاح فيه "مقاس" أو "size"."""
    options = product.options if isinstance(product.options, dict) else {}
    for key, values in options.items():
        k = str(key).lower()
        if "مقاس" in k or "size" in k:
            if isinstance(values, (list, tuple)):
                return str(key), [str(v) for v in values if str(v).strip()]
    return "", []


def fmt_price(value):
    if value is None:
        return ""
    value = Decimal(value)
    if value == value.to_integral_value():
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _widget_version():
    """رقم نسخة embed.js من وقت تعديله — حتى يشوف المتصفح التحديث فوراً بالديمو
    بدل نسخته المخزّنة."""
    path = settings.BASE_DIR / "apitokens" / "static" / "apitokens" / "embed.js"
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def card(product):
    on_sale = bool(product.compare_at_price and product.compare_at_price > product.price)
    discount = 0
    if on_sale:
        discount = int(round((1 - product.price / product.compare_at_price) * 100))
    size_key, sizes = size_options(product)
    return {
        "obj": product,
        "handle": product_handle(product),
        "url": demo_product_url(product),
        "title": product.title,
        "image": product.image_url,
        "price": fmt_price(product.price),
        "price_num": float(product.price),
        "old_price": fmt_price(product.compare_at_price) if on_sale else "",
        "on_sale": on_sale,
        "discount": discount,
        "in_stock": product.in_stock,
        "category": product.category,
        "category_label": category_label(product.category) if product.category else "",
        "category_slug": category_slug(product.category) if product.category else "",
        "size_key": size_key,
        "sizes": sizes,
    }


def categories_of(cards):
    """فئات المتجر (للقائمة وبطاقات الفئات) — صورة كل فئة أول منتج فيها."""
    seen, out = {}, []
    for c in cards:
        if not c["category"]:
            continue
        if c["category"] in seen:
            seen[c["category"]]["count"] += 1
            continue
        entry = {
            "name": c["category"], "label": c["category_label"], "slug": c["category_slug"],
            "image": c["image"], "count": 1,
        }
        seen[c["category"]] = entry
        out.append(entry)
    return out


def category_sections(cards):
    """أقسام الصفحة الرئيسية: قسم لكل فئة (القائمة بتنط عليه بـ #cat-<slug>)،
    والمنتجات بلا فئة بقسم أخير."""
    sections = [dict(c, products=[x for x in cards if x["category"] == c["name"]]) for c in categories_of(cards)]
    rest = [x for x in cards if not x["category"]]
    if rest:
        sections.append({"label": "منتجات أخرى", "slug": "other", "products": rest, "count": len(rest)})
    return sections


def products_json(cards):
    """خريطة المنتجات (مفتاحها external_id، وإذا فاضي الـ sku) — منها بتقرا
    window.ControlCart.add العنوان والسعر والصورة."""
    data = {}
    for c in cards:
        p = c["obj"]
        key = p.external_id or p.sku
        data[key] = {
            "product_id": key,
            "sku": p.sku,
            "handle": c["handle"],
            "url": c["url"],
            "title": c["title"],
            "image": c["image"],
            "price": c["price_num"],
            "sizes": c["sizes"],
            "in_stock": c["in_stock"],
        }
    return data


def render_demo(request, template, merchant, cards, extra=None):
    context = {
        "store_name": STORE_NAME,
        "currency": "ر.س",
        "categories": categories_of(cards),
        "products_map": products_json(cards),
        "widget_src": static("apitokens/embed.js") + f"?v={_widget_version()}",
        "store_key": merchant.token,
    }
    context.update(extra or {})
    return render(request, f"apitokens/demo/{template}", context)


def all_cards(merchant):
    return [card(p) for p in merchant.products.all()]


# ---------------------------------------------------------------- الصفحات

@require_GET
def home(request):
    merchant = demo_merchant()
    cards = all_cards(merchant)
    newest = sorted(cards, key=lambda c: c["obj"].created_at, reverse=True)
    newest = [c for c in newest if c["in_stock"]][:8] or newest[:8]
    sale = [c for c in cards if c["on_sale"]][:8]
    return render_demo(request, "home.html", merchant, cards, {
        "new_arrivals": newest,
        "sale_products": sale,
        "max_discount": max((c["discount"] for c in sale), default=0),
        "sections": category_sections(cards),
    })


@require_GET
def product(request, handle):
    merchant = demo_merchant()
    cards = all_cards(merchant)
    current = next((c for c in cards if c["handle"] == handle), None)
    if current is None:
        raise Http404
    related = [
        c for c in cards
        if c["handle"] != handle and c["category"] and c["category"] == current["category"]
    ][:4]
    if len(related) < 4:
        related += [c for c in cards if c["handle"] != handle and c not in related][: 4 - len(related)]
    p = current["obj"]
    return render_demo(request, "product.html", merchant, cards, {
        "p": current,
        "product_key": p.external_id or p.sku,
        "description": p.description,
        "related": related,
        "size_message": f"ساعدني أعرف مقاسي المناسب في «{p.title}»",
    })


@require_GET
@ensure_csrf_cookie
def cart(request):
    merchant = demo_merchant()
    return render_demo(request, "cart.html", merchant, all_cards(merchant))


# نفس أرقام الشحن اللي بصفحة السلة (cart.html)
FREE_SHIPPING = Decimal("299")
SHIPPING_FEE = Decimal("25")
CART_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@require_POST
def checkout(request):
    """طلب تجريبي: بيحسب الإجمالي من أسعار الكتالوج (مش من العميل) وبيسجّله على المحادثة
    اللي رقم سلتها = cart_id (إن وجدت). ما في دفع حقيقي."""
    merchant = demo_merchant()
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    cart_id = str(body.get("cart_id") or "").strip()
    if not CART_ID_RE.match(cart_id):
        return JsonResponse({"ok": False, "error": "invalid_cart_id"}, status=400)
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 50:
        return JsonResponse({"ok": False, "error": "invalid_items"}, status=400)

    subtotal = Decimal("0")
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("product_id") or "").strip()
        try:
            qty = int(item.get("qty"))
        except (TypeError, ValueError):
            continue
        if not pid or not 1 <= qty <= 99:
            continue
        product = merchant.products.filter(Q(external_id=pid) | Q(sku=pid)).first()
        if product is not None:
            subtotal += product.price * qty
    if subtotal <= 0:
        return JsonResponse({"ok": False, "error": "empty_cart"}, status=400)
    total = subtotal + (Decimal("0") if subtotal >= FREE_SHIPPING else SHIPPING_FEE)

    order_id = "DEMO-" + secrets.token_hex(4).upper()
    conversation = Conversation.objects.filter(
        merchant=merchant, platform_cart_id=cart_id, order_id="",
    ).first()
    if conversation is not None:
        conversation.order_id = order_id
        conversation.order_total = total
        conversation.order_currency = (merchant.currency or "SAR")[:8]
        conversation.order_at = timezone.now()
        conversation.save(
            update_fields=["order_id", "order_total", "order_currency", "order_at", "updated_at"],
        )
    return JsonResponse({
        "ok": True,
        "order_id": order_id,
        "total": str(total),
        "attributed": conversation is not None,
    })
