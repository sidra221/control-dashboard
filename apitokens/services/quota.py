"""رصيد المتجر وتوزيعه بين زبائنه.

- الرصيد (ApiToken.image_quota) حوض واحد مشترك لكل زبائن المتجر؛ ما منقسمه عليهم مسبقاً.
- الحواجز: حد لكل زبون (tryons_per_shopper كل 24 ساعة)، سقف يومي للمتجر، حد طلبات، وحجز الرصيد
  قبل التوليد (QuotaReservation) حتى التجارب المتزامنة ما تتجاوز الرصيد.
- quota_status(): الحالة اللي بتعرضها البوابة والداشبورد وبتبني عليها نقاط الودجت.
"""
import logging
import math
import re
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apitokens.models import ApiToken, LlmUsage, QuotaReservation, TryOnEvent

logger = logging.getLogger(__name__)

SHOPPER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
WINDOW = timedelta(hours=24)

REASON_OK = "ok"
REASON_QUOTA_EXCEEDED = "quota_exceeded"
REASON_STORE_DAILY_LIMIT = "store_daily_limit"
REASON_SHOPPER_LIMIT = "shopper_limit_reached"
REASON_TOKEN_DISABLED = "token_disabled"
REASON_INVALID_TOKEN = "invalid_token"


def clean_shopper_id(value):
    """معرّف الزبون الثابت من الودجت: نفس قاعدة session_id (حروف/أرقام/_/-، من 8 لـ 64)، وإلا فاضي."""
    value = value.strip() if isinstance(value, str) else ""
    return value if SHOPPER_ID_PATTERN.match(value) else ""


def active_reservations(merchant, now=None):
    now = now or timezone.now()
    return QuotaReservation.objects.filter(merchant=merchant, expires_at__gt=now).count()


def daily_cap(merchant):
    """السقف اليومي للمتجر بعدد التجارب. None = ما في سقف يومي (رصيد يومي أصلاً)."""
    if merchant.period_days <= 1:
        return None
    cap = max(settings.QUOTA_DAILY_MIN, math.ceil(merchant.image_quota * settings.QUOTA_DAILY_SHARE))
    return min(cap, merchant.image_quota)


def quota_status(merchant, now=None):
    """حالة الرصيد الحالية. الفترة بتتجدّد بكسل عند أول استهلاك بعد انتهائها، فهون بنحسب المستهلك 0
    إذا الفترة انتهت (period_expired=True)."""
    now = now or timezone.now()
    quota = merchant.image_quota
    ends_at = merchant.period_start + timedelta(days=merchant.period_days)
    expired = now >= ends_at
    used = 0 if expired else merchant.images_used
    reserved = active_reservations(merchant, now)
    remaining = max(quota - used - reserved, 0)
    cap = daily_cap(merchant)
    used_today = TryOnEvent.objects.filter(
        merchant=merchant, success=True, created_at__gte=now - timedelta(hours=24),
    ).count()
    daily_remaining = None if cap is None else max(cap - used_today - reserved, 0)
    return {
        "quota": quota,
        "used": used,
        "reserved": reserved,
        "remaining": remaining,
        "percent_used": 100 if quota <= 0 else min(100, int(used * 100 / quota)),
        "period": merchant.period,
        "period_start": merchant.period_start,
        "period_expired": expired,
        "resets_at": ends_at,
        "daily_cap": cap,
        "used_today": used_today,
        "daily_remaining": daily_remaining,
        "exhausted": remaining <= 0,
        "daily_exhausted": daily_remaining is not None and daily_remaining <= 0,
    }


# ---------- حدود الزبون والـIP ----------

def _retry_after(leave_times, limit, now):
    """كم ثانية لحد ما العدد ينزل تحت `limit`. leave_times = لحظات خروج كل عنصر محسوب من النافذة.
    None إذا العدد أصلاً تحت الحد."""
    leave_times = sorted(leave_times)
    if len(leave_times) < limit:
        return None
    leaves_at = leave_times[len(leave_times) - limit]
    return max(1, math.ceil((leaves_at - now).total_seconds()))


