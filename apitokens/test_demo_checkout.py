"""طلب الديمو التجريبي: بيسجّل طلب على المحادثة حسب رقم السلة، وبيظهر مبيعات منسوبة بالإحصائيات."""

import json
from datetime import timedelta
from decimal import Decimal

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import ApiToken, Conversation, Product, TryOnEvent
from .services import tryon_stats

CART = "dc_0123456789abcdef"


def product(merchant, sku, price):
    return Product.objects.create(
        merchant=merchant, sku=sku, external_id=sku.lower(), title=sku, price=Decimal(price), image_url="https://x/y.jpg",
    )


class DemoCheckoutTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="Demo", image_quota=10, currency="SAR")
        self.other = ApiToken.objects.create(label="Other", image_quota=10)
        self.p1 = product(self.merchant, "P1", "100.00")
        self.p2 = product(self.merchant, "P2", "250.00")
        self.settings = override_settings(DEMO_STORE_KEY=self.merchant.token)
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    def post(self, body, client=None, **extra):
        client = client or self.client
        return client.post(reverse("demo-checkout"), json.dumps(body), content_type="application/json", **extra)

    def conv(self, merchant=None, session="s1", cart=CART, **kw):
        return Conversation.objects.create(merchant=merchant or self.merchant, session_id=session, platform_cart_id=cart, **kw)

    def test_records_order_on_matching_conversation(self):
        c = self.conv()
        r = self.post({"cart_id": CART, "items": [{"product_id": "p1", "qty": 2}]})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"] and data["attributed"])
        self.assertTrue(data["order_id"].startswith("DEMO-"))
        c.refresh_from_db()
        self.assertEqual(c.order_id, data["order_id"])
        self.assertEqual(c.order_total, Decimal("225.00"))  # 200 + شحن 25 (تحت 299)
        self.assertEqual(c.order_currency, "SAR")
        self.assertIsNotNone(c.order_at)

    def test_free_shipping_over_threshold_and_sku_lookup(self):
        c = self.conv()
        self.post({"cart_id": CART, "items": [{"product_id": "P2", "qty": 1}, {"product_id": "p1", "qty": 1}]})
        c.refresh_from_db()
        self.assertEqual(c.order_total, Decimal("350.00"))  # 350 >= 299 شحن مجاني

    def test_total_comes_from_catalog_not_client(self):
        c = self.conv()
        self.post({"cart_id": CART, "items": [{"product_id": "p1", "qty": 1, "price": 1}], "total": 1})
        c.refresh_from_db()
        self.assertEqual(c.order_total, Decimal("125.00"))

    def test_second_checkout_does_not_overwrite(self):
        c = self.conv()
        first = self.post({"cart_id": CART, "items": [{"product_id": "p1", "qty": 1}]}).json()
        second = self.post({"cart_id": CART, "items": [{"product_id": "p2", "qty": 5}]}).json()
        c.refresh_from_db()
        self.assertEqual(c.order_id, first["order_id"])
        self.assertFalse(second["attributed"])

    def test_unmatched_cart_is_ok_but_not_attributed(self):
        c = self.conv(cart="dc_someoneelse12345")
        data = self.post({"cart_id": CART, "items": [{"product_id": "p1", "qty": 1}]}).json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["attributed"])
        c.refresh_from_db()
        self.assertEqual(c.order_id, "")

    def test_other_merchants_conversation_is_never_touched(self):
        theirs = self.conv(merchant=self.other, session="x")
        self.post({"cart_id": CART, "items": [{"product_id": "p1", "qty": 1}]})
        theirs.refresh_from_db()
        self.assertEqual(theirs.order_id, "")

    def test_validation(self):
        for body in (
            {"cart_id": "short", "items": [{"product_id": "p1", "qty": 1}]},
            {"cart_id": "bad id with spaces!!", "items": [{"product_id": "p1", "qty": 1}]},
            {"cart_id": CART, "items": []},
            {"cart_id": CART, "items": "nope"},
            {"cart_id": CART, "items": [{"product_id": "ghost", "qty": 1}]},
            {"cart_id": CART, "items": [{"product_id": "p1", "qty": 0}]},
            {"cart_id": CART, "items": [{"product_id": "p1", "qty": 1000}]},
        ):
            self.assertEqual(self.post(body).status_code, 400, body)
        r = self.client.post(reverse("demo-checkout"), "not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_get_not_allowed_and_404_without_demo_key(self):
        self.assertEqual(self.client.get(reverse("demo-checkout")).status_code, 405)
        with override_settings(DEMO_STORE_KEY=""):
            r = self.post({"cart_id": CART, "items": [{"product_id": "p1", "qty": 1}]})
            self.assertEqual(r.status_code, 404)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        r = self.post({"cart_id": CART, "items": [{"product_id": "p1", "qty": 1}]}, client=client)
        self.assertEqual(r.status_code, 403)
        # الصفحة بتزرع كوكي CSRF ومنها بيجي التوكن
        page = client.get(reverse("demo-cart"))
        token = page.cookies["csrftoken"].value
        r = self.post(
            {"cart_id": CART, "items": [{"product_id": "p1", "qty": 1}]},
            client=client, HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN="http://testserver",
        )
        self.assertEqual(r.status_code, 200)

    def test_order_shows_up_as_attributed_revenue_when_tryon_came_first(self):
        self.conv(session="sess-a")
        ev = TryOnEvent.objects.create(merchant=self.merchant, session_id="sess-a", sku="P1", success=True)
        TryOnEvent.objects.filter(pk=ev.pk).update(created_at=timezone.now() - timedelta(hours=1))
        self.post({"cart_id": CART, "items": [{"product_id": "p1", "qty": 1}]})
        stats = tryon_stats.build_stats(self.merchant, "7", 7)
        self.assertEqual(stats["totals"]["attributed_orders"], 1)
        self.assertEqual(stats["totals"]["attributed_revenue"], Decimal("125.00"))

    def test_order_without_prior_tryon_is_not_attributed_revenue(self):
        self.conv(session="sess-b")
        self.post({"cart_id": CART, "items": [{"product_id": "p1", "qty": 1}]})
        stats = tryon_stats.build_stats(self.merchant, "7", 7)
        self.assertEqual(stats["totals"]["attributed_orders"], 0)


class WidgetCartIdHookTests(TestCase):
    def test_widget_and_demo_page_wire_the_cart_id(self):
        from django.conf import settings

        js = (settings.BASE_DIR / "apitokens" / "static" / "apitokens" / "embed.js").read_text()
        self.assertIn("window.ControlCart.cartId()", js)
        base = (settings.BASE_DIR / "apitokens" / "templates" / "apitokens" / "demo" / "base.html").read_text()
        self.assertIn("cartId: cartId", base)
