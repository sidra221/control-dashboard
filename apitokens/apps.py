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


class ApitokensConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apitokens"
    verbose_name = "توكنات API"