def _leave_times(merchant, now, **match):
    """لحظات خروج التجارب الناجحة (آخر 24 ساعة) والحجوزات الفعّالة المطابقة للفلتر من العدّ.
    التجربة بتطلع بعد 24 ساعة من وقتها، والحجز بينتهي بـ expires_at (لو فشلت العملية)."""
    events = TryOnEvent.objects.filter(
        merchant=merchant, success=True, created_at__gt=now - WINDOW, **match,
    ).values_list("created_at", flat=True)
    reservations = QuotaReservation.objects.filter(
        merchant=merchant, expires_at__gt=now, **match,
    ).values_list("expires_at", flat=True)
    return [created + WINDOW for created in events] + list(reservations)


def shopper_status(merchant, shopper_id, ip_hash, now=None):
    """كم تجربة باقية لهالزبون: {remaining: int أو None إذا بلا حد، retry_after_seconds: int أو None إذا ما في حظر}.
    الحد: tryons_per_shopper لمعرّف الزبون + حد IP احتياطي (× TRYON_IP_LIMIT_MULTIPLIER). بدون shopper_id
    (ودجت قديم بالكاش) بنعدّ بالـIP بس ضد limit × المضاعف."""
    now = now or timezone.now()
    limit = merchant.tryons_per_shopper
    if not limit:
        return {"remaining": None, "retry_after_seconds": None}
    ip_limit = limit * settings.TRYON_IP_LIMIT_MULTIPLIER
    remaining = None
    retry = None
    checks = []
    if shopper_id:
        checks.append((limit, {"shopper_id": shopper_id}))
    if ip_hash:
        checks.append((ip_limit, {"ip_hash": ip_hash}))
    for check_limit, match in checks:
        leaves = _leave_times(merchant, now, **match)
        left = max(check_limit - len(leaves), 0)
        remaining = left if remaining is None else min(remaining, left)
        wait = _retry_after(leaves, check_limit, now)
        if wait is not None:
            retry = wait if retry is None else max(retry, wait)
    if remaining is None:  # ما عرفنا الزبون أبداً (لا shopper_id ولا IP): ما في شي نحدّه فيه
        return {"remaining": limit, "retry_after_seconds": None}
    return {"remaining": remaining, "retry_after_seconds": retry if remaining == 0 else None}


def daily_retry_after(merchant, now=None):
    """ثواني لحد ما ينفتح مكان بالسقف اليومي للمتجر (None إذا ما في سقف أو لسا في مكان)."""
    now = now or timezone.now()
    cap = daily_cap(merchant)
    if cap is None:
        return None
    return _retry_after(_leave_times(merchant, now), cap, now)


def availability(merchant, shopper_id, ip_hash, now=None):
    """للودجت عند التحميل: هل زر التجربة متاح لهالزبون الآن؟ {tryon_available, shopper_tryons_left, retry_after_seconds}."""
    now = now or timezone.now()
    status = quota_status(merchant, now)
    shopper = shopper_status(merchant, shopper_id, ip_hash, now)
    available = (
        merchant.is_active
        and not status["exhausted"]
        and not status["daily_exhausted"]
        and (shopper["remaining"] is None or shopper["remaining"] > 0)
    )
    retry = None
    if not available and not status["exhausted"]:
        waits = []
        if status["daily_exhausted"]:
            waits.append(daily_retry_after(merchant, now))
        if shopper["remaining"] == 0:
            waits.append(shopper["retry_after_seconds"])
        waits = [w for w in waits if w is not None]
        retry = max(waits) if waits else None
    return {
        "tryon_available": bool(available),
        "shopper_tryons_left": shopper["remaining"],
        "retry_after_seconds": retry,
    }


# ---------- الحجز الذرّي ----------

@dataclass
class ReserveResult:
    ok: bool
    reason: str
    reservation: object = None
    retry_after_seconds: int = None
    shopper_remaining: int = None
    remaining: int = None


