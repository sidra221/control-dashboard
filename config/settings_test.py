"""إعدادات مخصّصة لتشغيل الاختبارات الآلية بس — نفس إعدادات الإنتاج
بالكامل، عدا قاعدة البيانات: SQLite معزولة بدل Postgres، حتى ما نحتاج
صلاحية CREATEDB لمستخدم قاعدة بيانات الإنتاج (وما منلمس .env أو أي
صلاحية حقيقية بالمرة).
"""

import os
import tempfile

from .settings import *  # noqa: F401,F403

# قاعدة الاختبار ملف مؤقت مش ذاكرة: اختبارات التزامن بالخيوط بدها كتّاب متعددين،
# و SQLite بالذاكرة (shared-cache) بيرمي "table is locked" بدل ما ينتظر. الملف بيتمسح
# بآخر الاختبارات.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"NAME": str(Path(tempfile.gettempdir()) / f"control_dashboard_test_{os.getpid()}.sqlite3")},
    }
}

# الاختبارات بتضرب نفس الـ endpoints مئات المرات بنفس التوكن — منرفع الحدود،
# واختبارات المعدّل نفسها بتخفّضها بـ override_settings.
RATE_LIMITS = {
    rule: (10**9, 60)
    for rule in (
        "chat_ip", "chat_ip_hour", "chat_token", "tryon_ip", "tryon_token",
        "event_ip", "pageproduct_ip", "memory_ip", "consume_ip", "consume_token",
    )
}
