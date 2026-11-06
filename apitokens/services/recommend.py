"""محرّك ترشيح وترتيب منتجات حتمي (deterministic) — متطلبات الزبون الصريحة
(لون، مقاس، فئة، ميزانية، توفر، منتجات مرفوضة) بتنفرض هون بالكود، مش بالبرومبت.

- دوال نقية فوق كائنات `Product` (بدون أي كتابة على الداتابيس، وبتقبل منتجات غير محفوظة).
- الناتج حقائق إنجليزية مبنية من حقول المنتج بس (Reason / describe_candidates)،
  مو نص للزبون — صياغة الرد للزبون مسؤولية البائع (seller) والحارس (guard).
- ما في أي استدعاء شبكة/LLM هون. embeddings بتنقبل جاهزة بس (query_vector).

القاعدة الذهبية: ما بنخفّف أبداً الميزانية ولا المرفوض ولا التوفر ولا المقاس؛
اللي بنخففه فقط (لما ما يكون في نتيجة مطابقة) هو اللون ثم الفئة القريبة.
"""

import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from . import guard, sizing
from .contracts import Candidate, Reason, Requirements, SearchResult
from .understanding import norm_text

DROP_KEYS = ("stock", "rejected", "budget", "category", "color", "avoid_color", "size")
DESCRIPTION_CHARS = 220
MAX_PER_PREFIX = 2


# =========================================================== جداول المرادفات
# كل المفاتيح بتتطبّع بـ norm_text (همزات/تاء مربوطة/تشكيل) وقت بناء الجدول،
# فكتابتها هون بأي إملاء بتنفع. الكلمات المنتهية بـ "ي" بتاخد مؤنثها تلقائياً.

def _build(spec, feminine=False):
    table = {}
    for canon, words in spec.items():
        for word in words:
            key = norm_text(word)
            if not key:
                continue
            table.setdefault(key, canon)
            if feminine and key.endswith("ي"):
                table.setdefault(key + "ه", canon)
    return table


_COLOR_SPEC = {
    "black": ["black", "اسود", "سوداء", "سودا", "سود", "أسود", "اسوود"],
    "white": ["white", "ابيض", "بيضاء", "بيضا", "ابيظ", "بيض", "offwhite"],
    "red": ["red", "احمر", "حمراء", "حمرا"],
    "blue": ["blue", "ازرق", "زرقاء", "زرقا", "سماوي", "ملكي", "royal blue", "sky blue", "baby blue"],
    "navy": ["navy", "navy blue", "dark blue", "كحلي", "نيفي", "ازرق غامق"],
    "green": ["green", "اخضر", "خضراء", "خضرا", "dark green", "mint", "نعناعي"],
    "olive": ["olive", "olive green", "زيتي", "زيتوني"],
    "yellow": ["yellow", "اصفر", "صفراء", "صفرا", "mustard", "خردلي"],
    "pink": ["pink", "وردي", "زهري", "blush", "rose", "baby pink"],
    "fuchsia": ["fuchsia", "fuschia", "فوشي", "فوشيا", "magenta"],
    "purple": ["purple", "violet", "بنفسجي", "موف", "mauve", "lavender", "لافندر", "lilac", "ليلكي"],
    "orange": ["orange", "برتقالي"],
    "brown": ["brown", "بني", "قهوي", "قهوه", "chocolate", "شوكولاتي"],
    "camel": ["camel", "جملي", "كاميل"],
    "beige": ["beige", "بيج", "بيجي", "tan", "nude", "نيود", "sand", "رملي"],
    "cream": ["cream", "كريمي", "كريم", "ivory", "عاجي"],
    "grey": ["grey", "gray", "رمادي", "رصاصي", "charcoal", "فحمي"],
    "silver": ["silver", "فضي"],
    "gold": ["gold", "golden", "ذهبي", "دهبي", "rose gold"],
    "burgundy": ["burgundy", "maroon", "wine", "عنابي", "خمري", "نبيتي", "bordeaux"],
    "turquoise": ["turquoise", "تركوازي", "فيروزي", "teal"],
    "khaki": ["khaki", "كاكي"],
}
COLOR_TABLE = _build(_COLOR_SPEC, feminine=True)

