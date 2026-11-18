# Control — مساعد تسوّق ذكي للمتاجر الإلكترونية

مشروع Django بيقدّم للمتاجر الإلكترونية **ودجت دردشة ذكي**: بيفهم احتياج الزبون، بيرشّح منتج من كتالوج
التاجر، وبيعرض للزبون شكله وهو لابس المنتج (**تجربة لبس افتراضية** عبر kie.ai) — كل هذا بسطر `<script>`
واحد. حوله: لوحة تحكم للـ Staff، بوابة ذاتية للتاجر، وتكاملات مع Shopify وسلة وزد وWooCommerce.

> التفاصيل التشغيلية الطويلة (النشر، الـ cron، nginx، النسخ الاحتياطي، إعداد كل منصة خطوة بخطوة)
> موجودة بـ [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## المكوّنات

| المكوّن | الرابط | الوصف |
| --- | --- | --- |
| **الودجت** (`embed.js`) | `/static/apitokens/embed.js` + `/widget/*` | فقاعة شات عائمة (Shadow DOM) بتتضاف بسطر `<script src=".../embed.js" data-store-key="<توكن المتجر>">`. نقاط الـ API العامة: `chat`، `tryon`، `event`، `page-product`، و`salla-key` / `zid-key` لسكربتات المنصات |
| **محرّك الدردشة** | `services/llm.py` | طبقة LLM عامة (Anthropic / OpenAI / Gemini / Groq) حسب `LLM_PROVIDER` — بيبيع بلغة ولهجة الزبون، بيعرض كروت المنتجات، بيعالج الاعتراضات، وبيعرض كود خصم |
| **تجربة اللبس** | `services/kieai.py` | kie.ai (موديل `gpt-image-2-image-to-image`): صورة الزبون + صورة المنتج → صورة واقعية. بتستهلك من رصيد صور المتجر |
| **لوحة تحكم Staff** | `/` (تسجيل دخول Staff) | إنشاء متاجر/توكنات برصيد صور دوري، الكتالوج (CSV)، ربط Shopify وWooCommerce، أكواد الخصم، المحادثات والمبيعات، إحصائيات التجارب، صفحتا سلة وزد، روابط دعوة البوابة وربط الحسابات المعلّقة بمتاجر |
| **بوابة التاجر** | `/portal/` | تسجيل ذاتي برقم الجوال + كلمة سر، تحقق واتساب OTP، صفحة اشتراك بكود، وداشبورد التاجر (انظر تحت) |
| **توزيع الرصيد** | `services/quota.py` | حد تجارب لكل زبون + حد IP + سقف يومي للمتجر + حجز الرصيد أثناء التوليد + تنبيهات 80%/100% |
| **متجر الديمو** | `/demo/` | متجر أزياء وهمي بيعرض الودجت كأنه على متجر حقيقي — بيشتغل بس إذا `DEMO_STORE_KEY` مضبوط (وإلا 404) |
| **API لـ n8n** | `POST /api/tokens/consume/` | بيتحقق من توكن (هيدر `X-Api-Token`) وبيخصم صور من الرصيد. دائماً HTTP 200 والنتيجة بحقل `ok` / `error` |
| **قياس المبيعات** | `Conversation` | كل محادثة + ضغطات الشراء/السلة + ربط الطلبات الفعلية بالمحادثة اللي جابتها |
| **سياسة الخصوصية** | `/privacy/` | صفحة عامة (بتعرض `SUPPORT_EMAIL`) — مطلوبة لبوابة شركاء زد |

## بوابة التاجر (`/portal/`)

1. **التسجيل:** `/portal/register/` (تسجيل عام) أو رابط دعوة من الستاف `/portal/enter/<key>/` مربوط بمتجر
   موجود. الاسم + رقم الجوال (بيتحوّل E.164 بمكتبة `phonenumbers`، البلد الافتراضي `LIGHTOTP_DEFAULT_REGION`)
   + كلمة سر (8 أحرف على الأقل). التسجيل العام محمي بـ honeypot، حد طلبات لكل IP، وسقف حسابات معلّقة
   (`PORTAL_MAX_PENDING_ACCOUNTS`). الستاف بيقدر يربط حساب معلّق بمتجر موجود من الداشبورد.
2. **التحقق:** `/portal/verify/` — كود من 6 أرقام على **واتساب عبر LightOTP**، صالح 10 دقائق، 5 محاولات،
   مع تهدئة متزايدة بين الإرسالات وسقف عام `PORTAL_OTP_MAX_SENDS_PER_HOUR`.
3. **الاشتراك:** `/portal/pending/` — حساب موثّق بلا متجر بيكتب اسم متجره، بيوصله **كود اشتراك** (8 خانات،
   صالح 30 دقيقة) على واتساب، وإدخاله مع الموافقة على السياسة بينشئ **متجر تجريبي** شهري برصيد
   `PORTAL_TRIAL_IMAGE_QUOTA` صورة ويفتح الداشبورد فوراً. سقف المتاجر التجريبية الجديدة
   `PORTAL_MAX_TRIAL_STORES_PER_DAY` بآخر 24 ساعة.
4. **الداشبورد:** `/portal/` — توكن المتجر وسطر التركيب
   (`<script src=".../static/apitokens/embed.js" data-store-key="...">`)، عدد المنتجات، مؤشرات المحادثات
   والتجارب، بطاقة الرصيد، وإعداد **حد التجارب لكل زبون** (1 / 3 / 5 / 10 / بلا حد) عبر `/portal/settings/`.
5. **الدخول:** `/portal/login/` — 5 محاولات فاشلة بتقفل الحساب 15 دقيقة، وفي حد محاولات لكل IP.

حدود الـ IP بالبوابة والودجت بتعتمد على `PORTAL_CLIENT_IP_HEADER` — خلف nginx لازم يكون `HTTP_X_REAL_IP`
(انظر [OPERATIONS](docs/OPERATIONS.md#nginx)).

## توزيع الرصيد

| الحد | المصدر | الوصف |
| --- | --- | --- |
| رصيد الفترة | `ApiToken.image_quota` + `period` | عدد صور التجربة بالفترة (يومي / أسبوعي / شهري) |
| لكل زبون | `ApiToken.tryons_per_shopper` (افتراضي 3) | تجارب ناجحة لكل `shopper_id` بنافذة 24 ساعة متحركة؛ 0 = بلا حد |
| لكل IP | حد الزبون × `TRYON_IP_LIMIT_MULTIPLIER` | احتياط لأن الـ IP ممكن يكون مشترك (بيتخزّن hash مملّح بس) |
| سقف يومي للمتجر | `max(QUOTA_DAILY_MIN, ceil(image_quota × QUOTA_DAILY_SHARE))` | حتى بوت ما يخلّص رصيد الفترة بساعة |
| حجز أثناء التوليد | `QUOTA_RESERVATION_TTL_SECONDS` | الحجز بينتهي لحاله لو انقطعت العملية |
| الشات | `CHAT_MAX_MESSAGES_PER_SHOPPER_DAY` | الشات ما بيستهلك من الرصيد، بس عليه حد عادل يومي |
| Rate limits | `RATE_LIMITS` بـ `settings.py` | نوافذ ثابتة لكل IP/توكن للـ tryon والـ chat والـ event والـ page-product |

**التنبيهات:** لما يوصل الاستهلاك 80% ولما يخلص (100%) بيتبعت للتاجر تنبيه واتساب (قالب Utility
`LIGHTOTP_QUOTA_ALERT_TEMPLATE_ID`، تنبيه واحد لكل عتبة بالفترة، لرقم الحساب الموثّق). بدون القالب التنبيه
بيظهر بالبوابة بس. بينبعت بعد التجربة مباشرة، و`send_quota_alerts` بالـ cron بيلحق أي تنبيه فات.

## التكاملات

| المنصة | طريقة الربط | تحديث الكتالوج | ملاحظات |
| --- | --- | --- | --- |
| **Shopify** | الستاف: صفحة "ربط Shopify" لكل متجر — تطبيق من Dev Dashboard (`dev.shopify.com`) بـ Client ID + Client secret (client credentials، توكن 24 ساعة بيتجدّد لحاله). المتاجر القديمة بـ Custom App قبل 2026 بتحط الـ Admin API access token الثابت | GraphQL Admin API `2026-07` + webhooks `PRODUCTS_CREATE/UPDATE/DELETE` و`ORDERS_CREATE` بتنسجّل تلقائياً على `/webhooks/shopify/<token>/` + `sync_shopify` | Scopes: `read_products` (إجباري)، `read_orders` (قياس الطلبات)، `read_legal_policies` (السياسات) |
| **سلة** | تطبيق Easy Mode: التاجر بيركّب التطبيق، سلة بتبعت التوكن بحدث `app.store.authorize` وبينعمل متجر لحاله برصيد `SALLA_DEFAULT_IMAGE_QUOTA` | webhook واحد `/webhooks/salla/` (موقّع بـ `SALLA_WEBHOOK_SECRET`) لأحداث `product.*` و`order.created` + `sync_salla` | الودجت بيدخل عبر App Snippet `static/apitokens/salla_snippet.js` |
| **زد** | OAuth: `/zid/install/` ← موافقة ← `/zid/callback/`، والتطبيق نفسه على `/zid/app/`. رصيد المتجر حسب باقته (`ZID_PLAN_QUOTAS`، وإلا `ZID_DEFAULT_IMAGE_QUOTA`) | webhook التطبيق `/webhooks/zid/app/` (هيدر `X-Control-Secret`) + webhooks المتجر `/webhooks/zid/store/<id>/<event>/` بتنسجّل تلقائياً (Basic Auth) + `sync_zid` | الودجت بيدخل عبر Custom Snippet `static/apitokens/zid_snippet.js`؛ التوكنات بتتجدّد بـ `refresh_zid_tokens` |
| **WooCommerce** | الستاف: صفحة "ربط WooCommerce" — رابط المتجر (https) + Consumer key/secret بصلاحية Read/Write | REST API v3 + webhooks `product.created/updated/deleted` و`order.created/updated` بتنسجّل تلقائياً على `/webhooks/woocommerce/<token>/` بسر خاص لكل متجر + `sync_woocommerce` | الودجت بيتركّب يدوياً: التاجر بيلصق سطر السكربت بموقعه |

خطوات الإعداد الكاملة لكل منصة (ببوابات الشركاء وبالداشبورد) بـ
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#إعداد-التكاملات).

**حماية الكتالوج:** المزامنة الكاملة ما بتحذف منتجات إذا الجلب ناقص، أو إذا رجع صفر، أو (لمتجر فيه 20 منتج
أو أكتر) أقل من نص المنتجات الموجودة — `services/sync_guard.py`. حذف منتج واحد بيوصل عبر الـ webhook عادي.

**حجم الكتالوج:** لحد 60 منتج المساعد بيشوفهم بكل التفاصيل، لحد 400 بشكل مختصر، وفوق هيك بيشوف الأقرب
دلالياً لطلب الزبون (embeddings `text-embedding-3-small` من OpenAI).

## التقنيات

Django 5 · Django REST Framework · PostgreSQL (أو SQLite محلياً) · gunicorn + nginx
· Anthropic / OpenAI / Gemini / Groq SDKs · kie.ai · LightOTP (واتساب) · `phonenumbers`

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

الداشبورد على `http://localhost:8000/` (تسجيل دخول Staff)، والبوابة على `/portal/`.
للتطوير بدون واتساب حقيقي: `PORTAL_OTP_BACKEND=console` مع `DJANGO_DEBUG=1` (الكود بينكتب باللوغ؛ مرفوض
لو `DEBUG` مطفي). لتجربة WooCommerce محلياً في بيئة Docker جاهزة بـ `dev/woocommerce-test/`.

## الاختبارات

```bash
DJANGO_SECRET_KEY=x python manage.py test --settings=config.settings_test
```

`config/settings_test.py` نفس إعدادات الإنتاج بس بقاعدة SQLite بالذاكرة، ومفاتيح LightOTP مفرّغة (ما في
أي إرسال حقيقي).

## متغيّرات البيئة (`.env`)

كلها بتنقرأ بـ `config/settings.py` (بيحمّل `.env` من جذر المشروع لحاله). "—" = فاضي افتراضياً.

| المتغيّر | الافتراضي | الوصف |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | `django-insecure-dev-only-change-me` | مفتاح جانغو السرّي — لازم يتغيّر بالإنتاج (كمان بيملّح hash الـ IP وأكواد الـ OTP) |
| `DJANGO_DEBUG` | `1` | `1` محلياً، `0` بالإنتاج (بيفعّل كوكيز secure وHSTS) |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | النطاقات المسموحة، مفصولة بفاصلة |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | الـ origins الموثوقة لـ CSRF |
| `DJANGO_HSTS_SECONDS` | `2592000` (30 يوم) | مدة HSTS، بس لما `DEBUG` مطفي |
| `SITE_BASE_URL` | `http://localhost:8000` | العنوان العام (https) — منه بتنبنى روابط الـ webhooks وروابط بوابات الشركاء |
| `POSTGRES_DB` | — | إذا فاضي → SQLite (`db.sqlite3`) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | — | مستخدم وكلمة سر Postgres |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `127.0.0.1` / `5432` | عنوان Postgres |
| `POSTGRES_CONN_MAX_AGE` | `60` | عمر الاتصال المستمر بالثواني |
| `LLM_PROVIDER` | `anthropic` | `anthropic` / `openai` / `gemini` / `groq` |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | — / `claude-sonnet-4-5-20250929` | مفتاح وموديل Anthropic |
| `OPENAI_API_KEY` | — | مفتاح OpenAI (للدردشة إذا المزوّد openai، وللـ embeddings بالكتالوجات الكبيرة) |
| `OPENAI_MODEL` | `gpt-4o` | موديل OpenAI (`.env.example` بيقترح `gpt-6-luna`) |
| `OPENAI_REASONING_EFFORT` | — | `low` / `medium` / `high` عبر Responses API؛ فاضي = ما بينبعت (لازم للموديلات القديمة متل `gpt-4o`) |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | — / `gemini-3.1-flash-lite` | مفتاح وموديل Gemini |
| `GROQ_API_KEY` / `GROQ_MODEL` | — / `llama-3.3-70b-versatile` | مفتاح وموديل Groq |
| `KIEAI_API_KEY` | — | مفتاح kie.ai لتوليد صور تجربة اللبس |
| `DEMO_STORE_KEY` | — | توكن المتجر اللي بيعرضه `/demo/`؛ فاضي = `/demo/` بيرجع 404 |
| `SALLA_APP_ID` / `SALLA_CLIENT_ID` / `SALLA_CLIENT_SECRET` | — | بيانات تطبيق سلة من بوابة الشركاء |
| `SALLA_WEBHOOK_SECRET` | — | سر التحقق من إشعارات سلة؛ فاضي = `/webhooks/salla/` بيرجع 503 |
| `SALLA_DEFAULT_IMAGE_QUOTA` | `20` | رصيد الصور لمتجر سلة الجديد |
| `ZID_CLIENT_ID` / `ZID_CLIENT_SECRET` | — | بيانات تطبيق زد (OAuth)؛ بدون `ZID_CLIENT_ID` → `/zid/install/` بيرجع 503 |
| `ZID_WEBHOOK_SECRET` | — | سر هيدر `X-Control-Secret` لإشعارات التطبيق، وكلمة سر Basic Auth لإشعارات المتاجر |
| `ZID_DEFAULT_IMAGE_QUOTA` | `20` | رصيد متجر زد إذا باقته مش بـ `ZID_PLAN_QUOTAS` |
| `ZID_PLAN_QUOTAS` | `{"Basic": 100, "Growth": 500, "Pro": 1000}` | JSON: اسم الباقة أو رقمها ← الرصيد الشهري |
| `SUPPORT_EMAIL` | — | إيميل الدعم بصفحة الخصوصية وبالبوابة |
| `PORTAL_CLIENT_IP_HEADER` | — (= `REMOTE_ADDR`) | هيدر IP الزبون الحقيقي؛ بالإنتاج `HTTP_X_REAL_IP` |
| `LIGHTOTP_API_KEY` | — | مفتاح LightOTP لإرسال أكواد واتساب |
| `LIGHTOTP_TEMPLATE_ID` | — | اختياري: قالب Utility لرسائل الخدمة (مش للأكواد) |
| `LIGHTOTP_DEFAULT_REGION` | `SA` | البلد الافتراضي للأرقام المحلية بدون `+` |
| `LIGHTOTP_QUOTA_ALERT_TEMPLATE_ID` | — | قالب Utility لتنبيه الرصيد (3 متغيّرات: اسم المتجر، النسبة، المتبقي) |
| `PORTAL_OTP_BACKEND` | `lightotp` | `lightotp` أو `console` (تطوير محلي بس) |
| `PORTAL_OTP_MAX_SENDS_PER_HOUR` | `120` | سقف عام لرسائل الـ OTP بالساعة |
| `PORTAL_MAX_PENDING_ACCOUNTS` | `500` | سقف الحسابات المعلّقة (بلا متجر) قبل رفض التسجيل الجديد |
| `PORTAL_TRIAL_IMAGE_QUOTA` | `20` | الرصيد الشهري للمتجر التجريبي من كود الاشتراك |
| `PORTAL_MAX_TRIAL_STORES_PER_DAY` | `30` | سقف المتاجر التجريبية الجديدة بآخر 24 ساعة |
| `TRYON_IP_LIMIT_MULTIPLIER` | `3` | حد الـ IP = هالمضاعف × حد الزبون |
| `QUOTA_DAILY_SHARE` / `QUOTA_DAILY_MIN` | `0.2` / `5` | السقف اليومي للمتجر (نسبة من رصيد الفترة، بحد أدنى) |
| `QUOTA_RESERVATION_TTL_SECONDS` | `180` | انتهاء حجز الرصيد أثناء التوليد |
| `CHAT_MAX_MESSAGES_PER_SHOPPER_DAY` | `150` | حد رسائل الشات لكل زبون باليوم |

## أوامر الإدارة

| الأمر | الوظيفة | بالإنتاج |
| --- | --- | --- |
| `sync_shopify` / `sync_salla` / `sync_zid` / `sync_woocommerce` | إعادة مزامنة المنتجات والمخزون لكل الربطات الفعّالة | كل ساعة (دقيقة 15) |
| `refresh_zid_tokens` | تجديد توكنات زد الأقدم من ~10 شهور قبل ما ينتهي الـ refresh token (سنة) | يومياً 03:40 |
| `send_quota_alerts` | إرسال تنبيهات الرصيد 80%/100% اللي لسا ما انبعتت | كل ساعة (دقيقة 25) |
| `purge_rate_buckets [--hours 24]` | حذف عدّادات الـ rate limit القديمة والحجوزات المنتهية | كل ساعة (دقيقة 35) |
| `check_otp_status` | سؤال LightOTP عن حالة تسليم رسائل الـ OTP بآخر 24 ساعة | كل 5 دقائق |
| `purge_unverified_accounts [--hours 24]` | حذف حسابات البوابة غير الموثّقة وبلا متجر الأقدم من N ساعة | يومياً 03:15 |
| `seed_demo_store [--skip-embeddings]` | تعبئة متجر الديمو (`DEMO_STORE_KEY`) بكتالوج عربي | يدوي |
| `eval_sales [--only a,b] [--model M] [--effort E] [--no-judge]` | تقييم آلي للمساعد بسيناريوهات بيع ثابتة ضد الموديل الحقيقي | يدوي، بعد أي تعديل على البرومبت أو الموديل |

جدول الـ cron الكامل بـ [`docs/OPERATIONS.md`](docs/OPERATIONS.md#الـ-cron).

## هيكل المشروع

```
apitokens/
  models.py               ApiToken, MerchantAccount, PhoneOtp, Product, Shopify/Salla/Zid/WooCommerce Connection,
                          ZidSubscription, DiscountCode, Conversation, TryOnEvent, QuotaReservation, …
  views.py                لوحة تحكم الستاف
  portal_auth.py          تسجيل/دخول البوابة، روابط الدعوة، حدود الـ IP
  portal_views.py         داشبورد التاجر، التحقق، صفحة الاشتراك، إعداد حد الزبون
  portal_otp.py           أكواد التحقق والاشتراك عبر LightOTP
  widget_views.py         نقاط API العامة للودجت
  api_views.py            /api/tokens/consume/ (n8n)
  demo_views.py           متجر الديمو /demo/
  zid_views.py            OAuth زد + صفحة التطبيق + /privacy/
  shopify_webhooks.py / salla_webhooks.py / zid_webhooks.py / woocommerce_webhooks.py
  services/
    llm.py  kieai.py  catalog.py  sync_guard.py  quota.py  quota_alerts.py  ratelimit.py
    lightotp.py  shopify.py  salla.py  zid.py  woocommerce.py  discounts.py  sizing.py  tryon_stats.py
  management/commands/    (انظر الجدول فوق)
  static/apitokens/       embed.js, demo.html, salla_snippet.js, zid_snippet.js
config/
  settings.py  settings_test.py  urls.py
dev/woocommerce-test/     بيئة WooCommerce محلية للتجربة (docker-compose)
docs/OPERATIONS.md        النشر والتشغيل
```

## النشر (ملخّص)

الإنتاج على `/opt/control-dashboard`: gunicorn (gthread) كخدمة systemd اسمها `control-dashboard`، خلف nginx
وCloudflare، مع Postgres. كل نشر: نسخة احتياطية ← نسخ الملفات المعدّلة ← `manage.py check` ← `migrate` ←
`collectstatic` ← `systemctl restart control-dashboard` ← smoke test. التفاصيل الكاملة بـ
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#النشر).
