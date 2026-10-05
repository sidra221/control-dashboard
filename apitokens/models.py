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
