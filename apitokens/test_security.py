"""اختبارات الأمان: (1) فصل مفتاح الودجت العلني عن اعتماد endpoint الاستهلاك مع
بقاء n8n الشرعي شغّال، (2) تحديد معدّل بمخزن مشترك: تزامن حقيقي (خيوط) وأعطال
المخزن (فشل مغلق للحساس، مفتوح مع خطأ للخفيف).
"""

import json
import re
import threading
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import checks
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings

from . import apps as app_checks
from .models import ApiToken, DiscountCode, Product, RateLimitBucket
from .services import consume_auth, llm, ratelimit

CONSUME_URL = "/api/tokens/consume/"
INTERNET = {"HTTP_X_FORWARDED_FOR": "203.0.113.9", "HTTP_X_FORWARDED_PROTO": "https", "REMOTE_ADDR": "127.0.0.1"}


def consume(headers=None, body=None, as_form=False, **meta):
    client = Client()
    payload = body if body is not None else {"count": 1}
    kwargs = {"data": payload} if as_form else {"data": json.dumps(payload), "content_type": "application/json"}
    return client.post(CONSUME_URL, **kwargs, **{**{"REMOTE_ADDR": "127.0.0.1"}, **meta}, **(headers or {}))


def pub(token):
    return {"HTTP_X_API_TOKEN": token}


def key_hdr(key):
    return {"HTTP_X_CONSUME_KEY": key}


class ConsumeAuthBase(TestCase):
    def setUp(self):
        # تاجر موجود قبل الفصل (n8n تبعه بيستخدم التوكن العلني): legacy مفعّل
        self.old = ApiToken.objects.create(label="Old", token="OLDPUBLICTOKEN01", image_quota=10,
                                           legacy_consume_enabled=True)
        self.old_key = self.old.rotate_consume_key()
        # تاجر جديد: legacy موقوف افتراضياً
        self.new = ApiToken.objects.create(label="New", token="NEWPUBLICTOKEN01", image_quota=10)
        self.new_key = self.new.rotate_consume_key()

    def used(self, merchant):
        merchant.refresh_from_db()
        return merchant.images_used


class PublicKeyCannotConsumeTests(ConsumeAuthBase):
    def test_public_widget_key_from_the_internet_never_authorizes_consume(self):
        for merchant in (self.old, self.new):  # حتى التاجر القديم يلي legacy مفعّل عندو
            for _ in range(5):
                data = consume(pub(merchant.token), **INTERNET).json()
                self.assertEqual(data, {"ok": False, "error": "legacy_credential_not_allowed"})
            self.assertEqual(self.used(merchant), 0)

    def test_spoofed_forwarded_for_prefix_does_not_help_an_attacker(self):
        # المهاجم بيكتب 127.0.0.1 ونginx بيضيف عنوانه الحقيقي بالآخر
        meta = {**INTERNET, "HTTP_X_FORWARDED_FOR": "127.0.0.1, 203.0.113.9"}
        self.assertEqual(consume(pub(self.old.token), **meta).json()["error"], "legacy_credential_not_allowed")
        meta = {**INTERNET, "HTTP_X_FORWARDED_FOR": "::1, 203.0.113.9"}
        self.assertEqual(consume(pub(self.old.token), **meta).json()["error"], "legacy_credential_not_allowed")
        self.assertEqual(self.used(self.old), 0)

    def test_proxied_request_without_a_client_address_is_rejected(self):
        # nginx حاط X-Forwarded-Proto بس: مصدر الطلب مجهول ← ما منعتبره داخلي
        markers = {
            "HTTP_X_FORWARDED_PROTO": "https",
            "HTTP_X_FORWARDED_HOST": "localhost",
            "HTTP_X_REAL_IP": "203.0.113.9",
            "HTTP_FORWARDED": "for=203.0.113.9;proto=https",
        }
        for header, value in markers.items():
            data = consume(pub(self.old.token), REMOTE_ADDR="127.0.0.1", **{header: value}).json()
            self.assertEqual(data["error"], "legacy_credential_not_allowed", header)
        self.assertEqual(self.used(self.old), 0)

    def test_legacy_off_merchant_rejects_the_public_key_even_from_loopback(self):
        data = consume(pub(self.new.token)).json()
        self.assertEqual(data["error"], "legacy_credential_not_allowed")
        self.assertEqual(self.used(self.new), 0)

    def test_legacy_mode_off_closes_the_old_path_for_everyone(self):
        with override_settings(CONSUME_LEGACY_MODE="off"):
            self.assertEqual(consume(pub(self.old.token)).json()["error"], "legacy_credential_not_allowed")
            self.assertTrue(consume(key_hdr(self.old_key)).json()["ok"])  # المفتاح الجديد ما بتأثر
        self.assertEqual(self.used(self.old), 1)

    def test_public_token_is_not_accepted_in_the_consume_key_slot(self):
        self.assertEqual(consume(key_hdr(self.old.token)).json()["error"], "invalid_consume_key")
        self.assertEqual(
            consume({"HTTP_AUTHORIZATION": f"Bearer {self.old.token}"}).json()["error"], "invalid_consume_key"
        )

    def test_invalid_consume_key_does_not_fall_back_to_the_public_token(self):
        headers = {**pub(self.old.token), **key_hdr("ck_wrong")}
        self.assertEqual(consume(headers).json()["error"], "invalid_consume_key")
        self.assertEqual(self.used(self.old), 0)


