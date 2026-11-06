"""حسابات الخصم اللي ما منتركها للموديل: نسبة الكود من وصفه، السعر بعد الكود،
هل الكود انعطى قبل بالمحادثة، وهل الرد فيه نسبة خصم مخترعة.

الموديل بيقرر *متى* يعطي الكود (لحظة التردد/الاعتراض على السعر)، بس الأرقام
(النسبة والسعر الجديد) والقيود (مرة وحدة، بعد ما الزبون يشوف منتج) كلها هون بالكود.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫", "0123456789.")
# "10%" أو "10 %" أو "%10" أو "١٠٪"
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*[%٪]|[%٪]\s*(\d+(?:[.,]\d+)?)")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def normalize_digits(text):
    return (text or "").translate(_ARABIC_DIGITS)


def percents_in(text):
    """كل النسب المئوية المذكورة بالنص (Decimal)."""
    found = []
    for a, b in _PERCENT.findall(normalize_digits(text)):
        try:
            found.append(Decimal((a or b).replace(",", ".")))
        except InvalidOperation:
            continue
    return found


def code_percent(description):
    """نسبة الكود من وصفه ("خصم 10% على كل المنتجات" ← 10). None إذا ما في نسبة واحدة واضحة
    بين 1 و90 — ساعتها ما منحسب سعر بدل ما نحسب غلط."""
    values = {p for p in percents_in(description) if Decimal(1) <= p <= Decimal(90)}
    return values.pop() if len(values) == 1 else None


def price_after(price, percent):
    try:
        price = Decimal(str(price))
    except InvalidOperation:
        return None
    return (price * (Decimal(100) - percent) / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def fmt_money(value):
    """129.00 ← "129"، 116.10 ← "116.10"."""
    value = Decimal(str(value)).quantize(Decimal("0.01"))
    return str(value.quantize(Decimal(1))) if value == value.to_integral() else str(value)


def mentions_amount(text, amount):
    """هل النص فيه هالمبلغ (بأرقام عربية أو لاتينية، 116.1 أو 116.10)."""
    target = Decimal(str(amount)).quantize(Decimal("0.01"))
    for raw in _NUMBER.findall(normalize_digits(text).replace(",", "")):
        try:
            if Decimal(raw).quantize(Decimal("0.01")) == target:
                return True
        except InvalidOperation:
            continue
    return False


def sale_percent(product):
    """نسبة التخفيض الحقيقي على المنتج (was_price) — مدوّرة."""
    if not product.compare_at_price or not product.price:
        return None
    was, now = Decimal(str(product.compare_at_price)), Decimal(str(product.price))
    if was <= now or was <= 0:
        return None
    return ((was - now) * 100 / was).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def codes_already_offered(messages, codes):
    """الأكواد الفعّالة اللي المساعد ذكرها برسائله السابقة (بالترتيب)."""
    offered = []
    for code in codes:
        needle = code.code.strip().lower()
        if needle and any(
            m["role"] == "assistant" and needle in m["content"].lower() for m in messages[:-1]
        ):
            offered.append(code)
    return offered


def invented_percent(text, allowed):
    """أول نسبة بالرد ما إلها أصل حقيقي (كود معطى أو تخفيض فعلي)، وإلا None.
    منسمح بفرق 1 للتدوير (29% مقابل 28.99%)."""
    allowed = [Decimal(a) for a in allowed if a is not None]
    for value in percents_in(text):
        if not any(abs(value - a) <= 1 for a in allowed):
            return value
    return None
