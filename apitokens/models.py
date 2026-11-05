import hashlib
import re
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


class ApiToken(models.Model):
    """توكن خارجي لعميل بيستخدم الـ webhook (مثلاً n8n/kie.ai) — مش مرتبط
    بحساب مستخدم، بس وسيلة تعريف + سقف استهلاك دوري.
    """

    class Period(models.TextChoices):
        DAILY = "daily", "يومياً"
        WEEKLY = "weekly", "أسبوعياً"
        MONTHLY = "monthly", "شهرياً"

    PERIOD_DAYS = {
        Period.DAILY: 1,
        Period.WEEKLY: 7,
        Period.MONTHLY: 30,
    }

    label = models.CharField(
        "الاسم",
        max_length=120,
        help_text="للتعريف بس (مثلاً اسم العميل أو الجهة) — مش مرتبط بحساب",
    )
    token = models.CharField("التوكن", max_length=64, unique=True, editable=False)
    image_quota = models.PositiveIntegerField("عدد الصور المسموح بها كل فترة")
    period = models.CharField(
        "الفترة", max_length=10, choices=Period.choices, default=Period.MONTHLY,
    )
    images_used = models.PositiveIntegerField("المستهلك بالفترة الحالية", default=0)
    period_start = models.DateTimeField("بداية الفترة الحالية", default=timezone.now)
    is_active = models.BooleanField("فعّال", default=True)
    currency = models.CharField(
        "العملة", max_length=8, blank=True, default="",
        help_text="رمز العملة (مثلاً USD أو SAR) — بيتعبّى لحاله من Shopify",
    )
    store_info = models.TextField(
        "معلومات المتجر للمساعد",
        blank=True,
        help_text=(
            "الشحن، الإرجاع والاستبدال، دليل المقاسات، طرق الدفع... — المساعد بيعتمد عليها "
            "بالرد على الزبون. لو المتجر مربوط بـ Shopify بتتعبّى لحالها من سياسات المتجر."
        ),
    )
    # مفتاح الاستهلاك (n8n): سرّي، منفصل عن `token` يلي هو مفتاح الودجت العلني. بنخزّن
    # الـ hash بس — النص الأصلي بيظهر مرة وحدة عند التوليد/التدوير.
    consume_key_hash = models.CharField(
        max_length=64, unique=True, null=True, blank=True, editable=False,
    )
    consume_key_prefix = models.CharField(max_length=12, blank=True, default="", editable=False)
    consume_key_rotated_at = models.DateTimeField(null=True, blank=True, editable=False)
    # قبول التوكن العلني القديم على endpoint الاستهلاك — انتقالي لحد ما n8n يتحوّل لمفتاح
    # الاستهلاك. التجار الموجودين قبل الفصل = True (حتى ما ينكسر n8n)، والجدد = False.
    legacy_consume_enabled = models.BooleanField(default=False)
    legacy_consume_last_used_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="api_tokens",
        verbose_name="أنشأه",
    )
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)

    class Engine(models.TextChoices):
        V1 = "v1", "v1 (قديم)"
        V2 = "v2", "v2 (ذاكرة دائمة)"

    # أي محرّك دردشة بيخدم هالتاجر: v1 = الحالي (التاريخ بالمتصفح)، v2 = ذاكرة بالسيرفر.
    engine = models.CharField(max_length=4, choices=Engine.choices, default=Engine.V1)
    # كم يوم بنحتفظ بذاكرة زبون ما رجع (بيتجدد بكل زيارة). بعدها بتنمسح.
    profile_retention_days = models.PositiveIntegerField(default=90)

    CONSUME_KEY_PREFIX = "ck_"

    class Meta:
        verbose_name = "توكن API"
        verbose_name_plural = "توكنات API"
        ordering = ["-created_at"]

    def __str__(self):
        return self.label

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = self._generate_unique_token()
        super().save(*args, **kwargs)

    @staticmethod
    def _generate_unique_token():
        while True:
            candidate = secrets.token_urlsafe(32)
            if not ApiToken.objects.filter(token=candidate).exists():
                return candidate

    @staticmethod
    def hash_consume_key(key):
        return hashlib.sha256((key or "").encode("utf-8")).hexdigest()

    def rotate_consume_key(self):
        """بيولّد مفتاح استهلاك جديد وبيبطل القديم فوراً. بيرجّع النص الأصلي
        (المرة الوحيدة اللي بيتوفر فيها)."""
        key = self.CONSUME_KEY_PREFIX + secrets.token_urlsafe(32)
        self.consume_key_hash = self.hash_consume_key(key)
        self.consume_key_prefix = key[:8]
        self.consume_key_rotated_at = timezone.now()
        self.save(update_fields=["consume_key_hash", "consume_key_prefix", "consume_key_rotated_at"])
        return key

    @property
    def period_days(self):
        return self.PERIOD_DAYS[self.period]

    def consume_quota(self, count=1):
        """بتخصم `count` صورة من رصيد الفترة الحالية (وبتصفّره إذا الفترة
        خلصت). لازم تُستدعى جوا select_for_update() حتى ما يصير تسابق.
        برجّع dict فيه ok/error أو ok/remaining.
        """
        from datetime import timedelta

        period_end = self.period_start + timedelta(days=self.period_days)
        if timezone.now() >= period_end:
            self.images_used = 0
            self.period_start = timezone.now()

        if self.images_used + count > self.image_quota:
            self.save(update_fields=["images_used", "period_start"])
            return {
                "ok": False,
                "error": "quota_exceeded",
                "remaining": max(self.image_quota - self.images_used, 0),
            }

        self.images_used += count
        self.save(update_fields=["images_used", "period_start"])
        return {
            "ok": True,
            "remaining": self.image_quota - self.images_used,
            "quota": self.image_quota,
            "period": self.period,
        }