class ConsumeKeyWorksTests(ConsumeAuthBase):
    def test_consume_key_works_from_anywhere_via_both_headers(self):
        a = consume(key_hdr(self.new_key), **INTERNET).json()
        b = consume({"HTTP_AUTHORIZATION": f"Bearer {self.new_key}"}, **INTERNET).json()
        self.assertTrue(a["ok"] and b["ok"])
        self.assertEqual(self.used(self.new), 2)

    def test_consume_key_identifies_the_merchant_without_the_public_token(self):
        data = consume(key_hdr(self.old_key)).json()
        self.assertEqual((data["ok"], data["quota"], data["remaining"]), (True, 10, 9))
        self.assertEqual(self.used(self.old), 1)
        self.assertEqual(self.used(self.new), 0)

    def test_key_is_stored_only_as_a_hash_and_is_prefixed(self):
        self.assertTrue(self.new_key.startswith("ck_"))
        self.new.refresh_from_db()
        self.assertEqual(self.new.consume_key_hash, ApiToken.hash_consume_key(self.new_key))
        self.assertNotIn(self.new_key, [str(v) for v in (self.new.consume_key_hash, self.new.consume_key_prefix)])
        self.assertEqual(self.new.consume_key_prefix, self.new_key[:8])
        self.assertEqual(len(self.new_key), len("ck_") + 43)

    def test_rotation_invalidates_the_previous_key_immediately(self):
        fresh = self.new.rotate_consume_key()
        self.assertNotEqual(fresh, self.new_key)
        self.assertEqual(consume(key_hdr(self.new_key)).json()["error"], "invalid_consume_key")
        self.assertTrue(consume(key_hdr(fresh)).json()["ok"])

    def test_disabled_merchant_is_rejected_with_either_credential(self):
        self.old.is_active = False
        self.old.save()
        self.assertEqual(consume(key_hdr(self.old_key)).json()["error"], "token_disabled")
        self.assertEqual(consume(pub(self.old.token)).json()["error"], "token_disabled")
        self.assertEqual(self.used(self.old), 0)

    def test_consume_key_is_not_a_widget_credential(self):
        client = Client()
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
        for headers in ({"HTTP_X_API_TOKEN": self.old_key}, key_hdr(self.old_key)):
            resp = client.post("/widget/chat/", data=body, content_type="application/json", **headers)
            self.assertEqual(resp.status_code, 401)
        resp = client.post("/widget/page-product/", data="{}", content_type="application/json",
                           HTTP_X_API_TOKEN=self.old_key)
        self.assertEqual(resp.status_code, 401)
        resp = client.post("/widget/event/", data="{}", content_type="application/json",
                           HTTP_X_API_TOKEN=self.old_key)
        self.assertEqual(resp.status_code, 401)

    def test_new_merchants_default_to_no_legacy_and_no_key(self):
        fresh = ApiToken.objects.create(label="Fresh", image_quota=1)
        self.assertFalse(fresh.legacy_consume_enabled)
        self.assertIsNone(fresh.consume_key_hash)
        self.assertEqual(consume(key_hdr("")).json()["error"], "missing_token")


