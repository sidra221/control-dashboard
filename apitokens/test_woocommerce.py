"""ربط WooCommerce: تحويل المنتجات، المزامنة، تسجيل webhooks، استقبالها، وربط الطلبات."""
import base64
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
import socket
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import ApiToken, Conversation, Product, WooCommerceConnection
from .services import catalog as catalog_service
from .services import woocommerce as woo
from . import woocommerce_webhooks


def addrinfo(*ips):
    return [
        (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
         (ip, 443, 0, 0) if ":" in ip else (ip, 443))
        for ip in ips
    ]


class PublicDnsMixin:
    """كل اسم متجر بالاختبارات يُترجم لعنوان عام — بدون أي استعلام DNS حقيقي."""

    def setUp(self):
        super().setUp()
        patcher = patch("apitokens.services.woocommerce.socket.getaddrinfo", return_value=addrinfo("93.184.216.34"))
        self.mock_dns = patcher.start()
        self.addCleanup(patcher.stop)


def woo_product(**overrides):
    payload = {
        "id": 501, "name": "Linen &amp; Co Dress", "slug": "linen-dress", "type": "simple",
        "status": "publish", "catalog_visibility": "visible", "sku": "LIN-1",
        "permalink": "https://shop.example.com/product/linen-dress/",
        "description": "<p>Soft <b>linen</b>.</p>", "short_description": "<p>Summer dress</p>",
        "price": "120.00", "regular_price": "150.00", "sale_price": "120.00", "on_sale": True,
        "stock_status": "instock", "purchasable": True,
        "images": [{"src": "https://shop.example.com/a.jpg"}],
        "categories": [{"name": "Dresses &amp; Skirts"}], "tags": [{"name": "summer"}, {"name": "linen"}],
        "attributes": [], "variations": [],
    }
    payload.update(overrides)
    return payload


def variable_product(**overrides):
    fields = dict(
        id=700, name="Tee", type="variable", sku="", price="40.00", regular_price="", sale_price="",
        on_sale=False,
        attributes=[
            {"name": "Size", "variation": True, "options": ["S", "M", "L"]},
            {"name": "Material", "variation": False, "options": ["Cotton"]},
        ],
        variations=[1, 2, 3],
    )
    fields.update(overrides)
    return woo_product(**fields)


def variation(var_id, size, stock="instock", **extra):
    return {
        "id": var_id, "status": "publish", "stock_status": stock, "purchasable": True,
        "attributes": [{"name": "Size", "option": size}], **extra,
    }


class SiteUrlTests(TestCase):
    def test_normalizes_common_pastes(self):
        for raw in ("shop.example.com", "https://Shop.Example.com/", "https://shop.example.com/wp-admin/",
                    "https://shop.example.com/wp-json/wc/v3"):
            self.assertEqual(woo.normalize_site_url(raw), "https://shop.example.com", raw)

    def test_keeps_subdirectory_installs(self):
        self.assertEqual(woo.normalize_site_url("https://example.com/store/wp-admin"), "https://example.com/store")

    def test_rejects_http_and_empty(self):
        self.assertEqual(woo.normalize_site_url("http://shop.example.com"), "")
        self.assertEqual(woo.normalize_site_url("  "), "")


class SsrfGuardTests(TestCase):
    @patch("apitokens.services.woocommerce.socket.getaddrinfo")
    def test_internal_addresses_are_refused(self, mock_dns):
        for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0",
                   "100.64.0.1", "224.0.0.1", "240.0.0.1", "::1", "::", "fe80::1", "fc00::1", "ff02::1",
                   "::ffff:127.0.0.1"):
            mock_dns.return_value = addrinfo(ip)
            with self.assertRaises(woo.WooCommerceError, msg=ip):
                woo.assert_public_host("https://shop.example.com")

    def test_literal_ip_urls_are_refused(self):
        for url in ("https://127.0.0.1", "https://10.0.0.1:8443", "https://169.254.169.254", "https://[::1]"):
            with self.assertRaises(woo.WooCommerceError, msg=url):
                woo.assert_public_host(url)

    @patch("apitokens.services.woocommerce.socket.getaddrinfo")
    def test_hostname_resolving_to_any_private_address_is_refused(self, mock_dns):
        mock_dns.return_value = addrinfo("93.184.216.34", "10.0.0.7")
        with self.assertRaisesMessage(woo.WooCommerceError, "عنوان داخلي"):
            woo.assert_public_host("https://evil.example.com")
        self.assertEqual(mock_dns.call_args.args[:2], ("evil.example.com", 443))

    @patch("apitokens.services.woocommerce.socket.getaddrinfo", side_effect=socket.gaierror("nx"))
    def test_unresolvable_host_is_refused(self, _dns):
        with self.assertRaises(woo.WooCommerceError):
            woo.assert_public_host("https://nope.example.com")

    @patch("apitokens.services.woocommerce.socket.getaddrinfo",
           return_value=addrinfo("93.184.216.34", "2606:2800::1"))
    def test_public_host_is_allowed(self, _dns):
        woo.assert_public_host("https://shop.example.com")
        woo.assert_public_host("https://shop.example.com:8443/store")

    def test_http_is_refused(self):
        with self.assertRaises(woo.WooCommerceError):
            woo.assert_public_host("http://shop.example.com")