_CATEGORY_SPEC = {
    "dress": ["dress", "dresses", "gown", "gowns", "frock", "فستان", "فساتين", "فستاني"],
    "abaya": ["abaya", "abayas", "abaaya", "عباية", "عباءة", "عبايات", "عباءات", "عبايه"],
    "kaftan": ["kaftan", "kaftans", "caftan", "caftans", "قفطان", "قفاطين", "جلابية", "جلابيه", "جلابيات"],
    "thobe": ["thobe", "thobes", "thawb", "thaub", "ثوب", "اثواب", "ثياب"],
    "shirt": ["shirt", "shirts", "t shirt", "tshirt", "tee", "tees", "polo", "قميص", "قمصان", "تيشيرت", "تيشرت", "تي شيرت", "بولو"],
    "blouse": ["blouse", "blouses", "بلوزة", "بلوزات", "بلوز"],
    "top": ["top", "tops", "crop top", "توب", "توبات"],
    "sweater": ["sweater", "sweaters", "jumper", "cardigan", "cardigans", "hoodie", "hoodies", "sweatshirt",
                "pullover", "كنزة", "كنزات", "سويتر", "هودي", "كارديجان", "بلوفر"],
    "pants": ["pants", "pant", "trousers", "trouser", "jeans", "jean", "leggings", "palazzo", "بنطلون", "بنطال",
              "بناطيل", "بنطلونات", "جينز", "سروال", "سراويل", "ليقنز", "بلازو"],
    "shorts": ["shorts", "short pants", "شورت", "شورتات"],
    "skirt": ["skirt", "skirts", "تنورة", "تنانير", "جيبة", "جيبات"],
    "jacket": ["jacket", "jackets", "coat", "coats", "blazer", "blazers", "bomber", "trench", "جاكيت",
               "جاكيتات", "معطف", "معاطف", "بليزر", "كوت", "بالطو"],
    "jumpsuit": ["jumpsuit", "jumpsuits", "romper", "playsuit", "overall", "overalls", "جمبسوت", "جامبسوت",
                 "اوفرول", "سالوبيت"],
    "set": ["set", "sets", "co ord", "coord", "two piece", "2 piece", "2 pcs", "طقم", "اطقم", "تنسيقة", "suit", "suits"],
    "nightwear": ["pajama", "pajamas", "pyjama", "pyjamas", "nightgown", "nightwear", "sleepwear", "lingerie",
                  "بيجامة", "بجامة", "بيجامات", "قميص نوم", "لانجري", "ملابس نوم"],
    "swimwear": ["swimsuit", "swimwear", "bikini", "burkini", "مايوه", "مايو", "بوركيني"],
    "shoes": ["shoe", "shoes", "sneaker", "sneakers", "boot", "boots", "heel", "heels", "sandal", "sandals",
              "loafer", "loafers", "flats", "slipper", "slippers", "حذاء", "احذية", "جزمة", "جزم", "صندل", "كوتشي",
              "بوت", "كعب", "شوز", "نعال"],
    "bag": ["bag", "bags", "handbag", "handbags", "clutch", "clutches", "purse", "backpack", "tote", "شنطة",
            "شنط", "حقيبة", "حقائب", "كلتش", "شنطه"],
    "hijab": ["hijab", "hijabs", "scarf", "scarves", "shawl", "shawls", "stole", "veil", "niqab", "حجاب",
              "طرحة", "طرح", "شيلة", "شيل", "شال", "شالات", "وشاح", "ايشارب", "اسدال", "نقاب"],
    "accessory": ["accessory", "accessories", "jewelry", "jewellery", "necklace", "necklaces", "earring",
                  "earrings", "bracelet", "bracelets", "ring", "rings", "watch", "watches", "belt", "belts",
                  "sunglasses", "اكسسوار", "اكسسوارات", "مجوهرات", "عقد", "اقراط", "حلق", "سوار", "اساور",
                  "خاتم", "ساعة", "ساعات", "حزام", "نظارة", "نظارات"],
}
CATEGORY_TABLE = _build(_CATEGORY_SPEC)

# عائلات فئات قريبة — بتستخدم بس لما ما في فئة مطابقة (تخفيف الفئة)
_FAMILIES = (
    frozenset({"dress", "abaya", "set", "jumpsuit", "kaftan"}),
    frozenset({"shirt", "top", "blouse", "sweater"}),
    frozenset({"pants", "skirt", "shorts"}),
)
RELATED = {}
for _family in _FAMILIES:
    for _cat in _family:
        RELATED.setdefault(_cat, set()).update(_family - {_cat})
RELATED = {k: frozenset(v) for k, v in RELATED.items()}

