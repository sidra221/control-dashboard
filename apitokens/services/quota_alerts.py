"""تنبيه التاجر على واتساب (قالب Utility) لما يوصل استهلاك رصيده 80% ولما يخلص (100%).

- بيتنادى بعد كل تجربة ناجحة (ومن أمر send_quota_alerts كشبكة أمان): maybe_send_quota_alerts(merchant)
  ما بيرمي استثناء أبداً — فشل التنبيه ما لازم يأثر على تجربة الزبون.
- تنبيه واحد لكل عتبة بالفترة: الحقل quota_alert_{80,100}_period بيحمل period_start اللي انرسل عنده.
  "الحجز" UPDATE شرطي (ما بيمسّ إلا الصف اللي حقله مو period_start الحالي)، فبس النداء اللي حدّث صف
  واحد بيرسل: نداءان متزامنان ما بيرسلوا مرتين.
- إذا الإرسال فشل: بنرجّع الحجز (النداء الجاي بيعيد المحاولة) بس بحد أقصى محاولة وحدة بالساعة لكل
  متجر+عتبة حتى ما نطرق مزوّد واقع.
- لو الاستهلاك قفز مباشرة للـ100% بنرسل تنبيه الـ100 بس، وبنعلّم العتبتين.
- ما بنكتب رقم الهاتف بأي لوغ.
"""
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q

from apitokens.models import ApiToken, MerchantAccount
from apitokens.services import lightotp
from apitokens.services.quota import quota_status

logger = logging.getLogger(__name__)

THRESHOLDS = (80, 100)
# بعد فشل الإرسال: ما منعيد المحاولة لنفس المتجر+العتبة قبل هالمدة (ثواني).
RETRY_COOLDOWN_SECONDS = 3600
LABEL_MAX = 60
_WHITESPACE = re.compile(r"\s+")


def _field(threshold):
    return f"quota_alert_{threshold}_period"


def _cooldown_key(merchant_pk, threshold):
    return f"quota-alert-retry:{merchant_pk}:{threshold}"


def _clean_label(label):
    # متغيّرات قوالب واتساب ما بتقبل أسطر جديدة ولا مسافات متتالية كتيرة
    return _WHITESPACE.sub(" ", str(label or "")).strip()[:LABEL_MAX] or "-"


def _reached(status, threshold):
    quota = status["quota"]
    if quota <= 0:
        return False
    if threshold >= 100:
        return status["used"] >= quota
    return status["used"] * 100 >= quota * threshold


def _fields_for(threshold):
    """الحقول اللي بنعلّمها عند إرسال هالعتبة: الـ100 بيعلّم الـ80 كمان (ما منرسل الاتنين)."""
    return [_field(t) for t in THRESHOLDS if t <= threshold]


def _claim(merchant, threshold, period_start):
    """بيحجز العتبة. True فقط إذا نحن اللي حدّثنا الصف (نداء متزامن تاني بيرجع 0)."""
    fields = _fields_for(threshold)
    target = _field(threshold)
    updated = ApiToken.objects.filter(pk=merchant.pk, period_start=period_start).filter(
        Q(**{target + "__isnull": True}) | ~Q(**{target: period_start})
    ).update(**{f: period_start for f in fields})
    return updated == 1


def _revert(merchant_pk, threshold, period_start, previous):
    """بيرجّع قيم الحجز السابقة (بس إذا لسا هي اللي حجزناها)."""
    for f in _fields_for(threshold):
        ApiToken.objects.filter(pk=merchant_pk, **{f: period_start}).update(**{f: previous[f]})


def _verified_phone(merchant):
    account = MerchantAccount.objects.filter(merchant=merchant, is_active=True, phone_verified=True).first()
    return account.phone if account else None


def _cooldown_active(merchant_pk, threshold):
    try:
        return bool(cache.get(_cooldown_key(merchant_pk, threshold)))
    except Exception:
        logger.warning("quota alert: cache read failed", exc_info=True)
        return False


def _start_cooldown(merchant_pk, threshold):
    try:
        cache.set(_cooldown_key(merchant_pk, threshold), 1, RETRY_COOLDOWN_SECONDS)
    except Exception:
        logger.warning("quota alert: cache write failed", exc_info=True)


def _alert_for(merchant, status):
    """العتبة اللي لازم ننبّه عليها هلق (100 تغلب 80) أو None إذا ما في أو انرسلت."""
    ps = status["period_start"]
    for threshold in sorted(THRESHOLDS, reverse=True):
        if _reached(status, threshold):
            if getattr(merchant, _field(threshold)) == ps:
                return None
            return threshold
    return None


def _send(merchant_pk):
    merchant = ApiToken.objects.filter(pk=merchant_pk, is_active=True).first()
    if merchant is None:
        return False
    api_key = getattr(settings, "LIGHTOTP_API_KEY", "")
    template_id = getattr(settings, "LIGHTOTP_QUOTA_ALERT_TEMPLATE_ID", "")
    if not api_key or not template_id:
        logger.debug("quota alert skipped: LightOTP key or alert template not configured")
        return False
    status = quota_status(merchant)
    threshold = _alert_for(merchant, status)
    if threshold is None:
        return False
    phone = _verified_phone(merchant)
    if phone is None:
        logger.debug("quota alert skipped: no verified portal account (merchant %s)", merchant.pk)
        return False
    if _cooldown_active(merchant.pk, threshold):
        logger.debug("quota alert skipped: retry cooldown (merchant %s, %s%%)", merchant.pk, threshold)
        return False

    period_start = status["period_start"]
    previous = {_field(t): getattr(merchant, _field(t)) for t in THRESHOLDS}
    if not _claim(merchant, threshold, period_start):
        return False  # نداء تاني سبقنا

    try:
        lightotp.send_utility(
            api_key=api_key,
            template_id=template_id,
            phone=phone,
            default_region=getattr(settings, "LIGHTOTP_DEFAULT_REGION", "SA"),
            body_params=[
                _clean_label(merchant.label),
                str(100 if threshold >= 100 else status["percent_used"]),
                str(status["remaining"]),
            ],
            idempotency_key=lightotp.event_key(f"quota-alert:{merchant.pk}:{threshold}:{period_start.isoformat()}"),
            lang="ar",
        )
    except Exception as exc:
        # LightOtpError / ValueError (رقم غير صالح) / أي شي تاني: رجّع الحجز وهدّي المحاولات
        code = getattr(exc, "code", type(exc).__name__)
        logger.warning("quota alert send failed (merchant %s, %s%%): %s", merchant.pk, threshold, str(code)[:80])
        try:
            _revert(merchant.pk, threshold, period_start, previous)
        finally:
            _start_cooldown(merchant.pk, threshold)
        return False
    logger.info("quota alert sent (merchant %s, %s%%)", merchant.pk, threshold)
    return threshold


def maybe_send_quota_alerts(merchant):
    """بيرجّع قائمة العتبات اللي انرسل تنبيهها بهالنداء ([] غالباً). ما بيرمي أبداً."""
    try:
        sent = _send(merchant.pk)
        return [sent] if sent else []
    except Exception:
        try:
            logger.exception("quota alert check failed")
        except Exception:
            pass
        return []
