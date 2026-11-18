# التشغيل والنشر — Control

مرجع تشغيلي للإنتاج. الملخّص والمكوّنات ومتغيّرات البيئة بـ [`README.md`](../README.md).

> لا تحط بهالملف (ولا بأي ملف بالريبو) أسرار حقيقية، عنوان IP السيرفر، أرقام جوالات، أو دومينات متاجر خاصة.

## بنية الإنتاج

| الجزء | القيمة |
| --- | --- |
| مسار الكود | `/opt/control-dashboard` (فيه `venv/` و`.env`) |
| خدمة التطبيق | systemd unit اسمها `control-dashboard` — gunicorn بـ worker class `gthread` |
| الـ proxy | nginx (بينهي TLS وبيمرّر لـ gunicorn) خلف Cloudflare |
| قاعدة البيانات | PostgreSQL (`POSTGRES_*` بـ `.env`) |
| الملفات الثابتة | `STATIC_ROOT = staticfiles/` عبر `collectstatic` |
| مستخدم الـ cron | `deploy` |
| اللوغات | `/var/log/control-dashboard/` |

ملاحظات من `config/settings.py`:

- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` و`USE_X_FORWARDED_HOST = True` — nginx لازم
  يمرّر `X-Forwarded-Proto` و`Host`.
- مع `DJANGO_DEBUG=0`: كوكيز `Secure` وHSTS (`DJANGO_HSTS_SECONDS`، بدون includeSubDomains).
- التحويل من http لـ https مسؤولية nginx، مش جانغو (`security.W008` مسكّت) — حتى الخدمات الداخلية اللي بتحكي
  مع gunicorn مباشرة (متل n8n) ما تاخد 301.
- تجربة اللبس بتاخد وقت (رفع صورة + انتظار kie.ai)، فالـ timeout تبع gunicorn وnginx لازم يكون طويل كفاية
  ليغطيها. ملف الـ unit مش موجود بالريبو — راجعه على السيرفر.

## nginx

حدود المحاولات بالبوابة والـ rate limits بالودجت بتعدّ حسب IP الزبون. خلف nginx الـ `REMOTE_ADDR` دائماً
`127.0.0.1`، فلازم:

```nginx
proxy_set_header X-Real-IP $remote_addr;       # بيكتب فوق أي قيمة جاية من الزبون
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header Host $host;
```

وبالـ `.env`:

```
PORTAL_CLIENT_IP_HEADER=HTTP_X_REAL_IP
```

الكود بياخد آخر عنصر بس من قيمة الهيدر (`portal_auth._client_ip`). بدون هالإعداد كل الزوار بيصيروا IP واحد
وبيوقفوا بعض.

## الـ cron

الملف `/etc/cron.d/control-dashboard-sync` (كل الأوامر بتشتغل كمستخدم `deploy` من `/opt/control-dashboard`
بـ `venv/bin/python manage.py ...`، واللوغات بـ `/var/log/control-dashboard/`):

| التوقيت | الأمر | ملاحظة |
| --- | --- | --- |
| `15 * * * *` | `sync_shopify; sync_salla; sync_zid; sync_woocommerce` | ورا بعض تحت `flock` (ما بيتداخل تشغيلين)، لوغ `sync.log` |
| `40 3 * * *` | `refresh_zid_tokens` | تجديد توكنات زد الأقدم من 300 يوم |
| `25 * * * *` | `send_quota_alerts` | تنبيهات الرصيد 80%/100% اللي فاتت |
| `35 * * * *` | `purge_rate_buckets` | تنظيف عدّادات الـ rate limit والحجوزات المنتهية |
| `*/5 * * * *` | `check_otp_status` | حالة تسليم رسائل الـ OTP من LightOTP |
| `15 3 * * *` | `purge_unverified_accounts` | حذف حسابات البوابة غير الموثّقة وبلا متجر (أقدم من 24 ساعة) |

شكل سطر نموذجي (عدّل أسماء ملفات اللوغ حسب الموجود على السيرفر):

```cron
15 * * * * deploy cd /opt/control-dashboard && flock -n /tmp/control-dashboard-sync.lock sh -c 'venv/bin/python manage.py sync_shopify; venv/bin/python manage.py sync_salla; venv/bin/python manage.py sync_zid; venv/bin/python manage.py sync_woocommerce' >> /var/log/control-dashboard/sync.log 2>&1
```

## النشر

النشر الحالي يدوي بنسخ الملفات المعدّلة (مش `git pull` على السيرفر). الخطوات:

1. **نسخة احتياطية** بمجلد بتوقيت النشر:
   ```bash
   TS=$(date +%Y%m%d-%H%M%S); B=/root/release-backup-$TS; mkdir -p "$B"
   sudo -u postgres pg_dump <db_name> > "$B/db.sql"          # أو بمستخدم قاعدة البيانات
   tar -czf "$B/code.tar.gz" -C /opt control-dashboard --exclude=venv --exclude=staticfiles
   cp /opt/control-dashboard/.env "$B/.env"
   ```
2. **نسخ الملفات المعدّلة** (أرشيف بالتغييرات بس) وفكّها فوق `/opt/control-dashboard` مع الحفاظ على المالك.
3. **الفحص والترحيل:**
   ```bash
   cd /opt/control-dashboard
   venv/bin/python manage.py check
   venv/bin/python manage.py migrate
   venv/bin/python manage.py collectstatic --noinput
   ```
   (`requirements.txt` تغيّر؟ → `venv/bin/pip install -r requirements.txt` قبل `check`.)
4. **إعادة التشغيل:** `systemctl restart control-dashboard` ثم `systemctl status control-dashboard`.
5. **Smoke test:** الداشبورد بيفتح، `/portal/login/` بترجع 200، `/static/apitokens/embed.js` بينزل، ومحادثة
   قصيرة بالودجت على متجر تجريبي.

**الرجوع:** فك `code.tar.gz` من مجلد النسخة، رجّع `.env`، واستعيد `db.sql` إذا كان في migration، ثم
`systemctl restart control-dashboard`.

أي تعديل على `embed.js` أو `demo.html` أو `salla_snippet.js` أو `zid_snippet.js` بدّه `collectstatic`. Cloudflare
ممكن يخزّن الملفات الثابتة بالكاش — امسح الكاش لهالملفات إذا التغيير ما بان.

## إعداد التكاملات

روابط الـ webhooks كلها مبنية من `SITE_BASE_URL`، فلازم يكون عنوان https عام صحيح قبل أي ربط.

### Shopify

من 2026 Shopify ما عاد بيسمح بإنشاء "Custom App" جديد من أدمن المتجر:

1. من `dev.shopify.com` (بنفس منظمة صاحب المتجر) أنشئ تطبيق.
2. فعّل الـ scopes: `read_products` (إجباري)، `read_orders` (لقياس الطلبات اللي جابها المساعد)،
   `read_legal_policies` (لسياسات الشحن والإرجاع).
3. Release للنسخة ثم Install app على المتجر.
4. بداشبورد الستاف ← المتجر ← "ربط Shopify": الدومين + **Client ID** + **Client secret**.

- التوكن بينجاب بـ client credentials (`/admin/oauth/access_token`)، صالح 24 ساعة وبيتجدّد لحاله. المتاجر
  القديمة اللي عندها Custom App قبل 2026 بتحط الـ Admin API access token الثابت بدل الـ Client ID.
- عند الربط بتنسجّل webhooks `PRODUCTS_CREATE|UPDATE|DELETE` و`ORDERS_CREATE` على
  `<SITE_BASE_URL>/webhooks/shopify/<token>/`، والتحقق بـ HMAC بالـ Client secret.
- GraphQL Admin API نسخة `2026-07`. منتجات draft/archived أو بدون مخزون ما بتوصل للمساعد.
- جوا Shopify الودجت بيضيف للسلة مباشرة وبيعلّم السلة بـ `_sw_conversation` لربط الطلب بالمحادثة.
- الودجت بيتركّب بسطر السكربت بثيم المتجر.

### سلة (Salla)

تطبيق من بوابة الشركاء `portal.salla.partners` بوضع **Easy Mode**:

1. حط `SALLA_APP_ID` و`SALLA_CLIENT_ID` و`SALLA_CLIENT_SECRET` و`SALLA_WEBHOOK_SECRET` بـ `.env`. صفحة
   "سلة" بداشبورد الستاف (`/salla/`) بتعرض أي وحدة ناقصة.
2. **Webhook URL** بالتطبيق: `<SITE_BASE_URL>/webhooks/salla/` (الصفحة بتعرضه جاهز للنسخ). استراتيجية
   التحقق: signature (HMAC-SHA256 بهيدر `X-Salla-Signature`) أو token (هيدر `Authorization` = السر نفسه) —
   الاتنين مدعومين بنفس `SALLA_WEBHOOK_SECRET`.
3. اشترك بأحداث المتجر: `product.created`، `product.updated`، `product.available`، `product.quantity.low`،
   `product.deleted`، `order.created` (وأحداث التطبيق `app.store.authorize` و`app.uninstalled`).
4. **App Snippet:** الصق محتوى `apitokens/static/apitokens/salla_snippet.js` (JS صافي، الصفحة بتعرضه).
   سلة بتحطه بكل صفحات المتاجر المركّبة؛ بيقرأ `store.id` وبيسأل `/widget/salla-key/<store_id>/` عن مفتاح
   الودجت وبيحمّل `embed.js`.

لما التاجر يركّب التطبيق، حدث `app.store.authorize` بينشئ متجر جديد برصيد `SALLA_DEFAULT_IMAGE_QUOTA`
وبيبلّش أول مزامنة بالخلفية. `app.uninstalled` بيوقف الربط. الستاف بيقدر يعمل resync من صفحة سلة.

> انتبه: `API_BASE` مكتوب ثابت جوا `salla_snippet.js` و`zid_snippet.js` (مش من `SITE_BASE_URL`). أي بيئة
> تانية (staging) لازم تعدّل الملف.

### زد (Zid)

تطبيق من `partner.zid.sa` ← My Apps:

1. من General settings ← API keys: `ZID_CLIENT_ID` و`ZID_CLIENT_SECRET`. ولّد `ZID_WEBHOOK_SECRET` عشوائي.
2. صفحة "زد" بداشبورد الستاف (`/zid/`) بتعرض القيم للصقها ببوابة الشركاء:

   | الحقل | القيمة |
   | --- | --- |
   | Application URL | `<SITE_BASE_URL>/zid/app/` |
   | Redirection URL | `<SITE_BASE_URL>/zid/install/` |
   | Callback URL | `<SITE_BASE_URL>/zid/callback/` |
   | Privacy Policy URL | `<SITE_BASE_URL>/privacy/` |
   | Webhook URL | `<SITE_BASE_URL>/webhooks/zid/app/` مع هيدر مخصّص `X-Control-Secret: <ZID_WEBHOOK_SECRET>` |

3. **Custom Snippets:** الصق `apitokens/static/apitokens/zid_snippet.js` جوا `<script>…</script>` (الصفحة
   بتعرضه ملفوف). بيبعت دومين المتجر لـ `/widget/zid-key/?host=...` وبيحمّل `embed.js`.
4. `ZID_PLAN_QUOTAS` (JSON بسطر واحد): اسم الباقة أو رقمها ← الرصيد الشهري. الباقة اللي مش موجودة بتاخد
   `ZID_DEFAULT_IMAGE_QUOTA`.

التدفق: التاجر بيضغط "تفعيل" ← `/zid/install/` (OAuth مع `state`) ← `/zid/callback/` بينشئ المتجر والربط،
بيطبّق الباقة، وبيبلّش بالخلفية أول مزامنة وتسجيل webhooks المتجر (`product.create|update|publish|delete`،
`order.create`) على `/webhooks/zid/store/<store_id>/<event>/` بـ Basic Auth (`control` : `ZID_WEBHOOK_SECRET`).
أحداث `app.market.subscription.*` بتحدّث الرصيد، و`app.market.application.uninstall` بيوقف المتجر.
الـ refresh token صالح سنة، و`refresh_zid_tokens` بيجدّد كل توكن عمره أكتر من 300 يوم.

### WooCommerce

1. بلوحة ووردبريس: `WooCommerce ← الإعدادات ← متقدم ← REST API ← إضافة مفتاح` بصلاحية **Read/Write** (الكتابة
   بس لتسجيل الـ webhooks؛ بدونها الكتالوج بيتزامن بس التحديث اللحظي وقياس الطلبات بيوقفوا مع رسالة تحذير).
2. بداشبورد الستاف ← المتجر ← "ربط WooCommerce": رابط المتجر (لازم `https`) + Consumer key + Consumer secret.
3. شروط: Permalinks مش على Plain، وجدار الحماية (Cloudflare/Wordfence) ما يحجب سيرفرنا.
4. الودجت **يدوي**: التاجر بيلصق سطر `<script src=".../embed.js" data-store-key="...">` بموقعه.

- REST API v3 (`/wp-json/wc/v3/`). webhooks `product.created|updated|deleted` و`order.created|updated` على
  `<SITE_BASE_URL>/webhooks/woocommerce/<token>/`، موقّعة بسر بيولّده الداشبورد لكل متجر (مش مفتاح التاجر).
- بس المنتجات المنشورة والظاهرة من نوع simple وvariable. ما في سياسات شحن/إرجاع تلقائية — التاجر بيكتبها
  بخانة "معلومات المتجر".
- **ربط الطلبات بالمحادثات تقريبي:** الطلب (processing/completed/on-hold) بينربط بأحدث محادثة ضغط زبونها
  "أضف للسلة" أو "اشتري" على منتج من الطلب خلال 24 ساعة (`acted_skus`).
- بيئة تجربة محلية: `dev/woocommerce-test/` (docker-compose + `setup.sh`).

## LightOTP (واتساب)

- `LIGHTOTP_API_KEY` من لوحة LightOTP (API Keys). أكواد التحقق والاشتراك بتروح بقالب LightOTP نفسه.
- `LIGHTOTP_QUOTA_ALERT_TEMPLATE_ID`: قالب Utility معتمد بـ 3 متغيّرات body (اسم المتجر، النسبة، المتبقي).
- `check_otp_status` كل 5 دقائق بيحدّث حالة التسليم للرسائل بآخر 24 ساعة.
- `PORTAL_OTP_MAX_SENDS_PER_HOUR` و`PORTAL_MAX_TRIAL_STORES_PER_DAY` بيحموا الرصيد من الإساءة.

## الاختبارات قبل النشر

```bash
DJANGO_SECRET_KEY=x python manage.py test --settings=config.settings_test
```

بتشتغل على SQLite بالذاكرة وما بتلمس `.env` الحقيقي ولا LightOTP.
