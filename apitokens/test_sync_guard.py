"""حماية الكتالوج من المسح بالغلط بالمزامنة الكاملة، وسقف وقت التوليد تحت مدة حجز الرصيد."""
from unittest import mock

from django.test import TestCase, override_settings

from .models import ApiToken, Product
from .services import catalog, kieai
from .services.sync_guard import CatalogIncomplete, should_prune


def _row(i):
    return {"external_id": f"p{i}", "sku": f"S{i}", "title": f"P{i}", "price": 10,
            "image_url": "https://e.com/a.png", "in_stock": True}


class PruneGuardTests(TestCase):
    def setUp(self):
        self.m = ApiToken.objects.create(label="M", image_quota=10)
        for i in range(40):
            Product.objects.create(merchant=self.m, external_id=f"p{i}", sku=f"S{i}", title=f"P{i}",
                                   price=10, image_url="https://e.com/a.png")

    def save(self, rows, complete=True):
        catalog._save_rows(self.m, rows, complete, "test")
        return self.m.products.count()

    def test_normal_removal_still_deletes(self):
        self.assertEqual(self.save([_row(i) for i in range(39)]), 39)

    def test_incomplete_fetch_never_deletes(self):
        self.assertEqual(self.save([_row(i) for i in range(5)], complete=False), 40)

    def test_empty_fetch_never_deletes(self):
        self.assertEqual(self.save([]), 40)

    def test_sharp_drop_does_not_delete(self):
        self.assertEqual(self.save([_row(i) for i in range(10)]), 40)

    def test_small_store_can_remove_most_products(self):
        Product.objects.filter(merchant=self.m).exclude(external_id__in=["p0", "p1", "p2"]).delete()
        self.assertEqual(self.save([_row(0)]), 1)

    def test_should_prune_rules(self):
        self.assertTrue(should_prune(self.m, 0, 0, True, "t"))
        self.assertFalse(should_prune(self.m, 3, 0, True, "t"))
        self.assertTrue(should_prune(self.m, 19, 2, True, "t"))
        self.assertFalse(should_prune(self.m, 20, 9, True, "t"))
        self.assertTrue(should_prune(self.m, 20, 10, True, "t"))

    def test_collect_rows_keeps_partial_and_flags_incomplete(self):
        def fetch():
            raise CatalogIncomplete("page_cap", [{"i": 1}, {"i": 2}])
        rows, complete = catalog._collect_rows(fetch, lambda raw: _row(raw["i"]))
        self.assertEqual((len(rows), complete), (2, False))

        def gen():
            yield {"i": 1}
            raise CatalogIncomplete("page_cap")
        rows, complete = catalog._collect_rows(gen, lambda raw: _row(raw["i"]))
        self.assertEqual((len(rows), complete), (1, False))


class WooPagingTests(TestCase):
    def _resp(self, header=None):
        r = mock.Mock()
        r.headers = {"X-WP-TotalPages": header} if header else {}
        return r

    def test_missing_total_pages_header_keeps_paging_until_a_short_page(self):
        from .services import woocommerce
        pages = [[{"id": i} for i in range(100)], [{"id": i} for i in range(100, 200)], [{"id": 200}]]
        with mock.patch.object(woocommerce, "_request", side_effect=[(self._resp(), p) for p in pages]):
            items = woocommerce._fetch_pages(None, "products", {})
        self.assertEqual(len(items), 201)

    def test_page_cap_raises_incomplete_with_partial(self):
        from .services import woocommerce
        full = [{"id": 1}] * 100
        with mock.patch.object(woocommerce, "MAX_PAGES", 2), \
                mock.patch.object(woocommerce, "_request", return_value=(self._resp(), full)):
            with self.assertRaises(CatalogIncomplete) as ctx:
                woocommerce._fetch_pages(None, "products", {})
        self.assertEqual(len(ctx.exception.partial), 200)


class TryonDeadlineTests(TestCase):
    @override_settings(QUOTA_RESERVATION_TTL_SECONDS=180)
    def test_polling_stops_before_the_reservation_expires(self):
        clock = {"t": 1000.0}

        def fake_sleep(s):
            clock["t"] += s

        pending = mock.Mock()
        pending.json.return_value = {"data": {"state": "generating"}}
        pending.raise_for_status.return_value = None
        with mock.patch.object(kieai.time, "monotonic", side_effect=lambda: clock["t"]), \
                mock.patch.object(kieai.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(kieai, "upload_image", return_value="https://e.com/p.png"), \
                mock.patch.object(kieai, "create_task", return_value="t1"), \
                mock.patch.object(kieai.requests, "get", return_value=pending):
            with self.assertRaises(kieai.TryOnError):
                kieai.generate_tryon("k", b"x", "p.png", "https://e.com/i.png", "prompt")
        self.assertLess(clock["t"] - 1000.0, 180 - kieai.TTL_MARGIN_SECONDS + 1)