class LegitimateN8nStillWorksTests(ConsumeAuthBase):
    """طلب n8n القديم بالضبط متل ما بيرسله اليوم: توكن علني بالهيدر، جسم JSON أو فورم،
    من نفس السيرفر مباشرة على gunicorn (بدون هيدرز الـ proxy)."""

    def test_existing_n8n_request_json_body(self):
        resp = consume(pub(self.old.token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "remaining": 9, "quota": 10, "period": "monthly"})
        self.old.refresh_from_db()
        self.assertIsNotNone(self.old.legacy_consume_last_used_at)  # للمتابعة قبل إطفاء القديم

    def test_existing_n8n_request_form_body_and_default_count(self):
        resp = consume(pub(self.old.token), body={"count": "2"}, as_form=True)
        self.assertEqual(resp.json()["remaining"], 8)
        resp = consume(pub(self.old.token), body={}, as_form=True)  # بدون count ← 1
        self.assertEqual(resp.json()["remaining"], 7)

    def test_ipv6_loopback_and_configured_internal_network(self):
        self.assertTrue(consume(pub(self.old.token), REMOTE_ADDR="::1").json()["ok"])
        with override_settings(CONSUME_LEGACY_ALLOWED_NETWORKS=["198.51.100.0/24"]):
            # n8n على سيرفر تاني، عبر nginx: عنوانه آخر X-Forwarded-For
            via_proxy = {"HTTP_X_FORWARDED_FOR": "198.51.100.7", "HTTP_X_FORWARDED_PROTO": "https"}
            self.assertTrue(consume(pub(self.old.token), **via_proxy).json()["ok"])
            self.assertEqual(consume(pub(self.old.token)).json()["error"], "legacy_credential_not_allowed")

    def test_response_contract_is_always_http_200(self):
        cases = [
            consume(),                                                    # بدون اعتماد
            consume(pub("nope")),                                         # توكن غلط
            consume(key_hdr("ck_nope")),
            consume(pub(self.old.token), body={"count": "x"}),            # عدد غلط
            consume(pub(self.old.token), body={"count": 0}),
            consume(pub(self.old.token), body={"count": 99}),             # فوق الرصيد
            consume(pub(self.new.token), **INTERNET),                     # علني من الإنترنت
        ]
        self.assertEqual({r.status_code for r in cases}, {200})
        self.assertEqual(
            [c.json()["error"] for c in cases],
            ["missing_token", "invalid_token", "invalid_consume_key", "invalid_count", "invalid_count",
             "quota_exceeded", "legacy_credential_not_allowed"],
        )
        self.assertEqual(self.used(self.old), 0)


class LegacyMigrationTests(TransactionTestCase):
    """التجار الموجودين وقت الفصل لازم يضل n8n تبعهم شغّال، والجدد بلا مسار قديم."""

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_merchants_keep_legacy_access_new_ones_do_not(self):
        before = [("apitokens", "0013_remove_shopper_limits_and_demo_accounts")]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old_apps = executor.loader.project_state(before).apps
        Historical = old_apps.get_model("apitokens", "ApiToken")
        existing = Historical.objects.create(label="Existing", token="EXISTINGTOKEN001", image_quota=5)

        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        merchant = ApiToken.objects.get(pk=existing.pk)
        self.assertTrue(merchant.legacy_consume_enabled)
        self.assertIsNone(merchant.consume_key_hash)
        self.assertEqual(consume(pub("EXISTINGTOKEN001")).json()["ok"], True)  # n8n القديم بلا أي تعديل
        self.assertEqual(consume(pub("EXISTINGTOKEN001"), **INTERNET).json()["error"],
                         "legacy_credential_not_allowed")  # والإنترنت لأ
        self.assertFalse(ApiToken.objects.create(label="Later", image_quota=1).legacy_consume_enabled)


class DashboardConsumeKeyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        self.client = Client()
        self.client.force_login(self.staff)

    def test_create_reveals_the_key_once_and_stores_only_its_hash(self):
        resp = self.client.post("/", {"label": "Shop", "image_quota": "5", "period": "monthly"})
        keys = re.findall(r"ck_[A-Za-z0-9_-]{43}", resp.content.decode())
        self.assertTrue(keys)
        merchant = ApiToken.objects.get(label="Shop")
        self.assertEqual(merchant.consume_key_hash, ApiToken.hash_consume_key(keys[0]))
        self.assertFalse(merchant.legacy_consume_enabled)
        again = self.client.get("/").content.decode()
        self.assertNotIn(keys[0], again)  # مش عم يظهر مرة تانية
        self.assertIn(merchant.consume_key_prefix, again)  # البادئة بس
        self.assertTrue(consume(key_hdr(keys[0]), **INTERNET).json()["ok"])

    def test_rotate_shows_new_key_once_and_kills_the_old_one(self):
        merchant = ApiToken.objects.create(label="M", image_quota=5)
        old = merchant.rotate_consume_key()
        resp = self.client.post("/", {"action": "rotate_consume_key", "token_pk": merchant.pk})
        new = re.findall(r"ck_[A-Za-z0-9_-]{43}", resp.content.decode())[0]
        self.assertNotEqual(new, old)
        self.assertEqual(consume(key_hdr(old)).json()["error"], "invalid_consume_key")
        self.assertTrue(consume(key_hdr(new)).json()["ok"])

    def test_toggle_legacy_and_staff_only(self):
        merchant = ApiToken.objects.create(label="M", image_quota=5, legacy_consume_enabled=True)
        self.client.post("/", {"action": "toggle_legacy_consume", "token_pk": merchant.pk})
        merchant.refresh_from_db()
        self.assertFalse(merchant.legacy_consume_enabled)
        self.assertEqual(consume(pub(merchant.token)).json()["error"], "legacy_credential_not_allowed")
        outsider = Client()
        resp = outsider.post("/", {"action": "rotate_consume_key", "token_pk": merchant.pk})
        self.assertEqual(resp.status_code, 302)  # لوجن
        merchant.refresh_from_db()
        self.assertIsNone(merchant.consume_key_hash)


# ======================================================================
# مخزن تحديد المعدّل: تزامن حقيقي
# ======================================================================

def hammer(store, key, threads, window=60):
    """N خيط بنفس اللحظة على عدّاد واحد ← قائمة القيم يلي رجعت."""
    values, errors = [], []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def work():
        try:
            barrier.wait(timeout=30)
            value = store.incr(key, window)
            with lock:
                values.append(value)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(repr(exc))
        finally:
            connection.close()

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    return values, errors


class DatabaseStoreConcurrencyTests(TransactionTestCase):
    def test_concurrent_increments_are_exact_and_never_lost(self):
        values, errors = hammer(ratelimit.DatabaseStore(), "rl:t:a:0", threads=32)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(values), list(range(1, 33)))  # ولا زيادة ضاعت ولا تكررت
        self.assertEqual(RateLimitBucket.objects.get(key="rl:t:a:0").count, 32)

    def test_exactly_limit_requests_pass_under_a_race(self):
        values, errors = hammer(ratelimit.DatabaseStore(), "rl:t:b:0", threads=40)
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for v in values if v <= 10), 10)

    def test_first_hit_creation_race_creates_a_single_row(self):
        # كل الخيوط بتبدأ بدون صف: سباق الإنشاء لازم ما يرمي ولا يكرر
        for i in range(5):
            values, errors = hammer(ratelimit.DatabaseStore(), f"rl:t:c{i}:0", threads=12)
            self.assertEqual(errors, [])
            self.assertEqual(sorted(values), list(range(1, 13)))
            self.assertEqual(RateLimitBucket.objects.filter(key=f"rl:t:c{i}:0").count(), 1)

    @override_settings(RATE_LIMITS={"chat_ip": (7, 60)})
    def test_full_hit_path_under_concurrency(self):
        results, lock = [], threading.Lock()
        barrier = threading.Barrier(24)

        def work():
            barrier.wait(timeout=30)
            decision = ratelimit.hit("chat_ip", "merchant1:1.2.3.4")
            with lock:
                results.append(decision)
            connection.close()

        pool = [threading.Thread(target=work) for _ in range(24)]
        [t.start() for t in pool]
        [t.join() for t in pool]
        self.assertEqual(sum(d.allowed for d in results), 7)
        self.assertFalse(any(d.unavailable for d in results))

    def test_selftest_command_passes(self):
        out = StringIO()
        call_command("ratelimit_selftest", "--threads", "16", "--limit", "5", stdout=out)
        self.assertIn("PASS", out.getvalue())
        self.assertIn("allowed=5", out.getvalue())
        self.assertFalse(RateLimitBucket.objects.filter(key__startswith="rl:selftest").exists())


