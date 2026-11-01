"""تجديد مفاتيح زد على جدول: الـ refresh token بيعيش سنة بس، والمفاتيح نفسها
3 سنين — يعني ما رح يجينا 401 يذكّرنا. إذا ما جدّدنا، الربط بيموت بصمت
والتاجر لازم يعيد التثبيت. شغّله يومياً من cron:

    python manage.py refresh_zid_tokens
"""

from django.core.management.base import BaseCommand

from apitokens.models import ZidConnection
from apitokens.services import zid as zid_service


class Command(BaseCommand):
    help = "Refresh Zid tokens older than ~10 months, before the 1-year refresh token expires"

    def handle(self, *args, **options):
        for connection in ZidConnection.objects.filter(is_active=True):
            if not zid_service.needs_scheduled_refresh(connection):
                continue
            name = connection.store_name or connection.store_id
            try:
                zid_service.refresh_tokens(connection)
                self.stdout.write(f"{name}: refreshed")
            except zid_service.ZidError as exc:
                self.stderr.write(f"{name}: {exc}")
