"""تنبيهات الرصيد على واتساب (80% و100%): مرة لكل عتبة بالفترة، ما بترمي أبداً، وبترجّع الحجز لما الإرسال يفشل.
ما في شبكة: lightotp.send_utility متبدّل بـ mock."""
import uuid
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import ApiToken
from .portal_testing import FAST_HASHERS, make_account
from .services import lightotp, quota_alerts
from .services.quota_alerts import maybe_send_quota_alerts

SEND = "apitokens.services.lightotp.send_utility"
CONFIG = dict(
    LIGHTOTP_API_KEY="test-key", LIGHTOTP_QUOTA_ALERT_TEMPLATE_ID="tpl-quota-alert", LIGHTOTP_DEFAULT_REGION="SA",
)


def ok_result(**kwargs):
    return lightotp.SendResult("msg-1", "Pending")


@FAST_HASHERS
@override_settings(**CONFIG)
class AlertBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.m = ApiToken.objects.create(label="Shop A", image_quota=100, images_used=0)
        self.account = make_account(self.m, phone="+966501234567")
        patcher = mock.patch(SEND, side_effect=ok_result)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def use(self, n, merchant=None):
        merchant = merchant or self.m
        ApiToken.objects.filter(pk=merchant.pk).update(images_used=n)

    def fresh(self, merchant=None):
        merchant = merchant or self.m
        merchant.refresh_from_db()
        return merchant

    def run_alerts(self, merchant=None):
        return maybe_send_quota_alerts(merchant or self.fresh())


class ThresholdTests(AlertBase):
    def test_below_eighty_sends_nothing(self):
        self.use(79)
        self.assertEqual(self.run_alerts(), [])
        self.send.assert_not_called()
        self.assertIsNone(self.fresh().quota_alert_80_period)

    def test_eighty_sends_once_per_period(self):
        self.use(80)
        self.assertEqual(self.run_alerts(), [80])
        self.assertEqual(self.send.call_count, 1)
        m = self.fresh()
        self.assertEqual(m.quota_alert_80_period, m.period_start)
        self.assertIsNone(m.quota_alert_100_period)
        self.assertEqual(self.run_alerts(), [])
        self.assertEqual(self.send.call_count, 1)

    def test_hundred_after_eighty_sends_second_alert_then_never_again(self):
        self.use(80)
        self.run_alerts()
        self.use(100)
        self.assertEqual(self.run_alerts(), [100])
        self.assertEqual(self.send.call_count, 2)
        self.run_alerts()
        self.use(100)
        self.assertEqual(self.send.call_count, 2)
        m = self.fresh()
        self.assertEqual((m.quota_alert_80_period, m.quota_alert_100_period), (m.period_start, m.period_start))

    def test_crossing_straight_to_exhausted_sends_only_the_hundred(self):
        self.use(100)
        self.assertEqual(self.run_alerts(), [100])
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.send.call_args.kwargs["body_params"][1], "100")
        m = self.fresh()
        self.assertEqual((m.quota_alert_80_period, m.quota_alert_100_period), (m.period_start, m.period_start))
        self.assertEqual(self.run_alerts(), [])
        self.assertEqual(self.send.call_count, 1)

    def test_overshoot_counts_as_exhausted(self):
        self.use(105)
        self.assertEqual(self.run_alerts(), [100])

    def test_new_period_rearms_both_thresholds(self):
        self.use(100)
        self.run_alerts()
        new_start = timezone.now() + timedelta(days=31)
        ApiToken.objects.filter(pk=self.m.pk).update(period_start=new_start, images_used=80)
        self.assertEqual(self.run_alerts(), [80])
        ApiToken.objects.filter(pk=self.m.pk).update(images_used=100)
        self.assertEqual(self.run_alerts(), [100])
        self.assertEqual(self.send.call_count, 3)

    def test_expired_period_counts_as_unused(self):
        ApiToken.objects.filter(pk=self.m.pk).update(
            images_used=100, period_start=timezone.now() - timedelta(days=40),
        )
        self.assertEqual(self.run_alerts(), [])
        self.send.assert_not_called()

    def test_stale_object_is_refreshed_from_db(self):
        stale = ApiToken.objects.get(pk=self.m.pk)
        self.use(85)
        self.assertEqual(maybe_send_quota_alerts(stale), [80])
        self.assertEqual(maybe_send_quota_alerts(stale), [])
        self.assertEqual(self.send.call_count, 1)

    def test_inactive_store_is_skipped(self):
        self.use(100)
        ApiToken.objects.filter(pk=self.m.pk).update(is_active=False)
        self.assertEqual(self.run_alerts(), [])
        self.send.assert_not_called()


