from pathlib import Path
import json
import os

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-change-me",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

# بيستخدم لبناء عنوان الـ webhook اللي منسجله عند Shopify — لازم يكون
# عنوان حقيقي يوصله إنترنت (https://...)، مش localhost.
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "http://localhost:8000").strip().rstrip("/")

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "DJANGO_CSRF_TRUSTED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if origin.strip()
]

# nginx بينهي الـ TLS وبيمرّر الطلب لجانغو على http.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

if not DEBUG:
    # الداشبورد بالإنتاج على HTTPS بس — الكوكيز ما بتنبعت على http عادي.
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # 30 يوم، وبدون includeSubDomains حتى ما نأثر على باقي مواقع الدومين.
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "2592000"))
    # التحويل من http لـ https بيصير بـ nginx. لو فعّلناه بجانغو كمان، أي خدمة
    # داخلية بتحكي مع gunicorn مباشرة على 127.0.0.1 (مثلاً n8n) رح تاخد 301.
    SILENCED_SYSTEM_CHECKS = ["security.W008"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apitokens",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apitokens.context_processors.language",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

POSTGRES_DB = os.environ.get("POSTGRES_DB", "").strip()

if POSTGRES_DB:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": POSTGRES_DB,
            "USER": os.environ.get("POSTGRES_USER", ""),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": int(os.environ.get("POSTGRES_CONN_MAX_AGE", "60")),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ar"
TIME_ZONE = "Asia/Amman"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# "anthropic" (افتراضي) أو "openai" أو "gemini" — أي مفتاح يوصل أول،
# منحط اسم المزوّد هون بدون ما نلمس كود الشات أبداً.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic").strip()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "").strip()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

KIEAI_API_KEY = os.environ.get("KIEAI_API_KEY", "").strip()

# توكن التاجر الوهمي يلي بيعرض متجر الديمو (/demo/) — متجر تجريبي منفرجي فيه
# ويدجت الشات كأنه مركّب على متجر حقيقي. فاضي = صفحات /demo/ بترجع 404.
DEMO_STORE_KEY = os.environ.get("DEMO_STORE_KEY", "").strip()

# تطبيق سلة (من بوابة الشركاء portal.salla.partners). التاجر بيركّب التطبيق،
# وسلة بتبعتلنا التوكن بإشعار app.store.authorize (Easy Mode).
SALLA_APP_ID = os.environ.get("SALLA_APP_ID", "").strip()
SALLA_CLIENT_ID = os.environ.get("SALLA_CLIENT_ID", "").strip()
SALLA_CLIENT_SECRET = os.environ.get("SALLA_CLIENT_SECRET", "").strip()
SALLA_WEBHOOK_SECRET = os.environ.get("SALLA_WEBHOOK_SECRET", "").strip()
# رصيد صور التجربة لحساب التاجر اللي بينعمل لحاله عند التركيب — لحد ما تتحدد الباقات.
SALLA_DEFAULT_IMAGE_QUOTA = int(os.environ.get("SALLA_DEFAULT_IMAGE_QUOTA", "20"))

# تطبيق زد (من بوابة الشركاء partner.zid.sa ← General settings ← API keys).
# التاجر بيضغط "تفعيل" بزد ← /zid/install/ ← موافقة OAuth ← /zid/callback/.
ZID_CLIENT_ID = os.environ.get("ZID_CLIENT_ID", "").strip()
ZID_CLIENT_SECRET = os.environ.get("ZID_CLIENT_SECRET", "").strip()
ZID_WEBHOOK_SECRET = os.environ.get("ZID_WEBHOOK_SECRET", "").strip()
ZID_DEFAULT_IMAGE_QUOTA = int(os.environ.get("ZID_DEFAULT_IMAGE_QUOTA", "20"))
# رصيد صور التجربة الشهري لكل باقة بزد — المفتاح اسم الباقة (plan_name) أو رقمها
# (plan_id) متل ما بتطلع ببوابة الشركاء. مؤقت لحد ما تتحدد الباقات النهائية.
ZID_PLAN_QUOTAS = json.loads(
    os.environ.get("ZID_PLAN_QUOTAS", "") or '{"Basic": 100, "Growth": 500, "Pro": 1000}'
)

# إيميل الدعم اللي بيظهر بصفحة سياسة الخصوصية (/privacy/).
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "").strip()

# تحديد معدّل الطلبات للـ endpoints العامة (services/ratelimit.py). العدّادات بمخزن
# مشترك بين كل workers وسيرفرات النشر:
#   "database" (الافتراضي) = جدول بقاعدة البيانات يلي كل نشر عندو أصلاً — بلا بنية جديدة.
#   "cache"                = cache جانغو، بس لو CACHES مضبوط على Redis/Memcached
#                            (فحص النظام بيرفض LocMem/File-based).
RATE_LIMIT_BACKEND = os.environ.get("RATE_LIMIT_BACKEND", "database").strip().lower()
# قواعد بتفشل مغلقة (503) لو مخزن العدّادات تعطّل. None = الافتراضي (الدردشة،
# التجربة، الاستهلاك). القواعد الباقية بتفشل مفتوحة مع خطأ بالسجل.
RATE_LIMIT_FAIL_CLOSED = None

# مصادقة endpoint الاستهلاك (services/consume_auth.py). التوكن العلني القديم مقبول
# انتقالياً بس من شبكات داخلية (افتراضياً loopback = n8n على نفس السيرفر). لو n8n على
# سيرفر تاني: حط عنوانه هون (CIDR مفصولة بفاصلة)، والأفضل تحويله لمفتاح الاستهلاك.
# CONSUME_LEGACY_MODE=off بيطفي المسار القديم كلياً.
CONSUME_LEGACY_MODE = os.environ.get("CONSUME_LEGACY_MODE", "internal").strip().lower()
CONSUME_LEGACY_ALLOWED_NETWORKS = [
    n.strip()
    for n in os.environ.get("CONSUME_LEGACY_ALLOWED_NETWORKS", "127.0.0.0/8,::1/128").split(",")
    if n.strip()
]
# عدد الـ proxies الموثوقة قدام جانغو (nginx = 1). 0 = تجاهل X-Forwarded-For.
TRUSTED_PROXY_COUNT = int(os.environ.get("TRUSTED_PROXY_COUNT", "1"))
# تعديل حدود المعدّل: {"chat_ip": (20, 60)} = 20 طلب كل 60 ثانية. فاضي = الافتراضي.
RATE_LIMITS = {}
