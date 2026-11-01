"""مزامنة دورية لكل متاجر زد المربوطة — بتلتقط أي تغيير ما وصل كإشعار.

    python manage.py sync_zid
"""

from django.core.management.base import BaseCommand

from apitokens.models import ZidConnection
from apitokens.services import catalog as catalog_service
from apitokens.services import zid as zid_service


class Command(BaseCommand):
    help = "Resync products (and stock) for every active Zid connection"

    def handle(self, *args, **options):
        for connection in ZidConnection.objects.filter(is_active=True).select_related("merchant"):
            name = connection.store_name or connection.store_id
            try:
                count = catalog_service.sync_zid(connection.merchant, connection)
                self.stdout.write(f"{name}: {count} products")
            except zid_service.ZidError as exc:
                self.stderr.write(f"{name}: {exc}")