# أشياء بتنلبس أو بتنحمل على الجسم — بس هي بتنفع معها تجربة اللبس بالصورة.
# (سنوبورد، شمع تزلج، بطاقة هدية... ما إلها معنى نجربها على صورة الزبون.)
WEARABLE_PATTERN = re.compile(
    r"\b(dress(es)?|gown|frock|shirts?|t-?shirts?|tee|tops?|blouses?|jackets?|coats?|hoodies?|"
    r"sweaters?|sweatshirts?|jumpers?|cardigans?|pants|trousers|jeans|shorts|skirts?|suits?|blazers?|"
    r"vests?|abayas?|thobes?|kaftans?|caftans?|jumpsuits?|corsets?|lingerie|swimwear|swimsuits?|"
    r"bikinis?|polos?|kimonos?|robes?|pajamas?|leggings|clothing|apparel|outfits?|"
    r"hats?|caps?|beanies?|scarf|scarves|hijabs?|gloves|sunglasses|glasses|eyewear|"
    r"bags?|handbags?|clutch(es)?|purses?|backpacks?|totes?|shoes?|sneakers?|boots?|heels|sandals?|"
    r"loafers?|watch(es)?|necklaces?|earrings?|bracelets?|jewel(le)?ry|belts?|ties?)\b"
    r"|فستان|فساتين|قميص|قمصان|تيشيرت|بلوزة|جاكيت|جاكيتات|معطف|جاكيت|هودي|كنزة|بنطلون|بنطال|جينز|"
    r"شورت|تنورة|بدلة|عباية|عبايات|ثوب|قفطان|جلابية|بيجامة|حجاب|شال|طاقية|قبعة|نظارة|شنطة|حقيبة|"
    r"حذاء|أحذية|جزمة|صندل|ساعة|عقد|حلق|اسوارة|أسوارة|إكسسوار|ملابس|لبس",
    re.IGNORECASE,
)


