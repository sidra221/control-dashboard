"""صفحات تفعيل تطبيق زد. التاجر بيضغط "تفعيل" بزد ← install (بيبلّش OAuth
فوراً، بلا تسجيل دخول قبله — شرط مراجعة زد) ← موافقة ← callback ← app."""

import logging
import secrets

from django.conf import settings
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from .models import ZidConnection
from .services import zid as zid_service

logger = logging.getLogger(__name__)

STATE_SESSION_KEY = "zid_oauth_state"
CONNECTION_SESSION_KEY = "zid_connection_id"


PRIVACY_POLICY_UPDATED = "2026-11-01"


@require_GET
def privacy(request):
    """سياسة الخصوصية — زد (وسلة) بيطلبوا رابطها إجبارياً للمراجعة."""
    return render(
        request,
        "apitokens/privacy.html",
        {"updated": PRIVACY_POLICY_UPDATED, "contact_email": settings.SUPPORT_EMAIL},
    )


@require_GET
def install(request):
    if not settings.ZID_CLIENT_ID:
        return render(request, "apitokens/zid_app.html", {"error": "not_configured"}, status=503)
    state = secrets.token_urlsafe(24)
    request.session[STATE_SESSION_KEY] = state
    return redirect(zid_service.authorize_url(state))


@require_GET
def callback(request):
    expected_state = request.session.pop(STATE_SESSION_KEY, None)
    code = request.GET.get("code", "")
    if not expected_state or not secrets.compare_digest(expected_state, request.GET.get("state", "")):
        return render(request, "apitokens/zid_app.html", {"error": "state"}, status=400)
    if not code:
        return render(request, "apitokens/zid_app.html", {"error": "denied"}, status=400)
    try:
        connection = zid_service.complete_installation(code)
    except zid_service.ZidError:
        logger.exception("zid_installation_failed")
        return render(request, "apitokens/zid_app.html", {"error": "exchange"}, status=502)
    # بعد التثبيت مباشرة منبلّش أول مزامنة بالخلفية (تاسك المزامنة).
    from .zid_webhooks import start_initial_sync

    start_initial_sync(connection.pk)
    request.session[CONNECTION_SESSION_KEY] = connection.pk
    return redirect("zid-app")


@require_GET
def app(request):
    """رابط التطبيق (App URL): التاجر بيفتحه من لوحة زد. بدون جلسة منرجع
    لـ OAuth — زد بتتخطى الموافقة إذا التطبيق مفعّل أصلاً."""
    connection = (
        ZidConnection.objects.filter(pk=request.session.get(CONNECTION_SESSION_KEY), is_active=True)
        .select_related("merchant")
        .first()
    )
    if connection is None:
        return redirect("zid-install")
    return render(
        request,
        "apitokens/zid_app.html",
        {"connection": connection, "product_count": connection.merchant.products.count()},
    )
