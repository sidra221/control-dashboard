"""كود التحقق (OTP) واتساب لحسابات بوابة التاجر: توليد، إرسال عبر LightOTP، وتحقق.

- الكود 6 أرقام (secrets)، صالح 10 دقايق، 5 محاولات غلط بالكتير، وما بينخزّن إلا HMAC-SHA256
  (مفتاحه SECRET_KEY) على `<pk>:<code>`. كود جديد ناجح الإرسال بيبطّل القديمة.
- التهدئة: نفس جدول LightOTP لكل رقم (30ث، 60، 120، 240، 480... حد أقصى 6 ساعات) محسوبة من صفوفنا
  اللي إلها message_id بآخر 6 ساعات. بالتهدئة ما منكلّم الـ API أبداً. الإرسال الفاشل ما بينحسب.
- حماية الرصيد: عدّاد إرسال لكل IP (كاش) وسقف عام PORTAL_OTP_MAX_SENDS_PER_HOUR.
- idempotencyKey = event_key("portal-otp:<pk>"): إعادة محاولة واحدة فورية بنفس المفتاح بس لانقطاع شبكة/5xx.
- PORTAL_OTP_BACKEND=console (تطوير محلي فقط، بيتطلب DEBUG=True): بيسجّل الكود بالـ log وما بيرسل شي.
- كود الاشتراك (purpose=subscribe): نفس الإرسال والحماية والتهدئة (مشتركة مع كود التوثيق لنفس الرقم)،
  بس 8 حروف/أرقام وصالح 30 دقيقة. إدخاله الصح بينشئ متجر (ApiToken) برصيد PORTAL_TRIAL_IMAGE_QUOTA
  وبيربطه بالحساب — مرة وحدة: الحساب المربوط ما بياخد كود تاني.
- أخطاء الإعداد/الرصيد/الرموز الدائمة: المستخدم بيشوف رسالة عامة، والتفاصيل بالـ log (الرقم مخفي والكود أبداً).

استيراد هالملف من portal_auth بيصير متأخر (جوّا دالة) لأنه بيستورد portal_auth.
"""
import hashlib
import hmac
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

import phonenumbers
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from . import portal_auth
from .models import ApiToken, MerchantAccount, PhoneOtp
from .services import lightotp

logger = logging.getLogger(__name__)

CODE_LENGTH = 6
OTP_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5

# تهدئة LightOTP لكل رقم: 30 * 2^(n-1) ثانية بعد آخر إرسال ناجح، حد أقصى 6 ساعات، وتصفّر بعد 6 ساعات
COOLDOWN_BASE_SECONDS = 30
COOLDOWN_CAP_SECONDS = 6 * 3600
COOLDOWN_WINDOW = timedelta(hours=6)
# لو LightOTP نفسه ردّ تهدئة (400) ما منعرف المدة بالضبط: تلميح آمن
LIGHTOTP_WAIT_HINT = 60

# كود الاشتراك: حروف وأرقام بدون المتشابهة (0/O، 1/I/L) — قالب LightOTP بيقبل 8 بالأكتر
SUBSCRIBE_CODE_LENGTH = 8
SUBSCRIBE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SUBSCRIBE_TTL = timedelta(minutes=30)
STORE_NAME_MAX = 100

IP_MAX_SENDS = 15
IP_SEND_WINDOW_SECONDS = 3600

# نتائج send_code
OK, WAIT, UNAVAILABLE, THROTTLED, VERIFIED = "ok", "wait", "unavailable", "throttled", "verified"
# نتائج verify_code
V_OK, V_WRONG, V_EXPIRED, V_EXHAUSTED, V_NONE = "ok", "wrong", "expired", "exhausted", "none"
# الحساب صار عنده متجر (send_subscription_code / activate_subscription)
LINKED = "linked"
# سقف المتاجر التجريبية اليومي ممتلئ (activate_subscription)
V_CLOSED = "closed"

_CODE_STRIP = re.compile(r"[\s​-‏‪-‮⁦-⁩]")
_CODE_RE = re.compile(r"[0-9]{%d}" % CODE_LENGTH)
_SUBSCRIBE_RE = re.compile(r"[%s]{%d}" % (SUBSCRIBE_ALPHABET, SUBSCRIBE_CODE_LENGTH))


