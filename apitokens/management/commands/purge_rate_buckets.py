"""بينظّف جداول الحدود: عدّادات RateBucket اللي نافذتها أقدم من N ساعة (الافتراضي 24 — أطول نافذة
بنستخدمها يوم الشات) وحجوزات الرصيد المنتهية (QuotaReservation). شغّلها من cron كل ساعة أو يومياً:

    python manage.py purge_rate_buckets [--hours 24]
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apitokens.models import QuotaReservation, RateBucket


class Command(BaseCommand):
    help = "Delete stale rate-limit buckets and expired quota reservations"

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24, help="bucket window age in hours (default 24)")

    def handle(self, *args, **options):
        hours = options["hours"]
        if hours < 0:
            raise CommandError("--hours must be >= 0")
        now = timezone.now()
        buckets, _ = RateBucket.objects.filter(window_start__lt=now - timedelta(hours=hours)).delete()
        reservations, _ = QuotaReservation.objects.filter(expires_at__lte=now).delete()
        self.stdout.write(f"deleted {buckets} rate bucket(s) and {reservations} expired reservation(s)")
