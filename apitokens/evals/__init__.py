"""أدوات التقييم (evals) لمحرّك المبيعات v2 — الحزمة بتحتوي:

- v2_checks.py     فحوصات حتمية (دوال نقية) على رد كل دور
- v2_scenarios.py  المتجر التجريبي + كتالوج السيناريوهات (بيانات بس)
- أوامر الإدارة: eval_v2 (تقييم حي)، bench_v2 (زمن الاستجابة)، kieai_live_check (kie.ai الحقيقي)

هون كمان دوال إحصاء صغيرة مشتركة بين الأوامر (percentile / describe).
"""


def percentile(values, p):
    """النسبة المئوية p (0..100) بالاستيفاء الخطي. قائمة فاضية = None."""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (len(data) - 1) * (p / 100.0)
    low = int(rank)
    high = min(low + 1, len(data) - 1)
    return data[low] + (data[high] - data[low]) * (rank - low)


def describe(values):
    """ملخّص إحصائي: n / mean / p50 / p95 / min / max (None لو ما في قيم)."""
    data = [float(v) for v in values]
    if not data:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "min": None, "max": None}
    return {
        "n": len(data),
        "mean": sum(data) / len(data),
        "p50": percentile(data, 50),
        "p95": percentile(data, 95),
        "min": min(data),
        "max": max(data),
    }