class ProductParsingTests(TestCase):
    def test_simple_product_row(self):
        row = woo.product_row_from_payload(woo_product())
        self.assertEqual(row["external_id"], "501")
        self.assertEqual(row["sku"], "LIN-1")
        self.assertEqual(row["title"], "Linen & Co Dress")
        self.assertEqual(row["category"], "Dresses & Skirts")
        self.assertEqual(row["tags"], "summer, linen")
        self.assertEqual(row["price"], Decimal("120.00"))
        self.assertEqual(row["compare_at_price"], Decimal("150.00"))
        self.assertEqual(row["description"], "Summer dress Soft linen .")
        self.assertEqual(row["product_url"], "https://shop.example.com/product/linen-dress/")
        self.assertEqual((row["options"], row["in_stock"], row["variant_id"]), ({}, True, ""))

    def test_no_compare_price_when_not_on_sale(self):
        row = woo.product_row_from_payload(woo_product(on_sale=False, price="150.00"))
        self.assertIsNone(row["compare_at_price"])

    def test_missing_sku_falls_back_to_woo_id(self):
        self.assertEqual(woo.product_row_from_payload(woo_product(sku=""))["sku"], "WOO-501")

    def test_out_of_stock_product_stays_but_not_in_stock(self):
        row = woo.product_row_from_payload(woo_product(stock_status="outofstock"))
        self.assertFalse(row["in_stock"])

    def test_excluded_products(self):
        for override in ({"status": "draft"}, {"catalog_visibility": "hidden"}, {"type": "grouped"},
                         {"type": "external"}, {"images": []}):
            self.assertIsNone(woo.product_row_from_payload(woo_product(**override)), override)

    def test_variable_product_lists_only_available_options(self):
        payload = variable_product(_variations=[
            variation(1, "S"), variation(2, "M", stock="outofstock"), variation(3, "L", stock="onbackorder"),
        ])
        row = woo.product_row_from_payload(payload)
        self.assertEqual(row["options"], {"Size": ["S", "L"]})
        self.assertTrue(row["in_stock"])
        self.assertEqual(row["sku"], "WOO-700")

    def test_any_value_variation_expands_to_all_parent_options(self):
        payload = variable_product(_variations=[variation(1, "")])
        self.assertEqual(woo.product_row_from_payload(payload)["options"], {"Size": ["S", "M", "L"]})

    def test_variable_product_with_no_available_variation_is_out_of_stock(self):
        payload = variable_product(_variations=[variation(1, "S", stock="outofstock")])
        row = woo.product_row_from_payload(payload)
        self.assertFalse(row["in_stock"])
        self.assertEqual(row["options"], {})

    def test_variable_product_without_variation_data_falls_back_to_parent(self):
        row = woo.product_row_from_payload(variable_product())
        self.assertEqual(row["options"], {"Size": ["S", "M", "L"]})
        self.assertTrue(row["in_stock"])


def fake_response(data, status=200, total_pages=1):
    resp = Mock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.headers = {"X-WP-TotalPages": str(total_pages)}
    resp.json.return_value = data
    return resp


