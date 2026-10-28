# Control — مساعد تسوّق ذكي للمتاجر الإلكترونية

لوحة تحكم + API لربط أي متجر إلكتروني (Shopify أو غيره) بودجت دردشة ذكي:
يفهم احتياج الزبون، يرشّح منتج من كتالوج التاجر، وبيطلع للزبون شكله وهو لابس المنتج
(تجربة لبس افتراضية) — كل هذا بسطر `<script>` واحد يُضاف لأي موقع.

## المكوّنات

| المكوّن | الوصف |
| --- | --- |
| **لوحة تحكم Staff** | توليد توكنات للتجار، سقف استهلاك دوري للصور، إدارة الكتالوج |
| **الودجت** (`embed.js`) | فقاعة شات عائمة بعزل Shadow DOM، تتضاف لأي موقع بسطر وحد |
| **محرّك الدردشة** | طبقة LLM عامة (Anthropic / OpenAI / Gemini / Groq) — بيبيع بلغة ولهجة الزبون، بيعرض المنتجات، بيعالج الاعتراضات، وبيعرض كود خصم بلحظة الإغلاق |
| **قياس المبيعات** | تسجيل كل محادثة + ضغطات الشراء/السلة + ربط طلبات Shopify الفعلية بالمحادثة اللي جابتها |
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
| `OPENAI_MODEL` / `OPENAI_REASONING_EFFORT` | الموديل (افتراضياً `gpt-6-luna`) وقديش يفكّر قبل الرد (`low`) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` / `GROQ_API_KEY` | مفتاح المزوّد المختار (حط مفتاح واحد بس حسب `LLM_PROVIDER`) |
| `KIEAI_API_KEY` | مفتاح kie.ai لتوليد صور تجربة اللبس |

## تدفق العمل

1. **Staff** يولّد توكن لتاجر جديد من الداشبورد (سقف صور + فترة تجديد).
2. التاجر يرفع كتالوجه (CSV) **أو** يربط متجر Shopify حقيقي من صفحة "ربط Shopify" —
   بعدها الكتالوج يتزامن ويتحدّث لحاله تلقائياً عبر webhooks.
3. التاجر يضيف سطر `<script>` الودجت لموقعه، بالـ `data-store-key` تبع توكنه.
4. الزبون يحكي مع الودجت، يحصل ترشيح، وممكن يجرّب شكله بالمنتج بصورته.

## ربط Shopify

من 2026 Shopify ما عاد بيسمح بإنشاء "Custom App" جديد من أدمن المتجر. الطريقة الحالية:

1. من `dev.shopify.com` (بنفس منظمة صاحب المتجر) أنشئ تطبيق.
2. فعّل الصلاحيات: `read_products` (إجباري)، `read_orders` (لقياس الطلبات اللي جابها المساعد)،
   `read_legal_policies` (لسياسات الشحن والإرجاع).
3. Release للنسخة ثم Install app على المتجر.
4. بصفحة "ربط Shopify" بالداشبورد حط الدومين + **Client ID** + **Client secret**.

التوكن صالح 24 ساعة وبيتجدّد لحاله. المتاجر القديمة اللي عندها Custom App قبل 2026
بتقدر تحط الـ Admin API access token الثابت بدل الـ Client ID.

- المزامنة عبر GraphQL Admin API (نسخة `2026-07`)، وwebhooks لحظية للمنتجات
  (`products/create|update|delete`) والطلبات (`orders/create`).
- منتجات draft/archived أو بدون مخزون ما بتوصل للمساعد. الخيارات المتوفرة (مقاسات،
  ألوان) والسعر قبل التخفيض بتوصله، وسياسات المتجر بتتعبّى بـ "معلومات المتجر".
- حجم الكتالوج: لحد 60 منتج المساعد بيشوفهم بكل تفاصيلهم، لحد 400 بيشوفهم كلهم بشكل مختصر،
  وفوق 400 بيشوف الـ 40 الأقرب دلالياً لطلب الزبون (embeddings من OpenAI، بتشتغل بأي لغة).
- جوا متجر Shopify، الودجت بيضيف المنتج للسلة مباشرة، وبيعلّم السلة بمعرّف المحادثة
  (`_sw_conversation`) حتى ينربط الطلب بالمحادثة.
- مزامنة دورية للمخزون (cron كل ساعة مثلاً): `python manage.py sync_shopify`

## تقييم أداء البيع

```bash
python manage.py eval_sales                     # كل السيناريوهات
python manage.py eval_sales --only gulf,sizes   # سيناريوهات محددة
python manage.py eval_sales --model gpt-6-luna --effort medium
```

بيشغّل محادثات زبائن ثابتة (لهجات، اعتراض على السعر، هدايا، مقاسات، شحن، منتج مخلّص...)
ضد الموديل الحقيقي، وبيفحص كل رد بالكود (اللغة، الكرت، الخصم، التكرار) وبحكم موديل
(اللهجة، الإقناع، الصدق). شغّله بعد أي تعديل على البرومبت أو تغيير الموديل.

## هيكل المشروع

```
apitokens/
  models.py            ApiToken, Product, ShopifyConnection, Conversation
  views.py             لوحة التحكم (توكنات، كتالوج، ربط Shopify، المحادثات والمبيعات)
  widget_views.py       نقاط API العامة للودجت (chat, tryon, event)
  shopify_webhooks.py    استقبال تحديثات Shopify اللحظية
  services/
    llm.py              طبقة LLM عامة (anthropic/openai/gemini/groq)
    kieai.py             تكامل تجربة اللبس
    shopify.py           GraphQL Admin API + تجديد التوكن + تحقق webhooks
    catalog.py           مزامنة Shopify + اختيار المنتجات الأقرب للزبون
  management/commands/
    sync_shopify.py      مزامنة دورية لكل المتاجر
    eval_sales.py        تقييم آلي لأداء المساعد بالبيع
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
