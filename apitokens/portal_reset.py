"""نسيت كلمة المرور (بوابة التاجر): كود واتساب (PhoneOtp purpose=reset) بدل رابط دعوة جديد.

1. /portal/password/forgot/: التاجر بيكتب رقمه (نفس تنظيف الدخول). لو في حساب فعّال وموثّق بهالرقم
   بنرسله كود إعادة تعيين. الرد دايماً نفسه (تحويل لصفحة الإدخال) سواء الحساب موجود أو لا، ونتيجة
   الإرسال (تهدئة/فشل) ما بتنعرض أبداً: ما في كشف للحسابات. وقت الرد بينمدّ لحد أدنى
   (PORTAL_RESET_MIN_SECONDS) حتى ما يفرق وقت الرقم الموجود (نداء LightOTP) عن غير الموجود.
2. /portal/password/reset/: الكود + كلمة مرور جديدة (نفس قواعد التسجيل). الكود الصح بيغيّر كلمة المرور،
   بيصفّر قفل الدخول، بيبطّل كل الجلسات التانية (البصمة مبنية على hash كلمة المرور) وبيدخّل التاجر.

الجلسة بتحمل الرقم المكتوب ووقت الطلب دايماً، ورقم الحساب بس لو مؤهّل. للرقم غير المؤهّل بنحاكي نفس
ردود الكود (غلط، ثم استنفاد بعد 5 محاولات، ثم انتهاء بعد 10 دقايق) حتى الصفحة تتصرف بنفس الشكل.
"""
import logging
import time

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from . import portal_otp
from .i18n import t
from .models import MerchantAccount
from .portal_auth import (
    SESSION_RESET_ACCOUNT, SESSION_RESET_PHONE, SESSION_RESET_STARTED, SESSION_RESET_TRIES, _RESET_STATE_KEYS,
    _client_ip, _harden, _landing, _lang, _phone_syntax, _session_account, forgot_throttled, login_session,
    normalize_phone, password_error_key, reset_throttled,
)

logger = logging.getLogger(__name__)

DEFAULT_MIN_SECONDS = 1.5

_RESET_ERRORS = {
    portal_otp.V_WRONG: "portal_reset_err_wrong",
    portal_otp.V_EXPIRED: "portal_reset_err_expired",
    portal_otp.V_NONE: "portal_reset_err_expired",  # ما في كود فعّال = نفس رسالة المنتهي
    portal_otp.V_EXHAUSTED: "portal_reset_err_exhausted",
}


def _pad(started):
    """بنمدّ وقت الرد لحد أدنى ثابت (الإرسال الحقيقي بياخد وقت، والرقم غير الموجود لا)."""
    floor = getattr(settings, "PORTAL_RESET_MIN_SECONDS", DEFAULT_MIN_SECONDS)
    left = floor - (time.monotonic() - started)
    if left > 0:
        time.sleep(left)


def _render_forgot(request, phone="", phone_error="", form_error="", status=200):
    ctx = {"phone": phone, "phone_error": phone_error, "form_error": form_error}
    return _harden(render(request, "apitokens/portal/password_forgot.html", ctx, status=status))


@require_http_methods(["GET", "POST"])
def forgot(request):
    if request.method == "GET":
        account = _session_account(request)
        if account is not None:
            return _harden(_landing(account))
        return _render_forgot(request, phone=request.session.get(SESSION_RESET_PHONE, ""))

    started = time.monotonic()
    lang = _lang(request)
    phone_raw = request.POST.get("phone", "")
    if forgot_throttled(request):
        return _render_forgot(request, phone_raw[:40], form_error=t("portal_reset_err_throttled", lang), status=429)
    phone = normalize_phone(phone_raw) or _phone_syntax(phone_raw)
    if phone is None:
        # صيغة الرقم بس (ما بتكشف شي عن الحسابات)
        return _render_forgot(request, phone_raw[:40], phone_error=t("portal_reset_err_phone", lang), status=400)

    for name in _RESET_STATE_KEYS:
        request.session.pop(name, None)
    request.session[SESSION_RESET_PHONE] = phone
    request.session[SESSION_RESET_STARTED] = time.time()
    request.session[SESSION_RESET_TRIES] = 0
    account = MerchantAccount.objects.filter(phone=phone).first()
    if account is not None and portal_otp.reset_eligible(account):
        request.session[SESSION_RESET_ACCOUNT] = account.pk
        outcome = portal_otp.send_reset_code(account, lang, _client_ip(request))
        if outcome.status != portal_otp.OK:
            logger.info("portal reset code not sent: account=%s outcome=%s", account.pk, outcome.status)
    _pad(started)
    return _harden(redirect("portal:password_reset"))


def _decoy_result(request, raw_code):
    """رقم غير مؤهّل: نفس تسلسل ردود الكود الحقيقي (غلط → استنفاد بعد 5 → انتهاء بعد 10 دقايق)."""
    started = request.session.get(SESSION_RESET_STARTED)
    tries = request.session.get(SESSION_RESET_TRIES, 0)
    if not isinstance(tries, int) or isinstance(tries, bool):
        tries = 0
    if not isinstance(started, (int, float)) or time.time() - started >= portal_otp.OTP_TTL.total_seconds():
        return portal_otp.V_EXPIRED
    if tries >= portal_otp.MAX_ATTEMPTS:
        return portal_otp.V_EXHAUSTED
    if portal_otp.normalize_code(raw_code) is None:
        return portal_otp.V_WRONG
    tries += 1
    request.session[SESSION_RESET_TRIES] = tries
    return portal_otp.V_EXHAUSTED if tries >= portal_otp.MAX_ATTEMPTS else portal_otp.V_WRONG


def _render_reset(request, errors=None, form_error="", status=200):
    phone = request.session.get(SESSION_RESET_PHONE, "")
    ctx = {
        "masked_phone": portal_otp.mask_phone(phone) if phone else "",
        "errors": errors or {},
        "form_error": form_error,
    }
    return _harden(render(request, "apitokens/portal/password_reset.html", ctx, status=status))


@require_http_methods(["GET", "POST"])
def reset(request):
    if SESSION_RESET_STARTED not in request.session:
        return _harden(redirect("portal:password_forgot"))
    if request.method == "GET":
        return _render_reset(request)

    lang = _lang(request)
    if reset_throttled(request):
        return _render_reset(request, form_error=t("portal_reset_err_throttled", lang), status=429)
    password = request.POST.get("password", "")
    pw_key = password_error_key(password)
    if pw_key:
        # كلمة المرور قبل الكود: خطأ فيها ما بيحرق محاولة
        return _render_reset(request, errors={"password": t(pw_key, lang)}, status=400)
    if password != request.POST.get("password_confirm", ""):
        return _render_reset(request, errors={"password_confirm": t("portal_register_err_mismatch", lang)}, status=400)

    password_hash = make_password(password)  # بالحالتين: نفس الكلفة
    raw_code = request.POST.get("code", "")
    account_id = request.session.get(SESSION_RESET_ACCOUNT)
    account = None
    if isinstance(account_id, int) and not isinstance(account_id, bool):
        account = MerchantAccount.objects.filter(pk=account_id).first()
    if account is None:
        result = _decoy_result(request, raw_code)
    else:
        result = portal_otp.reset_password(account, raw_code, password_hash)
    if result == portal_otp.V_OK:
        account.refresh_from_db()
        login_session(request, account)  # بيمسح حالة الإعادة وبيحط بصمة الـ hash الجديد
        return _harden(_landing(account))
    return _render_reset(request, errors={"code": t(_RESET_ERRORS[result], lang)}, status=400)
