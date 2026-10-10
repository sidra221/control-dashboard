# Control — مساعد تسوّق ذكي للمتاجر الإلكترونية

لوحة تحكم + API لربط أي متجر إلكتروني (Shopify أو غيره) بودجت دردشة ذكي:
يفهم احتياج الزبون، يرشّح منتج من كتالوج التاجر، وبيطلع للزبون شكله وهو لابس المنتج
(تجربة لبس افتراضية) — كل هذا بسطر `<script>` واحد يُضاف لأي موقع.

## المكوّنات

| المكوّن | الوصف |
| --- | --- |
| **لوحة تحكم Staff** | توليد توكنات للتجار، سقف استهلاك دوري للصور، إدارة الكتالوج |
| **الودجت** (`embed.js`) | فقاعة شات عائمة بعزل Shadow DOM، تتضاف لأي موقع بسطر وحد |
| **محرّك الدردشة** | طبقة LLM عامة (Anthropic / OpenAI / Gemini / Groq) بترشّح منتج واحد من الكتالوج |
| **تجربة اللبس** | تكامل مباشر مع kie.ai: صورة الزبون + صورة المنتج → صورة واحدة واقعية |
| **كتالوج المنتجات** | رفع يدوي عبر CSV، أو **ربط حي مع Shopify** (مزامنة تلقائية عبر webhooks) |

## التقنيات

Django 5 · Django REST Framework · PostgreSQL (أو SQLite محلياً) · gunicorn + nginx
· Anthropic / OpenAI / Gemini SDKs · Shopify Admin API

## التشغيل محلياً

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # عبّي المفاتيح يلي بدك ياها، انظر الجدول تحت

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

الداشبورد بيطلع على `http://localhost:8000/` (تسجيل دخول Staff مطلوب).

## متغيّرات البيئة (`.env`)

| المتغيّر | الوصف |
| --- | --- |
| `DJANGO_SECRET_KEY` | مفتاح سرّي لجانغو — غيّره بالإنتاج |
| `DJANGO_DEBUG` | `1` محلياً، `0` بالإنتاج |
| `DJANGO_ALLOWED_HOSTS` | النطاقات المسموحة، مفصولة بفاصلة |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | نفس الفكرة لحماية CSRF |
| `SITE_BASE_URL` | عنوان عام حقيقي (https) — يُستخدم لبناء رابط الـ webhook عند ربط تاجر بـ Shopify |
| `POSTGRES_*` | إعدادات قاعدة البيانات — اتركها فاضية لاستخدام SQLite محلياً |
| `LLM_PROVIDER` | `anthropic` / `openai` / `gemini` / `groq` — أي مزوّد يشغّل الدردشة |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / `GROQ_API_KEY` | مفتاح المزوّد المختار (حط مفتاح واحد بس حسب `LLM_PROVIDER`) |
| `KIEAI_API_KEY` | مفتاح kie.ai لتوليد صور تجربة اللبس |

## تدفق العمل

1. **Staff** يولّد توكن لتاجر جديد من الداشبورد (سقف صور + فترة تجديد).
2. التاجر يرفع كتالوجه (CSV) **أو** يربط متجر Shopify حقيقي من صفحة "ربط Shopify" —
   بعدها الكتالوج يتزامن ويتحدّث لحاله تلقائياً عبر webhooks.
3. التاجر يضيف سطر `<script>` الودجت لموقعه، بالـ `data-store-key` تبع توكنه.
4. الزبون يحكي مع الودجت، يحصل ترشيح، وممكن يجرّب شكله بالمنتج بصورته.

## ربط Shopify (Custom App)

مفصّل بالكامل بدليل منفصل — تطبيق خاص لكل متجر، بدون مراجعة من Shopify، ومزامنة
لحظية عبر webhooks (`products/create|update|delete`). راجع `apitokens/services/shopify.py`
و`apitokens/shopify_webhooks.py`.

## هيكل المشروع

```
apitokens/
  models.py            ApiToken, Product, ShopifyConnection
  views.py             لوحة التحكم (توكنات، كتالوج، ربط Shopify)
  widget_views.py       نقاط API العامة للودجت (chat, tryon)
  shopify_webhooks.py    استقبال تحديثات Shopify اللحظية
  services/
    llm.py              طبقة LLM عامة (anthropic/openai/gemini/groq)
    kieai.py             تكامل تجربة اللبس
    shopify.py           Admin API + تحقق webhooks
  static/apitokens/
    embed.js             الودجت القابل للتضمين
    demo.html             صفحة متجر تجريبية حية
config/
  settings.py / urls.py
```

## النشر (الإنتاج)

- `gunicorn` خلف `nginx` (انظر `--timeout 300` بالـ service، ضروري لاستجابة تجربة اللبس البطيئة نسبياً).
- `python manage.py collectstatic` بعد أي تعديل على `embed.js` أو `demo.html`.
- Postgres بدل SQLite (`POSTGRES_*` بالـ `.env`).
