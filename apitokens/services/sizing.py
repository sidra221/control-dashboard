"""ترشيح المقاس من قياسات الزبون (الطول/الوزن/الخصر) — حساب بالكود مش
بالموديل، حتى ما يخترع المساعد مقاس.

جدول التاجر بيتكتب بالداشبورد سطر لكل مقاس:
    S, 150-160, 45-55, 60-66
يعني: المقاس، الطول (سم)، الوزن (كغ)، الخصر (سم). أي عمود ممكن يكون "-" إذا
مش معروف. إذا التاجر ما حط جدول منستخدم جدول قياسي عام (والترشيح تقديري).
"""

import re

MEASURES = ("height", "weight", "waist")

# جدول قياسي عام تقريبي (مقاسات عربية/أوروبية شائعة) — بس لما التاجر ما يحط جدول.
DEFAULT_CHART = [
    {"size": "XS", "height": [145, 155], "weight": [38, 47], "waist": [56, 62]},
    {"size": "S", "height": [150, 162], "weight": [46, 56], "waist": [62, 68]},
    {"size": "M", "height": [158, 170], "weight": [55, 66], "waist": [68, 76]},
    {"size": "L", "height": [165, 176], "weight": [65, 78], "waist": [76, 84]},
    {"size": "XL", "height": [170, 182], "weight": [77, 90], "waist": [84, 94]},
    {"size": "XXL", "height": [175, 190], "weight": [89, 105], "waist": [94, 104]},
]

RANGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*$")
SIZE_OPTION_NAMES = re.compile(r"size|مقاس|المقاس|القياس|المقاسات", re.IGNORECASE)


class SizeChartError(ValueError):
    pass


def parse_chart(text):
    """نص الداشبورد ← قائمة مقاسات. برمي SizeChartError برسالة واضحة للسطر الغلط."""
    rows = []
    for number, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[,،]", line)]
        if len(parts) < 2 or not parts[0]:
            raise SizeChartError(f"line {number}")
        row = {"size": parts[0][:20]}
        for measure, value in zip(MEASURES, parts[1:]):
            if value in ("", "-"):
                continue
            match = RANGE_RE.match(value)
            if not match:
                raise SizeChartError(f"line {number}")
            low, high = float(match.group(1)), float(match.group(2))
            row[measure] = [min(low, high), max(low, high)]
        if not any(m in row for m in MEASURES):
            raise SizeChartError(f"line {number}")
        rows.append(row)
    return rows


def chart_to_text(chart):
    def fmt(value):
        return f"{value[0]:g}-{value[1]:g}" if value else "-"

    return "\n".join(
        ", ".join([row["size"]] + [fmt(row.get(m)) for m in MEASURES]) for row in chart or []
    )


def _distance(value, bounds):
    """0 جوا المدى، وإلا البعد عن أقرب طرف نسبةً لعرض المدى."""
    low, high = bounds
    if low <= value <= high:
        return 0.0
    width = max(high - low, 1.0)
    return (low - value if value < low else value - high) / width


def recommend(chart, measurements):
    """أقرب مقاس لقياسات الزبون. الخصر أدق مؤشر فوزنه أعلى. برجّع اسم
    المقاس أو None إذا ما في قياسات كفاية."""
    weights = {"waist": 2.0, "weight": 1.5, "height": 1.0}
    best, best_score = None, None
    for row in chart or []:
        score, used = 0.0, 0
        for measure, factor in weights.items():
            value = measurements.get(measure)
            if value and row.get(measure):
                score += factor * _distance(float(value), row[measure])
                used += 1
        if not used:
            continue
        score /= used
        if best_score is None or score < best_score:
            best, best_score = row["size"], score
    return best


def product_sizes(product):
    """المقاسات المتوفرة بالمنتج (من خيار اسمه مقاس/Size)، أو []."""
    for name, values in (product.options or {}).items():
        if SIZE_OPTION_NAMES.search(str(name)):
            return [str(v) for v in values]
    return []


def _norm(size):
    return re.sub(r"\s+", "", str(size)).upper()


def advise(chart, measurements, available_sizes):
    """المقاس المرشّح + هل متوفر بالمنتج + أقرب متوفر إذا لأ."""
    size = recommend(chart, measurements)
    if size is None:
        return None
    available = {_norm(s): s for s in available_sizes}
    if not available_sizes or _norm(size) in available:
        return {"size": available.get(_norm(size), size), "available": True, "alternative": None}
    order = [_norm(r["size"]) for r in chart]
    alternative = None
    if _norm(size) in order:
        idx = order.index(_norm(size))
        # الأقرب بالترتيب، ولو تعادلوا منفضّل الأكبر (الضيق أسوأ من الواسع).
        for offset in (1, -1, 2, -2, 3, -3):
            j = idx + offset
            if 0 <= j < len(order) and order[j] in available:
                alternative = available[order[j]]
                break
    return {"size": size, "available": False, "alternative": alternative}
