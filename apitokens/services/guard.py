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
    r"خصم|خصوم|تخفيض|تنزيلات|عرض\s*خاص|عروض|عرض\s*حصري|سعر\s*خاص|تخفیف|رعایت|"
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


def build_context(
    *, products=(), merchant_codes=(), approved_offer=None, cards=(), store_info="", customer_texts=(),
):
    """بيجمع كل الحقائق يلي بتبرّر أرقام/أكواد الرد. مصدرها كلها سيرفر: منتجات
    التاجر، معلومات المتجر، الكود المعتمد، وأرقام الزبون نفسه (ميزانيته)."""
    ctx = GroundingContext()
    ctx.merchant_codes = [str(c).strip() for c in merchant_codes if str(c).strip()]
    amounts, discount_percents, general_percents = [], [], []

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
    if code_hit and not ctx.approved_code:
        found.append(Violation(UNAPPROVED_PROMO, code_hit.group(0)))
    discount_hit = DISCOUNT_LEXICON.search(norm)
    if discount_hit and not ctx.approved_code and not ctx.sale_in_response:
        found.append(Violation(UNAPPROVED_PROMO, discount_hit.group(0)))
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
