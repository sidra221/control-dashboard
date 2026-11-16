"""مزامنة دورية لكل متاجر WooCommerce المربوطة — بتلتقط تغييرات المخزون
والنسخ (variations) اللي ما بتوصل دايماً كـ webhook. شغّلها من cron كل ساعة مثلاً:

    python manage.py sync_woocommerce
"""

from django.core.management.base import BaseCommand

from apitokens.models import WooCommerceConnection
from apitokens.services import catalog as catalog_service
from apitokens.services import woocommerce as woocommerce_service


class Command(BaseCommand):
    help = "Resync products (and stock) for every active WooCommerce connection"

    def handle(self, *args, **options):
        connections = WooCommerceConnection.objects.filter(is_active=True).select_related("merchant")
        for connection in connections:
            try:
                count = catalog_service.sync_woocommerce(connection.merchant, connection)
                self.stdout.write(f"{connection.site_url}: {count} products")
            except woocommerce_service.WooCommerceError as exc:
                self.stderr.write(f"{connection.site_url}: {exc}")
