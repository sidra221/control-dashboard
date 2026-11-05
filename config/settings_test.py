"""إعدادات مخصّصة لتشغيل الاختبارات الآلية بس — نفس إعدادات الإنتاج
بالكامل، عدا قاعدة البيانات: SQLite معزولة بدل Postgres، حتى ما نحتاج
صلاحية CREATEDB لمستخدم قاعدة بيانات الإنتاج (وما منلمس .env أو أي
صلاحية حقيقية بالمرة).
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# الاختبارات بتضرب نفس الـ endpoints مئات المرات بنفس التوكن — منرفع الحدود،
# واختبارات المعدّل نفسها بتخفّضها بـ override_settings.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
RATE_LIMITS = {
    rule: (10**9, 60)
    for rule in (
        "chat_ip", "chat_ip_hour", "chat_token", "tryon_ip", "tryon_token",
        "event_ip", "pageproduct_ip", "consume_token",
    )
}
