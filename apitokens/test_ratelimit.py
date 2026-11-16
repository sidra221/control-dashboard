"""اختبارات محدّد الطلبات (services/ratelimit) وأمر purge_rate_buckets."""
import re
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import IntegrityError
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from .models import ApiToken, QuotaReservation, RateBucket
from .services import ratelimit


@override_settings(RATE_LIMITS={"t": (3, 60), "short": (1, 10)})
class LimiterTests(TestCase):
    def test_allows_up_to_limit_then_blocks_with_retry_after(self):
        for _ in range(3):
            self.assertEqual(ratelimit.hit("t", "a"), (True, 0))
        allowed, retry = ratelimit.hit("t", "a")
        self.assertFalse(allowed)
        self.assertTrue(1 <= retry <= 60)
        # بعد الرفض بيضل مرفوض ضمن النافذة
        self.assertFalse(ratelimit.hit("t", "a")[0])

    def test_retry_after_counts_down_to_window_end(self):
        now = timezone.now()
        ratelimit.hit("t", "a", now=now)
        for _ in range(3):
            ratelimit.hit("t", "a", now=now)
        allowed, retry = ratelimit.hit("t", "a", now=now + timedelta(seconds=45))
        self.assertFalse(allowed)
        self.assertEqual(retry, 15)

    def test_window_resets_after_expiry(self):
        now = timezone.now()
        for _ in range(4):
            ratelimit.hit("t", "a", now=now)
        self.assertFalse(ratelimit.hit("t", "a", now=now + timedelta(seconds=59))[0])
        self.assertEqual(ratelimit.hit("t", "a", now=now + timedelta(seconds=60)), (True, 0))
        bucket = RateBucket.objects.get(key="t:a")
        self.assertEqual(bucket.count, 1)
        self.assertEqual(bucket.window_start, now + timedelta(seconds=60))

    def test_identifiers_and_names_are_isolated(self):
        for _ in range(4):
            ratelimit.hit("t", "a")
        self.assertTrue(ratelimit.hit("t", "b")[0])
        self.assertTrue(ratelimit.hit("short", "a")[0])
        self.assertFalse(ratelimit.hit("short", "a")[0])
        self.assertEqual(RateBucket.objects.filter(key__in=["t:a", "t:b", "short:a"]).count(), 3)

    def test_unknown_limit_name_is_unlimited(self):
        for _ in range(50):
            self.assertEqual(ratelimit.hit("nope", "a"), (True, 0))
        self.assertEqual(RateBucket.objects.count(), 0)

    def test_key_is_name_colon_ident(self):
        ratelimit.hit("t", "1.2.3.4")
        self.assertTrue(RateBucket.objects.filter(key="t:1.2.3.4").exists())

    def test_very_long_identifier_still_fits_the_key_column(self):
        ratelimit.hit("t", "x" * 500)
        key = RateBucket.objects.get().key
        self.assertLessEqual(len(key), 160)
        self.assertTrue(key.startswith("t:"))
        ratelimit.hit("t", "x" * 500)
        self.assertEqual(RateBucket.objects.get().count, 2)

    def test_lost_create_race_increments_the_winners_row(self):
        """worker تاني أنشأ الصف بين فحصنا وإنشائنا: unique بيرفض، ونحن بنزيد على صفّه (ما بنضيع عدّة)."""
        from django.db.models.query import QuerySet

        RateBucket.objects.create(key="short:r", window_start=timezone.now(), count=1)  # صف الفائز
        real_update = QuerySet.update
        blind = iter([0, 0])  # فحصنا ما شاف الصف بالـ UPDATE الأول والتاني

        def update(queryset, **kwargs):
            try:
                return next(blind)
            except StopIteration:
                return real_update(queryset, **kwargs)

        with patch.object(QuerySet, "update", update), \
                patch("apitokens.services.ratelimit.RateBucket.objects.create", side_effect=IntegrityError("dup")):
            allowed, _ = ratelimit.hit("short", "r")
        self.assertFalse(allowed)  # 1 (الفائز) + 1 (نحن) = 2 > حد 1
        self.assertEqual(RateBucket.objects.get(key="short:r").count, 2)

    def test_increment_is_a_single_atomic_update(self):
        """الزيادة بـ UPDATE ... count = count + 1 (F expression) مش قراءة ثم كتابة — حتى ما يضيع عدّ بين workers."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        ratelimit.hit("t", "a")
        with CaptureQueriesContext(connection) as ctx:
            ratelimit.hit("t", "a")
        sql = [q["sql"] for q in ctx.captured_queries]
        self.assertTrue(any('SET "count" = ("apitokens_ratebucket"."count" + 1)' in q for q in sql), sql)
        self.assertFalse(any("FOR UPDATE" in q for q in sql))
        self.assertEqual(RateBucket.objects.get(key="t:a").count, 2)

    def test_fails_open_when_database_breaks(self):
        with patch("apitokens.services.ratelimit.hit_key", side_effect=RuntimeError("db down")), \
                self.assertLogs("apitokens.services.ratelimit", level="ERROR"):
            self.assertEqual(ratelimit.hit("t", "a"), (True, 0))


class IpHashTests(TestCase):
    def request(self, **meta):
        return RequestFactory().get("/", **meta)

    def test_is_64_hex_and_never_the_raw_ip(self):
        value = ratelimit.ip_hash(self.request(REMOTE_ADDR="203.0.113.7"))
        self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertNotIn("203.0.113.7", value)

    def test_stable_per_ip_and_different_between_ips(self):
        a1 = ratelimit.ip_hash(self.request(REMOTE_ADDR="203.0.113.7"))
        a2 = ratelimit.ip_hash(self.request(REMOTE_ADDR="203.0.113.7"))
        b = ratelimit.ip_hash(self.request(REMOTE_ADDR="203.0.113.8"))
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)

    def test_salted_with_secret_key(self):
        before = ratelimit.ip_hash(self.request(REMOTE_ADDR="203.0.113.7"))
        with override_settings(SECRET_KEY="another-secret"):
            after = ratelimit.ip_hash(self.request(REMOTE_ADDR="203.0.113.7"))
        self.assertNotEqual(before, after)

    def test_honours_the_portal_client_ip_header(self):
        with override_settings(PORTAL_CLIENT_IP_HEADER="HTTP_X_REAL_IP"):
            req = self.request(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="198.51.100.9")
            self.assertEqual(ratelimit.client_ip(req), "198.51.100.9")
            self.assertEqual(ratelimit.ip_hash(req), ratelimit.hash_ip("198.51.100.9"))
        # بدون الإعداد: الهيدر ما بيتصدّق
        req = self.request(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="198.51.100.9", HTTP_X_FORWARDED_FOR="1.1.1.1")
        self.assertEqual(ratelimit.client_ip(req), "127.0.0.1")

    def test_unknown_address_gives_empty_hash(self):
        req = self.request()
        req.META.pop("REMOTE_ADDR", None)
        self.assertEqual(ratelimit.ip_hash(req), "")


class PurgeCommandTests(TestCase):
    def test_deletes_old_windows_and_expired_reservations_only(self):
        now = timezone.now()
        RateBucket.objects.create(key="old:1", window_start=now - timedelta(days=2), count=5)
        RateBucket.objects.create(key="fresh:1", window_start=now - timedelta(hours=2), count=5)
        merchant = ApiToken.objects.create(label="M", token="PURGETOKEN001", image_quota=5)
        QuotaReservation.objects.create(merchant=merchant, expires_at=now - timedelta(minutes=1))
        live = QuotaReservation.objects.create(merchant=merchant, expires_at=now + timedelta(minutes=2))
        out = StringIO()
        call_command("purge_rate_buckets", stdout=out)
        self.assertEqual(list(RateBucket.objects.values_list("key", flat=True)), ["fresh:1"])
        self.assertEqual(list(QuotaReservation.objects.values_list("pk", flat=True)), [live.pk])
        self.assertTrue(re.search(r"deleted 1 rate bucket.*1 expired reservation", out.getvalue()))

    def test_hours_option(self):
        now = timezone.now()
        RateBucket.objects.create(key="a", window_start=now - timedelta(hours=3), count=1)
        call_command("purge_rate_buckets", "--hours", "1", stdout=StringIO())
        self.assertEqual(RateBucket.objects.count(), 0)
