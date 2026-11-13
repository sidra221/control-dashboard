"""صفحات بوابة التاجر (قراءة فقط). كل شي بيتحسب من `request.portal_merchant`
اللي بيحطه الديكوريتر — ما بنقبل أي id من الرابط أو الـ GET، حتى ما يشوف تاجر بيانات تاجر تاني."""

from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from . import portal_otp
from .i18n import t
from .models import Conversation, PhoneOtp, TryOnEvent
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
    product_count = merchant.products.count()
    lang = _lang(request)
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
            # تركيب المساعد: سطر السكربت بتوكن المتجر، وعدد منتجاته (بلا منتجات المساعد ما عنده شي يقترحه)
            "widget_src": request.build_absolute_uri(static("apitokens/embed.js")),
            "product_count": product_count,
            "products_line": t("portal_install_products", lang).replace("{n}", str(product_count)),
            "support_email": settings.SUPPORT_EMAIL,
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


_SUBSCRIBE_ERRORS = {
    portal_otp.V_WRONG: "portal_sub_err_wrong",
    portal_otp.V_EXPIRED: "portal_sub_err_expired",
    portal_otp.V_EXHAUSTED: "portal_sub_err_exhausted",
    portal_otp.V_NONE: "portal_sub_err_none",
    portal_otp.V_CLOSED: "portal_sub_err_closed",
}


@portal_account_required
@require_http_methods(["GET", "POST"])
def pending(request):
    """حساب موثّق بلا متجر: صفحة الاشتراك. بتعرض ميزات الخدمة، وبتطلب اسم المتجر وبترسل كود الاشتراك
    على واتساب (action=request)، وإدخال الكود (action=activate) بينشئ المتجر وبيفتح لوحته."""
    account = request.portal_account
    if account.merchant_id is not None:
        return redirect("portal:home")
    lang = _lang(request)
    action = request.POST.get("action") if request.method == "POST" else None

    if action == "request":
        store_name = portal_otp.clean_store_name(request.POST.get("store_name"))
        if not store_name:
            return _render_subscribe(request, account, lang, name_error=t("portal_sub_err_name", lang),
                                     store_name=request.POST.get("store_name", ""), status=400)
        outcome = portal_otp.send_subscription_code(account, store_name, lang, _client_ip(request))
        if outcome.status == portal_otp.LINKED:
            return redirect("portal:home")
        portal_otp.remember_outcome(request, outcome)
        return redirect("portal:pending")

    if action == "activate":
        if verify_throttled(request):
            return _render_subscribe(request, account, lang, form_error=t("portal_otp_throttled", lang), status=429)
        status, _merchant = portal_otp.activate_subscription(account, request.POST.get("code", ""))
        if status in (portal_otp.V_OK, portal_otp.LINKED):
            return redirect("portal:home")  # الرئيسية بتطلب الموافقة على السياسة أول
        return _render_subscribe(request, account, lang, code_error=t(_SUBSCRIBE_ERRORS[status], lang), status=400)

    if action is not None:
        return redirect("portal:pending")
    return _render_subscribe(request, account, lang, flash=portal_otp.pop_flash(request))


def _render_subscribe(request, account, lang, flash=None, name_error="", code_error="", form_error="",
                      store_name=None, status=200):
    active = portal_otp.active_otp(account, purpose=PhoneOtp.PURPOSE_SUBSCRIBE)
    wait = portal_otp.page_wait_seconds(request, account)
    info = ""
    if flash == portal_otp.WAIT:
        form_error = t("portal_otp_wait", lang).replace("{n}", str(wait or portal_otp.LIGHTOTP_WAIT_HINT))
    elif flash == portal_otp.OK:
        pass  # ما في رسالة إضافية: مرحلة الكود بتقول أصلاً "أرسلنا كود الاشتراك … إلى الرقم"
    elif flash in _FLASH_TEXT:
        form_error = t(_FLASH_TEXT[flash][1], lang)
    ctx = {
        "account": account,
        "code_stage": active is not None or bool(code_error),
        "store_name": store_name if store_name is not None else (active.store_name if active else portal_otp.last_store_name(account)),
        "store_name_max": portal_otp.STORE_NAME_MAX,
        "code_length": portal_otp.SUBSCRIBE_CODE_LENGTH,
        "plan_line": t("portal_sub_plan", lang).replace("{n}", str(settings.PORTAL_TRIAL_IMAGE_QUOTA)),
        "masked_phone": portal_otp.mask_phone(account.phone),
        "wait_seconds": wait,
        "resend_in_label": t("portal_otp_resend_in", lang).replace("{n}", str(wait)),
        "info": info, "form_error": form_error, "name_error": name_error, "code_error": code_error,
    }
    return render(request, "apitokens/portal/pending.html", ctx, status=status)


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
