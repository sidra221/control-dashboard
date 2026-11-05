from django.apps import AppConfig
from django.core import checks

NON_SHARED_CACHES = (
    "django.core.cache.backends.locmem.LocMemCache",
    "django.core.cache.backends.filebased.FileBasedCache",
    "django.core.cache.backends.dummy.DummyCache",
)


@checks.register()
def check_rate_limit_backend(app_configs, **kwargs):
    """عدّاد معدّل على مخزن محلي (process أو سيرفر واحد) = حدود بتتضاعف بعدد
    الـ workers ومش بتنطبق بين السيرفرات. منرفضه بالفحص بدل ما ينكتشف بالإنتاج."""
    from django.conf import settings

    backend = getattr(settings, "RATE_LIMIT_BACKEND", "database")
    if backend == "database":
        return []
    if backend != "cache":
        return [checks.Error(
            f"RATE_LIMIT_BACKEND={backend!r} is not valid (use 'database' or 'cache').",
            id="apitokens.E001",
        )]
    cache_backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if cache_backend in NON_SHARED_CACHES:
        return [checks.Error(
            f"RATE_LIMIT_BACKEND='cache' needs a shared cache (Redis/Memcached) but CACHES['default'] "
            f"is {cache_backend}, which is local to one process or host.",
            hint="Configure a shared cache backend, or use RATE_LIMIT_BACKEND=database (default).",
            id="apitokens.E002",
        )]
    return []


SECRET_KEY_PLACEHOLDER_WORDS = ("change-me", "changeme", "secret")
SECRET_KEY_MIN_LENGTH = 50
SECRET_KEY_MIN_DISTINCT = 12
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver", "0.0.0.0"}


def secret_key_problem(key):
    """السبب لو المفتاح ضعيف/placeholder، أو None لو مقبول."""
    if not isinstance(key, str) or not key.strip():
        return "it is missing or empty"
    lowered = key.strip().lower()
    if lowered.startswith("django-insecure"):
        return "it starts with 'django-insecure' (Django's development placeholder)"
    if lowered == "change-me":
        return "it is the 'change-me' placeholder"
    for word in SECRET_KEY_PLACEHOLDER_WORDS:
        if word in lowered:
            return f"it contains the placeholder word {word!r}"
    if len(key) < SECRET_KEY_MIN_LENGTH:
        return f"it is shorter than {SECRET_KEY_MIN_LENGTH} characters"
    if len(set(key)) < SECRET_KEY_MIN_DISTINCT:
        return f"it has fewer than {SECRET_KEY_MIN_DISTINCT} distinct characters"
    return None


@checks.register()
def check_secret_key(app_configs, **kwargs):
    """بالإنتاج (DEBUG=False) مفتاح ضعيف = هويات الزبائن (توكنات الجلسة والمتسوّق v2) بتنزوّر.
    السيرفر ما بيشتغل بمفتاح placeholder."""
    from django.apps import apps
    from django.conf import settings

    # DEBUG المضبوط وقت الإقلاع (ready) مش بس الحالي: مشغّل اختبارات جانغو بيقلب
    # settings.DEBUG لـ False وقت `manage.py test`، وما بدنا نفشّل الاختبارات بمفتاح التطوير.
    # بالإنتاج (DJANGO_DEBUG=0) الاثنين False فالفحص بيشتغل.
    startup_debug = getattr(apps.get_app_config("apitokens"), "startup_debug", None)
    if settings.DEBUG or startup_debug:
        return []
    problem = secret_key_problem(getattr(settings, "SECRET_KEY", ""))
    if not problem:
        return []
    return [checks.Error(
        f"SECRET_KEY is not safe for production: {problem}. The v2 session and shopper identity "
        "tokens are signed with SECRET_KEY, so a weak or public key lets anyone forge customer "
        "identities (read or delete another shopper's memory); it also protects admin sessions and CSRF.",
        hint='Generate one with: python -c "import secrets;print(secrets.token_urlsafe(64))" '
             "and set it as DJANGO_SECRET_KEY in the production environment (never commit it). "
             "Rotating the key invalidates existing customer tokens (they start fresh).",
        id="apitokens.E003",
    )]


@checks.register()
def check_debug_on_public_host(app_configs, **kwargs):
    """DEBUG=True مع ALLOWED_HOSTS لدومين حقيقي = غالباً سيرفر إنتاج شغّال بوضع debug
    (بيعرض stack traces والإعدادات للعامة)."""
    from django.conf import settings

    if not settings.DEBUG:
        return []
    public = [h for h in settings.ALLOWED_HOSTS if h.strip().lower() not in LOCAL_HOSTS
              and not h.strip().lower().endswith(".localhost")]
    if not public:
        return []
    return [checks.Warning(
        f"DEBUG is True but ALLOWED_HOSTS contains non-local host(s) {public}. This looks like a "
        "production box running in debug mode (exposes tracebacks and settings, and skips the "
        "SECRET_KEY check).",
        hint="Set DJANGO_DEBUG=0 on production hosts.",
        id="apitokens.W002",
    )]


class ApitokensConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apitokens"
    verbose_name = "توكنات API"
    # DEBUG وقت الإقلاع، بيتعبّى بـ ready(). بيستخدمه فحص apitokens.E003.
    startup_debug = None

    def ready(self):
        from django.conf import settings

        self.startup_debug = bool(settings.DEBUG)