SIZE_ORDER = ("XS", "S", "M", "L", "XL", "XXL", "XXXL")
ONE_SIZE = "ONE"   # مقاس واحد/فري سايز
_SIZE_SPEC = {
    "XS": ["xs", "xxs", "x small", "xsmall", "extra small", "اكس سمول", "اكسترا سمول", "اكس اس"],
    "S": ["s", "sm", "small", "سمول", "سمال", "صغير", "اس"],
    "M": ["m", "md", "med", "medium", "ميديم", "ميدم", "ميديوم", "وسط", "متوسط"],
    "L": ["l", "lg", "large", "لارج", "لرج", "كبير"],
    "XL": ["xl", "x large", "xlarge", "extra large", "اكس لارج", "اكسترا لارج", "اكسل", "اكس ال", "1xl"],
    "XXL": ["xxl", "2xl", "2 xl", "xx large", "xxlarge", "2x large", "2x", "اكس اكس ال", "دبل اكس ال"],
    "XXXL": ["xxxl", "3xl", "3 xl", "xxx large", "xxxlarge", "3x large", "3x", "اكس اكس اكس ال"],
    ONE_SIZE: ["one size", "onesize", "free size", "freesize", "os", "فري سايز", "فري", "مقاس واحد", "حجم واحد"],
}
SIZE_TABLE = _build(_SIZE_SPEC)
_NUMERIC_SIZE = re.compile(r"^\d{2}$")

_STOPWORDS_RAW = """
a an the for with and or of in on to me my i we you your it this that these those do does is are was be
want need looking look show give get please something anything any have has had like some find can could
would should about from at by as just more less than also so too very really
size color colour price cheap expensive budget under over below above
من في على الى الي عن مع او و ابي ابغى ابغا ابغي ابى اريد ودي عندكم عندك عندي وش ايش شو هل لو ممكن
تكفى لي لى لا ما هذا هذه هاذا هادا هذي ذا ده كم فيه بس لون مقاس سعر اللون المقاس
"""
_STOPWORDS = frozenset(norm_text(w) for w in _STOPWORDS_RAW.split())


# ============================================================ مسح النصوص

def _variants(token, plural=False):
    out = [token]
    for prefix, floor in (("وال", 5), ("بال", 5), ("لل", 4), ("ال", 4)):
        if token.startswith(prefix) and len(token) > floor:
            out.append(token[len(prefix):])
            break
    if plural and token.isascii():
        if token.endswith("ies") and len(token) > 4:
            out.append(token[:-3] + "y")
        if token.endswith("es") and len(token) > 3:
            out.append(token[:-2])
        if token.endswith("s") and len(token) > 3:
            out.append(token[:-1])
    return out


_TABLES = {"color": COLOR_TABLE, "category": CATEGORY_TABLE, "size": SIZE_TABLE}


@lru_cache(maxsize=16384)
def _scan(text, kind):
    """كل القيم القياسية الموجودة بالنص بترتيب ظهورها (بدون تكرار)."""
    table = _TABLES[kind]
    plural = kind == "category"
    tokens = norm_text(text).split()
    found = []
    i, n = 0, len(tokens)
    while i < n:
        hit, step = None, 1
        if i + 1 < n:
            for a in _variants(tokens[i], plural):
                for b in _variants(tokens[i + 1], plural):
                    hit = table.get(f"{a} {b}")
                    if hit:
                        step = 2
                        break
                if hit:
                    break
        if hit is None:
            for a in _variants(tokens[i], plural):
                hit = table.get(a)
                if hit:
                    break
            if hit is None and kind == "size" and _NUMERIC_SIZE.match(tokens[i]) and 24 <= int(tokens[i]) <= 60:
                hit = str(int(tokens[i]))
        if hit and hit not in found:
            found.append(hit)
        i += step
    return tuple(found)


def _first(text, kind):
    if text is None:
        return None
    found = _scan(str(text), kind)
    return found[0] if found else None


def canonical_color(text):
    """أسود/اسود/سوداء/Black ← "black". None إذا مو لون معروف."""
    return _first(text, "color")


def canonical_category(text):
    """فستان/فساتين/dress ← "dress". None إذا مو فئة معروفة."""
    return _first(text, "category")


def canonical_size(text):
    """m/Medium/ميديم/وسط ← "M". المقاسات الرقمية (24..60) بتضل أرقام نصية."""
    return _first(text, "size")


