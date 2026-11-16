"""اختبارات توزيع الرصيد (services/quota): الحجز الذرّي، سقف المتجر اليومي، حد الزبون والـIP."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .models import ApiToken, LlmUsage, Product, QuotaReservation, TryOnEvent
from .services import quota

H1 = "a" * 64  # بصمة IP وهمية
H2 = "b" * 64


def make_merchant(token="QCORETOKEN001", quota_=100, **kw):
    return ApiToken.objects.create(label=token, token=token, image_quota=quota_, **kw)


def add_events(merchant, count, shopper_id="", ip_hash="", success=True, age=timedelta(0)):
    created = []
    for _ in range(count):
        event = TryOnEvent.objects.create(
            merchant=merchant, shopper_id=shopper_id, ip_hash=ip_hash, sku="S", success=success,
        )
        created.append(event.pk)
    TryOnEvent.objects.filter(pk__in=created).update(created_at=timezone.now() - age)
    return created


class CleanShopperIdTests(TestCase):
    def test_valid_ids_pass(self):
        for value in ("abcdefgh", "A1_b-2c3d", "x" * 64, "  abcdefgh12  "):
            self.assertEqual(quota.clean_shopper_id(value), value.strip())

    def test_invalid_ids_become_empty(self):
        for value in ("short", "x" * 65, "has space inside", "emoji-😀-chars", "../../etc/passwd", "a;b'c\"d<e>",
                      "", None, 12345678, ["abcdefgh"], {"a": 1}):
            self.assertEqual(quota.clean_shopper_id(value), "", repr(value))


class ReserveTests(TestCase):
    def setUp(self):
        self.merchant = make_merchant(quota_=5, tryons_per_shopper=0)  # بدون حد زبون، السقف اليومي = 5

    def test_reserve_creates_reservation_with_ttl(self):
        now = timezone.now()
        result = quota.reserve(self.merchant.pk, "shopper-001", H1, now=now)
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "ok")
        reservation = QuotaReservation.objects.get()
        self.assertEqual(reservation.shopper_id, "shopper-001")
        self.assertEqual(reservation.ip_hash, H1)
        self.assertEqual(reservation.expires_at, now + timedelta(seconds=180))
        self.assertEqual(result.reservation, reservation)

    def test_pool_plus_reservations_never_exceed_quota(self):
        self.merchant.images_used = 2
        self.merchant.save()
        results = [quota.reserve(self.merchant.pk, "", "") for _ in range(10)]
        self.assertEqual([r.ok for r in results], [True] * 3 + [False] * 7)
        self.assertTrue(all(r.reason == "quota_exceeded" and r.remaining == 0 for r in results[3:]))
        self.assertEqual(QuotaReservation.objects.count(), 3)

    def test_n_sequential_reserves_then_finalize_stay_within_quota(self):
        reservations = []
        for _ in range(8):
            result = quota.reserve(self.merchant.pk, "", "")
            if result.ok:
                reservations.append(result.reservation)
        self.assertEqual(len(reservations), 5)
        for reservation in reservations:
            quota.finalize_success(self.merchant.pk, reservation, "", "", "", "S", None)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 5)
        self.assertEqual(QuotaReservation.objects.count(), 0)
        self.assertFalse(quota.reserve(self.merchant.pk, "", "").ok)

    def test_release_frees_the_slot(self):
        held = [quota.reserve(self.merchant.pk, "", "").reservation for _ in range(5)]
        self.assertEqual(quota.reserve(self.merchant.pk, "", "").reason, "quota_exceeded")
        quota.release(held[0])
        self.assertTrue(quota.reserve(self.merchant.pk, "", "").ok)
        quota.release(held[0])  # مرتين/محذوف: ما بيرمي
        quota.release(None)

    def test_expired_reservation_does_not_count(self):
        now = timezone.now()
        stale = [quota.reserve(self.merchant.pk, "", "", now=now - timedelta(seconds=200)).reservation
                 for _ in range(5)]
        self.assertEqual(len(stale), 5)
        self.assertEqual(quota.active_reservations(self.merchant, now), 0)
        self.assertTrue(quota.reserve(self.merchant.pk, "", "", now=now).ok)

    def test_reservation_still_inside_ttl_counts(self):
        now = timezone.now()
        for _ in range(5):
            quota.reserve(self.merchant.pk, "", "", now=now - timedelta(seconds=100))
        self.assertEqual(quota.reserve(self.merchant.pk, "", "", now=now).reason, "quota_exceeded")

    def test_expired_period_is_treated_as_unused(self):
        self.merchant.images_used = 5
        self.merchant.period_start = timezone.now() - timedelta(days=31)
        self.merchant.save()
        self.assertTrue(quota.reserve(self.merchant.pk, "", "").ok)
        merchant, result = quota.finalize_success(self.merchant.pk, None, "", "", "", "S", None)
        self.assertTrue(result["ok"])
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 1)  # الفترة تجدّدت عند الخصم

    def test_inactive_and_missing_merchant(self):
        self.merchant.is_active = False
        self.merchant.save()
        self.assertEqual(quota.reserve(self.merchant.pk, "", "").reason, "token_disabled")
        self.assertEqual(quota.reserve(999999, "", "").reason, "invalid_token")
        self.assertEqual(QuotaReservation.objects.count(), 0)

    def test_check_order_inactive_then_pool_then_daily_then_shopper(self):
        m = make_merchant("QORDER000001", quota_=100, tryons_per_shopper=1)
        # الثلاثة مكسورين: سقف يومي (20)، زبون (1)، وحوض (مستهلك كامل)
        add_events(m, 20, shopper_id="shopper-001", ip_hash=H1)
        m.images_used = 100
        m.save()
        self.assertEqual(quota.reserve(m.pk, "shopper-001", H1).reason, "quota_exceeded")
        m.images_used = 20
        m.save()
        self.assertEqual(quota.reserve(m.pk, "shopper-001", H1).reason, "store_daily_limit")
        TryOnEvent.objects.filter(merchant=m).exclude(shopper_id="shopper-001").delete()
        TryOnEvent.objects.filter(merchant=m).update(created_at=timezone.now() - timedelta(hours=23))
        # السقف انفتح (الأحداث لسا بالنافذة لكن العدّ 20 = السقف)... نخفّف: نحذف 5 لننزل تحت السقف
        TryOnEvent.objects.filter(pk__in=list(TryOnEvent.objects.filter(merchant=m).values_list("pk", flat=True)[:5])).delete()
        self.assertEqual(quota.reserve(m.pk, "shopper-001", H1).reason, "shopper_limit_reached")
        m.is_active = False
        m.save()
        self.assertEqual(quota.reserve(m.pk, "shopper-001", H1).reason, "token_disabled")


class FinalizeTests(TestCase):
    def setUp(self):
        self.merchant = make_merchant(quota_=5, tryons_per_shopper=0)
        self.product = Product.objects.create(
            merchant=self.merchant, sku="DRESS-1", title="D", price="1", image_url="https://e.com/d.jpg",
        )

    def test_one_transaction_consumes_deletes_reservation_and_records_event(self):
        reservation = quota.reserve(self.merchant.pk, "shopper-001", H1).reservation
        merchant, result = quota.finalize_success(
            self.merchant.pk, reservation, "shopper-001", H1, "sess-abcdef12", "DRESS-1", self.product,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining"], 4)
        self.assertEqual(QuotaReservation.objects.count(), 0)
        event = TryOnEvent.objects.get()
        self.assertEqual(
            (event.merchant, event.shopper_id, event.ip_hash, event.session_id, event.sku, event.product, event.success),
            (self.merchant, "shopper-001", H1, "sess-abcdef12", "DRESS-1", self.product, True),
        )
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 1)

    def test_unexpected_consume_refusal_logs_error_but_still_records(self):
        self.merchant.images_used = 5
        self.merchant.save()
        with self.assertLogs("apitokens.services.quota", level="ERROR") as logs:
            merchant, result = quota.finalize_success(self.merchant.pk, None, "", "", "", "DRESS-1", self.product)
        self.assertFalse(result["ok"])
        self.assertIn("quota_consume_failed_after_reservation", logs.output[0])
        self.assertEqual(TryOnEvent.objects.filter(success=True).count(), 1)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 5)

    def test_event_failure_does_not_undo_the_charge(self):
        with patch("apitokens.services.quota.TryOnEvent.objects.create", side_effect=Exception("db")), \
                self.assertLogs("apitokens.services.quota", level="ERROR"):
            quota.finalize_success(self.merchant.pk, None, "", "", "", "DRESS-1", self.product)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.images_used, 1)


class DailyCapTests(TestCase):
    def setUp(self):
        self.merchant = make_merchant(quota_=100, tryons_per_shopper=0)  # سقف يومي = 20

    def test_cap_blocks_with_retry_after_until_oldest_leaves(self):
        add_events(self.merchant, 19, age=timedelta(hours=1))
        add_events(self.merchant, 1, age=timedelta(hours=23))  # أقدمها: بتطلع بعد ساعة
        result = quota.reserve(self.merchant.pk, "", "")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "store_daily_limit")
        self.assertTrue(3500 <= result.retry_after_seconds <= 3601)
        self.assertEqual(quota.daily_retry_after(self.merchant), result.retry_after_seconds)

    def test_cap_reopens_when_events_leave_the_window(self):
        add_events(self.merchant, 20, age=timedelta(hours=25))
        self.assertTrue(quota.reserve(self.merchant.pk, "", "").ok)

    def test_reservations_count_toward_the_cap(self):
        add_events(self.merchant, 18)
        self.assertTrue(quota.reserve(self.merchant.pk, "", "").ok)
        self.assertTrue(quota.reserve(self.merchant.pk, "", "").ok)
        self.assertEqual(quota.reserve(self.merchant.pk, "", "").reason, "store_daily_limit")

    def test_failed_events_do_not_count(self):
        add_events(self.merchant, 30, success=False)
        self.assertTrue(quota.reserve(self.merchant.pk, "", "").ok)

    def test_daily_period_has_no_extra_cap(self):
        merchant = make_merchant("QDAILY000001", 30, period="daily", tryons_per_shopper=0)
        add_events(merchant, 25)
        self.assertIsNone(quota.daily_cap(merchant))
        self.assertIsNone(quota.daily_retry_after(merchant))
        self.assertTrue(quota.reserve(merchant.pk, "", "").ok)


class ShopperLimitTests(TestCase):
    def setUp(self):
        self.merchant = make_merchant(quota_=1000, tryons_per_shopper=3)  # سقف يومي 200، حد IP = 9

    def test_three_per_rolling_day_then_blocked(self):
        for _ in range(3):
            result = quota.reserve(self.merchant.pk, "shopper-001", H1)
            self.assertTrue(result.ok)
            quota.finalize_success(self.merchant.pk, result.reservation, "shopper-001", H1, "", "S", None)
        blocked = quota.reserve(self.merchant.pk, "shopper-001", H1)
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.reason, "shopper_limit_reached")
        self.assertEqual(blocked.shopper_remaining, 0)
        self.assertTrue(86300 <= blocked.retry_after_seconds <= 86401)

    def test_shopper_remaining_counts_down(self):
        self.assertEqual(quota.reserve(self.merchant.pk, "shopper-001", H1).shopper_remaining, 2)
        self.assertEqual(quota.reserve(self.merchant.pk, "shopper-001", H1).shopper_remaining, 1)
        self.assertEqual(quota.reserve(self.merchant.pk, "shopper-001", H1).shopper_remaining, 0)
        self.assertEqual(quota.shopper_status(self.merchant, "shopper-001", H1)["remaining"], 0)

    def test_in_flight_reservations_count_against_the_shopper(self):
        for _ in range(3):
            self.assertTrue(quota.reserve(self.merchant.pk, "shopper-001", H1).ok)
        result = quota.reserve(self.merchant.pk, "shopper-001", H1)
        self.assertEqual(result.reason, "shopper_limit_reached")
        self.assertTrue(1 <= result.retry_after_seconds <= 181)  # أقرب حجز بينتهي

    def test_window_slides_as_oldest_event_ages_out(self):
        add_events(self.merchant, 1, "shopper-001", H1, age=timedelta(hours=23, minutes=30))
        add_events(self.merchant, 2, "shopper-001", H1, age=timedelta(hours=1))
        status = quota.shopper_status(self.merchant, "shopper-001", H1)
        self.assertEqual(status["remaining"], 0)
        self.assertTrue(1700 <= status["retry_after_seconds"] <= 1801)  # نص ساعة
        # نحرّك الأقدم خارج النافذة
        TryOnEvent.objects.filter(pk=TryOnEvent.objects.order_by("created_at").first().pk).update(
            created_at=timezone.now() - timedelta(hours=24, seconds=5),
        )
        status = quota.shopper_status(self.merchant, "shopper-001", H1)
        self.assertEqual(status, {"remaining": 1, "retry_after_seconds": None})
        self.assertTrue(quota.reserve(self.merchant.pk, "shopper-001", H1).ok)

    def test_failed_tryons_do_not_count(self):
        add_events(self.merchant, 5, "shopper-001", H1, success=False)
        self.assertEqual(quota.shopper_status(self.merchant, "shopper-001", H1)["remaining"], 3)

    def test_other_shoppers_are_unaffected(self):
        add_events(self.merchant, 3, "shopper-001", H1)
        self.assertTrue(quota.reserve(self.merchant.pk, "shopper-002", H2).ok)

    def test_limit_zero_means_unlimited(self):
        self.merchant.tryons_per_shopper = 0
        self.merchant.save()
        add_events(self.merchant, 50, "shopper-001", H1)
        status = quota.shopper_status(self.merchant, "shopper-001", H1)
        self.assertEqual(status, {"remaining": None, "retry_after_seconds": None})
        result = quota.reserve(self.merchant.pk, "shopper-001", H1)
        self.assertTrue(result.ok)
        self.assertIsNone(result.shopper_remaining)

    def test_ip_backstop_blocks_new_shopper_ids_at_three_times_the_limit(self):
        # 9 تجارب من نفس الـIP بمعرّفات مختلفة (مسح localStorage)
        for i in range(9):
            add_events(self.merchant, 1, f"clearing-{i:03d}-id", H1)
        result = quota.reserve(self.merchant.pk, "brand-new-shopper", H1)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "shopper_limit_reached")
        # IP تاني ما تأثر
        self.assertTrue(quota.reserve(self.merchant.pk, "brand-new-shopper", H2).ok)

    def test_ip_backstop_allows_below_three_times(self):
        for i in range(8):
            add_events(self.merchant, 1, f"clearing-{i:03d}-id", H1)
        self.assertTrue(quota.reserve(self.merchant.pk, "brand-new-shopper", H1).ok)

    def test_ip_multiplier_setting_is_respected(self):
        with self.settings(TRYON_IP_LIMIT_MULTIPLIER=1):
            add_events(self.merchant, 3, "other-shopper-1", H1)
            self.assertEqual(quota.reserve(self.merchant.pk, "brand-new-shopper", H1).reason, "shopper_limit_reached")

    def test_missing_shopper_id_counts_by_ip_only_against_the_multiplied_limit(self):
        add_events(self.merchant, 8, "", H1)
        status = quota.shopper_status(self.merchant, "", H1)
        self.assertEqual(status["remaining"], 1)
        self.assertTrue(quota.reserve(self.merchant.pk, "", H1).ok)
        self.assertEqual(quota.reserve(self.merchant.pk, "", H1).reason, "shopper_limit_reached")
        # لسا بنفس الـIP بس معرّف زبون جديد: الحد الاحتياطي بيمنعه كمان
        self.assertEqual(quota.reserve(self.merchant.pk, "brand-new-shopper", H1).reason, "shopper_limit_reached")

    def test_neither_shopper_id_nor_ip_cannot_be_limited(self):
        add_events(self.merchant, 20, "", "")
        self.assertTrue(quota.reserve(self.merchant.pk, "", "").ok)

    def test_retry_after_when_ip_is_the_blocker(self):
        for i in range(9):
            add_events(self.merchant, 1, f"clearing-{i:03d}-id", H1, age=timedelta(hours=2))
        result = quota.reserve(self.merchant.pk, "brand-new-shopper", H1)
        self.assertTrue(79000 <= result.retry_after_seconds <= 79201)  # 22 ساعة


class MerchantIsolationTests(TestCase):
    def test_other_merchants_events_and_reservations_do_not_count(self):
        a = make_merchant("QISOA0000001", quota_=5, tryons_per_shopper=1)
        b = make_merchant("QISOB0000001", quota_=5, tryons_per_shopper=1)
        add_events(a, 5, "shopper-001", H1)
        a.images_used = 5
        a.save()
        self.assertEqual(quota.reserve(a.pk, "shopper-001", H1).reason, "quota_exceeded")
        self.assertTrue(quota.reserve(b.pk, "shopper-001", H1).ok)
        self.assertEqual(quota.active_reservations(a), 0)
        self.assertEqual(quota.active_reservations(b), 1)
        self.assertEqual(quota.quota_status(b)["used_today"], 0)


class AvailabilityTests(TestCase):
    def test_available_fresh_shopper(self):
        m = make_merchant(quota_=20, tryons_per_shopper=3)
        self.assertEqual(
            quota.availability(m, "shopper-001", H1),
            {"tryon_available": True, "shopper_tryons_left": 3, "retry_after_seconds": None},
        )

    def test_unavailable_shopper_has_retry(self):
        m = make_merchant(quota_=100, tryons_per_shopper=1)
        add_events(m, 1, "shopper-001", H1, age=timedelta(hours=1))
        data = quota.availability(m, "shopper-001", H1)
        self.assertFalse(data["tryon_available"])
        self.assertEqual(data["shopper_tryons_left"], 0)
        self.assertTrue(82700 <= data["retry_after_seconds"] <= 82801)

    def test_pool_exhausted_has_no_retry(self):
        m = make_merchant(quota_=2, tryons_per_shopper=0)
        m.images_used = 2
        m.save()
        data = quota.availability(m, "shopper-001", H1)
        self.assertEqual(data, {"tryon_available": False, "shopper_tryons_left": None, "retry_after_seconds": None})

    def test_daily_cap_reached(self):
        m = make_merchant(quota_=100, tryons_per_shopper=0)
        add_events(m, 20)
        data = quota.availability(m, "shopper-001", H1)
        self.assertFalse(data["tryon_available"])
        self.assertTrue(data["retry_after_seconds"] > 0)


class LlmUsageRecordTests(TestCase):
    def setUp(self):
        self.merchant = make_merchant(quota_=5)

    def test_creates_then_increments_one_row_per_day(self):
        quota.record_llm_usage(self.merchant, 100, 20)
        quota.record_llm_usage(self.merchant, 50, 5)
        quota.record_llm_usage(self.merchant, None, None)
        row = LlmUsage.objects.get()
        self.assertEqual((row.calls, row.input_tokens, row.output_tokens), (3, 150, 25))
        self.assertEqual(row.day, timezone.localdate())

    def test_new_day_new_row_and_merchant_isolation(self):
        other = make_merchant("QLLMOTHER001", quota_=5)
        quota.record_llm_usage(self.merchant, 1, 1, today=timezone.localdate() - timedelta(days=1))
        quota.record_llm_usage(self.merchant, 1, 1)
        quota.record_llm_usage(other, 7, 7)
        self.assertEqual(LlmUsage.objects.filter(merchant=self.merchant).count(), 2)
        self.assertEqual(LlmUsage.objects.get(merchant=other).input_tokens, 7)

    def test_never_raises_and_ignores_garbage_tokens(self):
        quota.record_llm_usage(self.merchant, "x", -4)
        self.assertEqual(LlmUsage.objects.get().input_tokens, 0)
        with patch("apitokens.services.quota.LlmUsage.objects.filter", side_effect=Exception("db")), \
                self.assertLogs("apitokens.services.quota", level="ERROR"):
            quota.record_llm_usage(self.merchant, 1, 1)
