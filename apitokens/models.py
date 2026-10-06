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


class Product(models.Model):
    """منتج واحد من كتالوج التاجر (ApiToken). رفع ملف جديد بيعوّض كل
    منتجات هاد التاجر — مافي مزامنة حية، نسخة واحدة محدّثة بس.
    """

    merchant = models.ForeignKey(
        ApiToken, on_delete=models.CASCADE, related_name="products",
    )
    sku = models.CharField("SKU", max_length=64)
    title = models.CharField("الاسم", max_length=255)
    description = models.TextField("الوصف", blank=True)
    price = models.DecimalField("السعر", max_digits=10, decimal_places=2)
    image_url = models.URLField("رابط الصورة", max_length=500)
    product_url = models.URLField("رابط المنتج", max_length=500, blank=True)
    category = models.CharField("الفئة", max_length=120, blank=True)
    tags = models.CharField("الوسوم", max_length=500, blank=True)
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