# ============================================================ حقائق المنتج

def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def _options(product):
    options = getattr(product, "options", None)
    return options if isinstance(options, dict) else {}


def _is_color_key(name):
    key = str(name).lower()
    return "color" in key or "colour" in key or "لون" in key


def _tags(product):
    try:
        return list(product.tag_list)
    except AttributeError:
        return [t.strip() for t in str(getattr(product, "tags", "") or "").split(",") if t.strip()]


def _colors_with_source(product):
    """{لون قياسي: مصدره}. الخيارات (القيم المتوفرة فعلاً) بتغلب: إذا الخيار
    فيه ألوان معروفة بنعتمد عليها بس — عنوان "Black Dress" ما بيثبت إنو الأسود متوفر
    لو خيار اللون فيه Navy بس. بدون ألوان بالخيارات بنرجع للوسوم ثم العنوان."""
    from_options = {}
    for name, values in _options(product).items():
        if not _is_color_key(name):
            continue
        for value in _as_list(values):
            for color in _scan(value, "color"):
                from_options.setdefault(color, f"{name} option")
    if from_options:
        return from_options
    found = {}
    for tag in _tags(product):
        for color in _scan(tag, "color"):
            found.setdefault(color, "tags")
    for color in _scan(getattr(product, "title", "") or "", "color"):
        found.setdefault(color, "title")
    return found


def product_colors(product):
    return set(_colors_with_source(product))


def product_categories(product):
    """فئات قياسية من حقل الفئة والوسوم؛ العنوان بيضيف أول فئة فيه (أو كلها إذا
    ما في فئة/وسوم) — حتى "Wrap dress with belt" ما يتصنّف إكسسوار."""
    found = set(_scan(str(getattr(product, "category", "") or ""), "category"))
    for tag in _tags(product):
        found.update(_scan(tag, "category"))
    title = _scan(str(getattr(product, "title", "") or ""), "category")
    if found:
        found.update(title[:1])
    else:
        found.update(title)
    return found


def _size_values(product):
    try:
        return sizing.product_sizes(product)
    except AttributeError:
        return []


def product_sizes_available(product):
    """مقاسات قياسية من خيار المقاس. مجموعة فاضية = ما في خيار مقاس (أو غير مفهوم)."""
    found = set()
    for value in _size_values(product):
        found.update(_scan(value, "size"))
    return found


def _size_sort_key(raw):
    canon = canonical_size(raw)
    if canon in SIZE_ORDER:
        return (0, SIZE_ORDER.index(canon), str(raw))
    if canon and canon.isdigit():
        return (1, int(canon), str(raw))
    return (2, 0, str(raw))


def _sizes_as_written(product):
    seen, out = set(), []
    for raw in _size_values(product):
        if raw not in seen:
            seen.add(raw)
            out.append(raw)
    return sorted(out, key=_size_sort_key)


class _Profile:
    __slots__ = ("colors", "cats", "sizes")

    def __init__(self, product):
        self.colors = product_colors(product)
        self.cats = product_categories(product)
        self.sizes = product_sizes_available(product)


# ============================================================ أرقام