class RestClientTests(PublicDnsMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.merchant = ApiToken.objects.create(label="M", image_quota=1)
        self.conn = WooCommerceConnection.objects.create(
            merchant=self.merchant, site_url="https://shop.example.com", consumer_key="ck", consumer_secret="cs",
        )

    @patch("apitokens.services.woocommerce.requests.request")
    def test_store_info_uses_basic_auth_and_returns_currency(self, mock_req):
        mock_req.return_value = fake_response({"id": "woocommerce_currency", "value": "SAR"})
        self.assertEqual(woo.get_store_info(self.conn), {"currency": "SAR"})
        args, kwargs = mock_req.call_args
        self.assertEqual(args, ("GET", "https://shop.example.com/wp-json/wc/v3/settings/general/woocommerce_currency"))
        self.assertEqual(kwargs["auth"], ("ck", "cs"))

    @patch("apitokens.services.woocommerce.requests.request")
    def test_error_statuses_have_clear_messages(self, mock_req):
        for status, text in ((401, "غير صحيحة"), (403, "Read/Write"), (404, "Permalinks")):
            mock_req.return_value = fake_response({}, status=status)
            with self.assertRaisesMessage(woo.WooCommerceError, text):
                woo.get_store_info(self.conn)

    @patch("apitokens.services.woocommerce.requests.request")
    def test_html_response_is_reported_as_firewall(self, mock_req):
        resp = fake_response(None)
        resp.json.side_effect = ValueError("not json")
        mock_req.return_value = resp
        with self.assertRaisesMessage(woo.WooCommerceError, "جدار حماية"):
            woo.get_store_info(self.conn)

    @patch("apitokens.services.woocommerce.time.sleep")
    @patch("apitokens.services.woocommerce.requests.request")
    def test_throttle_is_retried(self, mock_req, _sleep):
        mock_req.side_effect = [fake_response({}, status=429), fake_response({"value": "USD"})]
        self.assertEqual(woo.get_store_info(self.conn)["currency"], "USD")

    @patch("apitokens.services.woocommerce.time.sleep")
    @patch("apitokens.services.woocommerce.requests.request")
    def test_retry_after_is_honored_and_capped(self, mock_req, mock_sleep):
        slow = fake_response({}, status=429)
        slow.headers["Retry-After"] = "7"
        huge = fake_response({}, status=429)
        huge.headers["Retry-After"] = "3600"
        mock_req.side_effect = [slow, huge, fake_response({"value": "USD"})]
        self.assertEqual(woo.get_store_info(self.conn)["currency"], "USD")
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [7, woo.MAX_RETRY_AFTER_SECONDS])

    @patch("apitokens.services.woocommerce.time.sleep")
    @patch("apitokens.services.woocommerce.requests.request")
    def test_throttle_without_retry_after_backs_off_exponentially_then_gives_up(self, mock_req, mock_sleep):
        bad_date = fake_response({}, status=429)
        bad_date.headers["Retry-After"] = "Wed, 21 Oct 2026 07:28:00 GMT"
        mock_req.side_effect = [fake_response({}, status=429), bad_date, fake_response({}, status=429),
                                fake_response({}, status=429)]
        with self.assertRaises(woo.WooCommerceError):
            woo.get_store_info(self.conn)
        self.assertEqual([c.args[0] for c in mock_sleep.call_args_list], [1, 2, 4])

    @patch("apitokens.services.woocommerce.requests.request")
    def test_redirects_are_not_followed(self, mock_req):
        mock_req.return_value = fake_response({}, status=301)
        with self.assertRaisesMessage(woo.WooCommerceError, "الرابط النهائي"):
            woo.get_store_info(self.conn)
        self.assertIs(mock_req.call_args.kwargs["allow_redirects"], False)

    @patch("apitokens.services.woocommerce.requests.request")
    def test_host_is_rechecked_before_every_request(self, mock_req):
        # DNS تغيّر بعد الربط ليشير لعنوان داخلي: لا يُرسل أي طلب.
        self.mock_dns.return_value = addrinfo("10.0.0.5")
        with self.assertRaises(woo.WooCommerceError):
            woo.get_store_info(self.conn)
        mock_req.assert_not_called()

    @patch("apitokens.services.woocommerce.requests.request")
    def test_delete_webhooks_removes_only_ours_and_ignores_errors(self, mock_req):
        url = "https://control.example.com/webhooks/woocommerce/tok/"
        calls = []

        def respond(method, api_url, params=None, **kwargs):
            calls.append((method, api_url.split("/wc/v3/")[1], params, kwargs["timeout"]))
            if method == "GET":
                return fake_response([
                    {"id": 1, "delivery_url": url}, {"id": 2, "delivery_url": "https://other.example.com/"},
                    {"id": 3, "delivery_url": url},
                ])
            if api_url.endswith("webhooks/1"):
                return fake_response({}, status=500)
            return fake_response({})
        mock_req.side_effect = respond
        self.assertEqual(woo.delete_webhooks(self.conn, url), 1)
        deletes = [c for c in calls if c[0] == "DELETE"]
        self.assertEqual([c[1] for c in deletes], ["webhooks/1", "webhooks/3"])
        self.assertTrue(all(c[2] == {"force": "true"} for c in deletes))
        self.assertTrue(all(c[3] == woo.CLEANUP_TIMEOUT_SECONDS for c in calls))

    @patch("apitokens.services.woocommerce.requests.request",
           side_effect=woo.requests.ConnectionError("down"))
    def test_delete_webhooks_never_raises(self, _req):
        self.assertEqual(woo.delete_webhooks(self.conn, "https://c.example.com/w/"), 0)

    @patch("apitokens.services.woocommerce.requests.request")
    def test_fetch_all_products_follows_pages_and_loads_variations(self, mock_req):
        def respond(method, url, params=None, **kwargs):
            if url.endswith("/products"):
                page = params["page"]
                data = [woo_product(id=1)] if page == 1 else [variable_product(id=2)]
                return fake_response(data, total_pages=2)
            if url.endswith("/products/2/variations"):
                return fake_response([variation(9, "S")])
            raise AssertionError(url)
        mock_req.side_effect = respond
        products = woo.fetch_all_products(self.conn)
        self.assertEqual([p["id"] for p in products], [1, 2])
        self.assertEqual(products[1]["_variations"][0]["id"], 9)
        self.assertNotIn("_variations", products[0])
        first_params = mock_req.call_args_list[0].kwargs["params"]
        self.assertEqual(first_params["status"], "publish")

    @patch("apitokens.services.woocommerce.requests.request")
    def test_register_webhooks_creates_missing_and_refreshes_existing_secret(self, mock_req):
        url = "https://control.example.com/webhooks/woocommerce/tok/"
        calls = []

        def respond(method, api_url, params=None, json=None, **kwargs):
            calls.append((method, api_url.split("/wc/v3/")[1], json))
            if method == "GET":
                return fake_response([{"id": 77, "topic": "product.created", "delivery_url": url, "status": "disabled"}])
            return fake_response({})
        mock_req.side_effect = respond
        self.assertTrue(woo.register_webhooks(self.conn, url))

        put = [c for c in calls if c[0] == "PUT"]
        self.assertEqual(len(put), 1)
        self.assertEqual(put[0][1], "webhooks/77")
        self.assertEqual(put[0][2], {"secret": self.conn.webhook_secret, "status": "active"})
        created = sorted(c[2]["topic"] for c in calls if c[0] == "POST")
        self.assertEqual(created, ["order.created", "order.updated", "product.deleted", "product.updated"])
        self.assertTrue(all(c[2]["delivery_url"] == url and c[2]["secret"] == self.conn.webhook_secret
                            for c in calls if c[0] == "POST"))

    @patch("apitokens.services.woocommerce.requests.request")
    def test_register_webhooks_returns_false_when_order_hooks_fail(self, mock_req):
        def respond(method, api_url, json=None, **kwargs):
            if method == "POST" and json["topic"].startswith("order."):
                return fake_response({}, status=403)
            return fake_response([] if method == "GET" else {})
        mock_req.side_effect = respond
        self.assertFalse(woo.register_webhooks(self.conn, "https://c.example.com/w/"))


class SyncTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=1)
        self.conn = WooCommerceConnection.objects.create(
            merchant=self.merchant, site_url="https://shop.example.com", consumer_key="ck", consumer_secret="cs",
        )

    @patch("apitokens.services.catalog.ensure_embeddings")
    @patch("apitokens.services.catalog.woocommerce_service.fetch_all_products")
    @patch("apitokens.services.catalog.woocommerce_service.get_store_info", return_value={"currency": "SAR"})
    def test_sync_upserts_removes_vanished_and_sets_currency(self, _info, mock_fetch, _emb):
        Product.objects.create(merchant=self.merchant, external_id="999", sku="OLD", title="gone",
                               price=1, image_url="https://x/y.jpg")
        Product.objects.create(merchant=self.merchant, external_id="", sku="CSV", title="manual",
                               price=1, image_url="https://x/y.jpg")
        mock_fetch.return_value = [woo_product(), woo_product(id=502, status="draft")]

        count = catalog_service.sync_woocommerce(self.merchant, self.conn)

        self.assertEqual(count, 1)
        self.assertEqual(set(self.merchant.products.values_list("external_id", flat=True)), {"501", ""})
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.currency, "SAR")
        self.conn.refresh_from_db()
        self.assertIsNotNone(self.conn.last_synced_at)


