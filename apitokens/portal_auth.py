"""دخول بوابة التاجر: حساب (رقم هاتف + كلمة مرور) بيتسجّل إما برابط دعوة من الإدارة (بيربطه
بالمتجر مباشرة) أو بالتسجيل العام (/portal/register/): حساب معلّق بدون متجر لحد ما الإدارة تربطه.

رابط الدعوة (ApiToken.portal_key_hash) = مفتاح سرّي طويل، ما بينخزّن أبداً إلا sha256 تبعه،
صالح 7 أيام وبيُستهلك بأول تسجيل ناجح. بعدها التاجر بيدخل من /portal/login/ برقمه وكلمة مروره.

الجلسة بتحمل portal_account_id + بصمة HMAC على hash كلمة المرور: أي تغيير/إعادة تعيين لكلمة
المرور أو تعطيل الحساب أو التاجر بيطلّعه من الجلسة عالطلب الجاي.

التسجيل (الدعوة والعام) بينشئ الحساب غير موثّق وبيرسل OTP واتساب (portal_otp.py) وبيحوّل لـ
portal:verify. الحساب غير الموثّق بيدخل بكلمة المرور بس كل صفحات البوابة بتحوّله لصفحة التحقق.
portal_otp بيستورد هالملف، فاستيراده هون بيصير جوّا الدوال (تفادي استيراد دائري).
"""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from functools import lru_cache, wraps

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponseNotFound
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from .i18n import DEFAULT_LANGUAGE, LANGUAGES, t
from .models import ApiToken, MerchantAccount
from .services import lightotp

# نسخة سياسة الخصوصية اللي بيوافق عليها التاجر. غيّرها لما تتغيّر السياسة جوهرياً،
# فبنطلب الموافقة من كل التجار من جديد.
PORTAL_TERMS_VERSION = "2026-11-09"

INVITE_TTL = timedelta(days=7)

SESSION_ACCOUNT = "portal_account_id"
SESSION_FINGERPRINT = "portal_session_fp"
# حالة صفحة التحقق (رسالة الإرسال + مهلة الانتظار): بتنمسح مع الخروج وعند أي دخول جديد
SESSION_OTP_FLASH = "portal_otp_flash"
SESSION_OTP_WAIT_UNTIL = "portal_otp_wait_until"
_OTP_STATE_KEYS = (SESSION_OTP_FLASH, SESSION_OTP_WAIT_UNTIL)
_SESSION_KEYS = (SESSION_ACCOUNT, SESSION_FINGERPRINT, *_OTP_STATE_KEYS)

MAX_FAILED_LOGINS = 5
LOCK_DURATION = timedelta(minutes=15)
IP_MAX_LOGIN_POSTS = 20
IP_WINDOW_SECONDS = 600
# التسجيل العام: عدّاد منفصل وحدّ أبطأ (10 طلبات POST بالساعة لكل IP) + سقف للحسابات المعلّقة
IP_MAX_REGISTER_POSTS = 10
REGISTER_WINDOW_SECONDS = 3600
DEFAULT_MAX_PENDING_ACCOUNTS = 500
# محاولات صفحة التحقق (POST) لكل IP: عدّاد منفصل عن الدخول
IP_MAX_VERIFY_POSTS = 30
VERIFY_WINDOW_SECONDS = 600
HONEYPOT_FIELD = "website"

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
NAME_MAX_LENGTH = 120