@dataclass(frozen=True)
class SendOutcome:
    status: str
    wait_seconds: int = 0  # للـ OK = التهدئة الجاية (حتى زر الإعادة يبدأ معدود)


# ---------- الكود ----------


def generate_code():
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def generate_subscription_code():
    return "".join(secrets.choice(SUBSCRIBE_ALPHABET) for _ in range(SUBSCRIBE_CODE_LENGTH))


def normalize_subscription_code(raw):
    """حروف كبيرة بدون مسافات أو شرطات. بيرجّع None لو مو 8 من الأبجدية."""
    if not isinstance(raw, str):
        return None
    value = _CODE_STRIP.sub("", raw.translate(portal_auth._DIGITS)).replace("-", "").upper()
    return value if _SUBSCRIBE_RE.fullmatch(value) else None


def clean_store_name(raw):
    """اسم المتجر: سطر واحد، مسافات مضغوطة، 2..100 حرف. بيرجّع "" لو مو صالح."""
    value = " ".join(str(raw or "").split())
    return value if 2 <= len(value) <= STORE_NAME_MAX else ""


def hash_code(otp_pk, code):
    msg = f"{otp_pk}:{code}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def normalize_code(raw):
    """أرقام عربية/فارسية → ASCII، بدون مسافات. بيرجّع None لو مو 6 أرقام بالضبط."""
    if not isinstance(raw, str):
        return None
    value = _CODE_STRIP.sub("", raw.translate(portal_auth._DIGITS))
    return value if _CODE_RE.fullmatch(value) else None


# ---------- الرقم ----------


def _split_phone(phone):
    digits = phone.lstrip("+")
    try:
        cc = str(phonenumbers.parse(phone, None).country_code)
    except phonenumbers.NumberParseException:
        cc = digits[:3]
    return cc, digits[len(cc):]


def mask_phone(phone, bullet="•"):
    """+966 •• ••• 4567: رمز الدولة وآخر 4 أرقام بس."""
    cc, national = _split_phone(phone)
    keep = 4 if len(national) >= 7 else 2
    hidden = len(national) - keep
    first = hidden // 2
    groups = [bullet * first, bullet * (hidden - first), national[hidden:]]
    return f"+{cc} " + " ".join(g for g in groups if g)


def _log_phone(phone):
    # للـ log: بدون مسافات وبنجمة ASCII
    cc, national = _split_phone(phone)
    keep = 4 if len(national) >= 7 else 2
    return f"+{cc}{'*' * (len(national) - keep)}{national[-keep:]}"


def _scrub(text, phone):
    """بنشيل الرقم الكامل من أي نص جاي من الـ API قبل ما نسجّله أو نخزّنه."""
    text = str(text)
    return text.replace(phone, _log_phone(phone)).replace(phone.lstrip("+"), _log_phone(phone))


# ---------- التهدئة ----------


def cooldown_seconds(sends):
    """المدة بعد آخر إرسال ناجح لما يكون في `sends` إرسالات ناجحة بنافذة الـ 6 ساعات."""
    if sends <= 0:
        return 0
    return min(COOLDOWN_BASE_SECONDS * 2 ** min(sends - 1, 20), COOLDOWN_CAP_SECONDS)


def cooldown_remaining(phone, now=None):
    """الثواني الباقية قبل ما نقدر نرسل لهالرقم (0 = متاح). من صفوفنا اللي إلها message_id."""
    now = now or timezone.now()
    agg = (
        PhoneOtp.objects.filter(phone=phone, created_at__gte=now - COOLDOWN_WINDOW)
        .exclude(message_id="")
        .aggregate(n=Count("id"), last=Max("created_at"))
    )
    if not agg["n"]:
        return 0
    elapsed = (now - agg["last"]).total_seconds()
    return max(0, math.ceil(cooldown_seconds(agg["n"]) - elapsed))


def active_otp(account, now=None, purpose=PhoneOtp.PURPOSE_VERIFY):
    """آخر كود لسا صالح (ما انستخدم، ما انتهى، ما استنفد محاولاته) أو None."""
    now = now or timezone.now()
    return (
        PhoneOtp.objects.filter(
            account=account, phone=account.phone, purpose=purpose, consumed_at__isnull=True,
            expires_at__gt=now, attempts__lt=MAX_ATTEMPTS,
        )
        .order_by("-created_at", "-pk")
        .first()
    )