class CacheStoreTests(TestCase):
    LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "rl-test"}}

    def test_cache_backend_is_also_exact_under_threads(self):
        from django.core.cache import cache

        with override_settings(RATE_LIMIT_BACKEND="cache", CACHES=self.LOCMEM):
            cache.clear()
            store = ratelimit.get_store()
            self.assertIsInstance(store, ratelimit.CacheStore)
            values, errors = hammer(store, "rl:cache:a:0", threads=24)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(values), list(range(1, 25)))

    def test_cache_key_expiring_between_add_and_incr_is_handled(self):
        with override_settings(RATE_LIMIT_BACKEND="cache", CACHES=self.LOCMEM), \
             patch("apitokens.services.ratelimit.cache.add", return_value=False), \
             patch("apitokens.services.ratelimit.cache.incr", side_effect=ValueError), \
             patch("apitokens.services.ratelimit.cache.set") as set_:
            self.assertEqual(ratelimit.CacheStore().incr("k", 60), 1)
            set_.assert_called_once()


class StoreHousekeepingTests(TestCase):
    def test_window_rollover_resets_the_count(self):
        with override_settings(RATE_LIMITS={"chat_ip": (1, 60)}):
            with patch("apitokens.services.ratelimit.time.time", return_value=600.0):
                self.assertTrue(ratelimit.hit("chat_ip", "k").allowed)
                self.assertFalse(ratelimit.hit("chat_ip", "k").allowed)
            with patch("apitokens.services.ratelimit.time.time", return_value=661.0):
                self.assertTrue(ratelimit.hit("chat_ip", "k").allowed)

    def test_purge_removes_only_expired_rows(self):
        from datetime import timedelta
        from django.utils import timezone

        RateLimitBucket.objects.create(key="old", count=3, expires_at=timezone.now() - timedelta(minutes=1))
        RateLimitBucket.objects.create(key="live", count=3, expires_at=timezone.now() + timedelta(minutes=1))
        call_command("purge_rate_limits", stdout=StringIO())
        self.assertEqual(list(RateLimitBucket.objects.values_list("key", flat=True)), ["live"])

    def test_system_check_rejects_non_shared_cache_backends(self):
        def run(backend, cache_backend):
            caches = {"default": {"BACKEND": cache_backend}}
            with override_settings(RATE_LIMIT_BACKEND=backend, CACHES=caches):
                return [e.id for e in app_checks.check_rate_limit_backend(None)]

        for local in app_checks.NON_SHARED_CACHES:
            self.assertEqual(run("cache", local), ["apitokens.E002"], local)
        self.assertEqual(run("cache", "django.core.cache.backends.redis.RedisCache"), [])
        self.assertEqual(run("cache", "django.core.cache.backends.memcached.PyMemcacheCache"), [])
        self.assertEqual(run("database", "django.core.cache.backends.locmem.LocMemCache"), [])
        self.assertEqual(run("redis", "x"), ["apitokens.E001"])
        self.assertEqual([e for e in checks.run_checks() if e.id.startswith("apitokens.")], [])


# ======================================================================
# أعطال المخزن: مغلق للحساس، مفتوح مع خطأ للخفيف
# ======================================================================

class FakeProvider:
    def is_configured(self):
        return True


def store_down(*args, **kwargs):
    raise DatabaseError("rate limit store is down")


