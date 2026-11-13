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

# أمان: ما بنسمح للاختبارات تتكلم مع LightOTP الحقيقي مهما كانت قيم .env (الاختبارات بتعمل mock للإرسال
# وبتحط مفاتيح وهمية بـ override_settings لما بدها)، وما بنسمح لـ .env يبدّل القناة.
LIGHTOTP_API_KEY = ""
LIGHTOTP_TEMPLATE_ID = ""
PORTAL_OTP_BACKEND = "lightotp"
PORTAL_OTP_MAX_SENDS_PER_HOUR = 120