# ---------- الإرسال ----------


def _deliver(otp, phone, code, lang):
    """بيرسل (أو بيسجّل بالـ console). بنعيد مرة وحدة وبنفس المفتاح بس لانقطاع شبكة/5xx (مو 429/تهدئة/دائم)."""
    if settings.PORTAL_OTP_BACKEND == "console":
        logger.info("portal OTP (console backend) to %s: %s", _log_phone(phone), code)
        return lightotp.SendResult(f"console-{otp.pk}", "Sent")
    event = f"portal-otp:{otp.pk}"  # pk فريد لكل كود (توثيق أو اشتراك)
    for attempt in (1, 2):
        try:
            return lightotp.send_otp_message(phone=phone, code=code, event=event, lang=lang)
        except lightotp.LightOtpError as exc:
            if attempt == 1 and (exc.code == "NetworkError" or exc.status >= 500):
                continue
            raise


def _classify(exc, phone):
    """(نتيجة للمستخدم، سبب مختصر للتخزين/اللوغ، مستوى اللوغ)."""
    if isinstance(exc, lightotp.LightOtpNotConfigured):
        return UNAVAILABLE, "NotConfigured", logging.ERROR
    if isinstance(exc, lightotp.LightOtpError):
        code = _scrub(exc.code, phone)[:150]
        if exc.status == 400 and exc.code not in lightotp.PERMANENT and exc.code != "InsufficientBalance":
            return WAIT, f"HTTP 400: {code}", logging.WARNING  # غالباً تهدئة LightOTP لهالرقم
        return UNAVAILABLE, f"HTTP {exc.status}: {code}", logging.ERROR
    if isinstance(exc, ValueError):
        return UNAVAILABLE, "InvalidNumber", logging.ERROR
    return UNAVAILABLE, f"Unexpected {type(exc).__name__}", logging.ERROR


def _unavailable_now(reason):
    logger.error("portal OTP unavailable: %s", reason)
    return SendOutcome(UNAVAILABLE)


def send_code(account, lang, request_ip):
    """بيولّد كود توثيق جديد وبيرسله لواتساب الحساب. بيرجّع SendOutcome (ok / wait / unavailable / throttled /
    verified). كل العملية بقفل صف الحساب: ضغطتين متوازيتين ما بيرسلوا مرتين."""
    return _send(
        account, lang, request_ip,
        purpose=PhoneOtp.PURPOSE_VERIFY, store_name="", ttl=OTP_TTL, make_code=generate_code,
        done=lambda locked: VERIFIED if locked.phone_verified else None,
    )


def send_subscription_code(account, store_name, lang, request_ip):
    """كود الاشتراك لحساب موثّق بلا متجر. نفس نتائج send_code، و LINKED لو الحساب صار عنده متجر."""

    def done(locked):
        if locked.merchant_id is not None:
            return LINKED
        if not locked.phone_verified:
            return UNAVAILABLE  # ما بيصير عبر الواجهة (الصفحة بتطلب التوثيق أول)
        if trial_cap_reached():
            logger.error("portal subscription closed: daily trial store cap reached")
            return UNAVAILABLE  # ما منصرف رسالة واتساب على كود ما رح ينفع
        return None

    return _send(
        account, lang, request_ip,
        purpose=PhoneOtp.PURPOSE_SUBSCRIBE, store_name=store_name, ttl=SUBSCRIBE_TTL,
        make_code=generate_subscription_code, done=done,
    )


