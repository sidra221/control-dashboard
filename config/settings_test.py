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
