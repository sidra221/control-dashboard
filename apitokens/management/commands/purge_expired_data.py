"""بيطبّق سياسة الاحتفاظ (v2): بيمسح ذاكرة الزبائن اللي انتهت صلاحيتها (آخر زيارة +
profile_retention_days لكل تاجر) مع رسائلهم، وتتبّع الأدوار الأقدم من N يوم. شغّله يومياً (cron).

    python manage.py purge_expired_data
    python manage.py purge_expired_data --trace-days 30
"""

from django.core.management.base import BaseCommand

from apitokens.services import memory


class Command(BaseCommand):
    help = "Delete expired customer memory and old turn traces"

    def add_arguments(self, parser):
        parser.add_argument("--trace-days", type=int, default=60)

    def handle(self, *args, **opts):
        profiles, traces = memory.purge_expired(trace_days=opts["trace_days"])
        self.stdout.write(f"forgot {profiles} expired customers, deleted {traces} old traces")