@patch("apitokens.widget_views.llm.get_provider", return_value=FakeProvider())
class StoreFailureTests(TestCase):
    def setUp(self):
        self.m = ApiToken.objects.create(label="F", token="FAILTOKEN00001", image_quota=3,
                                         legacy_consume_enabled=True)
        self.key = self.m.rotate_consume_key()
        Product.objects.create(merchant=self.m, sku="D", title="Dress", price="10", image_url="https://x/1.jpg")

    def chat(self):
        return Client().post(
            "/widget/chat/", data=json.dumps({"messages": [{"role": "user", "content": "هلا"}]}),
            content_type="application/json", HTTP_X_API_TOKEN=self.m.token,
        )

    def test_chat_fails_closed_without_calling_the_llm(self, _p):
        with patch("apitokens.services.ratelimit.DatabaseStore.incr", side_effect=store_down), \
             patch("apitokens.widget_views.llm.chat_with_retry") as llm_call, \
             self.assertLogs("apitokens.services.ratelimit", level="ERROR") as logs:
            resp = self.chat()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"], "rate_limit_unavailable")
        self.assertIn("Retry-After", resp)
        llm_call.assert_not_called()
        self.assertTrue(any("mode=closed" in line for line in logs.output))  # مش صامت

    @override_settings(KIEAI_API_KEY="k")
    def test_tryon_fails_closed_and_never_touches_quota_or_the_generator(self, _p):
        from django.core.files.uploadedfile import SimpleUploadedFile

        photo = SimpleUploadedFile("me.jpg", b"\xff\xd8 fake", content_type="image/jpeg")
        with patch("apitokens.services.ratelimit.DatabaseStore.incr", side_effect=store_down), \
             patch("apitokens.widget_views.kieai.generate_tryon") as gen:
            resp = Client().post("/widget/tryon/", {"sku": "D", "photo": photo}, HTTP_X_API_TOKEN=self.m.token)
        self.assertEqual(resp.status_code, 503)
        gen.assert_not_called()
        self.assertEqual(ApiToken.objects.get(pk=self.m.pk).images_used, 0)

    def test_consume_fails_closed_with_its_always_200_contract(self, _p):
        with patch("apitokens.services.ratelimit.DatabaseStore.incr", side_effect=store_down):
            for headers in (key_hdr(self.key), pub(self.m.token)):
                resp = consume(headers)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json(), {"ok": False, "error": "rate_limit_unavailable"})
        self.assertEqual(ApiToken.objects.get(pk=self.m.pk).images_used, 0)

    def test_light_endpoints_fail_open_but_log_an_error(self, _p):
        body = json.dumps({"type": "buy_click", "session_id": "sess-fail-0001"})
        with patch("apitokens.services.ratelimit.DatabaseStore.incr", side_effect=store_down), \
             self.assertLogs("apitokens.services.ratelimit", level="ERROR") as logs:
            event = Client().post("/widget/event/", data=body, content_type="application/json",
                                  HTTP_X_API_TOKEN=self.m.token)
            page = Client().post("/widget/page-product/", data="{}", content_type="application/json",
                                 HTTP_X_API_TOKEN=self.m.token)
        self.assertEqual((event.status_code, page.status_code), (200, 200))
        self.assertTrue(any("mode=open" in line for line in logs.output))

    def test_cache_backend_failure_behaves_the_same(self, _p):
        with override_settings(RATE_LIMIT_BACKEND="cache"), \
             patch("apitokens.services.ratelimit.cache.add", side_effect=OSError("redis down")), \
             patch("apitokens.widget_views.llm.chat_with_retry") as llm_call:
            resp = self.chat()
        self.assertEqual(resp.status_code, 503)
        llm_call.assert_not_called()

    def test_fail_closed_set_is_configurable(self, _p):
        ok = llm.LLMResult(reply_text="أهلين", tool_calls=[])
        with override_settings(RATE_LIMIT_FAIL_CLOSED=["event_ip"]), \
             patch("apitokens.services.ratelimit.DatabaseStore.incr", side_effect=store_down), \
             patch("apitokens.widget_views.llm.chat_with_retry", return_value=ok):
            self.assertEqual(self.chat().status_code, 200)  # صار مفتوح بالإعداد
            event = Client().post("/widget/event/", data=json.dumps({"type": "buy_click", "session_id": "sess-cfg-00001"}),
                                  content_type="application/json", HTTP_X_API_TOKEN=self.m.token)
            self.assertEqual(event.status_code, 503)  # وهاد صار مغلق

    def test_normal_operation_unaffected_when_the_store_is_healthy(self, _p):
        ok = llm.LLMResult(reply_text="أهلين", tool_calls=[])
        with patch("apitokens.widget_views.llm.chat_with_retry", return_value=ok):
            self.assertEqual(self.chat().status_code, 200)
        self.assertGreaterEqual(RateLimitBucket.objects.count(), 3)  # chat_ip, chat_ip_hour, chat_token


class ConsumeBruteForceTests(ConsumeAuthBase):
    @override_settings(RATE_LIMITS={"consume_ip": (3, 60)})
    def test_unauthenticated_guessing_is_limited_per_ip_before_any_lookup(self):
        results = [consume(key_hdr(f"ck_guess{i}"), REMOTE_ADDR="127.0.0.1").json()["error"] for i in range(5)]
        self.assertEqual(results, ["invalid_consume_key"] * 3 + ["rate_limited"] * 2)
        # عنوان تاني ما بيتأثر
        self.assertEqual(consume(key_hdr("ck_x"), REMOTE_ADDR="10.9.9.9").json()["error"], "invalid_consume_key")