class Product(models.Model):
    """منتج واحد من كتالوج التاجر (ApiToken). لو التاجر بيرفع CSV يدوي، رفع
    ملف جديد بيعوّض كل منتجاته. لو مربوط بـ Shopify (انظر ShopifyConnection)،
    المنتجات بتتحدّث لحالها عبر webhooks وبيتعرّف عليها بـ external_id.
    """

    merchant = models.ForeignKey(
        ApiToken, on_delete=models.CASCADE, related_name="products",
    )
    external_id = models.CharField(
        "معرّف خارجي (Shopify)",
        max_length=64,
        blank=True,
        default="",
        help_text="معرّف المنتج بمصدره الخارجي (مثلاً Shopify) — فاضي لمنتجات CSV اليدوية",
    )
    sku = models.CharField("SKU", max_length=64)
    title = models.CharField("الاسم", max_length=255)
    description = models.TextField("الوصف", blank=True)
    price = models.DecimalField("السعر", max_digits=10, decimal_places=2)
    image_url = models.URLField("رابط الصورة", max_length=500)
    product_url = models.URLField("رابط المنتج", max_length=500, blank=True)
    category = models.CharField("الفئة", max_length=120, blank=True)
    tags = models.CharField("الوسوم", max_length=500, blank=True)
    compare_at_price = models.DecimalField(
        "السعر قبل التخفيض", max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="سعر أعلى حقيقي من المتجر — لو موجود، المنتج عليه تخفيض فعلي",
    )
    # {"Size": ["S", "M"], "Color": ["Black"]} — بس القيم المتوفرة فعلياً
    options = models.JSONField("الخيارات المتوفرة", default=dict, blank=True)
    in_stock = models.BooleanField("متوفر", default=True)
    variant_id = models.CharField(
        "معرّف النسخة (Shopify)", max_length=64, blank=True, default="",
        help_text="أول نسخة متوفرة — بتستخدم لإضافة المنتج للسلة مباشرة من الشات",
    )
    # متجه دلالي (embedding) لاختيار المنتجات الأقرب لطلب الزبون بالكتالوجات الكبيرة
    embedding = models.JSONField(null=True, blank=True, editable=False)
    embedding_source = models.CharField(max_length=64, blank=True, default="", editable=False)
    created_at = models.DateTimeField("تاريخ الإضافة", auto_now_add=True)

    class Meta:
        verbose_name = "منتج"
        verbose_name_plural = "منتجات الكتالوج"
        ordering = ["category", "title"]

    def __str__(self):
        return f"{self.title} ({self.merchant.label})"

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def supports_tryon(self):
        """هل بينفع الزبون يجرّب هالمنتج على صورته؟ (ملابس وإكسسوارات بس)"""
        return bool(WEARABLE_PATTERN.search(f"{self.title} {self.category} {self.tags}"))