@override_settings(SITE_BASE_URL="https://control.example.com")
class WebhookTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=1)
        self.conn = WooCommerceConnection.objects.create(
            merchant=self.merchant, site_url="https://shop.example.com", consumer_key="ck", consumer_secret="cs",
            webhook_secret="whsec",
        )
        self.client = Client()
        self.url = f"/webhooks/woocommerce/{self.merchant.token}/"

    def send(self, topic, payload, secret="whsec"):
        body = json.dumps(payload).encode()
        sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
        return self.client.post(
            self.url, data=body, content_type="application/json",
            HTTP_X_WC_WEBHOOK_TOPIC=topic, HTTP_X_WC_WEBHOOK_SIGNATURE=sig,
        )

    def test_ping_is_accepted_without_signature(self):
        resp = self.client.post(self.url, data="webhook_id=12", content_type="application/x-www-form-urlencoded")
        self.assertEqual(resp.status_code, 200)

    def test_unknown_store_token_is_404(self):
        resp = self.client.post("/webhooks/woocommerce/nope/", data=b"{}", content_type="application/json")
        self.assertEqual(resp.status_code, 404)

    def test_bad_signature_is_rejected(self):
        self.assertEqual(self.send("product.created", woo_product(), secret="wrong").status_code, 401)
        self.assertFalse(Product.objects.exists())

    def test_missing_signature_is_rejected(self):
        resp = self.client.post(self.url, data=b"{}", content_type="application/json",
                                HTTP_X_WC_WEBHOOK_TOPIC="product.created")
        self.assertEqual(resp.status_code, 401)

    def test_product_created_updated_and_deleted(self):
        self.assertEqual(self.send("product.created", woo_product()).status_code, 200)
        self.assertEqual(Product.objects.get(merchant=self.merchant, external_id="501").price, Decimal("120.00"))

        self.send("product.updated", woo_product(price="99.00", on_sale=False))
        self.assertEqual(Product.objects.get(external_id="501").price, Decimal("99.00"))

        self.send("product.deleted", {"id": 501})
        self.assertFalse(Product.objects.exists())

    def test_product_going_draft_is_removed(self):
        self.send("product.created", woo_product())
        self.send("product.updated", woo_product(status="draft"))
        self.assertFalse(Product.objects.exists())

    @patch("apitokens.services.woocommerce.requests.request")
    def test_variable_product_is_saved_from_payload_without_rest_call(self, mock_req):
        self.assertEqual(self.send("product.created", variable_product()).status_code, 200)
        self.assertEqual(Product.objects.get(external_id="700").options, {"Size": ["S", "M", "L"]})
        mock_req.assert_not_called()

    @patch("apitokens.services.woocommerce.requests.request")
    def test_variable_update_keeps_known_available_options_until_hourly_sync(self, mock_req):
        Product.objects.create(merchant=self.merchant, external_id="700", sku="WOO-700", title="Tee", price=1,
                               image_url="https://x/y.jpg", options={"Size": ["M", "XL"]})
        self.send("product.updated", variable_product(name="Tee v2"))
        product = Product.objects.get(external_id="700")
        # XL لم يعد خياراً في المنتج، وS وL معروف من آخر مزامنة أنها غير متوفرة.
        self.assertEqual((product.title, product.options), ("Tee v2", {"Size": ["M"]}))
        mock_req.assert_not_called()

    def test_inactive_connection_or_merchant_is_a_no_op(self):
        self.conn.is_active = False
        self.conn.save()
        self.assertEqual(self.send("product.created", woo_product()).status_code, 200)
        self.assertFalse(Product.objects.exists())

        self.conn.is_active = True
        self.conn.save()
        self.merchant.is_active = False
        self.merchant.save()
        self.assertEqual(self.send("product.created", woo_product()).status_code, 200)
        self.assertFalse(Product.objects.exists())

        self.merchant.is_active = True
        self.merchant.save()
        self.send("product.created", woo_product())
        self.conn.is_active = False
        self.conn.save()
        self.assertEqual(self.send("product.deleted", {"id": 501}).status_code, 200)
        self.assertTrue(Product.objects.filter(external_id="501").exists())

    # ---- ربط الطلبات بالمحادثات ----

    def make_conversation(self, session="sess-aaaaaa", skus=("LIN-1",), add_to_carts=1, buy_clicks=0, age_hours=1):
        convo = Conversation.objects.create(
            merchant=self.merchant, session_id=session, recommended_skus=list(skus),
            add_to_carts=add_to_carts, buy_clicks=buy_clicks,
        )
        stamp = timezone.now() - timedelta(hours=age_hours)
        Conversation.objects.filter(pk=convo.pk).update(started_at=stamp, updated_at=stamp)
        return convo

    def order(self, **overrides):
        payload = {
            "id": 9001, "status": "processing", "currency": "SAR", "total": "240.00",
            "date_created_gmt": timezone.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "line_items": [{"product_id": 501, "variation_id": 0, "quantity": 2}],
        }
        payload.update(overrides)
        return payload

    def setUpProduct(self):
        self.send("product.created", woo_product())

    def test_paid_order_is_linked_to_matching_conversation(self):
        self.setUpProduct()
        convo = self.make_conversation()
        self.assertEqual(self.send("order.created", self.order()).status_code, 200)
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "9001")
        self.assertEqual(convo.order_total, Decimal("240.00"))
        self.assertEqual(convo.order_currency, "SAR")
        self.assertIsNotNone(convo.order_at)

    def test_pending_order_is_not_counted_until_it_is_paid(self):
        self.setUpProduct()
        convo = self.make_conversation()
        self.send("order.created", self.order(status="pending"))
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "")
        self.send("order.updated", self.order(status="processing"))
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "9001")

    def test_failed_cancelled_and_draft_orders_never_count(self):
        self.setUpProduct()
        convo = self.make_conversation()
        for status in ("failed", "cancelled", "refunded", "checkout-draft", "trash"):
            self.send("order.updated", self.order(status=status))
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "")

    def test_repeated_order_events_do_not_claim_a_second_conversation(self):
        self.setUpProduct()
        first = self.make_conversation(session="sess-aaaaaa", age_hours=2)
        second = self.make_conversation(session="sess-bbbbbb", age_hours=1)
        self.send("order.created", self.order())
        self.send("order.updated", self.order(status="completed"))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.order_id, second.order_id), ("", "9001"))  # الأحدث بينربط، مرة وحدة

    def test_product_clicked_from_page_card_links_even_when_not_recommended_by_chat(self):
        self.setUpProduct()
        convo = self.make_conversation(skus=())
        Conversation.objects.filter(pk=convo.pk).update(acted_skus=["LIN-1"])
        self.send("order.created", self.order())
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "9001")

    def test_order_for_other_product_is_ignored(self):
        self.setUpProduct()
        convo = self.make_conversation(skus=("OTHER",))
        self.send("order.created", self.order())
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "")

    def test_conversation_without_cart_or_buy_click_is_ignored(self):
        self.setUpProduct()
        convo = self.make_conversation(add_to_carts=0, buy_clicks=0)
        self.send("order.created", self.order())
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "")

    def test_buy_click_alone_is_enough(self):
        self.setUpProduct()
        convo = self.make_conversation(add_to_carts=0, buy_clicks=1)
        self.send("order.created", self.order())
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "9001")

    def test_conversation_older_than_window_is_ignored(self):
        self.setUpProduct()
        convo = self.make_conversation(age_hours=30)
        self.send("order.created", self.order())
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "")

    def test_other_merchants_conversation_is_never_linked(self):
        self.setUpProduct()
        other = ApiToken.objects.create(label="Other", image_quota=1)
        convo = Conversation.objects.create(
            merchant=other, session_id="sess-zzzzzz", recommended_skus=["LIN-1"], add_to_carts=1,
        )
        self.send("order.created", self.order())
        convo.refresh_from_db()
        self.assertEqual(convo.order_id, "")


