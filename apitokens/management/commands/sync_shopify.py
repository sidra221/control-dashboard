"""مزامنة دورية لكل متاجر Shopify المربوطة — بتلتقط تغييرات المخزون اللي
ما بتوصل دايماً كـ webhook. شغّلها من cron كل ساعة مثلاً:

    python manage.py sync_shopify
"""

from django.core.management.base import BaseCommand

from apitokens.models import ShopifyConnection
from apitokens.services import catalog as catalog_service
from apitokens.services import shopify as shopify_service


class Command(BaseCommand):
    help = "Resync products (and stock) for every active Shopify connection"

    def handle(self, *args, **options):
        connections = ShopifyConnection.objects.filter(is_active=True).select_related("merchant")
        for connection in connections:
            try:
                count = catalog_service.sync_shopify(connection.merchant, connection)
                self.stdout.write(f"{connection.shop_domain}: {count} products")
            except shopify_service.ShopifyError as exc:
                self.stderr.write(f"{connection.shop_domain}: {exc}")
