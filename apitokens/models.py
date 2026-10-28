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
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="api_tokens",
        verbose_name="أنشأه",
    )
    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)

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
    order_id = models.CharField(max_length=64, blank=True, default="")
    order_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    order_currency = models.CharField(max_length=8, blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