def _dec(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _money(value):
    d = _dec(value)
    if d is None:
        return str(value)
    return str(int(d)) if d == d.to_integral_value() else f"{d:.2f}"


def _has_sale(product):
    price, was = _dec(getattr(product, "price", None)), _dec(getattr(product, "compare_at_price", None))
    return price is not None and was is not None and was > price


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _parse_amount(value):
    """"500" / "٥٠٠" / "500 ريال" / "SAR 300" ← Decimal. أكثر من رقم أو غير مفهوم ← None."""
    text = guard.normalize(str(value or "")).strip()
    amount = guard.to_decimal(text) if text else None
    if amount is None:
        numbers = _NUMBER_RE.findall(text)
        if len(numbers) != 1:
            return None
        amount = guard.to_decimal(numbers[0])
    if amount is None or amount <= 0:
        return None
    return amount


# ============================================================ المتطلبات

def _fact_order(indexed):
    index, fact = indexed
    stamp = getattr(fact, "updated_at", None) or getattr(fact, "created_at", None)
    try:
        ts = stamp.timestamp() if stamp else 0.0
    except (AttributeError, OverflowError, OSError, ValueError):
        ts = 0.0
    pk = getattr(fact, "pk", None) or getattr(fact, "id", None) or 0
    return (ts, pk, index)


# inferred بهالمفاتيح بيصير فلتر، لأن الزبون قاله بصياغة ما طابقت الاقتباس الحرفي.
# الستايل والمناسبة والنوع خامات بتضل بالذاكرة للبائع بدون ما تضيّق البحث.
_INFERRED_FILTER_KEYS = frozenset({"budget_max", "budget_min", "size", "color", "category"})


def _fact_source(fact):
    return getattr(fact, "source", "explicit") or "explicit"


def requirements_from_facts(facts, excluded_skus=(), exempt_skus=(), include_out_of_stock=False):
    """حقائق الذاكرة ← Requirements. الصريح فلتر. الميزانية والمقاس واللون والفئة
    المستنتجة كمان فلاتر (إعادة صياغة). لو في صريح ومُستنتج لنفس المفتاح، الصريح بيغلب.
    الأحدث بيغلب (updated_at ثم الترتيب)."""
    active = []
    for index, fact in enumerate(facts or ()):
        if getattr(fact, "status", "active") != "active":
            continue
        source = _fact_source(fact)
        if source == "inferred" and getattr(fact, "key", "") not in _INFERRED_FILTER_KEYS:
            continue
        if source not in ("explicit", "inferred"):
            continue
        active.append((index, fact))
    ordered = [f for _, f in sorted(active, key=_fact_order)]   # الأقدم أولاً

    def newest_amount(key):
        inferred_amount = None
        for fact in reversed(ordered):
            if fact.key != key:
                continue
            amount = _parse_amount(fact.value)
            if amount is None:
                continue
            if _fact_source(fact) == "explicit":
                return amount
            if inferred_amount is None:
                inferred_amount = amount
        return inferred_amount

    def _take(kind):
        rows = [f for f in ordered if _fact_source(f) == kind]
        return rows

    explicit_rows, inferred_rows = _take("explicit"), _take("inferred")

    color_state = {}   # لون ← (موضع، polarity). ضمن نفس المصدر الأحدث بيغلب؛ المستنتج ما بيغلب صريح.

    def _paint_colors(rows, into, only_missing):
        for pos, fact in enumerate(rows):
            if fact.key != "color":
                continue
            canon = canonical_color(fact.value)
            polarity = getattr(fact, "polarity", "like")
            if not canon or polarity not in ("like", "avoid"):
                continue
            if only_missing and canon in into:
                continue
            into[canon] = (pos, polarity)

    _paint_colors(explicit_rows, color_state, only_missing=False)
    _paint_colors(inferred_rows, color_state, only_missing=True)
    colors = tuple(c for c, (_, pol) in sorted(color_state.items(), key=lambda kv: kv[1][0]) if pol == "like")
    avoid = tuple(c for c, (_, pol) in sorted(color_state.items(), key=lambda kv: kv[1][0]) if pol == "avoid")

    def _sizes_and_categories(rows):
        self_sizes, gift_sizes, categories = [], [], []
        for fact in rows:
            value = str(fact.value or "").strip()
            polarity = getattr(fact, "polarity", "like")
            if fact.key == "size" and polarity == "like":
                canon = canonical_size(value)
                bucket = gift_sizes if getattr(fact, "subject", "self") == "gift" else self_sizes
                if canon and canon not in bucket:
                    bucket.append(canon)
            elif fact.key == "category" and polarity == "like":
                canon = canonical_category(value)
                if canon and canon not in categories:
                    categories.append(canon)
        return self_sizes, gift_sizes, categories

    self_sizes, gift_sizes, categories = _sizes_and_categories(explicit_rows)
    inf_self, inf_gift, inf_categories = _sizes_and_categories(inferred_rows)
    if not self_sizes and not gift_sizes:
        self_sizes, gift_sizes = inf_self, inf_gift
    if not categories:
        categories = inf_categories
    has_recipient = any(f.key == "recipient" and str(f.value or "").strip() for f in explicit_rows)
    sizes = gift_sizes if (gift_sizes and has_recipient) else self_sizes

    return Requirements(
        budget_max=newest_amount("budget_max"),
        budget_min=newest_amount("budget_min"),
        colors=colors,
        avoid_colors=avoid,
        sizes=tuple(sizes),
        categories=tuple(categories),
        excluded_skus=frozenset(str(s) for s in (excluded_skus or ())),
        exempt_skus=frozenset(str(s) for s in (exempt_skus or ())),
        include_out_of_stock=bool(include_out_of_stock),
    )


# ============================================================ الفلاتر الصلبة

def _violations(product, req, prof):
    """الفلاتر بالترتيب الثابت: stock, rejected, budget, category, color, avoid_color, size."""
    out = []
    sku = str(getattr(product, "sku", ""))
    exempt = sku in req.exempt_skus
    if not req.include_out_of_stock and not getattr(product, "in_stock", True):
        out.append("stock")
    if not exempt and sku in req.excluded_skus:
        out.append("rejected")
    if not exempt and (req.budget_max is not None or req.budget_min is not None):
        price = _dec(getattr(product, "price", None))
        if price is not None:
            if (req.budget_max is not None and price > req.budget_max) or (
                req.budget_min is not None and price < req.budget_min
            ):
                out.append("budget")
    if req.categories and not (set(req.categories) & prof.cats):
        out.append("category")
    if req.colors and not (set(req.colors) & prof.colors):
        out.append("color")
    if req.avoid_colors and prof.colors and prof.colors <= set(req.avoid_colors):
        out.append("avoid_color")
    if req.sizes and prof.sizes and ONE_SIZE not in prof.sizes and not (prof.sizes & set(req.sizes)):
        out.append("size")
    return out


def check_product(product, req):
    """[] إذا المنتج بيحقق المتطلبات، وإلا أسماء المتطلبات المخالَفة."""
    req = req or Requirements()
    return _violations(product, req, _Profile(product))


# ============================================================ الترتيب

def _stem(token):
    for prefix in ("ال",):
        if token.startswith(prefix) and len(token) > 4:
            token = token[len(prefix):]
    if token.isascii() and len(token) > 3:
        if token.endswith("ies"):
            return token[:-3] + "y"
        if token.endswith(("sses", "shes", "ches", "xes")):
            return token[:-2]
        if token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
    return token


def _stems(text):
    return {_stem(t) for t in norm_text(text).split()}


def _query_tokens(query):
    out = []
    for token in norm_text(query).split():
        if token in _STOPWORDS:
            continue
        stem = _stem(token)
        if stem not in out:
            out.append(stem)
    return out


# مع متجه استعلام، التشابه الدلالي هو إشارة الترتيب الأساسية (الوزن أعلى من تطابق الجذوع).
SEMANTIC_WEIGHT = 10.0
# تحت هالعتبة المرشّح قريب مش مطابق: البرومبت بيطلب من البائع يقول هالشي بصراحة.
CLOSE_SIMILARITY = 0.35
CLOSE_WARNING = "close alternative: weak semantic match to the request"


def _cosine(a, b):
    try:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
    except TypeError:
        return 0.0
    return dot / (na * nb) if na and nb else 0.0


def _similarity(product, query_vector):
    embedding = getattr(product, "embedding", None)
    if not query_vector or not embedding:
        return None
    return max(_cosine(query_vector, embedding), 0.0)


def _score(product, prof, req, qtokens, qcolors, qcats, shown, query_vector):
    score = 0.0
    if qtokens:
        fields = (
            (3.0, _stems(getattr(product, "title", "") or "")),
            (2.0, set().union(*(_stems(t) for t in _tags(product))) if _tags(product) else set()),
            (2.0, _stems(getattr(product, "category", "") or "")),
            (1.0, _stems(getattr(product, "description", "") or "")),
        )
        total = 0.0
        for token in qtokens:
            total += max((w for w, stems in fields if token in stems), default=0.0)
        score += 4.0 * total / (3.0 * len(qtokens))
    if req.colors and set(req.colors) & prof.colors:
        score += 1.5
    if req.categories and set(req.categories) & prof.cats:
        score += 1.5
    score += 1.0 * len(qcolors & prof.colors) + 1.0 * len(qcats & prof.cats)
    price = _dec(getattr(product, "price", None))
    if req.budget_max is not None and price is not None and req.budget_max * Decimal("0.5") <= price <= req.budget_max:
        score += 0.5
    if _has_sale(product):
        score += 0.3
    if str(getattr(product, "sku", "")) not in shown:
        score += 0.6
    similarity = _similarity(product, query_vector)
    if similarity is not None:
        score += SEMANTIC_WEIGHT * similarity
    return round(score, 6)


def _prefix(product):
    return " ".join(norm_text(getattr(product, "title", "") or "").split()[:2])


def _diversify(ranked, limit):
    """MMR-lite: بحد أقصى 2 بنفس بادئة العنوان قبل ما نعبّي الباقي."""
    picks, deferred, seen = [], [], Counter()
    for cand in ranked:
        prefix = _prefix(cand.product)
        if len(picks) < limit and seen[prefix] < MAX_PER_PREFIX:
            seen[prefix] += 1
            picks.append(cand)
        else:
            deferred.append(cand)
    picks.extend(deferred[: max(limit - len(picks), 0)])
    return picks[:limit]


def _size_warnings(product, prof, req):
    if not req.sizes:
        return []
    if not prof.sizes:
        return ["size not verifiable"]
    if ONE_SIZE in prof.sizes and not (prof.sizes & set(req.sizes)):
        return ["one size only; fit not verified"]
    return []


def _rank(rows, req, qtokens, qcolors, qcats, shown, query_vector, limit, *, exact, relaxed, warn):
    scored = []
    for product, prof in rows:
        score = _score(product, prof, req, qtokens, qcolors, qcats, shown, query_vector)
        scored.append((score, str(getattr(product, "sku", "")), product, prof))
    scored.sort(key=lambda t: (-t[0], t[1]))
    ranked = []
    for score, _, product, prof in scored:
        warnings = list(warn(product, prof)) + _size_warnings(product, prof, req)
        similarity = _similarity(product, query_vector)
        if similarity is not None and similarity < CLOSE_SIMILARITY:
            warnings.append(CLOSE_WARNING)
        ranked.append(Candidate(
            product=product, score=score, exact=exact, relaxed=relaxed, warnings=warnings,
        ))
    return _diversify(ranked, limit)


def search(products, req, *, query="", limit=8, shown_skus=(), relax=True, query_vector=None):
    req = req or Requirements()
    items = list(products or [])
    limit = max(int(limit or 0), 0)
    shown = {str(s) for s in (shown_skus or ())}
    qtokens = _query_tokens(query)
    qcolors, qcats = set(_scan(query or "", "color")), set(_scan(query or "", "category"))

    dropped = dict.fromkeys(DROP_KEYS, 0)
    exact_rows, color_rows, category_rows = [], [], []
    for product in items:
        prof = _Profile(product)
        violated = _violations(product, req, prof)
        if not violated:
            exact_rows.append((product, prof))
            continue
        dropped[violated[0]] += 1
        if violated == ["color"]:
            color_rows.append((product, prof))
        elif violated == ["category"]:
            category_rows.append((product, prof))

    def finish(candidates, relaxed_fields, note):
        for cand in candidates:
            cand.reasons = explain(cand.product, req, query)
        return SearchResult(
            candidates=candidates,
            exact_count=sum(1 for c in candidates if c.exact),
            relaxed_fields=relaxed_fields,
            considered=len(items),
            dropped=dropped,
            note=note,
        )

    rank = lambda rows, **kw: _rank(rows, req, qtokens, qcolors, qcats, shown, query_vector, limit, **kw)  # noqa: E731
    if exact_rows:
        return finish(rank(exact_rows, exact=True, relaxed=(), warn=lambda p, f: ()), (), "")

    if relax and limit:
        if req.colors and color_rows:
            wanted = "/".join(req.colors)

            def color_warn(product, prof):
                own = ", ".join(sorted(prof.colors)) or None
                return [f"requested color {wanted} not available; "
                        + (f"this one is {own}" if own else "this one has no listed color")]

            cands = rank(color_rows, exact=False, relaxed=("color",), warn=color_warn)
            return finish(cands, ("color",), "no_exact_match")

        if req.categories and category_rows:
            related = set()
            for cat in req.categories:
                related |= RELATED.get(cat, frozenset())
            rows = [(p, f) for p, f in category_rows if f.cats & related]
            if rows:
                wanted = "/".join(req.categories)

                def category_warn(product, prof):
                    return [f"requested category {wanted} not available; this one is "
                            + ", ".join(sorted(prof.cats & related))]

                cands = rank(rows, exact=False, relaxed=("category",), warn=category_warn)
                return finish(cands, ("category",), "no_exact_match")

    return finish([], (), "nothing_matches")


# ============================================================ حقائق وشرح

def explain(product, req, query=""):
    """أسباب مبنية من حقول المنتج والمتطلبات فقط — ما في اختراع خامات/ميزات."""
    req = req or Requirements()
    reasons = []
    prof = _Profile(product)
    sources = _colors_with_source(product)

    wanted_cats = [c for c in req.categories if c in prof.cats]
    if not req.categories:
        wanted_cats = [c for c in _scan(query or "", "category") if c in prof.cats]
    for cat in wanted_cats:
        reasons.append(Reason("category", f"category: {cat}"))

    wanted_colors = [c for c in req.colors if c in sources]
    if not req.colors:
        wanted_colors = [c for c in _scan(query or "", "color") if c in sources]
    for color in wanted_colors:
        reasons.append(Reason("color", f"comes in {color} ({sources[color]})"))

    if req.sizes and prof.sizes:
        hit = [s for s in req.sizes if s in prof.sizes]
        if hit:
            label = "size" if len(hit) == 1 else "sizes"
            reasons.append(Reason("size", f"{label} {', '.join(hit)} available (sizes: {', '.join(_sizes_as_written(product))})"))
        elif ONE_SIZE in prof.sizes:
            reasons.append(Reason("size", "available in one size"))

    price = _dec(getattr(product, "price", None))
    if price is not None:
        low, high = req.budget_min, req.budget_max
        if high is not None and price <= high and (low is None or price >= low):
            if low is None:
                reasons.append(Reason("budget", f"price {_money(price)} is within the {_money(high)} budget"))
            else:
                reasons.append(Reason("budget", f"price {_money(price)} is within the {_money(low)}-{_money(high)} budget"))
        elif high is None and low is not None and price >= low:
            reasons.append(Reason("budget", f"price {_money(price)} is above the {_money(low)} minimum"))

    if _has_sale(product):
        reasons.append(Reason("sale", f"real sale: was {_money(product.compare_at_price)}, now {_money(price)}"))

    qtokens = set(_query_tokens(query))
    if qtokens:
        for tag in _tags(product):
            if qtokens & _stems(tag):
                reasons.append(Reason("tag", f"tagged: {tag}"))

    if getattr(product, "in_stock", True):
        reasons.append(Reason("stock", "in stock"))
    return reasons


def _description_excerpt(product):
    text = " ".join(str(getattr(product, "description", "") or "").split())
    return text if len(text) <= DESCRIPTION_CHARS else text[:DESCRIPTION_CHARS] + "…"


def _row(cand, currency=""):
    product = getattr(cand, "product", cand)
    row = {
        "sku": product.sku,
        "title": product.title,
        "price": _money(product.price),
    }
    if _has_sale(product):
        row["was_price"] = _money(product.compare_at_price)
    row.update({
        "currency": currency or "",
        "category": getattr(product, "category", "") or "",
        "colors": sorted(product_colors(product)),
        "sizes": _sizes_as_written(product),
        "description": _description_excerpt(product),
        "tryon": bool(product.supports_tryon),
        "exact": bool(getattr(cand, "exact", True)),
        "why": [r.text for r in getattr(cand, "reasons", [])],
        "warnings": list(getattr(cand, "warnings", [])),
    })
    return row


def describe_candidates(candidates, currency=""):
    """صفوف مضغوطة قابلة لـ JSON للبرومبت/نتائج الأدوات."""
    return [_row(c, currency) for c in candidates or []]


def compare(products, req=None):
    """جنب-لجنب لـ 2-3 منتجات: حقائق فقط."""
    products = list(products or [])
    items = [_row(Candidate(product=p, reasons=explain(p, req, "") if req else []), "") for p in products]
    for item in items:
        item.pop("currency", None)
    differences = []

    def line(label, values):
        if len({repr(v) for v in values}) > 1:
            differences.append(f"{label}: " + " vs ".join(f"{i['sku']} {v}" for i, v in zip(items, values)))

    if len(items) > 1:
        line("price", [i["price"] for i in items])
        line("colors", [", ".join(i["colors"]) or "none listed" for i in items])
        line("sizes", [", ".join(i["sizes"]) or "none listed" for i in items])
        line("category", [i["category"] or "none" for i in items])
        line("sale", [("was " + i["was_price"]) if "was_price" in i else "no sale" for i in items])
        line("stock", [("in stock" if getattr(p, "in_stock", True) else "out of stock") for p in products])
        line("try-on", [("yes" if i["tryon"] else "no") for i in items])

    priced = [(_dec(p.price), str(p.sku)) for p in products if _dec(p.price) is not None]
    return {
        "items": items,
        "differences": differences,
        "cheapest_sku": min(priced)[1] if priced else None,
    }
