"""تحديد معدّل الطلبات للـ endpoints العامة (مفتاح الودجت علني، فأي حدا بيقدر
يستدعيها). نافذة ثابتة فوق cache جانغو — تقريبية عمداً: بتحمي من الإساءة
(تكلفة LLM، حرق رصيد الصور) وما بتحاول تكون عدّاد دقيق.

لو الـ cache تعطّل منفتح (fail-open) ونسجّل — الأفضل خدمة الزبون من حجبه
بسبب عطل بالحماية.
"""

import logging
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# اسم القاعدة ← (الحد الأقصى، طول النافذة بالثواني)
DEFAULT_LIMITS = {
    "chat_ip": (20, 60),
    "chat_ip_hour": (200, 3600),
    "chat_token": (1000, 60),
    "tryon_ip": (6, 60),
    "tryon_token": (60, 60),
    "event_ip": (60, 60),
    "pageproduct_ip": (30, 60),
    "consume_token": (300, 60),
}


def client_ip(request):
    """عنوان الزبون. وراء nginx بنوثق بآخر عنوان بـ X-Forwarded-For (يلي أضافه
    nginx نفسه)، مش الأول لأنه العميل بيقدر يكتبه."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded and getattr(settings, "TRUSTED_PROXY_COUNT", 1) > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _limit(rule):
    overrides = getattr(settings, "RATE_LIMITS", {}) or {}
    return overrides.get(rule) or DEFAULT_LIMITS[rule]


def hit(rule, key):
    """بيزيد العدّاد وبيرجع (مسموح؟، ثواني الانتظار)."""
    limit, window = _limit(rule)
    bucket = int(time.time() // window)
    cache_key = f"rl:{rule}:{key}:{bucket}"
    try:
        if cache.add(cache_key, 1, timeout=window + 5):
            count = 1
        else:
            count = cache.incr(cache_key)
    except Exception:  # noqa: BLE001 — فشل الـ cache ما لازم يوقف الخدمة
        logger.exception("ratelimit_backend_failed rule=%s", rule)
        return True, 0
    if count > limit:
        return False, window - int(time.time() % window)
    return True, 0


def check(request, rules_and_keys):
    """rules_and_keys: [(rule, key), ...]. برجّع (True, 0) أو (False, retry_after)."""
    worst = 0
    allowed = True
    for rule, key in rules_and_keys:
        ok, retry = hit(rule, key)
        if not ok:
            allowed = False
            worst = max(worst, retry)
    return allowed, worst
