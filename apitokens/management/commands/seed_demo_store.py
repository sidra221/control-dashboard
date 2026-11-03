"""بيعبّي المتجر التجريبي (اللي مفتاحه DEMO_STORE_KEY) بكتالوج عربي كامل،
معلومات المتجر، وجدول المقاسات. آمن للتكرار: بيستبدل منتجات هالمتجر بس.

    python manage.py seed_demo_store
"""

from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apitokens import demo_catalog
from apitokens.models import ApiToken, Product, SizeChart
from apitokens.services.catalog import ensure_embeddings

SIZE_OPTION = "المقاس"


class Command(BaseCommand):
    help = "Fill the demo store (DEMO_STORE_KEY) with the Arabic demo catalog"

    def add_arguments(self, parser):
        parser.add_argument("--skip-embeddings", action="store_true")

    def handle(self, *args, **options):
        demo_key = getattr(settings, "DEMO_STORE_KEY", "")
        merchant = ApiToken.objects.filter(token=demo_key).first() if demo_key else None
        if merchant is None:
            raise CommandError("DEMO_STORE_KEY is not set or doesn't match a merchant")

        with transaction.atomic():
            merchant.products.all().delete()
            # بالعكس: أول الكتالوج (فساتين السهرة) بيطلع "وصل حديثاً" وبالواجهة.
            for item in reversed(demo_catalog.PRODUCTS):
                Product.objects.create(
                    merchant=merchant,
                    external_id=item["external_id"],
                    sku=item["external_id"].upper(),
                    title=item["title"],
                    description=item["description"],
                    price=Decimal(item["price"]),
                    compare_at_price=Decimal(item["compare_at_price"]) if item["compare_at_price"] else None,
                    image_url=item["image_url"],
                    product_url=f"{settings.SITE_BASE_URL}/demo/products/{item['handle']}/",
                    category=item["category"],
                    tags=item["tags"],
                    options={SIZE_OPTION: item["sizes"]} if item["sizes"] else {},
                    in_stock=item["in_stock"],
                )
            merchant.currency = "SAR"
            merchant.store_info = demo_catalog.STORE_INFO
            merchant.save(update_fields=["currency", "store_info"])
            SizeChart.objects.update_or_create(
                merchant=merchant, category="", defaults={"chart": demo_catalog.SIZE_CHART},
            )

        if not options["skip_embeddings"]:
            ensure_embeddings(list(merchant.products.filter(in_stock=True)))
        self.stdout.write(f"{merchant.label}: {merchant.products.count()} products")