def _send(account, lang, request_ip, *, purpose, store_name, ttl, make_code, done):
    backend = settings.PORTAL_OTP_BACKEND
    if backend == "console" and not settings.DEBUG:
        return _unavailable_now("console backend refused (DEBUG is off)")
    if backend not in ("lightotp", "console"):
        return _unavailable_now(f"unknown PORTAL_OTP_BACKEND {backend!r}")

    now = timezone.now()
    with transaction.atomic():
        locked = MerchantAccount.objects.select_for_update().filter(pk=account.pk).first()
        if locked is None:
            return SendOutcome(UNAVAILABLE)
        finished = done(locked)
        if finished:
            return SendOutcome(finished)
        phone = locked.phone

        remaining = cooldown_remaining(phone, now)
        if remaining:
            return SendOutcome(WAIT, remaining)  # بدون أي نداء للـ API
        cap = settings.PORTAL_OTP_MAX_SENDS_PER_HOUR
        sent_last_hour = (
            PhoneOtp.objects.filter(created_at__gte=now - timedelta(hours=1)).exclude(message_id="").count()
        )
        if sent_last_hour >= cap:
            return _unavailable_now(f"global hourly cap reached ({cap})")
        if portal_auth._count_ip(request_ip, "otpsend", IP_MAX_SENDS, IP_SEND_WINDOW_SECONDS):
            return SendOutcome(THROTTLED)

        code = make_code()
        otp = PhoneOtp.objects.create(
            account=locked, phone=phone, purpose=purpose, store_name=store_name, expires_at=now + ttl,
        )
        otp.code_hash = hash_code(otp.pk, code)
        otp.save(update_fields=["code_hash"])
        try:
            result = _deliver(otp, phone, code, lang)
        except Exception as exc:  # كل شي: الخطأ ما لازم يفلت للمستخدم ولا يترك كود قابل للاستخدام
            status, reason, level = _classify(exc, phone)
            logger.log(level, "portal OTP send failed: otp=%s phone=%s reason=%s", otp.pk, _log_phone(phone), reason)
            # كود ميت: ما بيدخل بالتهدئة (بدون message_id) وما بيقدر المستخدم يحرق محاولات عليه
            otp.consumed_at = timezone.now()
            otp.send_error = reason[:200]
            otp.save(update_fields=["consumed_at", "send_error"])
            return SendOutcome(status, LIGHTOTP_WAIT_HINT if status == WAIT else 0)

        otp.message_id = str(result.message_id)[:64]
        otp.message_status = (result.status or "Pending")[:20]
        otp.save(update_fields=["message_id", "message_status"])
        # الكود الجديد وصل لـ LightOTP: القديمة (من نفس النوع) بتبطل (لو فشل الإرسال القديم بيضل صالح)
        PhoneOtp.objects.filter(account=locked, purpose=purpose, consumed_at__isnull=True).exclude(pk=otp.pk).update(
            consumed_at=timezone.now()
        )
        return SendOutcome(OK, cooldown_remaining(phone))


# ---------- التحقق ----------


def verify_code(account, raw_code):
    """بيتحقق من الكود. المحاولة بتنعدّ تحت select_for_update (التخمين المتوازي ما بيتجاوز الحد)."""
    code = normalize_code(raw_code)
    now = timezone.now()
    with transaction.atomic():
        locked = MerchantAccount.objects.select_for_update().filter(pk=account.pk).first()
        if locked is None:
            return V_NONE
        otp = _latest_open(locked, PhoneOtp.PURPOSE_VERIFY)
        if otp is None:
            return V_NONE
        if otp.expires_at <= now:
            return V_EXPIRED
        if otp.attempts >= MAX_ATTEMPTS:
            return V_EXHAUSTED
        if code is None:
            return V_WRONG  # صيغة غلط: ما فيها تخمين، ما بتنعدّ
        otp.attempts += 1
        if hmac.compare_digest(otp.code_hash, hash_code(otp.pk, code)):
            otp.consumed_at = now
            otp.save(update_fields=["attempts", "consumed_at"])
            PhoneOtp.objects.filter(
                account=locked, purpose=PhoneOtp.PURPOSE_VERIFY, consumed_at__isnull=True
            ).update(consumed_at=now)
            locked.phone_verified = True
            locked.phone_verified_at = now
            locked.save(update_fields=["phone_verified", "phone_verified_at"])
            return V_OK
        otp.save(update_fields=["attempts"])
        return V_EXHAUSTED if otp.attempts >= MAX_ATTEMPTS else V_WRONG


def _latest_open(locked, purpose):
    return (
        PhoneOtp.objects.select_for_update()
        .filter(account=locked, phone=locked.phone, purpose=purpose, consumed_at__isnull=True)
        .order_by("-created_at", "-pk")
        .first()
    )


