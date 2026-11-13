"""صفحات بوابة التاجر (قراءة فقط). كل شي بيتحسب من `request.portal_merchant`
اللي بيحطه الديكوريتر — ما بنقبل أي id من الرابط أو الـ GET، حتى ما يشوف تاجر بيانات تاجر تاني."""

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from . import portal_otp
from .i18n import t
from .models import Conversation, TryOnEvent
from .portal_auth import (
    PORTAL_TERMS_VERSION, _client_ip, _landing, _lang, accept_terms, portal_account_required, portal_required,
    portal_session_required, terms_accepted_required, verify_throttled,
)
from .services import tryon_stats as tryon_stats_service


def _conversation_kpis(merchant):
    """نفس منطق صفحة المحادثات عند الستاف (آخر 30 يوم) بس لهالتاجر."""
    since = timezone.now() - timedelta(days=30)
    recent = Conversation.objects.filter(merchant=merchant, started_at__gte=since)
    agg = recent.aggregate(
        total=Count("id"),
        intent=Count("id", filter=Q(buy_clicks__gt=0) | Q(add_to_carts__gt=0)),
        orders=Count("id", filter=~Q(order_id="")),
        revenue=Sum("order_total"),
    )
    with_recommendation = sum(1 for skus in recent.values_list("recommended_skus", flat=True) if skus)
    return {
        "total": agg["total"],
        "with_recommendation": with_recommendation,
        "intent": agg["intent"],
        "orders": agg["orders"],
        "revenue": agg["revenue"] or 0,
    }


@portal_required
@terms_accepted_required
def home(request):
    merchant = request.portal_merchant
    range_key = tryon_stats_service.clean_range(request.GET.get("range", tryon_stats_service.DEFAULT_RANGE))
    window_days = tryon_stats_service.clean_window(request.GET.get("window", tryon_stats_service.DEFAULT_WINDOW))
    stats = tryon_stats_service.build_stats(merchant, range_key, window_days)
    # حالة فاضية: لا تجارب ناجحة ولا محادثات أبداً (مش بس ضمن الفترة المختارة).
    has_data = (
        TryOnEvent.objects.filter(merchant=merchant, success=True).exists()
        or Conversation.objects.filter(merchant=merchant).exists()
    )
    return render(
        request,
        "apitokens/portal/home.html",
        {
            "merchant": merchant,
            "stats": stats,
            "conv": _conversation_kpis(merchant),
            "range_key": range_key,
            "window_days": window_days,
            "currency": merchant.currency,
            "has_data": has_data,
        },
    )


@portal_required
def accept(request):
    """موافقة التاجر على سياسة الخصوصية قبل أول عرض للوحة. بتنسجّل بالنسخة والوقت."""
    merchant = request.portal_merchant
    if merchant.terms_accepted_version == PORTAL_TERMS_VERSION:
        return redirect("portal:home")
    error = False
    if request.method == "POST":
        if request.POST.get("agree") == "1":
            accept_terms(merchant)
            return redirect("portal:home")
        error = True
    return render(
        request,
        "apitokens/portal/accept.html",
        {"merchant": merchant, "version": PORTAL_TERMS_VERSION, "error": error},
        status=400 if error else 200,
    )


@portal_account_required
def pending(request):
    """حساب اتسجّل بس لسا ما انربط بمتجر. لو انربط → عالرئيسية (وهناك الموافقة على السياسة أول)."""
    account = request.portal_account
    if account.merchant_id is not None:
        return redirect("portal:home")
    return render(request, "apitokens/portal/pending.html", {"account": account})


# ---------- تحقق الواتساب (OTP) ----------

_VERIFY_ERRORS = {
    portal_otp.V_WRONG: "portal_otp_err_wrong",
    portal_otp.V_EXPIRED: "portal_otp_err_expired",
    portal_otp.V_EXHAUSTED: "portal_otp_err_exhausted",
    portal_otp.V_NONE: "portal_otp_err_none",
}
_FLASH_TEXT = {
    portal_otp.OK: ("info", "portal_otp_sent"),
    portal_otp.UNAVAILABLE: ("error", "portal_otp_unavailable"),
    portal_otp.THROTTLED: ("error", "portal_otp_throttled"),
}


def _render_verify(request, account, lang, flash=None, code_error="", form_error="", status=200):
    wait = portal_otp.page_wait_seconds(request, account)
    info = ""
    if flash == portal_otp.WAIT:
        form_error = t("portal_otp_wait", lang).replace("{n}", str(wait or portal_otp.LIGHTOTP_WAIT_HINT))
    elif flash in _FLASH_TEXT:
        kind, key = _FLASH_TEXT[flash]
        if kind == "info":
            info = t(key, lang)
        else:
            form_error = t(key, lang)
    hint = ""
    if not (info or form_error or code_error) and portal_otp.active_otp(account) is None:
        hint = t("portal_otp_no_active", lang)
    ctx = {
        "masked_phone": portal_otp.mask_phone(account.phone),
        "wait_seconds": wait,
        "resend_in_label": t("portal_otp_resend_in", lang).replace("{n}", str(wait)),
        "info": info, "form_error": form_error, "code_error": code_error, "hint": hint,
    }
    return render(request, "apitokens/portal/verify.html", ctx, status=status)


@portal_session_required
@require_http_methods(["GET", "POST"])
def verify(request):
    """صفحة إدخال الكود. GET بيعرضها، POST بيتحقق. الإعادة بـ POST على portal:verify_resend."""
    account = request.portal_account
    if account.phone_verified:
        return _landing(account)
    lang = _lang(request)
    if request.method == "GET":
        return _render_verify(request, account, lang, flash=portal_otp.pop_flash(request))
    if verify_throttled(request):
        return _render_verify(request, account, lang, form_error=t("portal_otp_throttled", lang), status=429)
    result = portal_otp.verify_code(account, request.POST.get("code", ""))
    if result == portal_otp.V_OK:
        account.refresh_from_db()
        return _landing(account)
    return _render_verify(request, account, lang, code_error=t(_VERIFY_ERRORS[result], lang), status=400)


@portal_session_required
@require_POST
def verify_resend(request):
    """زر إعادة الإرسال: كود جديد (لو التهدئة سمحت) ثم رجوع لصفحة التحقق (PRG) مع رسالة النتيجة."""
    account = request.portal_account
    if account.phone_verified:
        return _landing(account)
    outcome = portal_otp.send_code(account, _lang(request), _client_ip(request))
    portal_otp.remember_outcome(request, outcome)
    return redirect("portal:verify")
