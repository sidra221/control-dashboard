"""متجر الديمو (/demo/) — متجر أزياء وهمي "متجر الأناقة" منعرض فيه ويدجت
الشات كأنه مركّب على متجر سعودي حقيقي. المنتجات بتجي من كتالوج التاجر يلي
توكنه = settings.DEMO_STORE_KEY؛ إذا مو مضبوط أو التاجر مو موجود ← 404.

السلة كلها بالمتصفح (localStorage)، ما في طلبات ولا دفع حقيقي أبداً.
"""

from decimal import Decimal
from urllib.parse import urlparse

from django.conf import settings
from django.http import Http404
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_http_methods

from .models import ApiToken, DemoCustomer, TryOnRecord
from .widget_views import DEMO_CUSTOMER_SESSION_KEY, _shopper_tryons_left

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


def current_customer(request, merchant):
    customer_id = request.session.get(DEMO_CUSTOMER_SESSION_KEY)
    if not customer_id:
        return None
    return DemoCustomer.objects.filter(pk=customer_id, merchant=merchant).first()


def tries_label(left):
    """نص عدد التجارب الباقية بالعربي (المثنى والجمع صح)."""
    if left == 0:
        return "انتهت تجاربك"
    if left == 1:
        return "تبقّت لك تجربة واحدة"
    if left == 2:
        return "تبقّت لك تجربتان"
    if 3 <= left <= 10:
        return f"تبقّت لك {left} تجارب"
    return f"تبقّت لك {left} تجربة"


def tryon_allowance(merchant, customer):
    """عدد التجارب الكلي والمتبقي لحساب الزبون (None للمتبقي = بلا حد). الحد بيجي
    من إعداد التاجر بالداشبورد، فأي تعديل عليه بينعكس فوراً على كل الحسابات."""
    limit = merchant.tryons_per_shopper
    if customer is None or not limit:
        return {"limit": limit, "left": None}
    left = _shopper_tryons_left(merchant, customer.shopper_id, "")
    return {
        "limit": limit,
        "left": left,
        "used": limit - left,
        "label": tries_label(left),
        # النقاط بتطلع بس للباقات الصغيرة، غير هيك بيكفي الرقم
        "dots": [i < left for i in range(limit)] if limit <= 10 else [],
        "percent_used": round((limit - left) * 100 / limit),
    }


def render_demo(request, template, merchant, cards, extra=None):
    customer = current_customer(request, merchant)
    context = {
        "customer": customer,
        "allowance": tryon_allowance(merchant, customer),
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
def cart(request):
    merchant = demo_merchant()
    return render_demo(request, "cart.html", merchant, all_cards(merchant))


# ---------------------------------------------------------------- الحسابات

MIN_PASSWORD_LENGTH = 6


def _safe_next(request, default="/demo/"):
    target = request.POST.get("next") or request.GET.get("next") or ""
    ok = url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure())
    return target if ok and target.startswith("/demo/") else default


def _auth_page(request, merchant, template, extra):
    context = {"next": _safe_next(request)}
    context.update(extra)
    return render_demo(request, template, merchant, [], context)


@require_http_methods(["GET", "POST"])
def register(request):
    merchant = demo_merchant()
    if current_customer(request, merchant):
        return redirect(_safe_next(request))
    values = {"name": "", "email": ""}
    error = ""
    if request.method == "POST":
        values["name"] = request.POST.get("name", "").strip()[:80]
        values["email"] = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        try:
            validate_email(values["email"])
            valid_email = True
        except ValidationError:
            valid_email = False
        if not values["name"]:
            error = "يرجى إدخال الاسم."
        elif not valid_email:
            error = "يرجى إدخال بريد إلكتروني صحيح."
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"كلمة المرور يجب ألا تقل عن {MIN_PASSWORD_LENGTH} أحرف."
        elif DemoCustomer.objects.filter(merchant=merchant, email=values["email"]).exists():
            error = "يوجد حساب مسجّل بهذا البريد الإلكتروني، يمكنك تسجيل الدخول."
        else:
            customer = DemoCustomer(merchant=merchant, name=values["name"], email=values["email"])
            customer.set_password(password)
            customer.save()
            request.session.cycle_key()
            request.session[DEMO_CUSTOMER_SESSION_KEY] = customer.pk
            return redirect(_safe_next(request))
    return _auth_page(request, merchant, "register.html", {"values": values, "error": error})


@require_http_methods(["GET", "POST"])
def login(request):
    merchant = demo_merchant()
    if current_customer(request, merchant):
        return redirect(_safe_next(request))
    email = ""
    error = ""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        customer = DemoCustomer.objects.filter(merchant=merchant, email=email).first()
        if customer is not None and customer.check_password(request.POST.get("password", "")):
            request.session.cycle_key()
            request.session[DEMO_CUSTOMER_SESSION_KEY] = customer.pk
            return redirect(_safe_next(request))
        error = "البريد الإلكتروني أو كلمة المرور غير صحيحة."
    return _auth_page(request, merchant, "login.html", {"email": email, "error": error})


@require_http_methods(["GET", "POST"])
def account(request):
    """صفحة "اشتراكي": الباقة واستهلاكها وزر التجديد. التجديد تجريبي بلا دفع —
    بيرجّع تجارب الحساب كاملة حتى يشوف المدير الدورة كلها."""
    merchant = demo_merchant()
    customer = current_customer(request, merchant)
    if customer is None:
        return redirect("/demo/login/?next=/demo/account/")
    renewed = False
    if request.method == "POST" and request.POST.get("action") == "renew":
        TryOnRecord.objects.filter(merchant=merchant, shopper_id=customer.shopper_id).delete()
        return redirect("/demo/account/?renewed=1")
    renewed = request.GET.get("renewed") == "1"
    return render_demo(request, "account.html", merchant, [], {"renewed": renewed})


@require_http_methods(["POST"])
def logout(request):
    request.session.pop(DEMO_CUSTOMER_SESSION_KEY, None)
    return redirect("/demo/")
