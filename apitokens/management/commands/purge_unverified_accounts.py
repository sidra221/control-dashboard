"""بيحذف حسابات البوابة غير الموثّقة وبدون متجر اللي أقدم من N ساعة (الافتراضي 24): تسجيلات
ما كملت الـ OTP (غلط بالرقم، أو حاجز أرقام). أكوادها بتنحذف معها (cascade). ما بيلمس حساب موثّق
ولا حساب إله متجر. شغّلها من cron يومياً:

    python manage.py purge_unverified_accounts [--hours 24]
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apitokens.models import MerchantAccount


class Command(BaseCommand):
    help = "Delete unverified, store-less portal accounts older than N hours"

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24, help="minimum age in hours (default 24)")

    def handle(self, *args, **options):
        hours = options["hours"]
        if hours < 0:
            raise CommandError("--hours must be >= 0")
        cutoff = timezone.now() - timedelta(hours=hours)
        _, per_model = MerchantAccount.objects.filter(
            phone_verified=False, merchant__isnull=True, created_at__lt=cutoff
        ).delete()
        count = per_model.get(MerchantAccount._meta.label, 0)
        self.stdout.write(f"deleted {count} unverified account(s) older than {hours}h")
