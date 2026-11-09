"""اختبارات إحصائيات التجارب الافتراضية (services/tryon_stats + view).

الخدمة بتنختبر بـ now ثابت وبدون أي اعتماد على التمبلت.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import ApiToken, Conversation, Product, TryOnEvent
from .services.tryon_stats import build_stats

NOW = timezone.make_aware(datetime(2026, 6, 15, 14, 0))


def ago(**kwargs):
    return NOW - timedelta(**kwargs)


class StatsBase(TestCase):
    def setUp(self):
        self.merchant = ApiToken.objects.create(label="M1", image_quota=100)
        self.other = ApiToken.objects.create(label="M2", image_quota=100)

    def tryon(self, when, sku="A", session="sess-0001", success=True, merchant=None, error=""):
        ev = TryOnEvent.objects.create(
            merchant=merchant or self.merchant, session_id=session, sku=sku, success=success, error=error,
        )
        TryOnEvent.objects.filter(pk=ev.pk).update(created_at=when)
        return ev

    def order(self, session="sess-0001", at=None, total="100.00", order_id="o1", merchant=None):
        return Conversation.objects.create(
            merchant=merchant or self.merchant, session_id=session, order_id=order_id,
            order_total=Decimal(total) if total is not None else None, order_at=at,
        )

    def stats(self, **kw):
        kw.setdefault("range_key", "30")
        kw.setdefault("window_days", 7)
        kw.setdefault("now", NOW)
        return build_stats(self.merchant, **kw)


class AttributionTests(StatsBase):
    def test_order_inside_window_is_attributed(self):
        self.tryon(ago(days=3))
        self.order(at=ago(days=1), total="55.50")
        t = self.stats()["totals"]
        self.assertEqual(t["attributed_orders"], 1)
        self.assertEqual(t["attributed_revenue"], Decimal("55.50"))

    def test_order_outside_window_not_attributed(self):
        self.tryon(ago(days=10))
        self.order(at=ago(days=1))
        self.assertEqual(self.stats(window_days=7)["totals"]["attributed_orders"], 0)
        self.assertEqual(self.stats(window_days=14)["totals"]["attributed_orders"], 1)

    def test_window_is_clamped(self):
        self.tryon(ago(days=10))
        self.order(at=ago(days=1))
        # قيمة غير مدعومة بترجع للافتراضي 7
        self.assertEqual(self.stats(window_days=99)["totals"]["attributed_orders"], 0)
        self.assertEqual(self.stats(window_days="x")["totals"]["attributed_orders"], 0)

    def test_tryon_before_range_start_still_attributes(self):
        # التجربة قبل بداية المدى بس الطلب جواته وضمن النافذة
        self.tryon(ago(days=8))
        self.order(at=ago(days=6))
        s = self.stats(range_key="7", window_days=7)
        self.assertEqual(s["totals"]["attributed_orders"], 1)
        self.assertEqual(s["totals"]["tryons"], 0)

    def test_order_before_tryon_not_attributed(self):
        self.tryon(ago(days=1))
        self.order(at=ago(days=2))
        self.assertEqual(self.stats()["totals"]["attributed_orders"], 0)

    def test_failed_tryon_does_not_count_or_attribute(self):
        self.tryon(ago(days=2), success=False, error="boom")
        self.order(at=ago(days=1))
        t = self.stats()["totals"]
        self.assertEqual(t["tryons"], 0)
        self.assertEqual(t["failed"], 1)
        self.assertEqual(t["unique_shoppers"], 0)
        self.assertEqual(t["attributed_orders"], 0)

    def test_conversation_without_order_at_not_attributed(self):
        self.tryon(ago(days=2))
        self.order(at=None)
        self.assertEqual(self.stats()["totals"]["attributed_orders"], 0)

    def test_conversation_without_order_id_not_attributed(self):
        self.tryon(ago(days=2))
        self.order(at=ago(days=1), order_id="")
        self.assertEqual(self.stats()["totals"]["attributed_orders"], 0)

    def test_order_outside_range_not_counted(self):
        self.tryon(ago(days=41))
        self.order(at=ago(days=40))
        self.assertEqual(self.stats(range_key="30", window_days=30)["totals"]["attributed_orders"], 0)

    def test_order_counted_once_with_many_tryons(self):
        for hours in (50, 30, 10):
            self.tryon(ago(hours=hours), sku="A")
        self.tryon(ago(hours=20), sku="B")
        self.order(at=ago(hours=1), total="80")
        s = self.stats()
        self.assertEqual(s["totals"]["attributed_orders"], 1)
        self.assertEqual(s["totals"]["attributed_revenue"], Decimal("80"))
        self.assertEqual(sum(d["orders"] for d in s["chart"]), 1)

    def test_null_total_counts_as_zero_revenue(self):
        self.tryon(ago(days=2))
        self.order(at=ago(days=1), total=None)
        t = self.stats()["totals"]
        self.assertEqual(t["attributed_orders"], 1)
        self.assertEqual(t["attributed_revenue"], Decimal("0"))

    def test_other_session_does_not_attribute(self):
        self.tryon(ago(days=2), session="sess-other")
        self.order(session="sess-0001", at=ago(days=1))
        self.assertEqual(self.stats()["totals"]["attributed_orders"], 0)


class TotalsTests(StatsBase):
    def test_empty_merchant_returns_zeros(self):
        s = self.stats()
        self.assertEqual(s["totals"], {
            "tryons": 0, "failed": 0, "unique_shoppers": 0, "per_shopper": 0.0,
            "attributed_orders": 0, "attributed_revenue": Decimal("0"),
        })
        self.assertEqual(s["top_products"], [])
        self.assertEqual(s["events"], [])
        self.assertEqual(s["products"], [])
        self.assertEqual(len(s["chart"]), 30)
        self.assertTrue(all(d["tryons"] == 0 and d["orders"] == 0 for d in s["chart"]))

    def test_unique_shoppers_and_per_shopper(self):
        self.tryon(ago(days=1), session="s1")
        self.tryon(ago(days=1), session="s1")
        self.tryon(ago(days=1), session="s1")
        self.tryon(ago(days=1), session="s2")
        self.tryon(ago(days=1), session="")  # بلا جلسة: ما بيعدّ كزبون
        self.tryon(ago(days=1), session="s3", success=False)
        t = self.stats()["totals"]
        self.assertEqual(t["tryons"], 5)
        self.assertEqual(t["failed"], 1)
        self.assertEqual(t["unique_shoppers"], 2)
        self.assertEqual(t["per_shopper"], 2.5)

    def test_events_outside_range_excluded(self):
        self.tryon(ago(days=3))
        self.tryon(ago(days=20))
        self.assertEqual(self.stats(range_key="7")["totals"]["tryons"], 1)
        self.assertEqual(self.stats(range_key="30")["totals"]["tryons"], 2)

    def test_cross_merchant_isolation(self):
        self.tryon(ago(days=2), merchant=self.other, session="x1")
        self.order(session="x1", at=ago(days=1), merchant=self.other)
        # نفس الجلسة عند تاجر تاني ما بتسند لتاجرنا
        self.order(session="sess-0001", at=ago(days=1))
        self.tryon(ago(days=2), session="x1")  # تجربة تاجرنا بجلسة x1، بس طلب x1 عند التاني
        s = self.stats()
        self.assertEqual(s["totals"]["tryons"], 1)
        self.assertEqual(s["totals"]["attributed_orders"], 0)
        self.assertEqual(len(s["events"]), 1)
        self.assertEqual([p["sku"] for p in s["products"]], ["A"])
        other = build_stats(self.other, "30", 7, now=NOW)
        self.assertEqual(other["totals"]["attributed_orders"], 1)
        self.assertEqual(other["totals"]["tryons"], 1)


class ChartTests(StatsBase):
    def test_chart_zero_filled_and_ordered(self):
        self.tryon(ago(days=1))
        self.tryon(ago(days=1))
        self.tryon(ago(days=3))
        self.order(at=ago(days=1))
        chart = self.stats(range_key="7")["chart"]
        self.assertEqual(len(chart), 7)
        dates = [d["date"] for d in chart]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(dates[-1], "2026-06-15")
        self.assertEqual(dates[0], "2026-06-09")
        by_date = {d["date"]: d for d in chart}
        self.assertEqual(by_date["2026-06-14"], {"date": "2026-06-14", "tryons": 2, "orders": 1})
        self.assertEqual(by_date["2026-06-12"]["tryons"], 1)
        self.assertEqual(by_date["2026-06-13"], {"date": "2026-06-13", "tryons": 0, "orders": 0})

    def test_chart_orders_use_order_date_not_tryon_date(self):
        self.tryon(ago(days=5))
        self.order(at=ago(days=1))
        by_date = {d["date"]: d for d in self.stats(range_key="7")["chart"]}
        self.assertEqual(by_date["2026-06-10"]["orders"], 0)
        self.assertEqual(by_date["2026-06-14"]["orders"], 1)

    def test_chart_ignores_failed_and_unattributed_orders(self):
        self.tryon(ago(days=1), success=False)
        self.order(at=ago(days=1))
        chart = self.stats(range_key="7")["chart"]
        self.assertEqual(sum(d["tryons"] + d["orders"] for d in chart), 0)


class TopProductsAndFilterTests(StatsBase):
    def setUp(self):
        super().setUp()
        Product.objects.create(
            merchant=self.merchant, sku="A", title="Dress A", price="10", image_url="https://x/a.jpg",
        )
        for i in range(3):
            self.tryon(ago(days=2, hours=i), sku="A", session=f"sa{i}")
        self.tryon(ago(days=2), sku="B", session="sa0")  # sku محذوف/بدون منتج
        self.tryon(ago(days=2), sku="B", session="sb1")
        self.tryon(ago(days=2), sku="B", session="sb1", success=False)
        self.order(session="sa0", at=ago(days=1), total="40", order_id="o1")
        self.order(session="sb1", at=ago(days=1), total="60", order_id="o2")

    def test_top_products_order_names_and_orders(self):
        top = self.stats()["top_products"]
        self.assertEqual(top, [
            {"sku": "A", "name": "Dress A", "tryons": 3, "orders": 1},
            {"sku": "B", "name": "B", "tryons": 2, "orders": 2},
        ])

    def test_top_products_limited_to_ten(self):
        for i in range(12):
            self.tryon(ago(days=1), sku=f"X{i:02d}", session=f"sx{i}")
        self.assertEqual(len(self.stats()["top_products"]), 10)

    def test_products_dropdown(self):
        self.assertEqual(self.stats()["products"], [{"sku": "B", "name": "B"}, {"sku": "A", "name": "Dress A"}])

    def test_sku_filter_applies_everywhere(self):
        s = self.stats(sku="B")
        self.assertEqual(s["totals"]["tryons"], 2)
        self.assertEqual(s["totals"]["failed"], 1)
        self.assertEqual(s["totals"]["attributed_orders"], 2)
        self.assertEqual(s["totals"]["attributed_revenue"], Decimal("100"))
        self.assertEqual([p["sku"] for p in s["top_products"]], ["B"])
        self.assertTrue(all(e["sku"] == "B" for e in s["events"]))
        self.assertEqual(len(s["events"]), 3)
        s = self.stats(sku="A")
        self.assertEqual(s["totals"]["attributed_orders"], 1)
        self.assertEqual(s["totals"]["attributed_revenue"], Decimal("40"))
        # قائمة المنتجات للفلتر ما بتتأثر بالفلتر
        self.assertEqual(len(s["products"]), 2)

    def test_sku_filter_unknown_gives_zeros(self):
        s = self.stats(sku="ZZZ")
        self.assertEqual(s["totals"]["tryons"], 0)
        self.assertEqual(s["totals"]["attributed_orders"], 0)


class EventsTests(StatsBase):
    def test_events_newest_first_capped_and_fields(self):
        for i in range(105):
            self.tryon(NOW - timedelta(minutes=i + 1), session="abcdefghijkl")
        events = self.stats()["events"]
        self.assertEqual(len(events), 100)
        times = [e["created_at"] for e in events]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(events[0]["session_short"], "abcdefgh")
        self.assertEqual(set(events[0]), {"created_at", "sku", "name", "success", "error", "session_short", "has_order"})

    def test_blank_session_and_error_fields(self):
        self.tryon(ago(hours=1), session="", success=False, error="timeout")
        e = self.stats()["events"][0]
        self.assertEqual((e["session_short"], e["success"], e["error"], e["has_order"]), ("", False, "timeout", False))

    def test_has_order_flag(self):
        self.tryon(ago(days=2), session="s1")
        self.tryon(ago(days=2), session="s2")
        self.tryon(ago(days=2), session="s1", success=False)
        self.order(session="s1", at=ago(days=1))
        flags = {(e["session_short"], e["success"]): e["has_order"] for e in self.stats()["events"]}
        self.assertEqual(flags, {("s1", True): True, ("s2", True): False, ("s1", False): False})

    def test_identified_filter_only_affects_events(self):
        self.tryon(ago(days=2), session="known")
        self.tryon(ago(days=2), session="anon")
        self.tryon(ago(days=2), session="")
        Conversation.objects.create(merchant=self.merchant, session_id="known")
        # محادثة تاجر تاني بنفس الجلسة ما بتعتبر الزبون معروف عندنا
        Conversation.objects.create(merchant=self.other, session_id="anon")
        s = self.stats(shoppers="identified")
        self.assertEqual([e["session_short"] for e in s["events"]], ["known"])
        self.assertEqual(s["totals"]["tryons"], 3)
        self.assertEqual(len(self.stats(shoppers="all")["events"]), 3)


class RangeTests(StatsBase):
    def test_unknown_range_falls_back_to_30(self):
        s = self.stats(range_key="bogus")
        self.assertEqual(len(s["chart"]), 30)
        self.assertEqual(s["range_end"], NOW)
        self.assertEqual(len(self.stats(range_key=7)["chart"]), 7)

    def test_range_end_is_now(self):
        self.assertEqual(self.stats(range_key="7")["range_end"], NOW)

    def test_period_active_starts_at_period_start(self):
        self.merchant.period = ApiToken.Period.WEEKLY
        self.merchant.period_start = ago(days=3)
        self.merchant.save()
        self.tryon(ago(days=2))
        self.tryon(ago(days=4))  # قبل بداية الفترة
        s = self.stats(range_key="period")
        self.assertEqual(s["range_start"], ago(days=3))
        self.assertEqual(s["totals"]["tryons"], 1)
        self.assertEqual(len(s["chart"]), 4)

    def test_period_expired_uses_last_period_days(self):
        self.merchant.period = ApiToken.Period.WEEKLY
        self.merchant.period_start = ago(days=20)  # الفترة خلصت ولسا ما انعمل ريسيت
        self.merchant.save()
        self.tryon(ago(days=2))
        self.tryon(ago(days=10))
        s = self.stats(range_key="period")
        self.assertEqual(len(s["chart"]), 7)
        self.assertEqual(s["totals"]["tryons"], 1)

    def test_period_start_in_future_is_clamped(self):
        self.merchant.period_start = NOW + timedelta(days=1)
        self.merchant.save()
        s = self.stats(range_key="period")
        self.assertEqual(s["range_start"], NOW)
        self.assertEqual(len(s["chart"]), 1)


class TryOnStatsViewTests(StatsBase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user("staff-ts", password="x", is_staff=True)

    def url(self, merchant=None):
        return f"/{(merchant or self.merchant).pk}/tryon-stats/"

    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get(self.url()).status_code, 302)

    def test_non_staff_redirected(self):
        user = get_user_model().objects.create_user("plain-ts", password="x")
        self.client.force_login(user)
        self.assertEqual(self.client.get(self.url()).status_code, 302)

    def test_staff_gets_context(self):
        self.client.force_login(self.staff)
        self.tryon(timezone.now() - timedelta(hours=1))
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 200)
        ctx = resp.context
        self.assertEqual(ctx["merchant"], self.merchant)
        self.assertEqual(ctx["range_key"], "30")
        self.assertEqual(ctx["window_days"], 7)
        self.assertEqual(ctx["tab"], "overview")
        self.assertEqual(ctx["shoppers"], "all")
        self.assertEqual(ctx["selected_sku"], "")
        self.assertEqual(ctx["currency"], self.merchant.currency)
        self.assertEqual(ctx["stats"]["totals"]["tryons"], 1)

    def test_params_are_sanitised(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url(), {
            "range": "period", "window": "14", "tab": "activity", "sku": "A", "shoppers": "identified",
        })
        ctx = resp.context
        self.assertEqual((ctx["range_key"], ctx["window_days"], ctx["tab"]), ("period", 14, "activity"))
        self.assertEqual((ctx["selected_sku"], ctx["shoppers"]), ("A", "identified"))
        resp = self.client.get(self.url(), {"range": "x", "window": "5", "tab": "z", "shoppers": "q"})
        ctx = resp.context
        self.assertEqual((ctx["range_key"], ctx["window_days"], ctx["tab"], ctx["shoppers"]), ("30", 7, "overview", "all"))

    def test_unknown_merchant_404(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/999999/tryon-stats/").status_code, 404)