class MessageTests(AlertBase):
    def test_call_arguments_and_idempotency_key(self):
        self.use(80)
        self.run_alerts()
        kw = self.send.call_args.kwargs
        ps = self.fresh().period_start
        self.assertEqual(kw["api_key"], "test-key")
        self.assertEqual(kw["template_id"], "tpl-quota-alert")
        self.assertEqual(kw["phone"], "+966501234567")
        self.assertEqual(kw["default_region"], "SA")
        self.assertEqual(kw["lang"], "ar")
        self.assertEqual(kw["body_params"], ["Shop A", "80", "20"])
        self.assertEqual(kw["idempotency_key"], lightotp.event_key(f"quota-alert:{self.m.pk}:80:{ps.isoformat()}"))
        uuid.UUID(kw["idempotency_key"])
        self.assertNotIn("header_param", kw)

    def test_params_are_strings_with_actual_percent_and_remaining(self):
        self.use(83)
        self.run_alerts()
        params = self.send.call_args.kwargs["body_params"]
        self.assertEqual(params, ["Shop A", "83", "17"])
        self.assertTrue(all(isinstance(p, str) for p in params))

    def test_exhausted_message_says_100_and_zero(self):
        self.use(100)
        self.run_alerts()
        self.assertEqual(self.send.call_args.kwargs["body_params"], ["Shop A", "100", "0"])

    def test_idempotency_key_differs_by_threshold_and_period(self):
        self.use(80)
        self.run_alerts()
        self.use(100)
        self.run_alerts()
        k80, k100 = (c.kwargs["idempotency_key"] for c in self.send.call_args_list)
        self.assertNotEqual(k80, k100)
        ApiToken.objects.filter(pk=self.m.pk).update(period_start=timezone.now() + timedelta(days=31), images_used=80)
        self.run_alerts()
        k80_next = self.send.call_args.kwargs["idempotency_key"]
        self.assertNotIn(k80_next, (k80, k100))

    def test_retry_after_failure_reuses_the_same_idempotency_key(self):
        self.use(80)
        self.send.side_effect = lightotp.LightOtpError("NetworkError", 0, True)
        self.run_alerts()
        cache.clear()
        self.send.side_effect = ok_result
        self.assertEqual(self.run_alerts(), [80])
        first, second = (c.kwargs["idempotency_key"] for c in self.send.call_args_list)
        self.assertEqual(first, second)

    def test_label_is_sanitized_for_whatsapp(self):
        ApiToken.objects.filter(pk=self.m.pk).update(label="  My\nShop   \t  name " + "x" * 200, images_used=90)
        self.run_alerts()
        label = self.send.call_args.kwargs["body_params"][0]
        self.assertTrue(label.startswith("My Shop name x"))
        self.assertNotIn("\n", label)
        self.assertLessEqual(len(label), 60)


class RecipientAndConfigTests(AlertBase):
    def assertNothingSentNorClaimed(self):
        self.send.assert_not_called()
        m = self.fresh()
        self.assertIsNone(m.quota_alert_80_period)
        self.assertIsNone(m.quota_alert_100_period)

    def test_unverified_account_gets_nothing(self):
        make_account(self.m, phone="+966501234567", verified=False)
        self.use(90)
        self.assertEqual(self.run_alerts(), [])
        self.assertNothingSentNorClaimed()

    def test_inactive_account_gets_nothing(self):
        make_account(self.m, phone="+966501234567", is_active=False)
        self.use(90)
        self.assertEqual(self.run_alerts(), [])
        self.assertNothingSentNorClaimed()

    def test_no_account_gets_nothing(self):
        self.account.delete()
        self.use(90)
        self.assertEqual(self.run_alerts(), [])
        self.assertNothingSentNorClaimed()

    def test_alert_is_sent_once_account_becomes_verified_later(self):
        make_account(self.m, phone="+966501234567", verified=False)
        self.use(90)
        self.run_alerts()
        make_account(self.m, phone="+966501234567", verified=True)
        self.assertEqual(self.run_alerts(), [80])

    def test_only_this_stores_account_is_used(self):
        other = ApiToken.objects.create(label="Shop B", image_quota=100)
        make_account(other, phone="+966507654321")
        self.use(90)
        self.run_alerts()
        self.assertEqual(self.send.call_args.kwargs["phone"], "+966501234567")

    @override_settings(LIGHTOTP_API_KEY="")
    def test_missing_api_key_skips_silently(self):
        self.use(90)
        self.assertEqual(self.run_alerts(), [])
        self.assertNothingSentNorClaimed()

    @override_settings(LIGHTOTP_QUOTA_ALERT_TEMPLATE_ID="")
    def test_missing_template_id_skips_silently(self):
        self.use(90)
        self.assertEqual(self.run_alerts(), [])
        self.assertNothingSentNorClaimed()

    def test_alert_is_sent_once_config_appears_later(self):
        self.use(90)
        with override_settings(LIGHTOTP_QUOTA_ALERT_TEMPLATE_ID=""):
            self.run_alerts()
        self.assertEqual(self.run_alerts(), [80])