def trial_cap_reached(now=None):
    """المتاجر اللي انعملت بكود الاشتراك (بلا منشئ ستاف ومربوطة بحساب) بآخر 24 ساعة وصلت السقف؟"""
    now = now or timezone.now()
    recent = ApiToken.objects.filter(
        created_by__isnull=True, portal_account__isnull=False, created_at__gte=now - timedelta(hours=24)
    ).count()
    return recent >= settings.PORTAL_MAX_TRIAL_STORES_PER_DAY


def activate_subscription(account, raw_code):
    """بيتحقق من كود الاشتراك، ولو صح بينشئ متجر برصيد التجربة وبيربطه بالحساب (بقفل الحساب: ضغطتين
    ما بيعملوا متجرين). بيرجّع (نتيجة، المتجر أو None). النتائج متل verify_code، و LINKED لو كان مربوط."""
    code = normalize_subscription_code(raw_code)
    now = timezone.now()
    with transaction.atomic():
        locked = MerchantAccount.objects.select_for_update().filter(pk=account.pk).first()
        if locked is None:
            return V_NONE, None
        if locked.merchant_id is not None:
            return LINKED, locked.merchant
        otp = _latest_open(locked, PhoneOtp.PURPOSE_SUBSCRIBE)
        if otp is None:
            return V_NONE, None
        if otp.expires_at <= now:
            return V_EXPIRED, None
        if otp.attempts >= MAX_ATTEMPTS:
            return V_EXHAUSTED, None
        if code is None:
            return V_WRONG, None
        otp.attempts += 1
        if not hmac.compare_digest(otp.code_hash, hash_code(otp.pk, code)):
            otp.save(update_fields=["attempts"])
            return (V_EXHAUSTED if otp.attempts >= MAX_ATTEMPTS else V_WRONG), None
        if trial_cap_reached(now):
            otp.save(update_fields=["attempts"])  # الكود بيضل صالح لما يرجع في مكان
            return V_CLOSED, None
        otp.consumed_at = now
        otp.save(update_fields=["attempts", "consumed_at"])
        PhoneOtp.objects.filter(
            account=locked, purpose=PhoneOtp.PURPOSE_SUBSCRIBE, consumed_at__isnull=True
        ).update(consumed_at=now)
        merchant = ApiToken.objects.create(
            label=otp.store_name or locked.name,
            image_quota=settings.PORTAL_TRIAL_IMAGE_QUOTA,
            period=ApiToken.Period.MONTHLY,
        )
        locked.merchant = merchant
        locked.save(update_fields=["merchant"])
        logger.info("portal subscription activated: account=%s merchant=%s", locked.pk, merchant.pk)
        return V_OK, merchant


def last_store_name(account):
    """آخر اسم متجر طلب عليه الحساب كود (لتعبئة النموذج وإعادة الإرسال)."""
    return (
        PhoneOtp.objects.filter(account=account, purpose=PhoneOtp.PURPOSE_SUBSCRIBE)
        .exclude(store_name="").order_by("-created_at", "-pk").values_list("store_name", flat=True).first()
        or ""
    )


# ---------- حالة صفحة التحقق (بالجلسة) ----------


def remember_outcome(request, outcome):
    """بنحفظ نتيجة الإرسال للصفحة الجاية (PRG). مهلة الانتظار بتضل بالجلسة لحد ما تخلص."""
    request.session[portal_auth.SESSION_OTP_FLASH] = outcome.status
    if outcome.status == WAIT:
        request.session[portal_auth.SESSION_OTP_WAIT_UNTIL] = time.time() + outcome.wait_seconds


def pop_flash(request):
    return request.session.pop(portal_auth.SESSION_OTP_FLASH, None)


def page_wait_seconds(request, account):
    """الثواني الباقية لزر إعادة الإرسال: الأكبر بين تهدئتنا (قاعدة البيانات) وتلميح LightOTP (الجلسة)."""
    until = request.session.get(portal_auth.SESSION_OTP_WAIT_UNTIL)
    session_wait = 0
    if isinstance(until, (int, float)) and not isinstance(until, bool):
        session_wait = max(0, math.ceil(until - time.time()))
    return max(cooldown_remaining(account.phone), session_wait)
