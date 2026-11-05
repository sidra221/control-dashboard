"""بيمسح عدّادات تحديد المعدّل المنتهية (جدول RateLimitBucket). الطلبات نفسها
بتنظّف احتمالياً، فهاد اختياري — cron أسبوعي كافي."""

from django.core.management.base import BaseCommand

from apitokens.services import ratelimit


class Command(BaseCommand):
    help = "Delete expired rate-limit counters"

    def handle(self, *args, **opts):
        self.stdout.write(f"deleted {ratelimit.get_store().purge_expired()} expired counters")
