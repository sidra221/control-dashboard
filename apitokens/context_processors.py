import re

from django.conf import settings

from .i18n import DEFAULT_LANGUAGE, get_translations


def language(request):
    lang = getattr(request, "session", {}).get("lang", DEFAULT_LANGUAGE)
    return {
        "LANG": lang,
        "DIR": "rtl" if lang == "ar" else "ltr",
        "T": get_translations(lang),
        # تواصل الدعم: كل "تواصل معنا" بالبوابة والخصوصية بيفتح واتساب الدعم
        "SUPPORT_WHATSAPP_URL": _whatsapp_url(getattr(settings, "SUPPORT_WHATSAPP", "")),
    }


def _whatsapp_url(number):
    digits = re.sub(r"\D", "", number or "")
    return f"https://wa.me/{digits}" if digits else ""