@override_settings(SITE_BASE_URL="https://control.example.com")
class ConnectViewTests(PublicDnsMixin, TestCase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(user)
        self.merchant = ApiToken.objects.create(label="متجر ووو", image_quota=5)
        self.url = f"/{self.merchant.pk}/woocommerce/"
        self.form = {
            "action": "connect", "site_url": "shop.example.com",
            "consumer_key": "ck_1", "consumer_secret": "cs_1",
        }

    def test_requires_staff(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_page_renders_instructions(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("REST API", html)
        self.assertIn('name="consumer_secret"', html)

    @patch("apitokens.views.start_woocommerce_sync")
    @patch("apitokens.views.woocommerce_service.get_store_info", return_value={"currency": "SAR"})
    def test_connect_saves_and_starts_background_sync(self, _info, mock_start):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(self.url, self.form, follow=True)
        conn = WooCommerceConnection.objects.get(merchant=self.merchant)
        self.assertEqual(conn.site_url, "https://shop.example.com")
        self.assertEqual((conn.consumer_key, conn.consumer_secret), ("ck_1", "cs_1"))
        self.assertTrue(conn.webhook_secret)
        mock_start.assert_called_once_with(
            conn.pk, f"https://control.example.com/webhooks/woocommerce/{self.merchant.token}/",
        )
        self.assertIn("في الخلفية", resp.content.decode())

    @patch("apitokens.views.start_woocommerce_sync")
    @patch("apitokens.views.woocommerce_service.get_store_info", return_value={"currency": "SAR"})
    def test_reconnect_keeps_the_webhook_secret(self, *_mocks):
        self.client.post(self.url, self.form)
        secret = WooCommerceConnection.objects.get(merchant=self.merchant).webhook_secret
        self.client.post(self.url, {**self.form, "consumer_key": "ck_2"})
        conn = WooCommerceConnection.objects.get(merchant=self.merchant)
        self.assertEqual((conn.consumer_key, conn.webhook_secret), ("ck_2", secret))

    @patch("apitokens.views.start_woocommerce_sync")
    @patch("apitokens.services.woocommerce.requests.request")
    def test_private_store_url_is_refused_before_any_request(self, mock_req, mock_start):
        for ip in ("127.0.0.1", "10.0.0.8", "169.254.169.254"):
            self.mock_dns.return_value = addrinfo(ip)
            resp = self.client.post(self.url, self.form, follow=True)
            self.assertIn("عنوان داخلي", resp.content.decode())
        resp = self.client.post(self.url, {**self.form, "site_url": "https://[::1]"}, follow=True)
        self.assertIn("عنوان داخلي", resp.content.decode())
        mock_req.assert_not_called()
        mock_start.assert_not_called()
        self.assertFalse(WooCommerceConnection.objects.exists())

    @patch("apitokens.views.woocommerce_service.get_store_info",
           side_effect=woo.WooCommerceError("مفاتيح الـ API غير صحيحة"))
    def test_bad_keys_do_not_save_a_connection(self, _info):
        resp = self.client.post(self.url, self.form, follow=True)
        self.assertFalse(WooCommerceConnection.objects.exists())
        self.assertIn("غير صحيحة", resp.content.decode())

    def test_http_url_and_missing_fields_are_rejected(self):
        self.client.post(self.url, {**self.form, "site_url": "http://shop.example.com"})
        self.client.post(self.url, {**self.form, "consumer_secret": ""})
        self.assertFalse(WooCommerceConnection.objects.exists())

    @patch("apitokens.views.start_woocommerce_sync")
    def test_resync_runs_in_background(self, mock_start):
        conn = WooCommerceConnection.objects.create(
            merchant=self.merchant, site_url="https://shop.example.com", consumer_key="a", consumer_secret="b",
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {"action": "resync"})
        mock_start.assert_called_once_with(
            conn.pk, f"https://control.example.com/webhooks/woocommerce/{self.merchant.token}/",
        )

    @patch("apitokens.views.woocommerce_service.delete_webhooks")
    def test_disconnect_cleans_up_webhooks_products_and_connection(self, mock_delete):
        conn = WooCommerceConnection.objects.create(
            merchant=self.merchant, site_url="https://shop.example.com", consumer_key="a", consumer_secret="b",
        )
        other = ApiToken.objects.create(label="Other", image_quota=1)
        for merchant, ext, sku in ((self.merchant, "501", "W1"), (self.merchant, "", "CSV"), (other, "501", "O1")):
            Product.objects.create(merchant=merchant, external_id=ext, sku=sku, title="x", price=1,
                                   image_url="https://x/y.jpg")
        resp = self.client.post(self.url, {"action": "disconnect"}, follow=True)
        self.assertFalse(WooCommerceConnection.objects.exists())
        mock_delete.assert_called_once()
        self.assertEqual(mock_delete.call_args.args[0].site_url, conn.site_url)
        self.assertEqual(mock_delete.call_args.args[1],
                         f"https://control.example.com/webhooks/woocommerce/{self.merchant.token}/")
        self.assertEqual(set(Product.objects.values_list("merchant__label", "sku")),
                         {("متجر ووو", "CSV"), ("Other", "O1")})
        self.assertIn("REST API", resp.content.decode())

    @patch("apitokens.services.woocommerce.requests.request", side_effect=woo.requests.Timeout("slow"))
    def test_disconnect_succeeds_even_when_store_is_unreachable(self, _req):
        WooCommerceConnection.objects.create(
            merchant=self.merchant, site_url="https://shop.example.com", consumer_key="a", consumer_secret="b",
        )
        self.client.post(self.url, {"action": "disconnect"})
        self.assertFalse(WooCommerceConnection.objects.exists())

    def test_dashboard_links_the_woocommerce_page(self):
        html = self.client.get("/").content.decode()
        self.assertIn(self.url, html)


class InitialSyncTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=1)
        self.conn = WooCommerceConnection.objects.create(
            merchant=self.merchant, site_url="https://shop.example.com", consumer_key="ck", consumer_secret="cs",
        )

    @patch("apitokens.woocommerce_webhooks.threading.Thread")
    def test_start_initial_sync_spawns_daemon_thread(self, mock_thread):
        woocommerce_webhooks.start_initial_sync(self.conn.pk, "https://c.example.com/w/")
        kwargs = mock_thread.call_args.kwargs
        self.assertIs(kwargs["target"], woocommerce_webhooks._initial_sync)
        self.assertEqual(kwargs["args"], (self.conn.pk, "https://c.example.com/w/"))
        self.assertTrue(kwargs["daemon"])
        mock_thread.return_value.start.assert_called_once()

    @patch("apitokens.woocommerce_webhooks.db_connection")
    @patch("apitokens.woocommerce_webhooks.woocommerce_service.register_webhooks", return_value=True)
    @patch("apitokens.services.catalog.sync_woocommerce", return_value=3)
    def test_initial_sync_syncs_then_registers_webhooks_and_closes_db(self, mock_sync, mock_hooks, mock_db):
        woocommerce_webhooks._initial_sync(self.conn.pk, "https://c.example.com/w/")
        mock_sync.assert_called_once()
        self.assertEqual(mock_hooks.call_args.args[1], "https://c.example.com/w/")
        mock_db.close.assert_called_once()

    @patch("apitokens.woocommerce_webhooks.db_connection")
    @patch("apitokens.woocommerce_webhooks.woocommerce_service.register_webhooks")
    @patch("apitokens.services.catalog.sync_woocommerce", side_effect=woo.WooCommerceError("down"))
    def test_initial_sync_failure_is_logged_not_raised(self, _sync, mock_hooks, mock_db):
        with self.assertLogs("apitokens.woocommerce_webhooks", level="ERROR"):
            woocommerce_webhooks._initial_sync(self.conn.pk, "https://c.example.com/w/")
        mock_hooks.assert_not_called()
        mock_db.close.assert_called_once()


class WidgetEventSkuTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=1)
        Product.objects.create(merchant=self.merchant, sku="LIN-1", title="Dress", price=1, image_url="https://x/y.jpg")
        self.convo = Conversation.objects.create(merchant=self.merchant, session_id="sess-aaaaaa")

    def event(self, **body):
        return self.client.post(
            "/widget/event/", data=json.dumps({"type": "add_to_cart", "session_id": "sess-aaaaaa", **body}),
            content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token,
        )

    def test_event_records_the_sku_once(self):
        self.event(sku="LIN-1")
        self.event(sku="LIN-1")
        self.convo.refresh_from_db()
        self.assertEqual((self.convo.acted_skus, self.convo.add_to_carts), (["LIN-1"], 2))

    def test_unknown_or_other_merchants_sku_is_ignored(self):
        other = ApiToken.objects.create(label="Other", image_quota=1)
        Product.objects.create(merchant=other, sku="THEIRS", title="x", price=1, image_url="https://x/y.jpg")
        self.event(sku="THEIRS")
        self.event(sku="made-up")
        self.convo.refresh_from_db()
        self.assertEqual((self.convo.acted_skus, self.convo.add_to_carts), ([], 2))

    def test_event_without_sku_still_counts(self):
        self.assertEqual(self.event().status_code, 200)
        self.convo.refresh_from_db()
        self.assertEqual((self.convo.acted_skus, self.convo.add_to_carts), ([], 1))