def reserve(merchant_pk, shopper_id, ip_hash, now=None):
    """بتحجز تجربة وحدة من الحوض تحت قفل صف التاجر. الترتيب: التاجر فعّال ← الحوض ← السقف اليومي ←
    حد الزبون/الـIP. القفل بيمنع تجارب متزامنة من تجاوز الرصيد (الحجوزات الفعّالة بتنحسب من الحوض)."""
    now = now or timezone.now()
    with transaction.atomic():
        merchant = ApiToken.objects.select_for_update().filter(pk=merchant_pk).first()
        if merchant is None:
            return ReserveResult(False, REASON_INVALID_TOKEN)
        if not merchant.is_active:
            return ReserveResult(False, REASON_TOKEN_DISABLED)
        status = quota_status(merchant, now)
        if status["exhausted"]:
            return ReserveResult(False, REASON_QUOTA_EXCEEDED, remaining=0)
        if status["daily_exhausted"]:
            return ReserveResult(
                False, REASON_STORE_DAILY_LIMIT, retry_after_seconds=daily_retry_after(merchant, now) or 1,
                remaining=status["remaining"],
            )
        shopper = shopper_status(merchant, shopper_id, ip_hash, now)
        if shopper["remaining"] is not None and shopper["remaining"] <= 0:
            return ReserveResult(
                False, REASON_SHOPPER_LIMIT, retry_after_seconds=shopper["retry_after_seconds"] or 1,
                shopper_remaining=0, remaining=status["remaining"],
            )
        reservation = QuotaReservation.objects.create(
            merchant=merchant, shopper_id=shopper_id or "", ip_hash=ip_hash or "",
            expires_at=now + timedelta(seconds=settings.QUOTA_RESERVATION_TTL_SECONDS),
        )
        left = None if shopper["remaining"] is None else max(shopper["remaining"] - 1, 0)
        return ReserveResult(
            True, REASON_OK, reservation=reservation, shopper_remaining=left,
            remaining=max(status["remaining"] - 1, 0),
        )


def release(reservation):
    """بتلغي الحجز (فشل التوليد). آمنة لو انحذف أو None."""
    if reservation is None:
        return
    QuotaReservation.objects.filter(pk=reservation.pk).delete()


def finalize_success(merchant_pk, reservation, shopper_id, ip_hash, session_id, sku, product):
    """بمعاملة وحدة: بتحذف الحجز، بتخصم من الرصيد (consume_quota بتصفّر الفترة المنتهية)، وبتسجّل التجربة
    الناجحة. بترجّع (merchant, نتيجة consume_quota). لو الخصم رفض بشكل غير متوقع بنسجّل ERROR بس بنسلّم
    الصورة لأنه الحجز كان ضامن المكان. فشل تسجيل الحدث وحده ما بيلغي الخصم."""
    with transaction.atomic():
        merchant = ApiToken.objects.select_for_update().get(pk=merchant_pk)
        if reservation is not None:
            QuotaReservation.objects.filter(pk=reservation.pk).delete()
        result = merchant.consume_quota(1)
        if not result.get("ok"):
            logger.error(
                "quota_consume_failed_after_reservation merchant=%s result=%s", merchant.pk, result,
            )
        try:
            with transaction.atomic():
                TryOnEvent.objects.create(
                    merchant=merchant, session_id=session_id or "", shopper_id=shopper_id or "",
                    ip_hash=ip_hash or "", product=product, sku=(sku or "")[:64], success=True,
                )
        except Exception:
            logger.exception("tryon_event_record_failed merchant=%s sku=%s", merchant.pk, sku)
    return merchant, result


# ---------- قياس كلفة الشات ----------

def record_llm_usage(merchant, input_tokens=None, output_tokens=None, today=None):
    """استدعاء LLM واحد للمتجر باليوم: calls += 1 والتوكنات لو معروفة. تحديثات F() ذرّية، وما بترمي أبداً."""
    try:
        today = today or timezone.localdate()
        in_tokens = input_tokens if isinstance(input_tokens, int) and input_tokens > 0 else 0
        out_tokens = output_tokens if isinstance(output_tokens, int) and output_tokens > 0 else 0

        def bump():
            return LlmUsage.objects.filter(merchant=merchant, day=today).update(
                calls=F("calls") + 1,
                input_tokens=F("input_tokens") + in_tokens,
                output_tokens=F("output_tokens") + out_tokens,
            )

        with transaction.atomic():
            if bump():
                return
            try:
                with transaction.atomic():
                    LlmUsage.objects.create(
                        merchant=merchant, day=today, calls=1,
                        input_tokens=in_tokens, output_tokens=out_tokens,
                    )
            except IntegrityError:
                bump()
    except Exception:
        logger.exception("llm_usage_record_failed merchant=%s", getattr(merchant, "pk", None))
