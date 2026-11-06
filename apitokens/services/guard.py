"""مدقّق المخرجات — نقطة العبور الوحيدة لأي نص بيشوفه الزبون.

القاعدة: ما في خصم، نسبة، سعر، كود، أو وعد ترويجي بيوصل للزبون إلا إذا
كان مصدره بيانات حقيقية بالسيرفر (كتالوج التاجر، معلومات المتجر، كود خصم
فعّال اعتمده الكود). الموديل ما بيُصدَّق بأي رقم أو كود من عنده.

بيفحص بالجملة (مش بالرد كامل) حتى نقدر نحذف الجملة المخالفة بس لو فشلت
إعادة المحاولة. الفحص ما إله علاقة بلغة الزبون: الأرقام والأكواد حتمية،
وقاموس الكلمات الترويجية متعدد اللغات (طبقة تانية، مش الوحيدة).
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

UNAPPROVED_CODE = "unapproved_code"
UNVERIFIED_PERCENT = "unverified_percent"
UNVERIFIED_AMOUNT = "unverified_amount"
UNAPPROVED_PROMO = "unapproved_promo"
UNVERIFIED_STORE_PROMO = "unverified_store_promo"

_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

NUMBER = r"\d+(?:[.,]\d+)*"
PCT_WORDS = (
    r"(?:%|٪|percent|per\s?cent|بالمئة|بالمية|بالمائة|في\s?المئة|في\s?المية|"
    r"pour\s?cent|por\s?ciento|por\s?cento|prozent)"
)
PERCENT_AFTER = re.compile(rf"({NUMBER})\s*{PCT_WORDS}", re.IGNORECASE)
PERCENT_BEFORE = re.compile(rf"(?:%|٪|yüzde)\s*({NUMBER})", re.IGNORECASE)
PERCENT_MARKER = re.compile(PCT_WORDS, re.IGNORECASE)

CURRENCY = (
    r"(?:[$€£]|\b(?:usd|sar|sr|aed|eur|gbp|kwd|qar|bhd|omr|jod|egp)\b|"
    r"ريال|ر\.\s?س|دولار|درهم|دينار|جنيه|ليرة)"
)
AMOUNT_PRE = re.compile(rf"{CURRENCY}\s*({NUMBER})", re.IGNORECASE)
AMOUNT_POST = re.compile(rf"({NUMBER})\s*{CURRENCY}", re.IGNORECASE)

# كلمات "الكود/الكوبون": مسموحة بس لما يكون في عرض كود معتمد (تخفيض المنتج
# الحقيقي ما بيبرّر ذكر كود). العربي بدون حدود كلمات (بخصم، والكود...).
CODE_LEXICON = re.compile(
    r"كود|كوبون|قسيمة|قسائم|بروموكود|coupon|voucher|\bcodes?\b|c[oó]digo|cup[oó]n|cupom|"
    r"gutschein|kupon|kobon|code\s+promo",
    re.IGNORECASE,
)
# كلام خصم/تخفيض: مسموح لما في عرض معتمد أو تخفيض حقيقي (compare_at) على كرت بالرد.
DISCOUNT_LEXICON = re.compile(
    r"خصم|خصوم|تخفيض|تنزيلات|عرض\s*خاص|عرض\s*حصري|سعر\s*خاص|تخفیف|رعایت|"
    # "عروض" = offers، لكن "معروض" = displayed (كلمة عادية جداً): لازم الكلمة كاملة، بدون "م" قبلها
    r"(?<![\u0621-\u064a])(?:[وبلفك]?ال|[وبلفك])?عروض(?:نا|ك|ي|ه|ها|هم)?(?![\u0621-\u064a])|"
    r"discount|promo|markdown|special\s+(?:price|offer)|price\s+drop|(?:\d|%|٪)\s*off\b|"
    r"r[eé]duction|remise|rabais|descuento|rebaja|desconto|promo[cç][aã]o|rabatt|indirim|sconto|"
    r"khasm|5asm|takhfeed|takhfid",
    re.IGNORECASE,
)

# وعود المتجر (شحن مجاني، هدية، 1+1) — مسموحة فقط إذا نفس العبارة مكتوبة بمعلومات المتجر.
STORE_PROMO = re.compile(
    r"free\s+(?:shipping|delivery|gift)|(?:شحن|توصيل)\s*(?:مجاني|مجانا|مجانًا|مجاناً)|هدية\s*مجانية|"
    r"livraison\s+gratuite|env[ií]o\s+gratis|frete\s+gr[aá]tis|kostenlos(?:er)?\s+versand|"
    r"[üu]cretsiz\s+kargo|buy\s+one\s+get\s+one|\bbogo\b",
    re.IGNORECASE,
)

# ---- "ما في خصم" الصادق مقابل الترويج
# جملة بتنفي وجود خصم/كود ("ما عندي كود حالياً"، "there is no discount right now") مش ترويج، فمسموحة
# حتى لو ما في كود معتمد. لازم كل الشروط: كلمة نفي قريبة من كلمة الخصم/الكود (كل ذكر منفي)، وكلمة
# توفّر/قدرة ("فيه/عندي/متاح/أقدر/available/have"...)، بدون أرقام، وبدون شرط/وعد مؤجَّل ("لو/إذا/بعدين/if/when").
# هيك "ما أحلى هالخصم" أو "ما في كود، بس لو اشتريت قطعتين بنخفّض" ما بتعدّي. الأرقام والنسب والأكواد
# الفعلية بتنفحص بشكل مستقل وما بتتأثر.
_AR = "\u0621-\u064a"
NEGATION = re.compile(
    rf"(?<![{_AR}])(?:ما|مو|مب|لا|ليس|ليست|غير|بدون|مافي|مافيه|ماعندي|ماعندنا|ماعندك|ماعندكم|ماني|ماقدر|مااقدر|ماأقدر)(?![{_AR}])"
    r"|(?<![a-z'])(?:no|not|none|never|without|cannot|can't|cant|don't|dont|doesn't|doesnt|isn't|isnt|aren't|arent|"
    r"there's\s+no|there\s+is\s+no|nothing)(?![a-z'])",
    re.IGNORECASE,
)
AVAILABILITY = re.compile(
    rf"(?<![{_AR}])(?:في|فيه|عندي|عندنا|عندك|عندكم|يوجد|توجد|متاح|متاحة|متوفر|متوفرة|موجود|موجودة|اقدر|أقدر|نقدر|أقدم|اقدم|"
    rf"أعطيك|اعطيك|أوعد|اوعد|أعرض|اعرض|نملك|حالياً|حاليا|الحين|حالي|للحين|الآن)(?![{_AR}])"
    r"|(?<![a-z])(?:available|have|has|offer|offering|give|promise|currently|right\s+now|at\s+the\s+moment|there\s+is|there's|exist|exists)(?![a-z])",
    re.IGNORECASE,
)
DEFERRAL = re.compile(
    rf"(?<![{_AR}])(?:لو|إذا|اذا|إن|لين|لما|متى|بعدين|بعد|مستقبلاً|لاحقاً|لاحقا|قريباً|قريبا|إلا|الا|بس\s+لو|لكن\s+لو)(?![{_AR}])"
    r"|(?<![a-z])(?:if|when|once|unless|later|soon|maybe|might|future|until|after)(?![a-z])",
    re.IGNORECASE,
)
_WORD = re.compile(rf"[{_AR}a-z0-9']+", re.IGNORECASE)
_CLAUSE_SPLIT = re.compile(rf"[،,;؛:]|(?<![{_AR}])(?:بس|لكن)(?![{_AR}])|(?<![a-z])(?:but|however|though)(?![a-z])", re.IGNORECASE)
_NEG_BEFORE, _NEG_AFTER = 4, 3


def _negated_in_clause(clause):
    hits = [m.span() for m in DISCOUNT_LEXICON.finditer(clause)] + [m.span() for m in CODE_LEXICON.finditer(clause)]
    if not hits:
        return True   # جملة فرعية بدون كلمة خصم ما بتعنينا هون
    negations = [m.span() for m in NEGATION.finditer(clause)]
    if not negations:
        return False
    words = [m.span() for m in _WORD.finditer(clause)]

    def negated(start, end):
        before = [w for w in words if w[1] <= start][-_NEG_BEFORE:]
        after = [w for w in words if w[0] >= end][:_NEG_AFTER]
        window = before + after
        if not window:
            return False
        lo, hi = min(w[0] for w in window), max(w[1] for w in window)
        return any(n[0] >= lo and n[1] <= hi for n in negations)

    return all(negated(a, b) for a, b in hits)


def is_truthful_no_discount(norm):
    """norm = جملة مطبّعة (normalize). صح فقط لو الجملة بتنفي وجود خصم/كود بدون أي وعد أو رقم:
    كل جملة فرعية فيها كلمة خصم/كود لازم تكون منفية بحد ذاتها."""
    has_hit = DISCOUNT_LEXICON.search(norm) or CODE_LEXICON.search(norm)
    if not has_hit or re.search(r"\d", norm) or DEFERRAL.search(norm) or not AVAILABILITY.search(norm):
        return False
    return all(_negated_in_clause(c) for c in _CLAUSE_SPLIT.split(norm))


# كود مكتوب بجنب كلمة "كود": إذا في كود معتمد، أي كود تاني (حروف كبيرة/أرقام) بيعتبر كود مخترَع.
CODE_TOKEN_AFTER_WORD = re.compile(
    r"(?:كود|الكود|كوبون|الكوبون|بروموكود|coupon|voucher|promo\s*code|code|codes)\s*[:：=\-]?\s*(?:هو\s*|is\s*)?"
    r"[\"'«“]?([A-Za-z0-9][A-Za-z0-9_-]{2,})",
    re.IGNORECASE,
)


def _invented_code_tokens(sentence, approved, product_skus=()):
    approved = (approved or "").strip().lower()
    found = []
    for m in CODE_TOKEN_AFTER_WORD.finditer(sentence.translate(_DIGIT_MAP)):
        tok = m.group(1)
        looks_like_code = any(ch.isdigit() for ch in tok) or (tok.isupper() and len(tok) >= 3)
        if looks_like_code and tok.lower() != approved and tok.lower() not in product_skus:
            found.append(tok)
    return found


SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟…])\s+|\n+")

SAFE_REPLY = {
    "ar": "خلّني أساعدك بشكل أدق — وش أكثر شي يهمّك في القطعة؟",
    "en": "Let me help you more precisely — what matters most to you in this piece?",
}


def normalize(text):
    """أرقام عربية/فارسية ← لاتينية، وفواصل عربية، وحروف صغيرة."""
    return (text or "").translate(_DIGIT_MAP).replace("٫", ".").replace("٬", ",").lower()


def to_decimal(raw):
    s = raw
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+", s) else s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _numbers(text):
    return [d for d in (to_decimal(m) for m in re.findall(NUMBER, normalize(text))) if d is not None]


def _percents(text):
    norm = normalize(text)
    found = [m for m in PERCENT_AFTER.findall(norm)] + [m for m in PERCENT_BEFORE.findall(norm)]
    return [d for d in (to_decimal(m) for m in found) if d is not None]


@dataclass
class Violation:
    kind: str
    snippet: str


@dataclass
class GroundingContext:
    approved_code: str = ""
    # نسب الخصم المسموحة: وصف الكود المعتمد + نسبة التخفيض الحقيقي (compare_at).
    discount_percents: list = field(default_factory=list)
    # نسب مسموحة بسياق غير الخصم (مثلاً "100% قطن" من وصف المنتج).
    general_percents: list = field(default_factory=list)
    amounts: list = field(default_factory=list)
    merchant_codes: list = field(default_factory=list)
    sale_in_response: bool = False
    store_promo_allowed: bool = False
    store_info_norm: str = ""
    product_skus: set = field(default_factory=set)   # أكواد المنتجات (SKU) مش أكواد خصم


def build_context(
    *, products=(), merchant_codes=(), approved_offer=None, cards=(), store_info="", customer_texts=(),
):
    """بيجمع كل الحقائق يلي بتبرّر أرقام/أكواد الرد. مصدرها كلها سيرفر: منتجات
    التاجر، معلومات المتجر، الكود المعتمد، وأرقام الزبون نفسه (ميزانيته)."""
    ctx = GroundingContext()
    ctx.merchant_codes = [str(c).strip() for c in merchant_codes if str(c).strip()]
    amounts, discount_percents, general_percents = [], [], []
    ctx.product_skus = {str(getattr(p, "sku", "")).lower() for p in products if getattr(p, "sku", "")}

    def add_price(value):
        value = Decimal(str(value))
        amounts.append(value)
        for rounded in (value.to_integral_value(), value.to_integral_value(rounding="ROUND_FLOOR"),
                        value.to_integral_value(rounding="ROUND_CEILING")):
            amounts.append(rounded)

    for p in products:
        add_price(p.price)
        general_percents.extend(_percents(f"{p.title} {p.description}"))
        amounts.extend(_numbers(p.title))
        amounts.extend(_numbers(p.description))
        if p.compare_at_price:
            add_price(p.compare_at_price)
            saving = Decimal(str(p.compare_at_price)) - Decimal(str(p.price))
            if saving > 0:
                add_price(saving)
                pct = (saving / Decimal(str(p.compare_at_price)) * 100)
                for step in (-1, 0, 1):
                    discount_percents.append(pct.to_integral_value() + step)
    ctx.sale_in_response = any(c.get("compare_at_price") for c in cards)

    if approved_offer:
        ctx.approved_code = approved_offer["code"]
        description = approved_offer.get("description", "")
        discount_percents.extend(_percents(description))
        amounts.extend(_numbers(description))

    store_norm = normalize(store_info)
    ctx.store_info_norm = store_norm
    ctx.store_promo_allowed = bool(STORE_PROMO.search(store_norm))
    amounts.extend(_numbers(store_info))
    general_percents.extend(_percents(store_info))
    for text in customer_texts:
        amounts.extend(_numbers(text))

    ctx.amounts = amounts
    ctx.discount_percents = discount_percents
    ctx.general_percents = general_percents + discount_percents
    return ctx


def _in(value, allowed, tolerance=Decimal("0.005")):
    return any(abs(value - a) <= tolerance for a in allowed)


def _sentence_violations(sentence, ctx):
    found = []
    norm = normalize(sentence)

    for code in ctx.merchant_codes:
        if len(code) < 3 or code.lower() == ctx.approved_code.lower():
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(code.lower())}(?![a-z0-9])", norm):
            found.append(Violation(UNAPPROVED_CODE, code))
    code_hit = CODE_LEXICON.search(norm)
    discount_hit = DISCOUNT_LEXICON.search(norm)
    # "ما عندي كود حالياً": صدق مش ترويج (أي رقم/وعد/كود فعلي بالجملة بيلغي هالاستثناء).
    truthful_no_discount = bool(code_hit or discount_hit) and not ctx.approved_code and is_truthful_no_discount(norm)
    if code_hit and not ctx.approved_code and not truthful_no_discount:
        found.append(Violation(UNAPPROVED_PROMO, code_hit.group(0)))
    if discount_hit and not ctx.approved_code and not ctx.sale_in_response and not truthful_no_discount:
        found.append(Violation(UNAPPROVED_PROMO, discount_hit.group(0)))
    if ctx.approved_code:
        for tok in _invented_code_tokens(sentence, ctx.approved_code, ctx.product_skus):
            found.append(Violation(UNAPPROVED_CODE, tok))
    has_discount_words = bool(discount_hit or code_hit)

    percents = _percents(sentence)
    allowed = ctx.discount_percents if has_discount_words else ctx.general_percents
    for value in percents:
        if not _in(value, allowed):
            found.append(Violation(UNVERIFIED_PERCENT, f"{value}%"))
    # نسبة مكتوبة بالحروف ("عشرين بالمئة"): ما فينا نتحقق منها.
    spelled = len(PERCENT_MARKER.findall(norm)) - len(percents)
    if spelled > 0:
        found.append(Violation(UNVERIFIED_PERCENT, "spelled-out percent"))

    amounts = [m for m in AMOUNT_PRE.findall(norm)] + [m for m in AMOUNT_POST.findall(norm)]
    for raw in amounts:
        value = to_decimal(raw)
        if value is not None and not _in(value, ctx.amounts):
            found.append(Violation(UNVERIFIED_AMOUNT, raw))

    if STORE_PROMO.search(norm) and not ctx.store_promo_allowed:
        found.append(Violation(UNVERIFIED_STORE_PROMO, STORE_PROMO.search(norm).group(0)))
    return found


def sentences(text):
    return [s for s in SENTENCE_SPLIT.split(text or "") if s.strip()]


def find_violations(text, ctx):
    found = []
    for sentence in sentences(text):
        found.extend(_sentence_violations(sentence, ctx))
    return found


def strip_violations(text, ctx):
    """بيحذف الجمل المخالفة بس. لو ما بقي شي برجّع نص فاضي."""
    kept = [s for s in sentences(text) if not _sentence_violations(s, ctx)]
    return " ".join(kept).strip()


def kinds(violations):
    return sorted({v.kind for v in violations})


CORRECTION_TEXT = (
    "\n\nCORRECTION — read before replying: your previous reply contained claims the store has not "
    "authorized ({kinds}). Rewrite your reply to the customer's last message WITHOUT any discount, "
    "code, percentage, price figure, free-shipping or other promotional claim that is not written "
    "literally in the catalog, the store information or the active discount codes above. Do not "
    "calculate prices or savings yourself. Use the product's real value or a genuine alternative "
    "instead, in the customer's language."
)