class SallaConnection(models.Model):
    """ربط تاجر (ApiToken) بمتجر سلة. بينعمل لحاله لما التاجر يركّب تطبيقنا من
    متجر تطبيقات سلة — سلة بتبعت التوكن بإشعار app.store.authorize.

    refresh_token بينستخدم مرة وحدة بس: استخدامه مرتين بيلغي الربط والتاجر
    لازم يعيد التركيب — لهيك التجديد دايماً جوا قفل على هالسطر.
    """

    merchant = models.OneToOneField(
        ApiToken, on_delete=models.CASCADE, related_name="salla_connection",
    )
    salla_merchant_id = models.CharField("رقم التاجر بسلة", max_length=32, unique=True)
    store_name = models.CharField("اسم المتجر", max_length=255, blank=True, default="")
    store_domain = models.CharField("رابط المتجر", max_length=255, blank=True, default="")
    access_token = models.TextField(blank=True, default="")
    refresh_token = models.TextField(blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.CharField(max_length=500, blank=True, default="")
    is_active = models.BooleanField("فعّال", default=True)
    last_synced_at = models.DateTimeField("آخر مزامنة", null=True, blank=True)
    created_at = models.DateTimeField("تاريخ الربط", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "ربط سلة"
        verbose_name_plural = "ربط سلة"

    def __str__(self):
        return f"{self.store_name or self.salla_merchant_id} ({self.merchant.label})"


class ZidConnection(models.Model):
    """ربط تاجر (ApiToken) بمتجر زد. بينعمل لحاله بعد موافقة OAuth لما التاجر
    يفعّل تطبيقنا من متجر تطبيقات زد.

    زد بتعطي مفتاحين لازم ينبعتوا مع بعض بكل طلب: authorization (Bearer) و
    manager token. الـ refresh بيعيش سنة بس (أقصر من المفاتيح بسنتين) — لازم
    نجدّد على جدول، مش لما يفشل طلب.
    """

    merchant = models.OneToOneField(
        ApiToken, on_delete=models.CASCADE, related_name="zid_connection",
    )
    store_id = models.CharField("رقم المتجر بزد", max_length=32, unique=True)
    store_uuid = models.CharField(max_length=64, blank=True, default="", db_index=True)
    store_name = models.CharField("اسم المتجر", max_length=255, blank=True, default="")
    store_url = models.CharField("رابط المتجر", max_length=255, blank=True, default="")
    authorization_token = models.TextField(blank=True, default="")
    manager_token = models.TextField(blank=True, default="")
    refresh_token = models.TextField(blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    tokens_refreshed_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField("فعّال", default=True)
    last_synced_at = models.DateTimeField("آخر مزامنة", null=True, blank=True)
    created_at = models.DateTimeField("تاريخ الربط", auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "ربط زد"
        verbose_name_plural = "ربط زد"

    def __str__(self):
        return f"{self.store_name or self.store_id} ({self.merchant.label})"


class ZidSubscription(models.Model):
    """آخر حالة لاشتراك متجر بباقاتنا على زد. منفصل عن الربط لأنه زد بتقبض
    وبتبعت app.market.subscription.active قبل ما التاجر يكمّل OAuth — يعني
    الاشتراك ممكن يوصل قبل ما يكون عنا ربط للمتجر."""

    store_id = models.CharField(max_length=32, unique=True)
    store_uuid = models.CharField(max_length=64, blank=True, default="", db_index=True)
    plan_id = models.CharField(max_length=32, blank=True, default="")
    plan_name = models.CharField("الباقة", max_length=120, blank=True, default="")
    plan_type = models.CharField(max_length=32, blank=True, default="")
    is_active = models.BooleanField("الاشتراك فعّال", default=False)
    end_date = models.DateTimeField(null=True, blank=True)
    last_event = models.CharField(max_length=80, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "اشتراك زد"
        verbose_name_plural = "اشتراكات زد"

    def __str__(self):
        return f"{self.store_id} — {self.plan_name or '-'}"


class ShopifyConnection(models.Model):
    """ربط حي بين تاجر (ApiToken) ومتجر Shopify حقيقي عبر تطبيق خاص
    (Custom App). الكتالوج بيتزامن أول مرة بالكامل، وبعدها بيتحدّث لحاله
    عبر webhooks (products/create|update|delete).
    """

    merchant = models.OneToOneField(
        ApiToken, on_delete=models.CASCADE, related_name="shopify_connection",
    )
    shop_domain = models.CharField(
        "نطاق متجر Shopify", max_length=255,
        help_text="مثال: my-store.myshopify.com",
    )
    client_id = models.CharField(
        "Client ID", max_length=255, blank=True, default="",
        help_text="لتطبيقات Dev Dashboard (من 2026) — المفتاح بيتجدّد لحاله",
    )
    access_token = models.CharField("Admin API Access Token", max_length=255, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    primary_domain = models.CharField(
        "النطاق الأساسي للمتجر", max_length=255, blank=True, default="",
        help_text="الدومين اللي بيشوفه الزبون (مش myshopify) — لروابط المنتجات",
    )
    api_secret = models.CharField(
        "API Secret", max_length=255,
        help_text="من إعدادات التطبيق الخاص بـ Shopify — بيستخدم للتحقق من الـ webhooks",
    )
    is_active = models.BooleanField("فعّال", default=True)
    last_synced_at = models.DateTimeField("آخر مزامنة", null=True, blank=True)
    created_at = models.DateTimeField("تاريخ الربط", auto_now_add=True)

    class Meta:
        verbose_name = "ربط Shopify"
        verbose_name_plural = "ربط Shopify"

    def __str__(self):
        return f"{self.shop_domain} ({self.merchant.label})"


class DiscountCode(models.Model):
    """كود خصم يقدر المساعد الذكي يعرضه على زبون حاسم إنه رح يشتري —
    إدارته يدوية بالداشبورد، وصلاحيته الفعلية (نسبة الخصم، تاريخ الانتهاء...)
    محسوبة ومفعّلة عند التاجر بمتجره الحقيقي، احنا بس منعرضه بالمحادثة.
    """

    merchant = models.ForeignKey(
        ApiToken, on_delete=models.CASCADE, related_name="discount_codes",
    )
    code = models.CharField("الكود", max_length=64)
    description = models.CharField(
        "الوصف", max_length=255, blank=True,
        help_text="مثلاً: خصم 10% على كل المنتجات",
    )
    is_active = models.BooleanField("فعّال", default=True)
    # فترة صلاحية اختيارية — فاضي = بلا حد. السياسة (policy.valid_codes) بتتجاهل الكود خارج الفترة.
    starts_at = models.DateTimeField("يبدأ من", null=True, blank=True)
    expires_at = models.DateTimeField("ينتهي في", null=True, blank=True)
    created_at = models.DateTimeField("تاريخ الإضافة", auto_now_add=True)

    class Meta:
        verbose_name = "كود خصم"
        verbose_name_plural = "أكواد الخصم"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} ({self.merchant.label})"


class Conversation(models.Model):
    """محادثة وحدة بالودجت — منسجّلها حتى التاجر يشوف شو صار، وحتى نقيس
    أداء المساعد بالبيع (عرض منتجات، إضافة للسلة، طلبات فعلية من Shopify).
    """

    merchant = models.ForeignKey(
        ApiToken, on_delete=models.CASCADE, related_name="conversations",
    )
    session_id = models.CharField(max_length=64, db_index=True)
    messages = models.JSONField(default=list)
    recommended_skus = models.JSONField(default=list)
    discount_code = models.CharField(max_length=64, blank=True, default="")
    tryons = models.PositiveIntegerField(default=0)
    buy_clicks = models.PositiveIntegerField(default=0)
    add_to_carts = models.PositiveIntegerField(default=0)
    # رقم سلة الزبون بالمنصة (سلة) — منه منعرف أي طلب إجا من هالمحادثة.
    platform_cart_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    order_id = models.CharField(max_length=64, blank=True, default="")
    order_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    order_currency = models.CharField(max_length=8, blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # --- v2: ذاكرة بالسيرفر ---
    profile = models.ForeignKey(
        "CustomerProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="conversations",
    )
    engine_version = models.PositiveSmallIntegerField(default=1)
    turn_count = models.PositiveIntegerField(default=0)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    # حالة صغيرة: مواضيع انسأل عنها، تنبيهات توضيح معلّقة...
    state = models.JSONField(default=dict, blank=True)
    # قفل دور (turn) بدون transaction طويلة — بيمنع رسالتين متزامنتين بنفس المحادثة.
    processing_since = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "محادثة"
        verbose_name_plural = "المحادثات"
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "session_id"], name="unique_conversation_per_session",
            )
        ]

    def __str__(self):
        return f"{self.merchant.label} — {self.session_id[:8]}"

    @property
    def converted(self):
        return bool(self.order_id)

    @property
    def transcript(self):
        """نص المحادثة للداشبورد: v2 من جدول Message، v1 من الـ JSON القديم."""
        if self.engine_version >= 2:
            return [
                {"role": m.role, "content": m.content}
                for m in self.chat_messages.exclude(role="event").order_by("seq")
            ]
        return self.messages


class SizeChart(models.Model):
    """جدول مقاسات التاجر. category فاضية = الجدول العام لكل المنتجات، وإلا
    بيخص الفئة (مثلاً "فساتين") وبيتقدّم على العام."""

    merchant = models.ForeignKey(ApiToken, on_delete=models.CASCADE, related_name="size_charts")
    category = models.CharField("الفئة", max_length=120, blank=True, default="")
    chart = models.JSONField("المقاسات", default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "جدول مقاسات"
        verbose_name_plural = "جداول المقاسات"
        constraints = [
            models.UniqueConstraint(fields=["merchant", "category"], name="unique_size_chart_per_category"),
        ]

    def __str__(self):
        return f"{self.merchant.label} — {self.category or '*'}"


class RateLimitBucket(models.Model):
    """عدّاد نافذة واحدة لقاعدة تحديد معدّل (services/ratelimit.py). بنستخدم
    قاعدة البيانات يلي كل نشر عندو أصلاً كمخزن مشترك بين الـ workers والسيرفرات،
    بدل ما نضيف Redis. المفتاح بيحتوي رقم النافذة فبتنتهي الصفوف القديمة لحالها."""

    key = models.CharField(max_length=190, unique=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = "عدّاد معدّل"
        verbose_name_plural = "عدّادات المعدّل"

    def __str__(self):
        return f"{self.key} = {self.count}"


# ======================================================================
# v2: ذاكرة الزبون بالسيرفر (كل الجداول مربوطة بالتاجر ومعزولة بينهم)
# ======================================================================


def _new_public_id():
    return secrets.token_hex(16)


class CustomerProfile(models.Model):
    """زبون واحد عند تاجر واحد، عبر الزيارات. الهوية بتجي من توكن موقّع بالسيرفر
    (services/session_token.py) — مش من قيمة بيختارها المتصفح. نفس الشخص عند تاجرين
    = ملفين منفصلين بدون أي ربط."""

    merchant = models.ForeignKey(ApiToken, on_delete=models.CASCADE, related_name="customer_profiles")
    public_id = models.CharField(max_length=32, unique=True, default=_new_public_id, editable=False)
    language = models.CharField(max_length=12, blank=True, default="")
    gender_signal = models.CharField(max_length=8, blank=True, default="")  # "" | female | male
    visit_count = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = "ملف زبون"
        verbose_name_plural = "ملفات الزبائن"
        indexes = [models.Index(fields=["merchant", "expires_at"])]

    def __str__(self):
        return f"{self.merchant.label} — {self.public_id[:8]}"


class Message(models.Model):
    """رسالة بمحادثة v2 — سجل مضاف فقط (append-only). الـ meta بيحمل الكروت والعرض
    المرسلين مع رد المساعد، حتى نعيد رسم المحادثة بعد تنقل الصفحة بنفس الشكل."""

    class Role(models.TextChoices):
        USER = "user", "زبون"
        ASSISTANT = "assistant", "مساعد"
        EVENT = "event", "حدث"  # تجربة لبس، فتح صفحة منتج... (ما بتنعرض كرسالة)

    merchant = models.ForeignKey(ApiToken, on_delete=models.CASCADE, related_name="+")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="chat_messages")
    seq = models.PositiveIntegerField()
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField()
    meta = models.JSONField(default=dict, blank=True)
    client_msg_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["conversation_id", "seq"]
        constraints = [
            models.UniqueConstraint(fields=["conversation", "seq"], name="unique_message_seq"),
            models.UniqueConstraint(
                fields=["conversation", "client_msg_id"],
                condition=~models.Q(client_msg_id=""),
                name="unique_client_msg_id",
            ),
        ]


class PreferenceFact(models.Model):
    """معلومة عن تفضيلات الزبون مع مصدرها. explicit = قالها بكلامه (والاقتباس
    متحقق منه بالكود)، inferred = استنتاج (ما بنعتبره حقيقة ولا بنفلتر فيه)."""

    class Source(models.TextChoices):
        EXPLICIT = "explicit", "صريح"
        INFERRED = "inferred", "مستنتج"

    class Status(models.TextChoices):
        ACTIVE = "active", "فعّال"
        SUPERSEDED = "superseded", "استُبدل"
        RETRACTED = "retracted", "تراجع عنه الزبون"

    merchant = models.ForeignKey(ApiToken, on_delete=models.CASCADE, related_name="+")
    profile = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="facts")
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    subject = models.CharField(max_length=8, default="self")  # self | gift
    key = models.CharField(max_length=24)
    value = models.CharField(max_length=120)
    polarity = models.CharField(max_length=5, default="like")  # like | avoid
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.EXPLICIT)
    confidence = models.FloatField(default=1.0)
    evidence = models.CharField(max_length=300, blank=True, default="")
    learned_turn = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    superseded_by = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["profile", "status", "key"])]