class FailureTests(AlertBase):
    def test_provider_error_does_not_raise_reverts_claim_and_rate_limits(self):
        self.use(85)
        self.send.side_effect = lightotp.LightOtpError("InsufficientBalance", 402, False)
        self.assertEqual(self.run_alerts(), [])
        self.assertEqual(self.send.call_count, 1)
        self.assertIsNone(self.fresh().quota_alert_80_period)
        # النداء الجاي (ضمن الساعة) ما بيطرق المزوّد
        self.assertEqual(self.run_alerts(), [])
        self.assertEqual(self.send.call_count, 1)
        self.assertIsNone(self.fresh().quota_alert_80_period)

    def test_retry_is_allowed_after_the_cooldown(self):
        self.use(85)
        self.send.side_effect = lightotp.LightOtpError("NetworkError", 0, True)
        self.run_alerts()
        cache.clear()  # مرّت الساعة
        self.send.side_effect = ok_result
        self.assertEqual(self.run_alerts(), [80])
        self.assertEqual(self.send.call_count, 2)
        m = self.fresh()
        self.assertEqual(m.quota_alert_80_period, m.period_start)

    def test_cooldown_is_one_hour(self):
        self.assertEqual(quota_alerts.RETRY_COOLDOWN_SECONDS, 3600)
        self.use(85)
        self.send.side_effect = lightotp.LightOtpError("NetworkError", 0, True)
        with mock.patch.object(quota_alerts.cache, "set", wraps=quota_alerts.cache.set) as spy:
            self.run_alerts()
        self.assertEqual(spy.call_args.args[2], 3600)

    def test_cooldown_is_per_merchant_and_threshold(self):
        other = ApiToken.objects.create(label="Shop B", image_quota=100, images_used=90)
        make_account(other, phone="+966507654321")
        self.use(85)
        self.send.side_effect = lightotp.LightOtpError("NetworkError", 0, True)
        self.run_alerts()
        self.send.side_effect = ok_result
        # متجر تاني ما بيتأثر
        self.assertEqual(self.run_alerts(other), [80])
        # نفس المتجر بعتبة 100: ما في تهدئة عليها
        self.use(100)
        self.assertEqual(self.run_alerts(), [100])

    def test_invalid_phone_value_error_is_handled(self):
        self.use(85)
        self.send.side_effect = ValueError("bad phone")
        self.assertEqual(self.run_alerts(), [])
        self.assertIsNone(self.fresh().quota_alert_80_period)

    def test_unexpected_exception_is_handled(self):
        self.use(85)
        self.send.side_effect = RuntimeError("boom")
        self.assertEqual(self.run_alerts(), [])
        self.assertIsNone(self.fresh().quota_alert_80_period)

    def test_failed_hundred_reverts_both_fields_when_it_jumped(self):
        self.use(100)
        self.send.side_effect = lightotp.LightOtpError("NetworkError", 0, True)
        self.run_alerts()
        m = self.fresh()
        self.assertEqual((m.quota_alert_80_period, m.quota_alert_100_period), (None, None))

    def test_failed_hundred_keeps_earlier_eighty_mark(self):
        self.use(80)
        self.run_alerts()
        self.use(100)
        self.send.side_effect = lightotp.LightOtpError("NetworkError", 0, True)
        self.run_alerts()
        m = self.fresh()
        self.assertEqual(m.quota_alert_80_period, m.period_start)
        self.assertIsNone(m.quota_alert_100_period)

    def test_never_raises_when_everything_breaks(self):
        self.use(90)
        with mock.patch.object(quota_alerts, "quota_status", side_effect=RuntimeError("db down")):
            self.assertEqual(self.run_alerts(), [])
        with mock.patch.object(quota_alerts.ApiToken.objects, "filter", side_effect=RuntimeError("db down")):
            self.assertEqual(self.run_alerts(), [])
        self.assertEqual(maybe_send_quota_alerts(object()), [])
        self.send.assert_not_called()

    def test_cache_outage_does_not_break_or_block(self):
        self.use(90)
        with mock.patch.object(quota_alerts.cache, "get", side_effect=RuntimeError("cache down")):
            self.assertEqual(self.run_alerts(), [80])

    def test_logs_never_contain_the_phone_number(self):
        self.use(85)
        self.send.side_effect = lightotp.LightOtpError("InsufficientBalance", 402, False)
        with self.assertLogs("apitokens.services.quota_alerts", level="DEBUG") as logs:
            self.run_alerts()
        self.assertTrue(logs.output)
        self.assertNotIn("501234567", "\n".join(logs.output))


