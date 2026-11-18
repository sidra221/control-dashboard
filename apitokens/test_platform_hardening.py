"""تقوية تكاملات Shopify وسلة وزد: إشعارات موقوفة ومكررة، توقيع غير صالح،
إيقاف الإدارة أمام إعادة التثبيت، فشل تجديد التوكن، ودومين متجر زد وإشعاراته."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    ApiToken, Product, SallaConnection, ShopifyConnection, ZidConnection, ZidSubscription,
)
from .services import salla as salla_service
from .services import zid as zid_service


def shopify_product(**overrides):
    product = {
        "id": 111, "title": "Linen Shirt", "body_html": "<p>Soft</p>", "handle": "linen-shirt",
        "status": "active", "product_type": "Shirts", "tags": "",
        "variants": [{"id": 1, "sku": "LS-1", "price": "50.00", "inventory_quantity": 3,
                      "inventory_management": "shopify", "inventory_policy": "deny"}],
        "images": [{"src": "https://cdn.shopify.com/a.jpg"}], "image": {"src": "https://cdn.shopify.com/a.jpg"},
        "options": [],
    }
    product.update(overrides)
    return product


class ShopifyWebhookHardeningTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.merchant = ApiToken.objects.create(label="M", image_quota=1)
        self.connection = ShopifyConnection.objects.create(
            merchant=self.merchant, shop_domain="s.myshopify.com", access_token="t", api_secret="sec",
        )

    def send(self, topic, payload, webhook_id=None, signature=None):
        body = json.dumps(payload).encode()
        if signature is None:
            signature = base64.b64encode(hmac.new(b"sec", body, hashlib.sha256).digest()).decode()
        headers = {"HTTP_X_SHOPIFY_TOPIC": topic, "HTTP_X_SHOPIFY_HMAC_SHA256": signature}
        if webhook_id:
            headers["HTTP_X_SHOPIFY_WEBHOOK_ID"] = webhook_id
        return self.client.post(
            f"/webhooks/shopify/{self.merchant.token}/", data=body, content_type="application/json", **headers,
        )

    def test_inactive_connection_is_a_200_noop(self):
        self.connection.is_active = False
        self.connection.save()
        self.assertEqual(self.send("products/create", shopify_product()).status_code, 200)
        self.assertFalse(Product.objects.exists())

    def test_inactive_merchant_is_a_200_noop(self):
        self.merchant.is_active = False
        self.merchant.save()
        self.assertEqual(self.send("products/create", shopify_product()).status_code, 200)
        self.assertFalse(Product.objects.exists())

    def test_inactive_still_requires_valid_signature(self):
        self.connection.is_active = False
        self.connection.save()
        self.assertEqual(self.send("products/create", shopify_product(), signature="nope").status_code, 401)

    def test_non_ascii_signature_is_401_not_500(self):
        self.assertEqual(self.send("products/create", shopify_product(), signature="تزوير").status_code, 401)

    def test_duplicate_delivery_is_processed_once(self):
        self.assertEqual(self.send("products/create", shopify_product(), webhook_id="wh-1").status_code, 200)
        Product.objects.all().delete()
        # إعادة إرسال نفس الإشعار: 200 بدون معالجة.
        self.assertEqual(self.send("products/create", shopify_product(), webhook_id="wh-1").status_code, 200)
        self.assertFalse(Product.objects.exists())
        # إشعار جديد بمعرّف مختلف بينعالج عادي.
        self.send("products/create", shopify_product(), webhook_id="wh-2")
        self.assertTrue(Product.objects.filter(external_id="111").exists())

    def test_failed_processing_allows_retry(self):
        with patch("apitokens.shopify_webhooks._handle", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.send("products/create", shopify_product(), webhook_id="wh-9")
        self.send("products/create", shopify_product(), webhook_id="wh-9")
        self.assertTrue(Product.objects.filter(external_id="111").exists())


@override_settings(SALLA_WEBHOOK_SECRET="whsec_test")
class SallaSignatureTests(TestCase):
    def setUp(self):
        merchant = ApiToken.objects.create(label="متجر", image_quota=5)
        SallaConnection.objects.create(merchant=merchant, salla_merchant_id="777", access_token="AT")
        self.body = json.dumps({"event": "product.deleted", "merchant": 777, "data": {"id": 1}}).encode()

    def post(self, **headers):
        return self.client.post("/webhooks/salla/", data=self.body, content_type="application/json", **headers)

    def test_non_ascii_signature_is_401(self):
        self.assertEqual(self.post(HTTP_X_SALLA_SIGNATURE="تزوير").status_code, 401)

    def test_non_ascii_authorization_is_401(self):
        self.assertEqual(self.post(HTTP_AUTHORIZATION="Bearer سر").status_code, 401)

    def test_valid_signature_still_accepted(self):
        sig = hmac.new(b"whsec_test", self.body, hashlib.sha256).hexdigest()
        self.assertEqual(self.post(HTTP_X_SALLA_SIGNATURE=sig).status_code, 200)

    def test_bearer_token_strategy_accepted(self):
        self.assertEqual(self.post(HTTP_AUTHORIZATION="Bearer whsec_test").status_code, 200)
        self.assertEqual(self.post(HTTP_AUTHORIZATION="bearer whsec_test").status_code, 200)
        self.assertEqual(self.post(HTTP_AUTHORIZATION="Bearer wrong").status_code, 401)


class StaffSuspensionTests(TestCase):
    """إيقاف الإدارة لازم يصمد قدام إعادة تركيب سلة/زد وتفعيل اشتراك زد."""

    def setUp(self):
        user = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(user)

    def test_toggle_sets_and_clears_flag(self):
        from django.urls import reverse
        merchant = ApiToken.objects.create(label="M", image_quota=1)
        url = reverse("apitokens:toggle", args=[merchant.pk])
        self.client.post(url)
        merchant.refresh_from_db()
        self.assertEqual((merchant.is_active, merchant.suspended_by_staff), (False, True))
        self.client.post(url)
        merchant.refresh_from_db()
        self.assertEqual((merchant.is_active, merchant.suspended_by_staff), (True, False))

    def salla_authorize(self, merchant_id="1234"):
        with patch("apitokens.services.salla.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"merchant": {"name": "متجر", "domain": ""}}
            mock_get.return_value.raise_for_status.return_value = None
            return salla_service.save_authorization(
                merchant_id, {"access_token": "A", "refresh_token": "R", "expires": 4102444800},
            )

    def test_salla_reinstall_respects_staff_suspension(self):
        conn = self.salla_authorize()
        ApiToken.objects.filter(pk=conn.merchant_id).update(is_active=False, suspended_by_staff=True)
        conn = self.salla_authorize()
        conn.merchant.refresh_from_db()
        self.assertTrue(conn.is_active)
        self.assertFalse(conn.merchant.is_active)

    def test_salla_reinstall_after_uninstall_reactivates(self):
        self.salla_authorize()
        salla_service.deactivate("1234")
        conn = self.salla_authorize()
        conn.merchant.refresh_from_db()
        self.assertTrue(conn.merchant.is_active)

    def zid_install(self, store_id=4242):
        tokens = {"authorization": "A", "access_token": "M", "refresh_token": "R", "expires_in": 100}
        with patch("apitokens.services.zid.requests.post") as mock_post, \
                patch("apitokens.services.zid.requests.get") as mock_get:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = tokens
            mock_get.return_value.json.return_value = {"store": {"id": store_id, "title": "ورد"}}
            mock_get.return_value.raise_for_status.return_value = None
            return zid_service.complete_installation("CODE")

    def test_zid_reinstall_respects_staff_suspension(self):
        conn = self.zid_install()
        zid_service.deactivate(store_id="4242")
        ApiToken.objects.filter(pk=conn.merchant_id).update(suspended_by_staff=True)
        conn = self.zid_install()
        conn.merchant.refresh_from_db()
        self.assertFalse(conn.merchant.is_active)

    def test_zid_reinstall_without_suspension_reactivates(self):
        conn = self.zid_install()
        zid_service.deactivate(store_id="4242")
        conn = self.zid_install()
        conn.merchant.refresh_from_db()
        self.assertTrue(conn.merchant.is_active)

    def test_zid_subscription_activation_respects_staff_suspension(self):
        conn = self.zid_install()
        ApiToken.objects.filter(pk=conn.merchant_id).update(is_active=False, suspended_by_staff=True)
        zid_service.record_subscription_event({"event_name": "app.market.subscription.renew", "store_id": 4242})
        conn.merchant.refresh_from_db()
        self.assertFalse(conn.merchant.is_active)
        # الإدارة بترجّعه → الاشتراك الفعّال بيضل شغّال.
        ApiToken.objects.filter(pk=conn.merchant_id).update(is_active=True, suspended_by_staff=False)
        zid_service.record_subscription_event({"event_name": "app.market.subscription.renew", "store_id": 4242})
        conn.merchant.refresh_from_db()
        self.assertTrue(conn.merchant.is_active)


class SallaRefreshFailureTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="متجر", image_quota=5)
        self.connection = SallaConnection.objects.create(
            merchant=self.merchant, salla_merchant_id="888", access_token="AT", refresh_token="RT",
            token_expires_at=timezone.now() + timedelta(hours=1),
        )

    def token_response(self, status, text=""):
        resp = MagicMock(ok=status < 400, status_code=status, text=text)
        return resp

    def test_invalid_grant_deactivates_connection_and_logs_error(self):
        with patch("apitokens.services.salla.requests.post",
                   return_value=self.token_response(400, '{"error":"invalid_grant"}')), \
                self.assertLogs("apitokens.services.salla", level="ERROR") as logs:
            with self.assertRaises(salla_service.SallaReauthRequired):
                salla_service.get_access_token(self.connection)
        self.connection.refresh_from_db()
        self.assertFalse(self.connection.is_active)
        self.assertTrue(any(f"merchant={self.merchant.pk}" in line and "reauth" in line for line in logs.output))
        # الودجت نفسه بيضل شغّال على الكتالوج المحفوظ.
        self.merchant.refresh_from_db()
        self.assertTrue(self.merchant.is_active)

    def test_401_from_token_endpoint_is_permanent(self):
        with patch("apitokens.services.salla.requests.post", return_value=self.token_response(401)):
            with self.assertRaises(salla_service.SallaReauthRequired):
                salla_service.get_access_token(self.connection)
        self.connection.refresh_from_db()
        self.assertFalse(self.connection.is_active)

    def test_server_error_is_transient(self):
        with patch("apitokens.services.salla.requests.post", return_value=self.token_response(503)):
            with self.assertRaises(salla_service.SallaError) as ctx:
                salla_service.get_access_token(self.connection)
        self.assertNotIsInstance(ctx.exception, salla_service.SallaReauthRequired)
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.is_active)

    def test_sync_salla_reports_reauth(self):
        out, err = StringIO(), StringIO()
        with patch("apitokens.services.salla.requests.post", return_value=self.token_response(400, "invalid_grant")):
            call_command("sync_salla", stdout=out, stderr=err)
        self.assertIn("REAUTH REQUIRED", err.getvalue())
        self.assertIn(str(self.merchant.pk), err.getvalue())

    def test_reinstall_reactivates_connection_after_reauth(self):
        SallaConnection.objects.filter(pk=self.connection.pk).update(is_active=False)
        with patch("apitokens.services.salla.requests.get") as mock_get:
            mock_get.return_value.json.return_value = {"merchant": {}}
            salla_service.save_authorization("888", {"access_token": "A2", "refresh_token": "R2"})
        self.connection.refresh_from_db()
        self.assertTrue(self.connection.is_active)


def zid_response(body):
    resp = MagicMock(ok=True, status_code=200, content=b"{}")
    resp.json.return_value = body
    return resp


@override_settings(ZID_WEBHOOK_SECRET="zid-hook-secret", ZID_CLIENT_ID="8467",
                   SITE_BASE_URL="https://control.example.com")
class ZidStoreMaintenanceTests(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="ورد", image_quota=20)
        self.connection = ZidConnection.objects.create(
            merchant=self.merchant, store_id="4242", store_url="https://ward.zid.store/",
            authorization_token="A", manager_token="M", refresh_token="R",
        )

    def fake_api(self, store=None, hooks=None, fail_events=()):
        calls = []

        def handler(method, url, **kwargs):
            calls.append((method, url, kwargs.get("json")))
            if url.endswith("/managers/account/store"):
                return zid_response({"store": store or {}})
            if url.endswith("/managers/webhooks") and method == "GET":
                return zid_response({"webhooks": hooks or []})
            if url.endswith("/managers/webhooks") and method == "POST":
                if kwargs["json"]["event"] in fail_events:
                    return MagicMock(ok=False, status_code=422, text="nope", content=b"")
                return zid_response({})
            return zid_response({"results": [], "next": None})

        return calls, handler

    @patch("apitokens.services.catalog.ensure_embeddings")
    def test_sync_zid_refreshes_store_url(self, _emb):
        self.connection.webhooks_registered_at = timezone.now()
        self.connection.save()
        calls, handler = self.fake_api(store={"id": 4242, "url": "https://shop.ward.sa/", "title": "بوتيك الورد"})
        with patch("apitokens.services.zid.requests.request", side_effect=handler):
            call_command("sync_zid", stdout=StringIO(), stderr=StringIO())
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.store_url, "https://shop.ward.sa/")
        self.assertEqual(self.connection.store_name, "بوتيك الورد")
        # الإشعارات مسجّلة أصلاً — ما في POST.
        self.assertFalse([c for c in calls if c[0] == "POST"])
        # الودجت بيلاقي المتجر بالدومين الجديد.
        self.assertEqual(self.client.get("/widget/zid-key/", {"host": "shop.ward.sa"}).json()["store_key"],
                         self.merchant.token)

    @patch("apitokens.services.catalog.ensure_embeddings")
    def test_store_info_failure_does_not_block_sync(self, _emb):
        self.connection.webhooks_registered_at = timezone.now()
        self.connection.save()

        def handler(method, url, **kwargs):
            if url.endswith("/managers/account/store"):
                return MagicMock(ok=False, status_code=500, text="err", content=b"")
            return zid_response({"results": [], "next": None})

        with patch("apitokens.services.zid.requests.request", side_effect=handler):
            out = StringIO()
            call_command("sync_zid", stdout=out, stderr=StringIO())
        self.assertIn("0 products", out.getvalue())
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.store_url, "https://ward.zid.store/")

    @patch("apitokens.services.catalog.ensure_embeddings")
    def test_sync_zid_retries_unregistered_webhooks(self, _emb):
        calls, handler = self.fake_api()
        with patch("apitokens.services.zid.requests.request", side_effect=handler):
            call_command("sync_zid", stdout=StringIO(), stderr=StringIO())
        posts = [c for c in calls if c[0] == "POST"]
        self.assertEqual([c[2]["event"] for c in posts], zid_service.STORE_WEBHOOK_EVENTS)
        self.connection.refresh_from_db()
        self.assertIsNotNone(self.connection.webhooks_registered_at)

    def test_reregistration_skips_existing_hooks(self):
        existing = [{"event": e, "target_url": zid_service.store_webhook_url(self.connection, e)}
                    for e in ("product.create", "product.update")]
        calls, handler = self.fake_api(hooks=existing)
        with patch("apitokens.services.zid.requests.request", side_effect=handler):
            events = zid_service.register_store_webhooks(self.connection)
        self.assertEqual(events, zid_service.STORE_WEBHOOK_EVENTS)
        posted = [c[2]["event"] for c in calls if c[0] == "POST"]
        self.assertEqual(posted, ["product.publish", "product.delete", "order.create"])

    def test_missing_order_permission_still_counts_as_registered(self):
        calls, handler = self.fake_api(fail_events=("order.create",))
        with patch("apitokens.services.zid.requests.request", side_effect=handler):
            zid_service.register_store_webhooks(self.connection)
        self.connection.refresh_from_db()
        self.assertIsNotNone(self.connection.webhooks_registered_at)

    def test_failed_product_hook_leaves_registration_pending(self):
        calls, handler = self.fake_api(fail_events=("product.update",))
        with patch("apitokens.services.zid.requests.request", side_effect=handler):
            zid_service.register_store_webhooks(self.connection)
        self.connection.refresh_from_db()
        self.assertIsNone(self.connection.webhooks_registered_at)


@override_settings(ZID_CLIENT_ID="zid-client", ZID_CLIENT_SECRET="zid-secret", SITE_BASE_URL="https://control.example.com")
class ZidAppRevisitTests(TestCase):
    TOKENS = {"authorization": "AUTH1", "access_token": "MGR1", "refresh_token": "REF1", "expires_in": 100}
    STORE = {"store": {"id": 4242, "uuid": "u-1", "title": "بوتيك الورد", "url": "https://ward.zid.store/"}}

    def oauth_round_trip(self):
        """زيارة /zid/app/ بجلسة جديدة: install ← callback."""
        self.client.cookies.clear()
        self.assertRedirects(self.client.get("/zid/app/"), "/zid/install/", fetch_redirect_response=False)
        from urllib.parse import parse_qs, urlparse
        state = parse_qs(urlparse(self.client.get("/zid/install/")["Location"]).query)["state"][0]
        with patch("apitokens.services.zid.requests.post") as mock_post, \
                patch("apitokens.services.zid.requests.get") as mock_get, \
                patch("apitokens.zid_webhooks.start_initial_sync") as mock_sync:
            mock_post.return_value.ok = True
            mock_post.return_value.json.return_value = dict(self.TOKENS)
            mock_get.return_value.json.return_value = self.STORE
            mock_get.return_value.raise_for_status.return_value = None
            resp = self.client.get("/zid/callback/", {"code": "C", "state": state})
        self.assertRedirects(resp, "/zid/app/", fetch_redirect_response=False)
        return mock_sync

    def test_revisit_of_set_up_store_skips_sync_and_registration(self):
        self.assertEqual(self.oauth_round_trip().call_count, 1)
        ZidConnection.objects.update(last_synced_at=timezone.now(), webhooks_registered_at=timezone.now())
        self.assertEqual(self.oauth_round_trip().call_count, 0)
        self.assertEqual(self.client.get("/zid/app/").status_code, 200)

    def test_revisit_without_registered_webhooks_runs_setup_again(self):
        self.oauth_round_trip()
        ZidConnection.objects.update(last_synced_at=timezone.now())
        self.assertEqual(self.oauth_round_trip().call_count, 1)

    def test_revisit_does_not_reset_usage(self):
        self.oauth_round_trip()
        ZidSubscription.objects.create(store_id="4242", plan_name="Basic", is_active=True)
        ApiToken.objects.update(images_used=9)
        self.oauth_round_trip()
        self.assertEqual(ApiToken.objects.get().images_used, 9)

    def test_reinstall_after_uninstall_runs_setup(self):
        self.oauth_round_trip()
        ZidConnection.objects.update(last_synced_at=timezone.now(), webhooks_registered_at=timezone.now())
        zid_service.deactivate(store_id="4242")
        self.assertEqual(self.oauth_round_trip().call_count, 1)