class ProductReaction(models.Model):
    """كيف تفاعل الزبون مع منتج. latest non-shown reaction هو اللي بيحدد: rejected =
    ما نرشّحه تاني إلا إذا الزبون طلبه صراحة."""

    class Kind(models.TextChoices):
        SHOWN = "shown", "انعرض"
        VIEWED = "viewed", "فتح صفحته"
        ASKED = "asked", "سأل عنه"
        LIKED = "liked", "عجبه"
        NEUTRAL = "neutral", "محايد"
        REJECTED = "rejected", "رفضه"
        TRIED_ON = "tried_on", "جرّبه"
        BUY_CLICK = "buy_click", "ضغط اشتري"
        ADD_TO_CART = "add_to_cart", "أضافه للسلة"

    merchant = models.ForeignKey(ApiToken, on_delete=models.CASCADE, related_name="+")
    profile = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="reactions")
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    sku = models.CharField(max_length=64)
    title = models.CharField(max_length=255, blank=True, default="")
    reaction = models.CharField(max_length=12, choices=Kind.choices)
    reason_type = models.CharField(max_length=12, blank=True, default="")  # price|style|color|fit|quality|other|unknown
    reason_text = models.CharField(max_length=200, blank=True, default="")
    price_at_time = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    turn = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["profile", "reaction"]),
            models.Index(fields=["conversation", "sku"]),
        ]


