"""اختبارات عقد الودجت مع توزيع الرصيد: /widget/tryon/ و page-product و chat و event، وقياس كلفة الـLLM."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import (
    ApiToken, Conversation, LlmUsage, Product, QuotaReservation, RateBucket, TryOnEvent,
)
from .services import kieai, llm, quota, ratelimit

SHOPPER = "shopper-abc12345"
LOCAL_IP_HASH = ratelimit.hash_ip("127.0.0.1")  # عنوان Client() بالاختبارات
HIGH_LIMITS = {
    "tryon_ip": (1000, 60), "tryon_token": (1000, 60), "chat_ip": (1000, 60), "chat_ip_hour": (1000, 3600),
    "chat_token": (1000, 60), "event_ip": (1000, 60), "pageproduct_ip": (1000, 60),
}


def make_merchant(token="QWIDGET00001", quota_=10, **kw):
    return ApiToken.objects.create(label=token, token=token, image_quota=quota_, **kw)


class TryOnBase(TestCase):
    def setUp(self):
        self.merchant = make_merchant(quota_=10)
        self.product = Product.objects.create(
            merchant=self.merchant, sku="DRESS-1", title="Dress", price="10", image_url="https://e.com/d.jpg",
        )
        self.client = Client()

    def post(self, shopper_id=SHOPPER, session_id="sess-abcdef12", sku="DRESS-1", token=None, **meta):
        data = {"sku": sku, "photo": SimpleUploadedFile("me.jpg", b"fakejpg", content_type="image/jpeg")}
        if session_id is not None:
            data["session_id"] = session_id
        if shopper_id is not None:
            data["shopper_id"] = shopper_id
        return self.client.post(
            "/widget/tryon/", data=data, HTTP_X_API_TOKEN=self.merchant.token if token is None else token, **meta,
        )

    def used(self):
        self.merchant.refresh_from_db()
        return self.merchant.images_used


@override_settings(KIEAI_API_KEY="test-key", RATE_LIMITS=HIGH_LIMITS)
class TryOnContractTests(TryOnBase):
    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_success_contract_and_event_fields(self, gen):
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(), {"ok": True, "image_url": "https://img/out.png", "remaining": 9, "shopper_remaining": 2},
        )
        self.assertEqual(self.used(), 1)
        event = TryOnEvent.objects.get()
        self.assertTrue(event.success)
        self.assertEqual(event.shopper_id, SHOPPER)
        self.assertEqual(event.ip_hash, LOCAL_IP_HASH)
        self.assertEqual(event.session_id, "sess-abcdef12")
        self.assertEqual(QuotaReservation.objects.count(), 0)
        self.assertNotIn("127.0.0.1", event.ip_hash)

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_unlimited_shopper_remaining_is_null(self, _gen):
        self.merchant.tryons_per_shopper = 0
        self.merchant.save()
        data = self.post().json()
        self.assertTrue(data["ok"])
        self.assertIsNone(data["shopper_remaining"])

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_remaining_excludes_other_in_flight_reservations(self, _gen):
        QuotaReservation.objects.create(
            merchant=self.merchant, expires_at=timezone.now() + timedelta(seconds=100),
        )
        self.assertEqual(self.post().json()["remaining"], 8)

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_shopper_limit_reached_contract(self, gen):
        for _ in range(3):
            self.assertTrue(self.post().json()["ok"])
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["ok"], False)
        self.assertEqual(data["error"], "shopper_limit_reached")
        self.assertEqual(data["shopper_remaining"], 0)
        self.assertTrue(86000 < data["retry_after_seconds"] <= 86401)
        self.assertEqual(gen.call_count, 3)
        self.assertEqual(self.used(), 3)

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_store_daily_limit_contract(self, gen):
        self.merchant.tryons_per_shopper = 0
        self.merchant.image_quota = 100
        self.merchant.save()
        for i in range(20):
            TryOnEvent.objects.create(merchant=self.merchant, sku="S", success=True)
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual((data["ok"], data["error"]), (False, "store_daily_limit"))
        self.assertTrue(0 < data["retry_after_seconds"] <= 86401)
        self.assertNotIn("shopper_remaining", data)
        gen.assert_not_called()

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_quota_exceeded_contract_and_no_free_image(self, gen):
        self.merchant.images_used = 10
        self.merchant.save()
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": False, "error": "quota_exceeded", "remaining": 0})
        gen.assert_not_called()
        self.assertEqual(TryOnEvent.objects.count(), 0)
        self.assertEqual(QuotaReservation.objects.count(), 0)

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_last_slot_held_by_another_request_is_not_double_spent(self, gen):
        """طلب متزامن بنص التوليد حاجز آخر مكان: هالطلب لازم يرجع quota_exceeded وما يتولّد شي."""
        self.merchant.images_used = 9
        self.merchant.save()
        QuotaReservation.objects.create(
            merchant=self.merchant, expires_at=timezone.now() + timedelta(seconds=100),
        )
        self.assertEqual(self.post().json()["error"], "quota_exceeded")
        gen.assert_not_called()

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_expired_reservation_does_not_block(self, gen):
        self.merchant.images_used = 9
        self.merchant.save()
        QuotaReservation.objects.create(
            merchant=self.merchant, expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertTrue(self.post().json()["ok"])

    @patch("apitokens.widget_views.kieai.generate_tryon")
    def test_reservation_exists_while_generating_and_pool_check_sees_it(self, gen):
        seen = {}

        def during(**kwargs):
            seen["reservations"] = QuotaReservation.objects.filter(merchant=self.merchant).count()
            seen["remaining"] = quota.quota_status(self.merchant)["remaining"]
            return "https://img/out.png"

        gen.side_effect = during
        self.post()
        self.assertEqual(seen, {"reservations": 1, "remaining": 9})

    @patch("apitokens.widget_views.kieai.generate_tryon", side_effect=kieai.TryOnError("boom"))
    def test_kieai_failure_releases_reservation_and_does_not_count(self, gen):
        for _ in range(5):  # بتفوت حد الزبون (3) لو كانت الفاشلة بتنحسب
            resp = self.post()
            self.assertEqual(resp.status_code, 502)
            self.assertEqual(resp.json(), {"ok": False, "error": "boom"})
        self.assertEqual(self.used(), 0)
        self.assertEqual(QuotaReservation.objects.count(), 0)
        events = TryOnEvent.objects.all()
        self.assertEqual(events.count(), 5)
        self.assertTrue(all(not e.success and e.shopper_id == SHOPPER and e.ip_hash == LOCAL_IP_HASH for e in events))
        self.assertEqual(quota.shopper_status(self.merchant, SHOPPER, LOCAL_IP_HASH)["remaining"], 3)

    @patch("apitokens.widget_views.kieai.generate_tryon", side_effect=RuntimeError("network exploded"))
    def test_unexpected_exception_releases_and_records_failure(self, _gen):
        with self.assertLogs("apitokens.widget_views", level="ERROR"):
            resp = self.post()
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(resp.json()["ok"])
        self.assertNotIn("network exploded", resp.content.decode())
        self.assertEqual(self.used(), 0)
        self.assertEqual(QuotaReservation.objects.count(), 0)
        event = TryOnEvent.objects.get()
        self.assertFalse(event.success)

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_finalize_crash_never_delivers_a_free_image(self, _gen):
        with patch("apitokens.widget_views.quota.finalize_success", side_effect=RuntimeError("db")), \
                self.assertLogs("apitokens.widget_views", level="ERROR"):
            resp = self.post()
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn("image_url", resp.json())
        self.assertEqual(QuotaReservation.objects.count(), 0)
        self.assertEqual(self.used(), 0)

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_validation_and_auth_errors_never_reserve(self, gen):
        self.assertEqual(self.post(sku="NOPE").status_code, 404)
        self.assertEqual(self.post(token="WRONG").status_code, 401)
        self.assertEqual(self.post(token="").status_code, 401)
        self.merchant.is_active = False
        self.merchant.save()
        resp = self.post()
        self.assertEqual((resp.status_code, resp.json()["error"]), (403, "token_disabled"))
        self.assertEqual(QuotaReservation.objects.count(), 0)
        gen.assert_not_called()

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_bad_shopper_id_is_discarded(self, _gen):
        self.post(shopper_id="bad id; drop table")
        self.assertEqual(TryOnEvent.objects.get().shopper_id, "")

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_old_widget_without_shopper_id_is_limited_by_ip_backstop(self, _gen):
        self.merchant.image_quota = 100  # سقف يومي 20، فالحد الأول هو الـIP (9)
        self.merchant.save()
        for _ in range(9):
            self.assertTrue(self.post(shopper_id=None).json()["ok"])
        data = self.post(shopper_id=None).json()
        self.assertEqual(data["error"], "shopper_limit_reached")
        # IP تانية بتمر
        other = self.post(shopper_id=None, REMOTE_ADDR="198.51.100.20").json()
        self.assertTrue(other["ok"])

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_clearing_storage_does_not_dodge_the_limit(self, _gen):
        self.merchant.image_quota = 100
        self.merchant.save()
        for i in range(9):
            self.assertTrue(self.post(shopper_id=f"fresh-id-{i:04d}").json()["ok"])
        self.assertEqual(self.post(shopper_id="fresh-id-9999").json()["error"], "shopper_limit_reached")

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_conversation_counter_still_increments(self, _gen):
        convo = Conversation.objects.create(merchant=self.merchant, session_id="sess-abcdef12")
        self.post()
        convo.refresh_from_db()
        self.assertEqual(convo.tryons, 1)

    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_same_shopper_id_at_another_merchant_is_independent(self, _gen):
        other = make_merchant("QWIDGET00002", quota_=10)
        Product.objects.create(merchant=other, sku="DRESS-1", title="D", price="1", image_url="https://e.com/d.jpg")
        for _ in range(3):
            self.post()
        self.assertTrue(self.post(token=other.token).json()["ok"])
        self.assertEqual(self.post().json()["error"], "shopper_limit_reached")


@override_settings(KIEAI_API_KEY="test-key", RATE_LIMITS=HIGH_LIMITS)
class TryOnAlertTests(TryOnBase):
    @patch("apitokens.services.quota_alerts.maybe_send_quota_alerts")
    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_alerts_called_after_success_with_merchant(self, _gen, alerts):
        self.assertTrue(self.post().json()["ok"])
        alerts.assert_called_once()
        self.assertEqual(alerts.call_args.args[0].pk, self.merchant.pk)

    @patch("apitokens.services.quota_alerts.maybe_send_quota_alerts", side_effect=RuntimeError("whatsapp down"))
    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_alert_exception_never_breaks_the_response(self, _gen, alerts):
        with self.assertLogs("apitokens.widget_views", level="ERROR"):
            resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(self.used(), 1)

    @patch("apitokens.services.quota_alerts.maybe_send_quota_alerts")
    @patch("apitokens.widget_views.kieai.generate_tryon", side_effect=kieai.TryOnError("boom"))
    def test_no_alert_on_failure_or_refusal(self, _gen, alerts):
        self.post()
        self.merchant.images_used = 10
        self.merchant.save()
        self.post()
        alerts.assert_not_called()


@override_settings(KIEAI_API_KEY="test-key")
class TryOnRateLimitTests(TryOnBase):
    @override_settings(RATE_LIMITS={**HIGH_LIMITS, "tryon_ip": (2, 60)})
    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_ip_limit_returns_429_before_any_work(self, gen):
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 200)
        resp = self.post()
        self.assertEqual(resp.status_code, 429)
        data = resp.json()
        self.assertEqual((data["ok"], data["error"]), (False, "rate_limited"))
        self.assertTrue(1 <= data["retry_after_seconds"] <= 60)
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(self.used(), 2)
        self.assertEqual(resp["Access-Control-Allow-Origin"], "*")

    @override_settings(RATE_LIMITS={**HIGH_LIMITS, "tryon_ip": (2, 60)})
    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_ip_limit_is_per_ip(self, _gen):
        for _ in range(2):
            self.post()
        self.assertEqual(self.post().status_code, 429)
        self.assertEqual(self.post(REMOTE_ADDR="198.51.100.30").status_code, 200)

    @override_settings(RATE_LIMITS={**HIGH_LIMITS, "tryon_token": (1, 60)})
    @patch("apitokens.widget_views.kieai.generate_tryon", return_value="https://img/out.png")
    def test_token_limit_returns_429_and_is_per_merchant(self, gen):
        self.assertEqual(self.post().status_code, 200)
        resp = self.post(REMOTE_ADDR="198.51.100.31")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"], "rate_limited")
        other = make_merchant("QWIDGET00003", quota_=5)
        Product.objects.create(merchant=other, sku="DRESS-1", title="D", price="1", image_url="https://e.com/d.jpg")
        self.assertEqual(self.post(token=other.token).status_code, 200)
        self.assertEqual(gen.call_count, 2)

    @override_settings(RATE_LIMITS={**HIGH_LIMITS, "tryon_token": (1, 60)})
    def test_unknown_tokens_do_not_create_buckets(self):
        self.post(token="NOSUCHTOKEN")
        self.assertFalse(RateBucket.objects.filter(key__startswith="tryon_token").exists())


@override_settings(RATE_LIMITS=HIGH_LIMITS)
class PageProductQuotaTests(TestCase):
    def setUp(self):
        self.merchant = make_merchant("QPAGE0000001", quota_=10)
        self.product = Product.objects.create(
            merchant=self.merchant, sku="DRESS-1", title="Dress", price="10", image_url="https://e.com/d.jpg",
            product_url="https://shop.example.com/products/dress",
        )
        self.client = Client()

    def lookup(self, url="https://shop.example.com/products/dress", shopper_id=SHOPPER, **extra):
        body = {"page_url": url}
        if shopper_id is not None:
            body["shopper_id"] = shopper_id
        return self.client.post(
            "/widget/page-product/", data=json.dumps(body), content_type="application/json",
            HTTP_X_API_TOKEN=extra.pop("token", self.merchant.token), **extra,
        )

    def test_fields_for_a_fresh_shopper(self):
        data = self.lookup().json()
        self.assertEqual(data["product"]["sku"], "DRESS-1")
        self.assertEqual(data["shopper_tryons_left"], 3)
        self.assertIs(data["tryon_available"], True)
        self.assertIsNone(data["tryon_retry_after_seconds"])

    def test_fields_also_present_when_page_is_not_a_catalog_product(self):
        data = self.lookup(url="https://shop.example.com/other").json()
        self.assertIsNone(data["product"])
        self.assertEqual((data["shopper_tryons_left"], data["tryon_available"]), (3, True))

    def test_shopper_out_of_tryons(self):
        for _ in range(3):
            TryOnEvent.objects.create(
                merchant=self.merchant, shopper_id=SHOPPER, ip_hash=LOCAL_IP_HASH, sku="S", success=True,
            )
        data = self.lookup().json()
        self.assertEqual(data["shopper_tryons_left"], 0)
        self.assertIs(data["tryon_available"], False)
        self.assertTrue(86000 < data["tryon_retry_after_seconds"] <= 86401)
        # زبون تاني على IP تاني: متاح
        self.assertIs(self.lookup(shopper_id="another-shopper-1", REMOTE_ADDR="198.51.100.40").json()["tryon_available"], True)

    def test_unlimited_is_null_left_and_available(self):
        self.merchant.tryons_per_shopper = 0
        self.merchant.save()
        data = self.lookup().json()
        self.assertIsNone(data["shopper_tryons_left"])
        self.assertIs(data["tryon_available"], True)

    def test_pool_exhausted(self):
        self.merchant.images_used = 10
        self.merchant.save()
        data = self.lookup().json()
        self.assertIs(data["tryon_available"], False)
        self.assertIsNone(data["tryon_retry_after_seconds"])

    def test_store_daily_cap_reached(self):
        self.merchant.image_quota = 100
        self.merchant.tryons_per_shopper = 0
        self.merchant.save()
        for _ in range(20):
            TryOnEvent.objects.create(merchant=self.merchant, sku="S", success=True)
        data = self.lookup().json()
        self.assertIs(data["tryon_available"], False)
        self.assertTrue(data["tryon_retry_after_seconds"] > 0)

    def test_no_shopper_id_counts_by_ip(self):
        for _ in range(9):
            TryOnEvent.objects.create(merchant=self.merchant, ip_hash=LOCAL_IP_HASH, sku="S", success=True)
        data = self.lookup(shopper_id=None).json()
        self.assertEqual(data["shopper_tryons_left"], 0)
        self.assertIs(data["tryon_available"], False)

    def test_other_merchant_activity_is_ignored(self):
        other = make_merchant("QPAGE0000002", quota_=10)
        for _ in range(3):
            TryOnEvent.objects.create(merchant=other, shopper_id=SHOPPER, ip_hash=LOCAL_IP_HASH, sku="S", success=True)
        self.assertEqual(self.lookup().json()["shopper_tryons_left"], 3)

    @override_settings(RATE_LIMITS={**HIGH_LIMITS, "pageproduct_ip": (2, 60)})
    def test_rate_limited(self):
        self.assertEqual(self.lookup().status_code, 200)
        self.assertEqual(self.lookup().status_code, 200)
        resp = self.lookup()
        self.assertEqual(resp.status_code, 429)
        data = resp.json()
        self.assertEqual((data["ok"], data["error"]), (False, "rate_limited"))
        self.assertTrue(isinstance(data["retry_after_seconds"], int) and data["retry_after_seconds"] >= 1)


class FakeProvider:
    def __init__(self, results):
        self.results = results
        self.calls = 0

    def is_configured(self):
        return True

    def chat(self, system_prompt, messages, tools, force_tool=None):
        self.calls += 1
        result = self.results[min(self.calls - 1, len(self.results) - 1)]
        return result


@override_settings(RATE_LIMITS=HIGH_LIMITS)
class ChatLimitTests(TestCase):
    def setUp(self):
        self.merchant = make_merchant("QCHAT0000001", quota_=10)
        Product.objects.create(
            merchant=self.merchant, sku="SHIRT-1", title="Blue Shirt", price="29", image_url="https://e.com/1.jpg",
        )
        self.client = Client()
        patcher = patch("apitokens.widget_views.llm.get_provider", return_value=FakeProvider(
            [llm.LLMResult(reply_text="أهلا فيك", tool_calls=[])],
        ))
        patcher.start()
        self.addCleanup(patcher.stop)
        # ما بدنا ردّ بنص بدون أداة يفعّل استدعاءات إضافية
        self.chat_patch = patch("apitokens.widget_views.llm.chat_with_retry")
        self.mock_chat = self.chat_patch.start()
        self.addCleanup(self.chat_patch.stop)
        self.mock_chat.return_value = llm.LLMResult(reply_text="أهلا فيك", tool_calls=[])

    def chat(self, text="مرحبا", shopper_id=SHOPPER, token=None, **meta):
        body = {"messages": [{"role": "user", "content": text}]}
        if shopper_id is not None:
            body["shopper_id"] = shopper_id
        return self.client.post(
            "/widget/chat/", data=json.dumps(body), content_type="application/json",
            HTTP_X_API_TOKEN=token or self.merchant.token, **meta,
        )

    @override_settings(RATE_LIMITS={**HIGH_LIMITS, "chat_ip": (2, 60)})
    def test_chat_ip_limit(self):
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(self.chat().status_code, 200)
        resp = self.chat()
        self.assertEqual(resp.status_code, 429)
        data = resp.json()
        self.assertEqual((data["ok"], data["error"]), (False, "rate_limited"))
        self.assertTrue(1 <= data["retry_after_seconds"] <= 60)
        self.assertEqual(self.mock_chat.call_count, 2)
        self.assertEqual(self.chat(REMOTE_ADDR="198.51.100.50").status_code, 200)

    @override_settings(RATE_LIMITS={**HIGH_LIMITS, "chat_ip_hour": (2, 3600)})
    def test_chat_ip_hour_limit(self):
        self.chat()
        self.chat()
        resp = self.chat()
        self.assertEqual(resp.status_code, 429)
        self.assertTrue(resp.json()["retry_after_seconds"] <= 3600)

    @override_settings(RATE_LIMITS={**HIGH_LIMITS, "chat_token": (2, 60)})
    def test_chat_token_limit_is_shared_across_ips_but_not_merchants(self):
        self.chat(REMOTE_ADDR="198.51.100.1")
        self.chat(REMOTE_ADDR="198.51.100.2")
        self.assertEqual(self.chat(REMOTE_ADDR="198.51.100.3").status_code, 429)
        other = make_merchant("QCHAT0000002", quota_=5)
        self.assertEqual(self.chat(token=other.token, REMOTE_ADDR="198.51.100.4").status_code, 200)

    def test_bad_token_and_bad_requests_do_not_hit_the_token_limit_or_daily_cap(self):
        with override_settings(CHAT_MAX_MESSAGES_PER_SHOPPER_DAY=1):
            bad = self.client.post(
                "/widget/chat/", data=json.dumps({"messages": []}), content_type="application/json",
                HTTP_X_API_TOKEN=self.merchant.token,
            )
            self.assertEqual(bad.status_code, 400)
            self.assertEqual(self.chat(token="NOSUCH").status_code, 401)
            self.assertEqual(self.chat().status_code, 200)

    @override_settings(CHAT_MAX_MESSAGES_PER_SHOPPER_DAY=3)
    def test_per_shopper_daily_cap(self):
        for _ in range(3):
            self.assertEqual(self.chat().status_code, 200)
        resp = self.chat()
        self.assertEqual(resp.status_code, 429)
        data = resp.json()
        self.assertEqual((data["ok"], data["error"]), (False, "chat_limit_reached"))
        self.assertTrue(86000 < data["retry_after_seconds"] <= 86400)
        self.assertEqual(self.mock_chat.call_count, 3)  # الرابعة ما كلّفتنا استدعاء LLM
        # زبون تاني (نفس الـIP) مو متأثر
        self.assertEqual(self.chat(shopper_id="another-shopper-1").status_code, 200)

    @override_settings(CHAT_MAX_MESSAGES_PER_SHOPPER_DAY=2)
    def test_cap_falls_back_to_ip_without_shopper_id(self):
        self.assertEqual(self.chat(shopper_id=None).status_code, 200)
        self.assertEqual(self.chat(shopper_id=None).status_code, 200)
        self.assertEqual(self.chat(shopper_id=None).json()["error"], "chat_limit_reached")
        self.assertEqual(self.chat(shopper_id=None, REMOTE_ADDR="198.51.100.60").status_code, 200)
        # الودجت الجديد بمعرّف مختلف على نفس الـIP عدّاده لحاله
        self.assertEqual(self.chat().status_code, 200)

    @override_settings(CHAT_MAX_MESSAGES_PER_SHOPPER_DAY=1)
    def test_cap_is_per_merchant(self):
        other = make_merchant("QCHAT0000003", quota_=5)
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(self.chat().status_code, 429)
        self.assertEqual(self.chat(token=other.token).status_code, 200)

    @override_settings(CHAT_MAX_MESSAGES_PER_SHOPPER_DAY=1)
    def test_cap_window_resets_after_a_day(self):
        self.chat()
        self.assertEqual(self.chat().status_code, 429)
        RateBucket.objects.filter(key__startswith="chatday:").update(
            window_start=timezone.now() - timedelta(hours=24, seconds=1),
        )
        self.assertEqual(self.chat().status_code, 200)

    def test_chat_does_not_consume_image_quota(self):
        for _ in range(5):
            self.chat()
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 0)
        self.assertEqual(QuotaReservation.objects.count(), 0)

    def test_invalid_shopper_id_falls_back_to_ip_counter(self):
        self.chat(shopper_id="bad id!")
        self.assertTrue(RateBucket.objects.filter(key=f"chatday:{self.merchant.pk}:ip-{LOCAL_IP_HASH}").exists())


@override_settings(RATE_LIMITS=HIGH_LIMITS)
class EventLimitTests(TestCase):
    def setUp(self):
        self.merchant = make_merchant("QEVENT000001", quota_=5)
        Conversation.objects.create(merchant=self.merchant, session_id="sess-abcdef12")

    def send(self, **extra):
        return self.client.post(
            "/widget/event/",
            data=json.dumps({"type": "buy_click", "session_id": "sess-abcdef12", "shopper_id": SHOPPER}),
            content_type="application/json", HTTP_X_API_TOKEN=self.merchant.token, **extra,
        )

    def test_event_still_works_with_shopper_id(self):
        self.assertEqual(self.send().json(), {"ok": True})
        self.assertEqual(Conversation.objects.get().buy_clicks, 1)

    @override_settings(RATE_LIMITS={**HIGH_LIMITS, "event_ip": (2, 60)})
    def test_event_ip_limit(self):
        self.send()
        self.send()
        resp = self.send()
        self.assertEqual(resp.status_code, 429)
        data = resp.json()
        self.assertEqual((data["ok"], data["error"]), (False, "rate_limited"))
        self.assertTrue(data["retry_after_seconds"] >= 1)
        self.assertEqual(Conversation.objects.get().buy_clicks, 2)
        self.assertEqual(self.send(REMOTE_ADDR="198.51.100.70").status_code, 200)


@override_settings(RATE_LIMITS=HIGH_LIMITS)
class LlmUsageMeteringTests(TestCase):
    """كل استدعاء LLM بالشات (بما فيه الإعادة والأداة الإجبارية) بيتسجّل بـLlmUsage اليومي."""

    def setUp(self):
        self.merchant = make_merchant("QUSAGE000001", quota_=5)
        Product.objects.create(
            merchant=self.merchant, sku="SHIRT-1", title="Blue Shirt", price="29", image_url="https://e.com/1.jpg",
        )
        self.client = Client()

    def chat(self, text="مرحبا", token=None):
        return self.client.post(
            "/widget/chat/", data=json.dumps({"messages": [{"role": "user", "content": text}]}),
            content_type="application/json", HTTP_X_API_TOKEN=token or self.merchant.token,
        )

    def provider(self, *results):
        provider = FakeProvider(list(results))
        patcher = patch("apitokens.widget_views.llm.get_provider", return_value=provider)
        patcher.start()
        self.addCleanup(patcher.stop)
        return provider

    def row(self, merchant=None):
        return LlmUsage.objects.get(merchant=merchant or self.merchant, day=timezone.localdate())

    def test_single_call_with_tokens(self):
        self.provider(llm.LLMResult("أهلا", [], usage=llm.TokenUsage(120, 30)))
        self.assertEqual(self.chat().status_code, 200)
        row = self.row()
        self.assertEqual((row.calls, row.input_tokens, row.output_tokens), (1, 120, 30))

    def test_call_without_usage_counts_the_call_only(self):
        self.provider(llm.LLMResult("أهلا", []))
        self.chat()
        row = self.row()
        self.assertEqual((row.calls, row.input_tokens, row.output_tokens), (1, 0, 0))

    def test_partial_usage(self):
        self.provider(llm.LLMResult("أهلا", [], usage=llm.TokenUsage(50, None)))
        self.chat()
        row = self.row()
        self.assertEqual((row.calls, row.input_tokens, row.output_tokens), (1, 50, 0))

    def test_forced_tool_call_is_counted_too(self):
        # "بدي اشتريه" + رد بلا أداة = استدعاء إجباري لـrecommend_product
        forced = llm.LLMResult("", [{"name": "recommend_product", "input": {"sku": "SHIRT-1", "reason": "x"}}],
                               usage=llm.TokenUsage(70, 5))
        provider = self.provider(llm.LLMResult("تمام", [], usage=llm.TokenUsage(100, 10)), forced)
        resp = self.chat("بدي اشتريه")
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(provider.calls, 2)
        row = self.row()
        self.assertEqual(row.calls, provider.calls)
        self.assertEqual(row.input_tokens, 100 + 70 * (provider.calls - 1))
        self.assertEqual(row.output_tokens, 10 + 5 * (provider.calls - 1))

    def test_empty_reply_retry_is_counted(self):
        provider = self.provider(
            llm.LLMResult("", [], usage=llm.TokenUsage(10, 0)), llm.LLMResult("هلا", [], usage=llm.TokenUsage(10, 3)),
        )
        self.chat()
        self.assertEqual(provider.calls, 2)
        self.assertEqual(self.row().calls, 2)
        self.assertEqual((self.row().input_tokens, self.row().output_tokens), (20, 3))

    def test_accumulates_across_requests_in_one_row(self):
        self.provider(llm.LLMResult("أهلا", [], usage=llm.TokenUsage(10, 1)))
        self.chat()
        self.chat()
        self.assertEqual(LlmUsage.objects.count(), 1)
        self.assertEqual((self.row().calls, self.row().input_tokens), (2, 20))

    def test_merchant_isolation(self):
        other = make_merchant("QUSAGE000002", quota_=5)
        self.provider(llm.LLMResult("أهلا", [], usage=llm.TokenUsage(10, 1)))
        self.chat()
        self.chat(token=other.token)
        self.chat(token=other.token)
        self.assertEqual(self.row().calls, 1)
        self.assertEqual(self.row(other).calls, 2)

    def test_failed_llm_call_is_not_counted_and_recording_errors_never_break_the_reply(self):
        self.provider(llm.LLMResult("أهلا", [], usage=llm.TokenUsage(10, 1)))
        with patch("apitokens.services.quota.LlmUsage.objects.filter", side_effect=Exception("db")), \
                self.assertLogs("apitokens.services.quota", level="ERROR"):
            resp = self.chat()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["reply"], "أهلا")

    def test_provider_error_records_nothing(self):
        provider = MagicMock()
        provider.is_configured.return_value = True
        provider.chat.side_effect = llm.LLMError("overloaded")
        with patch("apitokens.widget_views.llm.get_provider", return_value=provider), \
                patch("apitokens.services.llm.time.sleep"):
            resp = self.chat()
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(LlmUsage.objects.exists())

    def test_works_with_mocked_chat_with_retry_returning_plain_results(self):
        with patch("apitokens.widget_views.llm.get_provider", return_value=FakeProvider([])), \
                patch("apitokens.widget_views.llm.chat_with_retry", return_value=MagicMock(
                    reply_text="hi", tool_calls=[], usage=SimpleNamespace(input_tokens="x", output_tokens=None),
                )) as mocked:
            self.assertEqual(self.chat().status_code, 200)
        self.assertEqual((self.row().calls, self.row().input_tokens), (mocked.call_count, 0))


class ProviderUsageExtractionTests(TestCase):
    """استخراج usage من ردود الـSDK (كلها fakes، بدون شبكة)."""

    def test_anthropic(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")], usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )
        client = MagicMock()
        client.messages.create.return_value = response
        with patch("anthropic.Anthropic", return_value=client):
            result = llm.AnthropicProvider("k", "m").chat("sys", [{"role": "user", "content": "x"}], [])
        self.assertEqual((result.reply_text, result.usage), ("hi", llm.TokenUsage(11, 7)))

    def test_openai_responses_api(self):
        response = SimpleNamespace(
            output=[], output_text="yo", usage=SimpleNamespace(input_tokens=21, output_tokens=9),
        )
        client = MagicMock()
        client.responses.create.return_value = response
        with patch("openai.OpenAI", return_value=client):
            result = llm.OpenAIProvider("k", "m").chat("sys", [{"role": "user", "content": "x"}], [])
        self.assertEqual(result.usage, llm.TokenUsage(21, 9))

    def test_openai_compatible_chat_completions(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="yo", tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=31, completion_tokens=4),
        )
        client = MagicMock()
        client.chat.completions.create.return_value = response
        with patch("openai.OpenAI", return_value=client):
            result = llm.OpenAIProvider("k", "m", base_url="https://api.groq.com/openai/v1").chat(
                "sys", [{"role": "user", "content": "x"}], [],
            )
        self.assertEqual(result.usage, llm.TokenUsage(31, 4))

    def test_gemini_counts_thinking_tokens_as_output(self):
        response = SimpleNamespace(
            function_calls=None, text="hey",
            usage_metadata=SimpleNamespace(prompt_token_count=41, candidates_token_count=6, thoughts_token_count=10),
        )
        client = MagicMock()
        client.models.generate_content.return_value = response
        with patch("google.genai.Client", return_value=client):
            result = llm.GeminiProvider("k", "m").chat("sys", [{"role": "user", "content": "x"}], [])
        self.assertEqual(result.usage, llm.TokenUsage(41, 16))

    def test_missing_or_broken_usage_is_none_and_never_raises(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hi")], usage=None,
        )
        client = MagicMock()
        client.messages.create.return_value = response
        with patch("anthropic.Anthropic", return_value=client):
            result = llm.AnthropicProvider("k", "m").chat("sys", [{"role": "user", "content": "x"}], [])
        self.assertIsNone(result.usage)
        self.assertIsNone(llm._usage(SimpleNamespace(), "usage", "input_tokens", "output_tokens"))
        self.assertIsNone(llm._usage(
            SimpleNamespace(usage=SimpleNamespace(input_tokens="x", output_tokens=True)),
            "usage", "input_tokens", "output_tokens",
        ))

        class Exploding:
            @property
            def usage(self):
                raise RuntimeError("boom")

        self.assertIsNone(llm._usage(Exploding(), "usage", "input_tokens", "output_tokens"))

    def test_gemini_without_thoughts_attr_and_partial(self):
        usage = llm._usage(
            SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=5, candidates_token_count=None)),
            "usage_metadata", "prompt_token_count", "candidates_token_count", "thoughts_token_count",
        )
        self.assertEqual(usage, llm.TokenUsage(5, None))

    def test_llm_result_usage_defaults_to_none(self):
        self.assertIsNone(llm.LLMResult(reply_text="x").usage)
