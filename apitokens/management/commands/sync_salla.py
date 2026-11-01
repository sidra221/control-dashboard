"""مزامنة دورية لكل متاجر سلة المربوطة — بتلتقط أي تغيير ما وصل كإشعار.
شغّلها من cron كل ساعة مثلاً:

    python manage.py sync_salla
"""

from django.core.management.base import BaseCommand

from apitokens.models import SallaConnection
from apitokens.services import catalog as catalog_service
from apitokens.services import salla as salla_service


class Command(BaseCommand):
    help = "Resync products (and stock) for every active Salla connection"

    def handle(self, *args, **options):
        connections = SallaConnection.objects.filter(is_active=True).select_related("merchant")
        for connection in connections:
            name = connection.store_name or connection.salla_merchant_id
            try:
                count = catalog_service.sync_salla(connection.merchant, connection)
                self.stdout.write(f"{name}: {count} products")
            except salla_service.SallaError as exc:
                self.stderr.write(f"{name}: {exc}")