class Objection(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "مفتوح"
        RESOLVED = "resolved", "انحلّ"

    merchant = models.ForeignKey(ApiToken, on_delete=models.CASCADE, related_name="+")
    profile = models.ForeignKey(CustomerProfile, on_delete=models.CASCADE, related_name="objections")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="objections")
    type = models.CharField(max_length=16)  # price|competitor_price|trust|quality|fit|shipping|timing|other
    sku = models.CharField(max_length=64, blank=True, default="")
    text = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    raised_turn = models.PositiveIntegerField(default=0)
    resolved_turn = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class TurnTrace(models.Model):
    """سجل قرارات كل دور: شو فهم المحلّل، شو تغيّر بالذاكرة، قيود، تدقيق المخرجات،
    وكم استدعاء LLM — حتى نقدر نفسّر أي رد (ولماذا انعرض/ما انعرض شي)."""

    merchant = models.ForeignKey(ApiToken, on_delete=models.CASCADE, related_name="+")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="traces")
    turn = models.PositiveIntegerField()
    engine = models.CharField(max_length=4, default="v2")
    analyzer = models.JSONField(default=dict, blank=True)
    memory_changes = models.JSONField(default=dict, blank=True)
    constraints = models.JSONField(default=dict, blank=True)
    guard = models.JSONField(default=dict, blank=True)
    llm_calls = models.PositiveSmallIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    models_used = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["merchant", "created_at"])]