_PHONE_STRIP = re.compile(r"[\s\-.()\u2010-\u2015\u2212\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_E164 = re.compile(r"\+[1-9][0-9]{7,14}")
# أرقام عربية-هندية (٠-٩) وفارسية (۰-۹) → ASCII
_DIGITS = {**{0x0660 + i: ord("0") + i for i in range(10)}, **{0x06F0 + i: ord("0") + i for i in range(10)}}


def hash_portal_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_portal_key(merchant):
    """بيولّد دعوة جديدة (القديمة بتبطل) ويرجّع النص الأصلي — بينعرض مرة وحدة بس."""
    key = secrets.token_urlsafe(32)
    merchant.portal_key_hash = hash_portal_key(key)
    merchant.portal_key_created_at = timezone.now()
    merchant.save(update_fields=["portal_key_hash", "portal_key_created_at"])
    return key


def revoke_portal_key(merchant):
    """بيلغي الدعوة وبيعطّل حساب التاجر (إن وجد) — جلساته بتموت عالطلب الجاي."""
    with transaction.atomic():
        merchant.portal_key_hash = ""
        merchant.portal_key_created_at = None
        merchant.save(update_fields=["portal_key_hash", "portal_key_created_at"])
        MerchantAccount.objects.filter(merchant=merchant).update(is_active=False)


def portal_status(merchant, now=None):
    """حالة البوابة لهالتاجر للستاف: الدعوة والحساب (بدون أي سرّ أو hash)."""
    now = now or timezone.now()
    invite, until = "none", None
    if merchant.portal_key_hash and merchant.portal_key_created_at:
        until = merchant.portal_key_created_at + INVITE_TTL
        invite = "pending" if until > now else "expired"
    elif merchant.portal_key_hash:
        invite = "expired"
    try:
        account = merchant.portal_account
    except MerchantAccount.DoesNotExist:
        account = None
    return {"invite": invite, "invite_until": until, "account": account}


# ---------- الهاتف وكلمة المرور ----------


def _phone_syntax(raw):
    """تنظيف الصيغة فقط (أرقام عربية، فواصل، 00 → +): بدون ما نحكم إنه الرقم صالح. للدخول."""
    if not isinstance(raw, str):
        return None
    value = _PHONE_STRIP.sub("", raw.translate(_DIGITS))
    if value.startswith("00"):
        value = "+" + value[2:]
    return value if _E164.fullmatch(value) else None


def normalize_phone(raw):
    """رقم دولي E.164 صالح فعلاً (مكتبة phonenumbers) بصيغة +<رمز الدولة><الرقم>، أو None.
    لازم رمز دولة صريح (+ أو 00): ما منخمّن من رقم محلي."""
    value = _phone_syntax(raw)
    if value is None:
        return None
    try:
        return lightotp.to_e164(value, getattr(settings, "LIGHTOTP_DEFAULT_REGION", "SA"))
    except ValueError:
        return None


def password_error_key(password):
    """مفتاح i18n لسبب ضعف كلمة المرور، أو None لو مقبولة. ما منرجّع رسائل جانغو الإنجليزية."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return "portal_register_err_pw_short"
    if len(password) > PASSWORD_MAX_LENGTH:
        return "portal_register_err_pw_long"
    try:
        validate_password(password)
    except ValidationError as exc:
        codes = {e.code for e in exc.error_list}
        if "password_too_short" in codes:
            return "portal_register_err_pw_short"
        if "password_too_common" in codes:
            return "portal_register_err_pw_common"
        return "portal_register_err_pw_weak"
    return None


@lru_cache(maxsize=1)
def _dummy_hash():
    # لرقم مو موجود: منعمل check_password على hash جاهز حتى ما يتغيّر وقت الرد
    return make_password("dummy-" + secrets.token_hex(16))


# ---------- الجلسة ----------


def session_fingerprint(account):
    """HMAC على hash كلمة المرور: لو تغيّرت الكلمة بتتغيّر البصمة وبتبطل الجلسات القديمة."""
    msg = f"{account.pk}:{account.password}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def login_session(request, account):
    request.session.cycle_key()
    for name in _OTP_STATE_KEYS:
        request.session.pop(name, None)
    request.session[SESSION_ACCOUNT] = account.pk
    request.session[SESSION_FINGERPRINT] = session_fingerprint(account)


def _harden(response):
    # same-origin مش no-referrer: مع no-referrer المتصفح بيبعت `Origin: null` بكل POST
    # وجانغو بيرفضه (CSRF) — فكانوا زر الموافقة والخروج واللغة ما بيشتغلوا. هيك بنضل
    # ما منسرّب الرابط لأي موقع تاني.
    response["Referrer-Policy"] = "same-origin"
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _clear_portal_session(request):
    # بس مفاتيح البوابة — جلسة الستاف (إن وجدت) بتضل متل ما هي
    for name in _SESSION_KEYS:
        request.session.pop(name, None)


def _session_account(request):
    """حساب صاحب الجلسة لو لسا صالح (حساب فعّال + بصمة مطابقة، والتاجر فعّال إن وُجد)، وإلا None.
    الحساب المعلّق (بدون متجر) صالح هون؛ portal_required هو اللي بيمنعه من صفحات المتجر."""
    account_id = request.session.get(SESSION_ACCOUNT)
    fingerprint = request.session.get(SESSION_FINGERPRINT)
    if isinstance(account_id, bool) or not isinstance(account_id, int):
        return None
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    account = MerchantAccount.objects.select_related("merchant").filter(pk=account_id).first()
    if account is None or not account.is_active:
        return None
    if account.merchant is not None and not account.merchant.is_active:
        return None
    if not hmac.compare_digest(session_fingerprint(account).encode(), fingerprint.encode()):
        return None
    return account


def _guard(view, verified, store):
    """verified: الحساب غير الموثّق (OTP) بيروح لصفحة التحقق. store: المعلّق (بدون متجر) لصفحة الانتظار."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        account = _session_account(request)
        if account is None:
            _clear_portal_session(request)
            return _harden(redirect("portal:login"))
        if verified and not account.phone_verified:
            return _harden(redirect("portal:verify"))
        if store and account.merchant is None:
            # حساب معلّق: ما في متجر لسا، ما بيشوف أي صفحة للمتجر
            return _harden(redirect("portal:pending"))
        request.portal_account = account
        if store:
            request.portal_merchant = account.merchant
        return _harden(view(request, *args, **kwargs))

    return wrapper


def portal_required(view):
    """جلسة صالحة + رقم موثّق + متجر. أي ناقص بيحوّل لصفحته."""
    return _guard(view, verified=True, store=True)


def portal_account_required(view):
    """أخف من portal_required: جلسة + رقم موثّق، بدون ما يطلب متجر."""
    return _guard(view, verified=True, store=False)


def portal_session_required(view):
    """جلسة وبصمة وتفعيل بس (صفحات التحقق والخروج): بدون شرط التوثيق ولا المتجر."""
    return _guard(view, verified=False, store=False)


def _landing(account):
    # وين بيروح الحساب بعد الدخول/التحقق: غير الموثّق للتحقق، المعلّق لصفحة الانتظار
    if not account.phone_verified:
        return redirect("portal:verify")
    return redirect("portal:pending" if account.merchant_id is None else "portal:home")


def terms_accepted_required(view):
    """بتنحط تحت portal_required: التاجر اللي ما وافق (أو وافق على نسخة قديمة) بيروح لصفحة الموافقة."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.portal_merchant.terms_accepted_version != PORTAL_TERMS_VERSION:
            return redirect("portal:accept")
        return view(request, *args, **kwargs)

    return wrapper


def accept_terms(merchant):
    merchant.terms_accepted_at = timezone.now()
    merchant.terms_accepted_version = PORTAL_TERMS_VERSION
    merchant.save(update_fields=["terms_accepted_at", "terms_accepted_version"])


def _lang(request):
    lang = request.session.get("lang", DEFAULT_LANGUAGE)
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


# ---------- التسجيل بالدعوة ----------


def _invite_merchant(key, lock=False):
    """التاجر صاحب الدعوة لو صالحة (فعّال + hash مطابق + أقل من 7 أيام)، وإلا None."""
    qs = ApiToken.objects.filter(is_active=True, portal_key_hash=hash_portal_key(key)).exclude(portal_key_hash="")
    if lock:
        qs = qs.select_for_update()
    merchant = qs.first()
    if merchant is None or merchant.portal_key_created_at is None:
        return None
    if timezone.now() - merchant.portal_key_created_at > INVITE_TTL:
        return None
    return merchant


def _not_found():
    # ردّ موحّد: ما منفرّق بين دعوة غلط/مستعملة/منتهية/تاجر معطّل
    return _harden(HttpResponseNotFound("Not found"))


def _shown_values(post):
    """القيم اللي بترجع للنموذج عند الخطأ: الاسم والرقم بس، ولا كلمة مرور أبداً."""
    return {"name": post.get("name", "").strip(), "phone": post.get("phone", "")[:40]}


def _validate_registration(post, lang):
    """تحقق مشترك بين التسجيل بالدعوة والعام. بيرجّع (الاسم، الرقم المنظّم، كلمة المرور، أخطاء).
    تعارض الرقم مع حساب موجود بيتقرّر تحت القفل بكل مسار (قواعد الاستيلاء)."""
    name = post.get("name", "").strip()
    phone = normalize_phone(post.get("phone", ""))
    password = post.get("password", "")
    confirm = post.get("password_confirm", "")
    errors = {}
    if not name:
        errors["name"] = t("portal_register_err_name", lang)
    elif len(name) > NAME_MAX_LENGTH:
        errors["name"] = t("portal_register_err_name_long", lang)
    if phone is None:
        errors["phone"] = t("portal_register_err_phone", lang)
    pw_key = password_error_key(password)
    if pw_key:
        errors["password"] = t(pw_key, lang)
    elif password != confirm:
        errors["password_confirm"] = t("portal_register_err_mismatch", lang)
    return name, phone, password, errors


def _render_register(request, merchant, values, errors, status=200):
    has_account = MerchantAccount.objects.filter(merchant=merchant).exists()
    return _harden(
        render(
            request,
            "apitokens/portal/register.html",
            {"merchant": merchant, "values": values, "errors": errors, "has_account": has_account},
            status=status,
        )
    )


@require_http_methods(["GET", "POST"])
def enter(request, key):
    merchant = _invite_merchant(key)
    if merchant is None:
        return _not_found()
    if request.method == "GET":
        return _render_register(request, merchant, {}, {})

    lang = _lang(request)
    name, phone, password, errors = _validate_registration(request.POST, lang)
    if errors:
        # ما منرجّع كلمات المرور أبداً للصفحة
        return _render_register(request, merchant, _shown_values(request.POST), errors, status=400)

    password_hash = make_password(password)
    phone_taken = False
    try:
        with transaction.atomic():
            # نعيد التحقق من الدعوة تحت قفل الصف: طلبين متوازيين ما بيستهلكوها مرتين
            locked = _invite_merchant(key, lock=True)
            if locked is None:
                return _not_found()
            account = MerchantAccount.objects.select_for_update().filter(merchant=locked).first()
            owner = MerchantAccount.objects.select_for_update().filter(phone=phone).first()
            if owner is not None and (account is None or owner.pk != account.pk):
                if owner.phone_verified or owner.merchant_id is not None:
                    # رقم موثّق، أو مربوط بمتجر تاني: ما منلمسه أبداً
                    phone_taken = True
                else:
                    owner.delete()  # حاجز رقم بدون متجر وغير موثّق (استيلاء): بنشيله
            if not phone_taken:
                existed = account is not None
                if not existed:
                    account = MerchantAccount(merchant=locked)
                    account.phone_verified = False
                else:
                    # إعادة تسجيل لحساب موجود = reset كامل (الجلسات القديمة بتموت لأن الـ hash تغيّر)
                    # بس التوثيق بيضل لو الرقم نفسه وموثّق؛ رقم جديد = OTP من جديد.
                    account.phone_verified = account.phone_verified and account.phone == phone
                if not account.phone_verified:
                    account.phone_verified_at = None
                account.name = name
                account.phone = phone
                account.password = password_hash
                account.is_active = True
                account.failed_logins = 0
                account.locked_until = None
                account.last_login_at = timezone.now()
                account.save()
                if existed:
                    _invalidate_otps(account)
                locked.portal_key_hash = ""
                locked.portal_key_created_at = None
                locked.save(update_fields=["portal_key_hash", "portal_key_created_at"])
    except IntegrityError:
        # سباق على رقم الهاتف الفريد
        phone_taken = True
    if phone_taken:
        errors = {"phone": t("portal_register_err_phone_taken", lang)}
        return _render_register(request, merchant, _shown_values(request.POST), errors, status=400)

    return _begin_verification(request, account, lang)


def _invalidate_otps(account):
    """بيبطّل كل أكواد الحساب اللي لسا ما انستخدمت (إعادة تسجيل = بداية من الصفر)."""
    account.otps.filter(consumed_at__isnull=True).update(consumed_at=timezone.now())


def _begin_verification(request, account, lang):
    """بعد التسجيل: دخول بالجلسة العادية + إرسال OTP (لو الرقم مو موثّق أصلاً) + صفحة التحقق.
    فشل الإرسال ما بيمنع الحساب: صفحة التحقق بتعرض الرسالة وزر إعادة الإرسال."""
    from . import portal_otp  # استيراد متأخر: portal_otp بيستورد هالملف

    login_session(request, account)
    if not account.phone_verified:
        outcome = portal_otp.send_code(account, lang, _client_ip(request))
        portal_otp.remember_outcome(request, outcome)
    return _harden(redirect("portal:verify"))


# ---------- تسجيل الدخول ----------

_OK, _BAD, _LOCKED = "ok", "bad", "locked"


def _client_ip(request):
    # الافتراضي REMOTE_ADDR بس (الهيدرز بتتزوّر). خلف nginx كل الزوار بيطلعوا 127.0.0.1 فالحد بيصير
    # واحد للكل — فعّل PORTAL_CLIENT_IP_HEADER (مثلاً HTTP_X_REAL_IP) ونفس nginx لازم يكتبه
    # بـ `proxy_set_header X-Real-IP $remote_addr;` (بيمحي أي قيمة من الزبون). آخر عنصر بس هو المضمون.
    header = getattr(settings, "PORTAL_CLIENT_IP_HEADER", "")
    if header:
        value = (request.META.get(header) or "").split(",")[-1].strip()
        if value:
            return value[:64]
    return request.META.get("REMOTE_ADDR") or "unknown"


def _count_ip(ip, scope, limit, window):
    """بيزيد عدّاد هالـ IP بالكاش (منفصل لكل scope) وبيرجّع True لو تجاوز الحد."""
    key = f"portal_{scope}_ip:" + ip
    cache.add(key, 0, window)
    try:
        count = cache.incr(key)
    except ValueError:  # انتهت صلاحية المفتاح بين add و incr
        cache.set(key, 1, window)
        count = 1
    return count > limit


def _throttled(request, scope, limit, window):
    """عدّاد POST لكل IP بالكاش، منفصل لكل scope (دخول / تسجيل / تحقق)."""
    return _count_ip(_client_ip(request), scope, limit, window)


def _ip_throttled(request):
    return _throttled(request, "login", IP_MAX_LOGIN_POSTS, IP_WINDOW_SECONDS)


def _register_throttled(request):
    return _throttled(request, "register", IP_MAX_REGISTER_POSTS, REGISTER_WINDOW_SECONDS)


def verify_throttled(request):
    return _throttled(request, "verify", IP_MAX_VERIFY_POSTS, VERIFY_WINDOW_SECONDS)


def _attempt_login(phone, password):
    """بيرجّع (حالة، حساب). كل المحاولة بقفل صف الحساب: التخمين المتوازي ما بيتجاوز العدّاد."""
    account_id = MerchantAccount.objects.filter(phone=phone).values_list("pk", flat=True).first() if phone else None
    if account_id is None:
        check_password(password, _dummy_hash())
        return _BAD, None
    with transaction.atomic():
        account = MerchantAccount.objects.select_for_update().filter(pk=account_id).first()
        if account is None:
            check_password(password, _dummy_hash())
            return _BAD, None
        now = timezone.now()
        if account.locked_until and account.locked_until > now:
            return _LOCKED, None
        ok = check_password(password, account.password)
        ok = ok and account.is_active
        # الحساب المعلّق (بدون متجر) بيدخل؛ اللي إله متجر لازم المتجر يكون فعّال
        ok = ok and (account.merchant_id is None or ApiToken.objects.filter(pk=account.merchant_id, is_active=True).exists())
        if ok:
            account.failed_logins = 0
            account.locked_until = None
            account.last_login_at = now
            account.save(update_fields=["failed_logins", "locked_until", "last_login_at"])
            return _OK, account
        failures = account.failed_logins + 1
        if failures >= MAX_FAILED_LOGINS:
            account.failed_logins = 0
            account.locked_until = now + LOCK_DURATION
        else:
            account.failed_logins = failures
        account.save(update_fields=["failed_logins", "locked_until"])
        return _BAD, None


def _render_login(request, phone="", error="", status=200):
    return _harden(render(request, "apitokens/portal/login.html", {"phone": phone, "form_error": error}, status=status))


@require_http_methods(["GET", "POST"])
def login(request):
    if request.method == "GET":
        account = _session_account(request)
        if account is not None:
            return _harden(_landing(account))
        return _render_login(request)

    lang = _lang(request)
    phone_raw = request.POST.get("phone", "")
    password = request.POST.get("password", "")
    shown_phone = phone_raw[:40]
    if _ip_throttled(request):
        return _render_login(request, shown_phone, t("portal_login_err_locked", lang), status=429)
    status, account = _attempt_login(normalize_phone(phone_raw) or _phone_syntax(phone_raw), password)
    if status == _LOCKED:
        return _render_login(request, shown_phone, t("portal_login_err_locked", lang), status=429)
    if status != _OK:
        return _render_login(request, shown_phone, t("portal_login_err_generic", lang), status=400)
    login_session(request, account)
    return _harden(_landing(account))


@require_POST
def logout(request):
    _clear_portal_session(request)
    return _harden(redirect("portal:login"))


# ---------- التسجيل العام (حساب معلّق لحد ما الإدارة تربطه بمتجر) ----------


def _max_pending():
    return getattr(settings, "PORTAL_MAX_PENDING_ACCOUNTS", DEFAULT_MAX_PENDING_ACCOUNTS)


def _render_signup(request, values=None, errors=None, form_error="", status=200):
    ctx = {"values": values or {}, "errors": errors or {}, "form_error": form_error, "honeypot": HONEYPOT_FIELD}
    return _harden(render(request, "apitokens/portal/signup.html", ctx, status=status))


@require_http_methods(["GET", "POST"])
def register(request):
    if request.method == "GET":
        account = _session_account(request)
        if account is not None:
            return _harden(_landing(account))
        return _render_signup(request)

    lang = _lang(request)
    if _register_throttled(request):
        return _render_signup(request, form_error=t("portal_login_err_locked", lang), status=429)
    account = _session_account(request)
    if account is not None:
        return _harden(_landing(account))
    # honeypot: الروبوت بيعبّي الحقل المخفي → نفس ردّ النجاح بس بدون ما ننشئ شي
    if request.POST.get(HONEYPOT_FIELD, "").strip():
        return _harden(redirect("portal:verify"))
    if MerchantAccount.objects.filter(merchant__isnull=True).count() >= _max_pending():
        return _render_signup(request, form_error=t("portal_signup_err_closed", lang), status=503)

    name, phone, password, errors = _validate_registration(request.POST, lang)
    if errors:
        return _render_signup(request, _shown_values(request.POST), errors, status=400)

    password_hash = make_password(password)
    taken = False
    try:
        with transaction.atomic():
            now = timezone.now()
            account = MerchantAccount.objects.select_for_update().filter(phone=phone).first()
            if account is None:
                account = MerchantAccount.objects.create(
                    merchant=None, name=name, phone=phone, password=password_hash,
                    is_active=True, phone_verified=False, last_login_at=now,
                )
            elif account.phone_verified or account.merchant_id is not None or not account.is_active:
                taken = True  # موثّق أو مربوط بمتجر: ما منلمسه
            else:
                # حساب غير موثّق بدون متجر (حاجز رقم محتمل): بنكتب فوقه بدل ما نقفل الرقم على صاحبه الحقيقي
                account.name = name
                account.password = password_hash
                account.failed_logins = 0
                account.locked_until = None
                account.last_login_at = now
                account.created_at = now
                account.save()
                _invalidate_otps(account)
    except IntegrityError:
        # سباق على رقم الهاتف الفريد
        taken = True
    if taken:
        errors = {"phone": t("portal_signup_err_phone_registered", lang)}
        return _render_signup(request, _shown_values(request.POST), errors, status=400)

    return _begin_verification(request, account, lang)
