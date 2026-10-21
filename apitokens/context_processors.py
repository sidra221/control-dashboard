from .i18n import DEFAULT_LANGUAGE, get_translations


def language(request):
    lang = getattr(request, "session", {}).get("lang", DEFAULT_LANGUAGE)
    return {
        "LANG": lang,
        "DIR": "rtl" if lang == "ar" else "ltr",
        "T": get_translations(lang),
    }