class ConcurrencyTests(AlertBase):
    def test_second_claim_loses(self):
        self.use(85)
        m = self.fresh()
        ps = m.period_start
        self.assertTrue(quota_alerts._claim(m, 80, ps))
        self.assertFalse(quota_alerts._claim(m, 80, ps))
        self.assertEqual(self.fresh().quota_alert_80_period, ps)

    def test_claim_loses_when_the_period_already_rolled_over(self):
        self.use(85)
        m = self.fresh()
        old_ps = m.period_start
        ApiToken.objects.filter(pk=m.pk).update(period_start=old_ps + timedelta(days=30))
        self.assertFalse(quota_alerts._claim(m, 80, old_ps))
        self.assertIsNone(self.fresh().quota_alert_80_period)

    def test_hundred_claim_marks_eighty_too_and_a_second_hundred_claim_loses(self):
        self.use(100)
        m = self.fresh()
        ps = m.period_start
        self.assertTrue(quota_alerts._claim(m, 100, ps))
        self.assertFalse(quota_alerts._claim(m, 100, ps))
        m = self.fresh()
        self.assertEqual((m.quota_alert_80_period, m.quota_alert_100_period), (ps, ps))

    def test_concurrent_call_during_send_does_not_double_send(self):
        self.use(85)
        nested = []

        def reentrant(**kwargs):
            nested.append(maybe_send_quota_alerts(ApiToken.objects.get(pk=self.m.pk)))
            return ok_result()

        self.send.side_effect = reentrant
        self.assertEqual(self.run_alerts(), [80])
        self.assertEqual(nested, [[]])
        self.assertEqual(self.send.call_count, 1)

    def test_call_that_lost_the_claim_sends_nothing(self):
        self.use(85)
        m = self.fresh()
        with mock.patch.object(quota_alerts, "_claim", return_value=False):
            self.assertEqual(maybe_send_quota_alerts(m), [])
        self.send.assert_not_called()


class CommandTests(AlertBase):
    def run_command(self):
        out = StringIO()
        call_command("send_quota_alerts", stdout=out)
        return out.getvalue()

    def test_counts_only_and_alerts_every_active_store_once(self):
        self.use(90)
        b = ApiToken.objects.create(label="Shop B", image_quota=10, images_used=10)
        make_account(b, phone="+966507654321")
        ApiToken.objects.create(label="Idle", image_quota=10, images_used=1)
        inactive = ApiToken.objects.create(label="Off", image_quota=10, images_used=10, is_active=False)
        make_account(inactive, phone="+966509999999")
        out = self.run_command()
        self.assertEqual(out.strip(), "checked=3 alerts_sent=2")
        self.assertEqual(self.send.call_count, 2)
        self.assertNotIn("+966", out)
        self.assertNotIn("Shop", out)
        # شبكة الأمان: تشغيل ثاني ما بيكرر
        self.assertEqual(self.run_command().strip(), "checked=3 alerts_sent=0")
        self.assertEqual(self.send.call_count, 2)

    def test_command_survives_provider_failure(self):
        self.use(90)
        self.send.side_effect = lightotp.LightOtpError("NetworkError", 0, True)
        self.assertEqual(self.run_command().strip(), "checked=1 alerts_sent=0")

    @override_settings(LIGHTOTP_API_KEY="")
    def test_command_without_config_sends_nothing(self):
        self.use(90)
        self.assertEqual(self.run_command().strip(), "checked=1 alerts_sent=0")
        self.send.assert_not_called()
