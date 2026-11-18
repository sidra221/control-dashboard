"""الصفحة التسويقية العامة (/welcome/) — PEPLO. بدون تسجيل دخول."""

from django.conf import settings
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.http import require_GET

_ASSETS_DIR = settings.BASE_DIR / "apitokens" / "static" / "apitokens" / "landing"


def _asset_version():
    """رقم نسخة من آخر تعديل لملفات الصفحة: السيرفر بيخزّن الثابت 30 يوم (وCloudflare كمان)،
    فبدون رقم بالرابط التعديلات ما بتبيّن للي فتح الصفحة قبل."""
    try:
        return int(max(f.stat().st_mtime for f in _ASSETS_DIR.iterdir() if f.is_file()))
    except (OSError, ValueError):
        return 0


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
            "og_image": request.build_absolute_uri(static("apitokens/landing/og.png")) + f"?v={_asset_version()}",
            "asset_v": _asset_version(),
            "page_url": request.build_absolute_uri(request.path),
        },
    )
