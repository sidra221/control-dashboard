"""الصفحة التسويقية العامة (/welcome/) — PEPLO. بدون تسجيل دخول."""

from django.conf import settings
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.http import require_GET


@require_GET
def landing(request):
    return render(
        request,
        "apitokens/landing/landing.html",
        {
            "whatsapp_url": f"https://wa.me/{settings.SUPPORT_WHATSAPP}" if settings.SUPPORT_WHATSAPP else "",
            # /demo/ بيرجع 404 إذا DEMO_STORE_KEY فاضي، فما منعرض زر الديمو بهالحالة
            "demo_enabled": bool(settings.DEMO_STORE_KEY),
            "widget_src": request.build_absolute_uri(static("apitokens/embed.js")),
            "og_image": request.build_absolute_uri(static("apitokens/landing/og.png")),
            "page_url": request.build_absolute_uri(request.path),
        },
    )
