"""اختبارات تسجيل التجارب الافتراضية (TryOnEvent) وتوقيت الطلب (order_at)."""

import base64
import hashlib
import hmac
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import (
    ApiToken, Conversation, Product, SallaConnection, ShopifyConnection, TryOnEvent, ZidConnection,
)
from .services import kieai


@override_settings(KIEAI_API_KEY="test-key")
class TryOnEventRecordingTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", token="TRYONTOKEN001", image_quota=5)
        self.product = Product.objects.create(
            merchant=self.merchant, sku="DRESS-1", title="Dress", price="10",
            image_url="https://example.com/d.jpg",
        )
        self.client = Client()

    def post(self, sku="DRESS-1", session_id=None, photo=True, token=None):
        data = {"sku": sku}
        if photo:
            data["photo"] = SimpleUploadedFile("me.jpg", b"fakejpg", content_type="image/jpeg")
        if session_id is not None:
            data["session_id"] = session_id
        return self.client.post(
            "/widget/tryon/", data=data,
            HTTP_X_API_TOKEN=self.merchant.token if token is None else token,
        )

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_success_records_one_event(self, _gen):
        resp = self.post(session_id="sess-abcdef12")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        event = TryOnEvent.objects.get()
        self.assertEqual(event.merchant, self.merchant)
        self.assertEqual(event.sku, "DRESS-1")
        self.assertEqual(event.product, self.product)
        self.assertEqual(event.session_id, "sess-abcdef12")
        self.assertTrue(event.success)
        self.assertEqual(event.error, "")

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_missing_session_id_is_blank(self, _gen):
        self.post()
        self.assertEqual(TryOnEvent.objects.get().session_id, "")

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_success_keeps_conversation_counter(self, _gen):
        convo = Conversation.objects.create(merchant=self.merchant, session_id="sess-abcdef12")
        self.post(session_id="sess-abcdef12")
        convo.refresh_from_db()
        self.assertEqual(convo.tryons, 1)

    @patch("apitokens.widget_views.kieai.generate_tryon", side_effect=kieai.TryOnError("boom " * 50))
    def test_kieai_failure_records_failed_event(self, _gen):
        resp = self.post(session_id="sess-abcdef12")
        self.assertEqual(resp.status_code, 502)
        event = TryOnEvent.objects.get()
        self.assertFalse(event.success)
        self.assertEqual(event.product, self.product)
        self.assertEqual(event.session_id, "sess-abcdef12")
        self.assertTrue(event.error.startswith("boom"))
        self.assertLessEqual(len(event.error), 120)
        # فشل التجربة ما بيخصم من الرصيد
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 0)

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_validation_failures_record_nothing(self, gen):
        self.assertEqual(self.post(token="").status_code, 401)
        self.assertEqual(self.post(photo=False).status_code, 400)
        self.assertEqual(self.post(sku="").status_code, 400)
        self.assertEqual(self.post(token="WRONG").status_code, 401)
        self.assertEqual(self.post(sku="NOPE").status_code, 404)
        self.merchant.image_quota = 0
        self.merchant.save()
        self.assertEqual(self.post().json()["error"], "quota_exceeded")
        self.assertEqual(TryOnEvent.objects.count(), 0)
        gen.assert_not_called()

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_recording_error_never_breaks_response(self, _gen):
        with patch("apitokens.widget_views.TryOnEvent.objects.create", side_effect=Exception("db down")), \
                self.assertLogs("apitokens.widget_views", level="ERROR"):
            resp = self.post(session_id="sess-abcdef12")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(TryOnEvent.objects.count(), 0)

    @override_settings(KIEAI_API_KEY="")
    def test_not_configured_records_nothing(self):
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(TryOnEvent.objects.count(), 0)


class ShopifyOrderAtTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=1)
        ShopifyConnection.objects.create(
            merchant=self.merchant, shop_domain="s.myshopify.com", access_token="t", api_secret="sec",
        )
        self.convo = Conversation.objects.create(merchant=self.merchant, session_id="sess-123456")

    def send(self, order_id):
        body = json.dumps({
            "id": order_id, "total_price": "10", "currency": "USD",
            "note_attributes": [{"name": "_sw_conversation", "value": "sess-123456"}],
        }).encode()
        sig = base64.b64encode(hmac.new(b"sec", body, hashlib.sha256).digest()).decode()
        return self.client.post(
            f"/webhooks/shopify/{self.merchant.token}/", data=body, content_type="application/json",
            HTTP_X_SHOPIFY_TOPIC="orders/create", HTTP_X_SHOPIFY_HMAC_SHA256=sig,
        )

    def test_order_at_set_once(self):
        self.assertIsNone(self.convo.order_at)
        before = timezone.now()
        self.assertEqual(self.send(1).status_code, 200)
        self.convo.refresh_from_db()
        first = self.convo.order_at
        self.assertIsNotNone(first)
        self.assertGreaterEqual(first, before)
        self.send(2)
        self.convo.refresh_from_db()
        self.assertEqual(self.convo.order_id, "1")
        self.assertEqual(self.convo.order_at, first)


@override_settings(SALLA_WEBHOOK_SECRET="whsec_test")
class SallaOrderAtTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=5)
        SallaConnection.objects.create(merchant=self.merchant, salla_merchant_id="2468")
        self.convo = Conversation.objects.create(
            merchant=self.merchant, session_id="sess-abcdef12", platform_cart_id="C-77",
        )

    def order(self, order_id):
        body = json.dumps({
            "event": "order.created", "merchant": 2468,
            "data": {"id": order_id, "cart": {"id": "C-77"},
                     "amounts": {"total": {"amount": 300, "currency": "SAR"}}},
        }).encode()
        return self.client.post(
            "/webhooks/salla/", data=body, content_type="application/json",
            HTTP_X_SALLA_SIGNATURE=hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest(),
        )

    def test_order_at_set_once(self):
        self.assertIsNone(self.convo.order_at)
        before = timezone.now()
        self.assertEqual(self.order(1).status_code, 200)
        self.convo.refresh_from_db()
        first = self.convo.order_at
        self.assertIsNotNone(first)
        self.assertGreaterEqual(first, before)
        self.order(2)
        self.convo.refresh_from_db()
        self.assertEqual(self.convo.order_id, "1")
        self.assertEqual(self.convo.order_at, first)


@override_settings(ZID_WEBHOOK_SECRET="zid-hook-secret", ZID_CLIENT_ID="8467",
                   SITE_BASE_URL="https://control.example.com")
class ZidOrderAtTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M", image_quota=5)
        ZidConnection.objects.create(
            merchant=self.merchant, store_id="4242", authorization_token="A", manager_token="M",
            refresh_token="R",
        )
        self.convo = Conversation.objects.create(
            merchant=self.merchant, session_id="sess-zid-0001", platform_cart_id="CART-9",
        )

    def order(self, order_id):
        auth = "Basic " + base64.b64encode(b"control:zid-hook-secret").decode()
        return self.client.post(
            "/webhooks/zid/store/4242/order.create/",
            data=json.dumps({"id": order_id, "cart": {"id": "CART-9"},
                             "order_total": "450.00", "currency_code": "SAR"}),
            content_type="application/json", HTTP_AUTHORIZATION=auth,
        )

    def test_order_at_set_once(self):
        self.assertIsNone(self.convo.order_at)
        before = timezone.now()
        self.assertEqual(self.order(1).status_code, 200)
        self.convo.refresh_from_db()
        first = self.convo.order_at
        self.assertIsNotNone(first)
        self.assertGreaterEqual(first, before)
        self.order(2)
        self.convo.refresh_from_db()
        self.assertEqual(self.convo.order_id, "1")
        self.assertEqual(self.convo.order_at, first)
