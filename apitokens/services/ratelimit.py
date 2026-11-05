"""تحديد معدّل الطلبات للـ endpoints العامة (مفتاح الودجت علني، فأي حدا بيقدر
يستدعيها). نافذة ثابتة، والعدّاد بمخزن مشترك بين كل workers وسيرفرات النشر.

المخزن (settings.RATE_LIMIT_BACKEND):
- "database" (الافتراضي): جدول RateLimitBucket بقاعدة البيانات يلي كل نشر عندو
  أصلاً. الزيادة ذرّية: UPDATE بـ F() جوا transaction (قفل الصف بيخلي الطلبات
  المتزامنة تتسلسل، والقراءة بعدها بنفس الـ transaction دقيقة)، وسباق إنشاء أول
  صف بيحلّه unique + إعادة UPDATE.
- "cache": cache جانغو — بس لو مضبوط على مخزن مشترك (Redis/Memcached). LocMem و
  File-based ما بينفعوا (process/host محلي) وفحص النظام (apps.py) بيرفضهم.

القواعد الحساسة (دردشة LLM، تجربة اللبس، الاستهلاك) بتفشل مغلقة: لو المخزن تعطّل
منرفض الطلب (503) ومنسجّل خطأ — ما بنوقف الحماية بصمت. القواعد الخفيفة (أحداث
الودجت، صفحة المنتج) بتفشل مفتوحة بس بخطأ بالسجل.
"""

import logging
import random
import time
from datetime import timedelta
from typing import NamedTuple

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

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
    "memory_ip": (30, 60),
    "consume_ip": (600, 60),
    "consume_token": (300, 60),
}
DEFAULT_FAIL_CLOSED = frozenset(
    {
        "chat_ip", "chat_ip_hour", "chat_token",
        "tryon_ip", "tryon_token",
        "consume_ip", "consume_token",
    }
)
UNAVAILABLE_RETRY_SECONDS = 5
PURGE_PROBABILITY = 0.005

PROXY_HEADERS = (
    "HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST",
    "HTTP_X_REAL_IP", "HTTP_FORWARDED",
)


class Decision(NamedTuple):
    allowed: bool
    retry_after: int = 0
    unavailable: bool = False  # المخزن تعطّل وقاعدة حساسة ← allowed=False


def forwarded_ips(request):
    raw = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def came_through_proxy(request):
    return any(h in request.META for h in PROXY_HEADERS)


def client_ip(request):
    """عنوان الزبون. وراء nginx بنوثق بآخر عنوان بـ X-Forwarded-For (يلي أضافه
    nginx نفسه)، مش الأول لأنه العميل بيقدر يكتبه."""
    parts = forwarded_ips(request)
    if parts and getattr(settings, "TRUSTED_PROXY_COUNT", 1) > 0:
        return parts[-1]
    return request.META.get("REMOTE_ADDR", "") or "unknown"


# ---------------------------------------------------------------- المخازن

class DatabaseStore:
    def incr(self, cache_key, window):
        from ..models import RateLimitBucket

        expires_at = timezone.now() + timedelta(seconds=window + 5)
        with transaction.atomic():
            updated = RateLimitBucket.objects.filter(key=cache_key).update(count=F("count") + 1)
            if not updated:
                try:
                    with transaction.atomic():
                        RateLimitBucket.objects.create(key=cache_key, count=1, expires_at=expires_at)
                    return 1
                except IntegrityError:  # طلب تاني أنشأه بنفس اللحظة
                    RateLimitBucket.objects.filter(key=cache_key).update(count=F("count") + 1)
            # الصف لسا مقفول بهالـ transaction، فالقيمة هون دقيقة (ما حدا زاد بعدنا).
            return RateLimitBucket.objects.values_list("count", flat=True).get(key=cache_key)

    def purge_expired(self):
        from ..models import RateLimitBucket

        deleted, _ = RateLimitBucket.objects.filter(expires_at__lt=timezone.now()).delete()
        return deleted

    def delete(self, cache_key):
        from ..models import RateLimitBucket

        RateLimitBucket.objects.filter(key=cache_key).delete()


class CacheStore:
    def incr(self, cache_key, window):
        if cache.add(cache_key, 1, timeout=window + 5):
            return 1
        try:
            return cache.incr(cache_key)
        except ValueError:  # انتهت صلاحية المفتاح بين add و incr
            cache.set(cache_key, 1, timeout=window + 5)
            return 1

    def purge_expired(self):
        return 0  # الـ cache بينظّف حاله

    def delete(self, cache_key):
        cache.delete(cache_key)


def get_store():
    backend = getattr(settings, "RATE_LIMIT_BACKEND", "database")
    return CacheStore() if backend == "cache" else DatabaseStore()


# ---------------------------------------------------------------- القواعد

def _limit(rule):
    overrides = getattr(settings, "RATE_LIMITS", {}) or {}
    return overrides.get(rule) or DEFAULT_LIMITS[rule]


def fail_closed_rules():
    configured = getattr(settings, "RATE_LIMIT_FAIL_CLOSED", None)
    return frozenset(configured) if configured is not None else DEFAULT_FAIL_CLOSED


def hit(rule, key):
    """بيزيد العدّاد ويرجّع Decision."""
    limit, window = _limit(rule)
    cache_key = f"rl:{rule}:{key}:{int(time.time() // window)}"
    store = get_store()
    try:
        count = store.incr(cache_key, window)
    except Exception:  # noqa: BLE001 — أي عطل بالمخزن
        closed = rule in fail_closed_rules()
        logger.error(
            "ratelimit_backend_failed rule=%s mode=%s", rule, "closed" if closed else "open", exc_info=True,
        )
        if closed:
            return Decision(False, UNAVAILABLE_RETRY_SECONDS, True)
        return Decision(True)
    if random.random() < PURGE_PROBABILITY:
        try:
            store.purge_expired()
        except Exception:  # noqa: BLE001 — التنظيف تحسين مش شرط
            logger.warning("ratelimit_purge_failed", exc_info=True)
    if count > limit:
        return Decision(False, window - int(time.time() % window))
    return Decision(True)


def check(request, rules_and_keys):
    """rules_and_keys: [(rule, key), ...] ← Decision مجمّع. أول عطل بمخزن بيوقف الفحص."""
    allowed, retry = True, 0
    for rule, key in rules_and_keys:
        decision = hit(rule, key)
        if decision.unavailable:
            return decision
        if not decision.allowed:
            allowed = False
            retry = max(retry, decision.retry_after)
    return Decision(allowed, retry)
