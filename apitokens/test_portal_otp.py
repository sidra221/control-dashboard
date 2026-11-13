"""اختبارات OTP واتساب لبوابة التاجر: توليد/تخزين الكود، الإرسال (تهدئة تصاعدية، إعادة محاولة، حدود
التكلفة، القناة المحلية)، صفحة التحقق، التسجيل بالمسارين وقواعد الاستيلاء على الأرقام، البوابة للحسابات
غير الموثّقة، شاشة الستاف، وأوامر الصيانة. كل شي بـ mock: ما في أي نداء شبكة حقيقي لـ LightOTP."""

import re
from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth.hashers import check_password
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError
from django.db.models import F
from django.db.models.query import QuerySet
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import portal_auth, portal_otp
from .i18n import TRANSLATIONS, t
from .models import ApiToken, MerchantAccount, PhoneOtp
from .portal_testing import (
    FAST_HASHERS, SEND_TARGET, _put_session, login_pending, login_portal, login_unverified, make_account,
    make_pending_account, patch_otp_send, sent_codes,
)
from .services import lightotp
from .test_portal_auth import GOOD_PHONE, PW, PortalBase, csrf_token

OTHER_PHONE = "+966509999999"
REAL_SEND = lightotp.send_otp_message  # الأصلي، قبل ما أي اختبار يبدّله بـ mock
LOGGER = "apitokens.portal_otp"
IP = "10.1.2.3"
VERIFY = reverse("portal:verify")
RESEND = reverse("portal:verify_resend")


def api_response(status, body=None):
    """رد وهمي من requests.post (ما في شبكة)."""
    return mock.Mock(status_code=status, **{"json.return_value": body if body is not None else {}})


class OtpBase(PortalBase):
    def new_account(self, phone=GOOD_PHONE, verified=False, merchant=None, password=PW, **kw):
        if merchant is not None:
            return make_account(merchant, phone=phone, password=password, verified=verified, **kw)
        return make_pending_account(phone=phone, password=password, verified=verified, **kw)

    def sign_in(self, account, client=None):
        _put_session(client or self.client, account)

    def send(self, account, ip=IP, lang="en"):
        return portal_otp.send_code(account, lang, ip)

    def last_code(self):
        return sent_codes(self.sender)[-1]

    def shift(self, seconds):
        """بنقدّم الوقت: كل الصفوف بتصير أقدم بـ seconds (التهدئة محسوبة من created_at)."""
        PhoneOtp.objects.update(created_at=F("created_at") - timedelta(seconds=seconds))

    def post_code(self, code, client=None):
        return (client or self.client).post(VERIFY, {"code": code})

    def started(self, **kw):
        """حساب غير موثّق مسجّل دخوله وانبعتله كود: (account, code)."""
        account = self.new_account(**kw)
        self.sign_in(account)
        self.assertEqual(self.send(account).status, portal_otp.OK)
        return account, self.last_code()

    def signup(self, client=None, **over):
        data = {"name": "Ahmad", "phone": GOOD_PHONE, "password": PW, "password_confirm": PW}
        if "password" in over:
            data["password_confirm"] = over["password"]
        data.update(over)
        return (client or self.client).post(reverse("portal:register"), data)

    def html(self, url=VERIFY, client=None):
        return (client or self.client).get(url).content.decode()


# ---------- الكود والتخزين ----------


class CodeTests(OtpBase):
    def test_codes_are_six_digits_and_vary(self):
        codes = {portal_otp.generate_code() for _ in range(200)}
        self.assertTrue(all(re.fullmatch(r"[0-9]{6}", c) for c in codes))
        self.assertGreater(len(codes), 150)

    def test_leading_zeros_kept(self):
        with mock.patch("secrets.randbelow", return_value=42):
            self.assertEqual(portal_otp.generate_code(), "000042")

    def test_hash_is_keyed_hmac_bound_to_pk_and_code(self):
        h = portal_otp.hash_code(7, "123456")
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertEqual(h, portal_otp.hash_code(7, "123456"))
        self.assertNotEqual(h, portal_otp.hash_code(8, "123456"))
        self.assertNotEqual(h, portal_otp.hash_code(7, "123457"))
        with override_settings(SECRET_KEY="another-secret-key-for-test"):
            self.assertNotEqual(h, portal_otp.hash_code(7, "123456"))

    def test_plaintext_code_is_never_stored(self):
        account, code = self.started()
        otp = PhoneOtp.objects.get()
        self.assertEqual(otp.code_hash, portal_otp.hash_code(otp.pk, code))
        self.assertNotIn(code, otp.code_hash)
        for obj in (otp, account):
            for field in obj._meta.concrete_fields:
                self.assertNotEqual(str(getattr(obj, field.attname)), code, field.name)
        dump = " ".join(str(v) for row in PhoneOtp.objects.values() for v in row.values())
        self.assertNotIn(f" {code} ", f" {dump} ")

    def test_normalize_code(self):
        n = portal_otp.normalize_code
        self.assertEqual(n("123456"), "123456")
        self.assertEqual(n(" 123 456 "), "123456")
        self.assertEqual(n("١٢٣٤٥٦"), "123456")  # عربي-هندي
        self.assertEqual(n("۱۲۳۴۵۶"), "123456")  # فارسي
        self.assertEqual(n("‎123 456‏"), "123456")
        for bad in ["", "12345", "1234567", "12a456", "１２３４５６", None, 123456, "123-456"]:
            self.assertIsNone(n(bad), repr(bad))

    def test_masked_phone(self):
        self.assertEqual(portal_otp.mask_phone("+966501234567"), "+966 •• ••• 4567")
        self.assertEqual(portal_otp.mask_phone("+14155552671"), "+1 ••• ••• 2671")
        self.assertEqual(portal_otp.mask_phone("+201001234567"), "+20 ••• ••• 4567")
        for phone in ("+966501234567", "+14155552671"):
            masked = portal_otp.mask_phone(phone)
            self.assertNotIn(phone[4:9], masked)
        self.assertEqual(portal_otp._log_phone("+966501234567"), "+966*****4567")


# ---------- الإرسال ----------


class SendSuccessTests(OtpBase):
    def test_success_saves_message_id_status_expiry_and_calls_service_once(self):
        account = self.new_account()
        outcome = self.send(account, lang="ar")
        self.assertEqual((outcome.status, outcome.wait_seconds), (portal_otp.OK, 30))
        otp = PhoneOtp.objects.get()
        self.assertEqual((otp.message_id, otp.message_status, otp.send_error), ("msg-1", "Pending", ""))
        self.assertEqual(otp.phone, GOOD_PHONE)
        self.assertLess(abs((otp.expires_at - otp.created_at) - timedelta(minutes=10)), timedelta(seconds=2), "10 دقايق")
        self.assertEqual(otp.attempts, 0)
        self.assertIsNone(otp.consumed_at)
        self.sender.assert_called_once()
        kw = self.sender.call_args.kwargs
        self.assertEqual((kw["phone"], kw["event"], kw["lang"]), (GOOD_PHONE, f"portal-otp:{otp.pk}", "ar"))

    @override_settings(LIGHTOTP_API_KEY="test-key", LIGHTOTP_TEMPLATE_ID="tmpl-1")
    def test_payload_follows_the_sendmessage_contract_and_idempotency_key_from_otp(self):
        with mock.patch(SEND_TARGET, REAL_SEND):  # الخدمة الحقيقية، بس requests.post mock
            with mock.patch("apitokens.services.lightotp.requests.post") as post:
                post.return_value = api_response(200, {"id": "abc-123", "messageStatus": "Pending"})
                account = self.new_account()
                self.assertEqual(self.send(account).status, portal_otp.OK)
        otp = PhoneOtp.objects.get()
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://api.lightotp.com/SendMessage")
        payload = post.call_args.kwargs["json"]
        # عقد /SendMessage: الكود والرقم واللغة والمفتاح بس — ما في templateId ولا bodyParameters
        self.assertEqual(set(payload), {"otpCode", "toPhoneE164", "languageCode", "idempotencyKey"})
        code = payload["otpCode"]
        self.assertRegex(code, r"^[0-9]{6}$")
        self.assertEqual(otp.code_hash, portal_otp.hash_code(otp.pk, code))
        self.assertEqual(payload["idempotencyKey"], lightotp.event_key(f"portal-otp:{otp.pk}"))
        self.assertEqual(payload["toPhoneE164"], GOOD_PHONE)
        self.assertEqual(payload["languageCode"], "en")
        self.assertEqual(post.call_args.kwargs["headers"]["Accept-Language"], "en")
        self.assertEqual((otp.message_id, otp.message_status), ("abc-123", "Pending"))

    def test_new_successful_code_invalidates_older_unconsumed_ones(self):
        account = self.new_account()
        self.send(account)
        self.shift(31)
        self.send(account)
        first, second = PhoneOtp.objects.order_by("pk")
        self.assertIsNotNone(first.consumed_at)
        self.assertIsNone(second.consumed_at)

    def test_failed_send_leaves_the_older_code_usable(self):
        account = self.new_account()
        self.send(account)
        old_code = self.last_code()
        self.shift(31)
        self.sender.side_effect = lightotp.LightOtpError("TemplateNotFound", 404, False)
        self.assertEqual(self.send(account).status, portal_otp.UNAVAILABLE)
        self.sign_in(account)
        self.assertEqual(portal_otp.verify_code(account, old_code), portal_otp.V_OK)

    def test_already_verified_account_gets_nothing(self):
        account = self.new_account(verified=True)
        self.assertEqual(self.send(account).status, portal_otp.VERIFIED)
        self.sender.assert_not_called()
        self.assertFalse(PhoneOtp.objects.exists())


class CooldownTests(OtpBase):
    def test_table_matches_lightotp(self):
        self.assertEqual([portal_otp.cooldown_seconds(n) for n in range(0, 7)], [0, 30, 60, 120, 240, 480, 960])
        self.assertEqual(portal_otp.cooldown_seconds(10), 15360)
        self.assertEqual(portal_otp.cooldown_seconds(11), 21600)  # سقف 6 ساعات
        self.assertEqual(portal_otp.cooldown_seconds(500), 21600)

    def test_escalation_30_60_120_240_480_and_no_api_call_while_cooling(self):
        account = self.new_account()
        for n, expected in enumerate([30, 60, 120, 240, 480], start=1):
            outcome = self.send(account)
            self.assertEqual((outcome.status, outcome.wait_seconds), (portal_otp.OK, expected), n)
            calls = self.sender.call_count
            # فورا: تهدئة، بدون نداء API وبدون صف جديد
            again = self.send(account)
            self.assertEqual(again.status, portal_otp.WAIT)
            self.assertIn(again.wait_seconds, range(expected - 1, expected + 1))
            self.assertEqual(self.sender.call_count, calls)
            self.assertEqual(PhoneOtp.objects.count(), n)
            # قبل ما تخلص بثانية: لسا تهدئة
            self.shift(expected - 1)
            self.assertEqual(self.send(account).status, portal_otp.WAIT)
            self.assertEqual(self.sender.call_count, calls)
            self.shift(2)  # بعد انتهائها
        self.assertEqual(PhoneOtp.objects.exclude(message_id="").count(), 5)

    def test_failed_sends_do_not_count_towards_cooldown(self):
        account = self.new_account()
        self.sender.side_effect = lightotp.LightOtpError("InsufficientBalance", 400, False)
        for _ in range(3):
            self.assertEqual(self.send(account).status, portal_otp.UNAVAILABLE)
        self.assertEqual(portal_otp.cooldown_remaining(GOOD_PHONE), 0)
        self.sender.side_effect = self.sender.default_effect
        outcome = self.send(account)
        self.assertEqual((outcome.status, outcome.wait_seconds), (portal_otp.OK, 30), "أول ناجح = 30 مو 240")

    def test_counter_resets_after_six_hours_without_a_successful_send(self):
        account = self.new_account()
        for _ in range(3):
            self.send(account)
            self.shift(portal_otp.cooldown_seconds(3) + 1)
        self.assertEqual(portal_otp.cooldown_remaining(GOOD_PHONE), 0)
        self.shift(6 * 3600)
        outcome = self.send(account)
        self.assertEqual((outcome.status, outcome.wait_seconds), (portal_otp.OK, 30))

    def test_cooldown_is_per_phone(self):
        a, b = self.new_account(), self.new_account(phone=OTHER_PHONE)
        self.send(a)
        self.assertEqual(self.send(b).status, portal_otp.OK)
        self.assertEqual(self.send(a).status, portal_otp.WAIT)

    def test_rows_without_message_id_are_ignored(self):
        account = self.new_account()
        PhoneOtp.objects.create(account=account, phone=GOOD_PHONE, expires_at=timezone.now(), send_error="x")
        self.assertEqual(portal_otp.cooldown_remaining(GOOD_PHONE), 0)

    def test_lightotp_side_cooldown_400_is_a_wait_hint_and_not_counted(self):
        account = self.new_account()
        self.sender.side_effect = lightotp.LightOtpError("Please wait 45 seconds before sending again", 400, True)
        outcome = self.send(account)
        self.assertEqual((outcome.status, outcome.wait_seconds), (portal_otp.WAIT, 60))
        self.assertEqual(self.sender.call_count, 1, "بدون إعادة محاولة")
        otp = PhoneOtp.objects.get()
        self.assertEqual(otp.message_id, "")
        self.assertIsNotNone(otp.consumed_at)
        self.assertEqual(otp.send_error[:8], "HTTP 400")
        self.assertEqual(portal_otp.cooldown_remaining(GOOD_PHONE), 0)

    def test_lightotp_cooldown_text_with_full_phone_is_scrubbed_before_storing(self):
        account = self.new_account()
        self.sender.side_effect = lightotp.LightOtpError(f"wait 30s for {GOOD_PHONE}", 400, True)
        self.send(account)
        self.assertNotIn(GOOD_PHONE, PhoneOtp.objects.get().send_error)
        self.assertNotIn(GOOD_PHONE[1:], PhoneOtp.objects.get().send_error)


class RetryAndErrorTests(OtpBase):
    """إعادة المحاولة والأخطاء بمستوى requests.post (المفتاح نفسه بالمحاولتين)."""

    def setUp(self):
        super().setUp()
        self.settings_cm = override_settings(LIGHTOTP_API_KEY="test-key", LIGHTOTP_TEMPLATE_ID="tmpl-1")
        self.settings_cm.enable()
        self.addCleanup(self.settings_cm.disable)
        real = mock.patch(SEND_TARGET, REAL_SEND)
        real.start()
        self.addCleanup(real.stop)
        post = mock.patch("apitokens.services.lightotp.requests.post")
        self.post = post.start()
        self.addCleanup(post.stop)
        self.account = self.new_account()

    def keys(self):
        return [call.kwargs["json"]["idempotencyKey"] for call in self.post.call_args_list]

    def test_one_immediate_retry_with_the_same_key_on_network_error(self):
        self.post.side_effect = [requests.ConnectionError("boom"), api_response(200, {"id": "m1", "messageStatus": "Pending"})]
        self.assertEqual(self.send(self.account).status, portal_otp.OK)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(len(set(self.keys())), 1)
        self.assertEqual(self.keys()[0], lightotp.event_key(f"portal-otp:{PhoneOtp.objects.get().pk}"))

    def test_one_retry_on_5xx_then_gives_up_after_second_failure(self):
        self.post.side_effect = [api_response(503, {}), api_response(500, {})]
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assertEqual(self.send(self.account).status, portal_otp.UNAVAILABLE)
        self.assertEqual(self.post.call_count, 2, "مرة وحدة بس")
        self.assertEqual(len(set(self.keys())), 1)

    def test_network_errors_twice_is_unavailable(self):
        self.post.side_effect = requests.ConnectionError("down")
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assertEqual(self.send(self.account).status, portal_otp.UNAVAILABLE)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(PhoneOtp.objects.get().send_error[:11], "HTTP 0: Net")

    def test_no_retry_on_429(self):
        self.post.return_value = api_response(429, {})
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assertEqual(self.send(self.account).status, portal_otp.UNAVAILABLE)
        self.assertEqual(self.post.call_count, 1)

    def test_no_retry_on_lightotp_cooldown_400(self):
        self.post.return_value = api_response(400, {"errorMessage": "Cooldown active, wait 28 seconds"})
        outcome = self.send(self.account)
        self.assertEqual((outcome.status, outcome.wait_seconds), (portal_otp.WAIT, 60))
        self.assertEqual(self.post.call_count, 1)

    def test_no_retry_on_permanent_codes(self):
        for code in sorted(lightotp.PERMANENT):
            cache.clear()  # حد الـ IP (15 إرسال/ساعة) أقل من عدد الرموز الدائمة
            self.post.reset_mock()
            self.post.return_value = api_response(400, {"errorMessage": code})
            with self.assertLogs(LOGGER, level="ERROR"):
                outcome = self.send(self.account)
            self.assertEqual(outcome.status, portal_otp.UNAVAILABLE, code)
            self.assertEqual(self.post.call_count, 1, code)

    def test_insufficient_balance_and_missing_sender_are_unavailable_without_retry(self):
        for status, code in ((400, "InsufficientBalance"), (404, "SenderNumberNotFound"), (404, "TemplateNotFound")):
            self.post.reset_mock()
            self.post.return_value = api_response(status, {"errorMessage": code})
            with self.assertLogs(LOGGER, level="ERROR"):
                self.assertEqual(self.send(self.account).status, portal_otp.UNAVAILABLE, code)
            self.assertEqual(self.post.call_count, 1)

    def test_garbage_200_response_is_unavailable_not_a_crash(self):
        self.post.return_value = api_response(200, {})  # بدون id
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assertEqual(self.send(self.account).status, portal_otp.UNAVAILABLE)
        self.assertEqual(PhoneOtp.objects.get().message_id, "")


class GenericFailureTests(OtpBase):
    """الإعداد/الرصيد/الرموز الدائمة: رسالة عامة للمستخدم، وتفاصيلها بالـ log بدون الكود والرقم الكامل."""

    def assert_log_is_clean(self, cm, code):
        text = "\n".join(cm.output)
        self.assertNotIn(code, text)
        self.assertNotIn(GOOD_PHONE, text)
        self.assertNotIn(GOOD_PHONE[1:], text)
        self.assertIn("+966*****4567", text)

    def resend_page(self, side_effect):
        account = self.new_account()
        self.sign_in(account)
        self.sender.side_effect = side_effect
        with self.assertLogs(LOGGER, level="ERROR") as cm:
            resp = self.client.post(RESEND, follow=True)
        return account, resp.content.decode(), cm

    def test_not_configured(self):
        account = self.new_account()
        self.sign_in(account)
        real = mock.patch(SEND_TARGET, REAL_SEND)  # الخدمة الحقيقية: المفاتيح فاضية بالاختبار
        real.start()
        self.addCleanup(real.stop)
        with self.assertLogs(LOGGER, level="ERROR") as cm:
            html = self.client.post(RESEND, follow=True).content.decode()
        self.assertIn("temporarily unavailable", html)
        self.assertEqual(PhoneOtp.objects.get().send_error, "NotConfigured")
        self.assertIn("NotConfigured", "\n".join(cm.output))

    def test_permanent_codes_and_insufficient_balance(self):
        for code, status in (("TemplateIsNotApproved", 400), ("ApiKeyExpired", 400), ("InsufficientBalance", 400),
                             ("BodyParameterCountMismatch", 400), ("InvalidphoneNumber", 400)):
            PhoneOtp.objects.all().delete()
            MerchantAccount.objects.all().delete()
            account, html, cm = self.resend_page(lightotp.LightOtpError(code, status, False))
            self.assertIn("temporarily unavailable", html, code)
            self.assertNotIn(code, html, "ما منعرض الخطأ التقني للمستخدم")
            otp = PhoneOtp.objects.get()
            self.assertEqual(otp.send_error, f"HTTP {status}: {code}")
            self.assertEqual(otp.message_id, "")
            self.assert_log_is_clean(cm, self.last_code())
            self.assertIn(code, "\n".join(cm.output), "الستاف بدو يشوف السبب")

    def test_failed_send_leaves_no_code_the_user_could_burn_attempts_on(self):
        account, _html, _cm = self.resend_page(lightotp.LightOtpError("TemplateNotFound", 404, False))
        otp = PhoneOtp.objects.get()
        self.assertIsNotNone(otp.consumed_at)
        self.assertIsNone(portal_otp.active_otp(account))
        # الكود المرسل (اللي ما وصل) ما بيفتح الحساب، وما بيحرق محاولات
        resp = self.post_code(self.last_code())
        self.assertEqual(resp.status_code, 400)
        otp.refresh_from_db()
        self.assertEqual(otp.attempts, 0)
        account.refresh_from_db()
        self.assertFalse(account.phone_verified)

    def test_unexpected_exception_is_unavailable_and_log_has_no_code(self):
        account, html, cm = self.resend_page(KeyError("id"))
        self.assertIn("temporarily unavailable", html)
        self.assert_log_is_clean(cm, self.last_code())

    def test_error_text_with_full_phone_is_scrubbed_from_logs(self):
        account, html, cm = self.resend_page(lightotp.LightOtpError(f"Weird for {GOOD_PHONE}", 500, True))
        self.assert_log_is_clean(cm, self.last_code())


class CostProtectionTests(OtpBase):
    def test_per_ip_send_throttle_15_per_hour(self):
        cache.clear()
        for i in range(portal_otp.IP_MAX_SENDS):
            acc = self.new_account(phone=f"+9665010{i:05d}")
            self.assertEqual(self.send(acc, ip="7.7.7.7").status, portal_otp.OK, i)
        extra = self.new_account(phone="+966502000001")
        self.assertEqual(self.send(extra, ip="7.7.7.7").status, portal_otp.THROTTLED)
        self.assertEqual(self.sender.call_count, portal_otp.IP_MAX_SENDS)
        self.assertFalse(PhoneOtp.objects.filter(account=extra).exists(), "المرفوض ما بيعمل صف")
        # IP تاني ما بيتأثر
        self.assertEqual(self.send(extra, ip="7.7.7.8").status, portal_otp.OK)

    def test_cooldown_hits_do_not_burn_the_ip_counter(self):
        account = self.new_account()
        self.send(account, ip="7.7.7.9")
        for _ in range(40):
            self.assertEqual(self.send(account, ip="7.7.7.9").status, portal_otp.WAIT)
        self.shift(31)
        self.assertEqual(self.send(account, ip="7.7.7.9").status, portal_otp.OK)

    def test_ip_throttle_uses_its_own_counter(self):
        account = self.new_account()
        self.send(account, ip="7.7.7.1")
        self.assertIsNotNone(cache.get("portal_otpsend_ip:7.7.7.1"))
        self.assertIsNone(cache.get("portal_login_ip:7.7.7.1"))

    @override_settings(PORTAL_OTP_MAX_SENDS_PER_HOUR=3)
    def test_global_hourly_cap_blocks_without_calling_the_api(self):
        owner = self.new_account(phone="+966500000001")
        for i in range(3):
            PhoneOtp.objects.create(
                account=owner, phone=f"+96650000010{i}", expires_at=timezone.now(), message_id=f"m{i}"
            )
        account = self.new_account()
        with self.assertLogs(LOGGER, level="ERROR") as cm:
            outcome = self.send(account)
        self.assertEqual(outcome.status, portal_otp.UNAVAILABLE)
        self.sender.assert_not_called()
        self.assertIn("cap", "\n".join(cm.output))
        # الأقدم من ساعة ما بينحسب، والصفوف بدون message_id (فاشلة) كمان لا
        PhoneOtp.objects.filter(message_id="m0").update(created_at=timezone.now() - timedelta(hours=1, minutes=1))
        self.assertEqual(self.send(account).status, portal_otp.OK)

    def test_default_cap_is_120(self):
        from django.conf import settings

        self.assertEqual(settings.PORTAL_OTP_MAX_SENDS_PER_HOUR, 120)

    @override_settings(PORTAL_OTP_MAX_SENDS_PER_HOUR=2)
    def test_failed_rows_do_not_count_toward_global_cap(self):
        owner = self.new_account(phone="+966500000001")
        for i in range(5):
            PhoneOtp.objects.create(account=owner, phone="+96650000010%d" % i, expires_at=timezone.now(), send_error="x")
        self.assertEqual(self.send(self.new_account()).status, portal_otp.OK)


class ConsoleBackendTests(OtpBase):
    @override_settings(PORTAL_OTP_BACKEND="console", DEBUG=False)
    def test_refused_when_debug_is_off(self):
        account = self.new_account()
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assertEqual(self.send(account).status, portal_otp.UNAVAILABLE)
        self.sender.assert_not_called()
        self.assertFalse(PhoneOtp.objects.exists())

    @override_settings(PORTAL_OTP_BACKEND="console", DEBUG=False)
    def test_refused_through_the_resend_view_too(self):
        account = self.new_account()
        self.sign_in(account)
        with self.assertLogs(LOGGER, level="ERROR"):
            html = self.client.post(RESEND, follow=True).content.decode()
        self.assertIn("temporarily unavailable", html)

    @override_settings(PORTAL_OTP_BACKEND="console", DEBUG=True)
    def test_works_with_debug_logs_code_and_never_calls_the_api(self):
        account = self.new_account()
        with mock.patch("apitokens.services.lightotp.requests.post") as post:
            with self.assertLogs(LOGGER, level="INFO") as cm:
                outcome = self.send(account)
        self.assertEqual(outcome.status, portal_otp.OK)
        self.sender.assert_not_called()
        post.assert_not_called()
        otp = PhoneOtp.objects.get()
        self.assertEqual(otp.message_id, f"console-{otp.pk}")
        code = re.search(r": (\d{6})$", cm.output[0]).group(1)
        self.assertEqual(otp.code_hash, portal_otp.hash_code(otp.pk, code))
        self.assertNotIn(GOOD_PHONE, "\n".join(cm.output))
        # التهدئة بتشتغل محلياً كمان: نفس تجربة المستخدم
        self.assertEqual(self.send(account).status, portal_otp.WAIT)
        self.assertEqual(portal_otp.verify_code(account, code), portal_otp.V_OK)

    @override_settings(PORTAL_OTP_BACKEND="smoke-signals", DEBUG=True)
    def test_unknown_backend_is_unavailable(self):
        with self.assertLogs(LOGGER, level="ERROR"):
            self.assertEqual(self.send(self.new_account()).status, portal_otp.UNAVAILABLE)

    @override_settings(PORTAL_OTP_BACKEND="console", DEBUG=True)
    def test_check_otp_status_skips_console_messages(self):
        self.send(self.new_account())
        with mock.patch("apitokens.services.lightotp.check_status") as check:
            call_command("check_otp_status", stdout=StringIO())
        check.assert_not_called()


# ---------- التحقق ----------


class VerifyViewTests(OtpBase):
    def test_get_page_attributes_masked_phone_and_countdown(self):
        account, _ = self.started()
        resp = self.client.get(VERIFY)
        self.assertEqual(resp.status_code, 200)
        self.assert_headers(resp)
        html = resp.content.decode()
        self.assertIn('<bdi dir="ltr" class="masked-phone">+966 •• ••• 4567</bdi>', html)
        self.assertNotIn(GOOD_PHONE, html)
        self.assertNotIn("501234567", html)
        for attr in ('inputmode="numeric"', 'autocomplete="one-time-code"', 'maxlength="6"', "autofocus",
                     'name="code"', 'pattern="[0-9٠-٩۰-۹]{6}"', "csrfmiddlewaretoken"):
            self.assertIn(attr, html, attr)
        # نفس مظهر الدخول: بدون لغة ولا لوغو ولا next
        self.assertIn('<body class="auth">', html)
        self.assertNotIn('class="lang-switch"', html)
        self.assertNotIn('class="brand-mark"', html)
        self.assertNotIn('name="next"', html)
        # عدّاد: التهدئة بعد أول إرسال = 30ث، الزر معطّل من السيرفر وبيبدأ منها
        m = re.search(r'id="resend-btn" data-seconds="(\d+)"', html)
        self.assertIn(int(m.group(1)), range(29, 31))
        self.assertRegex(html, r'data-ready="Resend code"( disabled|>)')
        self.assertIn("disabled>Resend in", html)
        self.assertIn('data-label="Resend in {n} s"', html)
        self.assertIn("Verify", html)
        self.assertIn(reverse("portal:logout"), html)
        self.assertIn("Use a different number / log out", html)

    def test_resend_button_enabled_when_no_cooldown(self):
        account = self.new_account()
        self.sign_in(account)
        html = self.html()
        self.assertIn('data-seconds="0"', html)
        self.assertNotRegex(html, r'id="resend-btn"[^>]*disabled')
        self.assertIn(">Resend code</button>", html)
        # ما في كود فعّال → تلميح
        self.assertIn("get a new message", html)

    def test_page_is_arabic_rtl_with_session_language(self):
        account, _ = self.started()
        self.set_lang("ar")
        html = self.html()
        self.assertIn('dir="rtl"', html)
        self.assertIn(TRANSLATIONS["portal_otp_title"]["ar"], html)
        self.assertIn(TRANSLATIONS["portal_otp_verify_button"]["ar"], html)
        self.assertIn("إعادة الإرسال بعد", html)

    def test_anonymous_goes_to_login(self):
        resp = self.client.get(VERIFY)
        self.assertRedirects(resp, reverse("portal:login"), fetch_redirect_response=False)
        self.assert_headers(resp)
        self.assertRedirects(self.client.post(VERIFY, {"code": "123456"}), reverse("portal:login"), fetch_redirect_response=False)

    def test_success_verifies_consumes_and_goes_to_pending_without_store(self):
        account, code = self.started()
        resp = self.post_code(code)
        self.assertRedirects(resp, reverse("portal:pending"), fetch_redirect_response=False)
        self.assert_headers(resp)
        account.refresh_from_db()
        self.assertTrue(account.phone_verified)
        self.assertLess(abs((timezone.now() - account.phone_verified_at).total_seconds()), 5)
        otp = PhoneOtp.objects.get()
        self.assertIsNotNone(otp.consumed_at)
        self.assertEqual(otp.attempts, 1)
        self.assertEqual(self.client.get(reverse("portal:pending")).status_code, 200)

    def test_success_with_store_goes_home_then_consent(self):
        account, code = self.started(merchant=self.a)
        self.assertRedirects(self.post_code(code), reverse("portal:home"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("portal:home")), reverse("portal:accept"), fetch_redirect_response=False)

    def test_verified_account_visiting_verify_is_sent_on(self):
        login_pending(self.client)
        self.assertRedirects(self.client.get(VERIFY), reverse("portal:pending"), fetch_redirect_response=False)
        login_portal(self.client, self.a)
        self.assertRedirects(self.client.get(VERIFY), reverse("portal:home"), fetch_redirect_response=False)

    def test_wrong_code_is_generic_counts_and_never_echoes_input(self):
        account, code = self.started()
        wrong = "000000" if code != "000000" else "111111"
        resp = self.post_code(wrong)
        self.assertEqual(resp.status_code, 400)
        html = resp.content.decode()
        self.assertIn("That code is incorrect", html)
        self.assertNotIn(f'value="{wrong}"', html)
        self.assertNotRegex(html, r"(?i)attempts? (left|remaining)|remaining attempts")
        self.assertEqual(PhoneOtp.objects.get().attempts, 1)
        account.refresh_from_db()
        self.assertFalse(account.phone_verified)
        self.assert_headers(resp)

    def test_malformed_input_is_rejected_without_counting(self):
        account, code = self.started()
        for bad in ["", "12", "abcdef", "1234567"]:
            self.assertEqual(self.post_code(bad).status_code, 400)
        self.assertEqual(PhoneOtp.objects.get().attempts, 0)
        self.assertEqual(self.client.post(VERIFY, {}).status_code, 400)

    def test_expired_code(self):
        account, code = self.started()
        PhoneOtp.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        resp = self.post_code(code)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("has expired", resp.content.decode())
        account.refresh_from_db()
        self.assertFalse(account.phone_verified)

    def test_code_valid_until_just_before_expiry(self):
        account, code = self.started()
        PhoneOtp.objects.update(expires_at=timezone.now() + timedelta(seconds=30))
        self.assertEqual(self.post_code(code).status_code, 302)

    def test_five_wrong_attempts_kill_the_code_even_for_the_right_one(self):
        account, code = self.started()
        wrong = "000000" if code != "000000" else "111111"
        for i in range(4):
            self.assertIn("incorrect", self.post_code(wrong).content.decode(), i)
        fifth = self.post_code(wrong)
        self.assertEqual(fifth.status_code, 400)
        self.assertIn("Too many incorrect attempts", fifth.content.decode())
        # الكود الصح بعد الاستنفاد مرفوض، والمحاولات ما بتزيد
        resp = self.post_code(code)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Too many incorrect attempts", resp.content.decode())
        self.assertEqual(PhoneOtp.objects.get().attempts, 5)
        account.refresh_from_db()
        self.assertFalse(account.phone_verified)
        # لازم كود جديد (بعد التهدئة)، وبيشتغل
        self.shift(31)
        self.client.post(RESEND)
        self.assertEqual(len(sent_codes(self.sender)), 2)
        self.assertRedirects(self.post_code(self.last_code()), reverse("portal:pending"), fetch_redirect_response=False)

    def test_new_code_invalidates_the_old_one(self):
        account, old = self.started()
        self.shift(31)
        self.client.post(RESEND)
        new = self.last_code()
        self.assertNotEqual(old, new)
        if old != new:
            self.assertEqual(self.post_code(old).status_code, 400)
        self.assertEqual(self.post_code(new).status_code, 302)

    def test_consumed_code_cannot_be_reused(self):
        account, code = self.started()
        self.assertEqual(self.post_code(code).status_code, 302)
        MerchantAccount.objects.filter(pk=account.pk).update(phone_verified=False, phone_verified_at=None)
        resp = self.post_code(code)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("no active code", resp.content.decode())
        account.refresh_from_db()
        self.assertFalse(account.phone_verified)

    def test_arabic_and_persian_digits_and_spaces_are_accepted(self):
        for table in ("٠١٢٣٤٥٦٧٨٩", "۰۱۲۳۴۵۶۷۸۹"):
            PhoneOtp.objects.all().delete()
            MerchantAccount.objects.all().delete()
            cache.clear()
            account, code = self.started()
            typed = " ".join(table[int(d)] for d in code[:3]) + " " + "".join(table[int(d)] for d in code[3:])
            resp = self.post_code(typed)
            self.assertEqual(resp.status_code, 302, typed)
            account.refresh_from_db()
            self.assertTrue(account.phone_verified)
            self.client = self.client_class()
            self.set_lang("en")

    def test_attempts_are_counted_under_row_locks(self):
        account, code = self.started()
        locked = []
        original = QuerySet.select_for_update

        def spy(qs, *a, **kw):
            locked.append(qs.model.__name__)
            return original(qs, *a, **kw)

        with mock.patch.object(QuerySet, "select_for_update", spy):
            self.post_code("000000" if code != "000000" else "111111")
        self.assertIn("PhoneOtp", locked)
        self.assertIn("MerchantAccount", locked)

    def test_cross_account_isolation(self):
        a, code_a = self.started(phone=GOOD_PHONE)
        b = self.new_account(phone=OTHER_PHONE)
        client_b = self.client_class()
        self.set_lang("en", client_b)
        self.sign_in(b, client_b)
        self.send(b, ip="10.9.9.9")
        code_b = self.last_code()
        # كود A بجلسة B: مرفوض، وما بيلمس A
        resp = self.post_code(code_a, client_b)
        self.assertEqual(resp.status_code, 400)
        b.refresh_from_db(), a.refresh_from_db()
        self.assertFalse(b.phone_verified)
        self.assertFalse(a.phone_verified)
        self.assertEqual(PhoneOtp.objects.get(account=a).attempts, 0)
        self.assertEqual(PhoneOtp.objects.get(account=b).attempts, 1)
        # وكود B ما بيفتح A
        self.assertEqual(self.post_code(code_b).status_code, 400)
        a.refresh_from_db()
        self.assertFalse(a.phone_verified)
        # كل واحد بكوده
        self.assertEqual(self.post_code(code_a).status_code, 302)
        self.assertEqual(self.post_code(code_b, client_b).status_code, 302)

    def test_code_for_old_phone_does_not_verify_new_phone(self):
        account, code = self.started()
        MerchantAccount.objects.filter(pk=account.pk).update(phone=OTHER_PHONE)
        self.assertEqual(self.post_code(code).status_code, 400)
        account.refresh_from_db()
        self.assertFalse(account.phone_verified)

    def test_verify_ip_throttle(self):
        account, code = self.started()
        wrong = "000000" if code != "000000" else "111111"
        for i in range(portal_auth.IP_MAX_VERIFY_POSTS):
            self.assertNotEqual(self.post_code(wrong).status_code, 429)
        resp = self.post_code(code)
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Too many requests", resp.content.decode())
        account.refresh_from_db()
        self.assertFalse(account.phone_verified, "حتى الكود الصح مرفوض تحت الـ throttle")

    def test_get_requests_do_not_count_for_the_verify_throttle(self):
        account, code = self.started()
        for _ in range(portal_auth.IP_MAX_VERIFY_POSTS + 5):
            self.client.get(VERIFY)
        self.assertEqual(self.post_code(code).status_code, 302)

    def test_only_get_and_post(self):
        self.sign_in(self.new_account())
        self.assertEqual(self.client.put(VERIFY).status_code, 405)

    def test_deactivated_account_is_kicked_out_of_verify(self):
        account, _ = self.started()
        MerchantAccount.objects.filter(pk=account.pk).update(is_active=False)
        self.assertRedirects(self.client.get(VERIFY), reverse("portal:login"), fetch_redirect_response=False)


class ResendViewTests(OtpBase):
    def test_requires_post_and_session(self):
        self.assertRedirects(self.client.post(RESEND), reverse("portal:login"), fetch_redirect_response=False)
        self.sign_in(self.new_account())
        self.assertEqual(self.client.get(RESEND).status_code, 405)
        self.sender.assert_not_called()

    def test_sends_then_countdown_follows_the_escalation(self):
        account = self.new_account()
        self.sign_in(account)
        resp = self.client.post(RESEND)
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.assert_headers(resp)
        html = self.html()
        self.assertIn("We sent you a new code", html)
        self.assertEqual(self.sender.call_count, 1)
        self.assertIn(int(re.search(r'data-seconds="(\d+)"', html).group(1)), (29, 30))
        # المرة التانية بعد التهدئة: 60
        self.shift(31)
        self.client.post(RESEND)
        html = self.html()
        self.assertIn(int(re.search(r'data-seconds="(\d+)"', html).group(1)), (59, 60))
        self.assertIn("disabled>Resend in 6", html)

    def test_pressing_during_cooldown_makes_no_api_call_and_shows_wait(self):
        account = self.new_account()
        self.sign_in(account)
        self.client.post(RESEND)
        self.html()  # بنفضّي رسالة الإرسال
        for _ in range(3):
            self.client.post(RESEND)
        self.assertEqual(self.sender.call_count, 1)
        html = self.html()
        self.assertIn("Please wait", html)
        self.assertRegex(html, r"Please wait \d+ seconds")
        self.assertEqual(PhoneOtp.objects.count(), 1)

    def test_flash_message_shows_once(self):
        self.sign_in(self.new_account())
        self.client.post(RESEND)
        self.assertIn("We sent you a new code", self.html())
        self.assertNotIn("We sent you a new code", self.html())

    def test_lightotp_side_wait_keeps_the_button_disabled_across_reloads(self):
        self.sign_in(self.new_account())
        self.sender.side_effect = lightotp.LightOtpError("wait 20 seconds", 400, True)
        self.client.post(RESEND)
        first = self.html()
        self.assertIn("Please wait 60 seconds", first)
        self.assertIn(int(re.search(r'data-seconds="(\d+)"', first).group(1)), range(58, 61))
        second = self.html()  # الرسالة راحت بس العدّاد لسا
        self.assertNotIn("Please wait", second)
        self.assertIn(int(re.search(r'data-seconds="(\d+)"', second).group(1)), range(58, 61))
        self.assertEqual(self.sender.call_count, 1)

    def test_unavailable_keeps_button_enabled_for_retry(self):
        self.sign_in(self.new_account())
        self.sender.side_effect = lightotp.LightOtpError("TemplateNotFound", 404, False)
        with self.assertLogs(LOGGER, level="ERROR"):
            self.client.post(RESEND)
        html = self.html()
        self.assertIn('data-seconds="0"', html)
        self.assertNotRegex(html, r'id="resend-btn"[^>]*disabled')

    def test_ip_throttle_message(self):
        for i in range(portal_otp.IP_MAX_SENDS):
            acc = self.new_account(phone=f"+9665010{i:05d}")
            self.send(acc)
        account = self.new_account(phone="+966502000001")
        self.sign_in(account)
        self.client.post(RESEND, REMOTE_ADDR=IP)
        self.assertIn("Too many requests", self.html())

    def test_default_language_is_session_language(self):
        self.sign_in(self.new_account())
        self.set_lang("ar")
        self.client.post(RESEND)
        self.assertEqual(self.sender.call_args.kwargs["lang"], "ar")
        self.shift(31)
        self.set_lang("en")
        self.client.post(RESEND)
        self.assertEqual(self.sender.call_args.kwargs["lang"], "en")

    def test_fresh_session_defaults_to_arabic(self):
        fresh = Client()
        account = self.new_account()
        _put_session(fresh, account)
        fresh.post(RESEND)
        self.assertEqual(self.sender.call_args.kwargs["lang"], "ar")

    def test_verified_account_is_redirected_away(self):
        login_pending(self.client)
        resp = self.client.post(RESEND)
        self.assertRedirects(resp, reverse("portal:pending"), fetch_redirect_response=False)
        self.sender.assert_not_called()

    def test_resend_state_is_cleared_on_logout_and_login(self):
        self.sign_in(self.new_account())
        self.sender.side_effect = lightotp.LightOtpError("wait", 400, True)
        self.client.post(RESEND)
        self.assertIn(portal_auth.SESSION_OTP_WAIT_UNTIL, self.client.session)
        self.client.post(reverse("portal:logout"))
        self.assertNotIn(portal_auth.SESSION_OTP_WAIT_UNTIL, self.client.session)
        self.assertNotIn(portal_auth.SESSION_OTP_FLASH, self.client.session)

    def test_double_press_race_sends_only_once(self):
        """طلبين متتاليين بنفس اللحظة (ضغطتين): الثاني بيلاقي التهدئة، ما بيكلّم الـ API."""
        account = self.new_account()
        first = self.send(account)
        second = self.send(account)
        self.assertEqual((first.status, second.status), (portal_otp.OK, portal_otp.WAIT))
        self.assertEqual(self.sender.call_count, 1)


# ---------- البوابة للحسابات غير الموثّقة ----------


class GatingTests(OtpBase):
    def test_unverified_account_is_redirected_to_verify_everywhere(self):
        login_unverified(self.client, self.a)
        for name in ("portal:home", "portal:accept", "portal:pending"):
            resp = self.client.get(reverse(name))
            self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
            self.assert_headers(resp)
        resp = self.client.post(reverse("portal:accept"), {"agree": "1"})
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.a.refresh_from_db()
        self.assertEqual(self.a.terms_accepted_version, "", "ما بيقدر يوافق وهو غير موثّق")

    def test_unverified_pending_account_goes_to_verify_not_pending(self):
        acc = self.new_account()
        self.sign_in(acc)
        self.assertRedirects(self.client.get(reverse("portal:pending")), VERIFY, fetch_redirect_response=False)

    def test_verify_resend_and_logout_stay_reachable(self):
        login_unverified(self.client, self.a)
        self.assertEqual(self.client.get(VERIFY).status_code, 200)
        self.assertEqual(self.client.post(RESEND).status_code, 302)
        self.assertRedirects(self.client.post(reverse("portal:logout")), reverse("portal:login"), fetch_redirect_response=False)

    def test_decorator_never_runs_wrapped_view_for_unverified(self):
        acc = make_account(self.a, verified=False)
        self.sign_in(acc)
        called = []
        from django.http import HttpResponse
        from django.test import RequestFactory

        def view(request):
            called.append(1)
            return HttpResponse("x")

        for deco in (portal_auth.portal_required, portal_auth.portal_account_required):
            req = RequestFactory().get("/x/")
            req.session = self.client.session
            resp = deco(view)(req)
            self.assertEqual((resp.status_code, resp["Location"]), (302, VERIFY))
        self.assertEqual(called, [])

    def test_verified_account_is_unaffected(self):
        login_portal(self.client, self.a)
        self.assertRedirects(self.client.get(reverse("portal:home")), reverse("portal:accept"), fetch_redirect_response=False)

    def test_login_of_unverified_account_goes_to_verify_and_sends_nothing(self):
        make_account(self.a, phone=GOOD_PHONE, password=PW, verified=False)
        resp = self.login_post()
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.assert_headers(resp)
        self.sender.assert_not_called()
        self.assertFalse(PhoneOtp.objects.exists())
        self.assertIn(portal_auth.SESSION_ACCOUNT, self.client.session)

    def test_login_with_wrong_password_for_unverified_still_generic(self):
        make_account(self.a, phone=GOOD_PHONE, password=PW, verified=False)
        resp = self.login_post(password="nope-nope-1")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("incorrect", resp.content.decode())

    def test_login_then_enter_the_still_valid_code_without_resend(self):
        acc = make_account(self.a, phone=GOOD_PHONE, password=PW, verified=False)
        self.send(acc)
        code = self.last_code()
        self.login_post()
        html = self.html()
        self.assertNotIn("get a new message", html, "في كود صالح: ما في تلميح")
        self.assertRedirects(self.post_code(code), reverse("portal:home"), fetch_redirect_response=False)

    def test_login_get_with_unverified_session_redirects_to_verify(self):
        login_unverified(self.client, self.a)
        self.assertRedirects(self.client.get(reverse("portal:login")), VERIFY, fetch_redirect_response=False)

    def test_deactivation_and_fingerprint_still_apply(self):
        acc = login_unverified(self.client, self.a)
        MerchantAccount.objects.filter(pk=acc.pk).update(password="x" + acc.password)
        self.assertRedirects(self.client.get(VERIFY), reverse("portal:login"), fetch_redirect_response=False)

    def test_headers_on_all_verification_responses(self):
        login_unverified(self.client, self.a)
        for resp in (self.client.get(VERIFY), self.client.post(VERIFY, {"code": "1"}), self.client.post(RESEND)):
            self.assert_headers(resp)


# ---------- مسارات التسجيل ----------


class PublicRegistrationTests(OtpBase):
    def test_creates_unverified_sends_otp_logs_in_and_redirects_to_verify(self):
        resp = self.signup()
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.assert_headers(resp)
        acc = MerchantAccount.objects.get()
        self.assertFalse(acc.phone_verified)
        self.assertIsNone(acc.phone_verified_at)
        self.assertIsNone(acc.merchant)
        self.assertEqual(self.client.session[portal_auth.SESSION_ACCOUNT], acc.pk)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.sender.call_args.kwargs["phone"], GOOD_PHONE)
        self.assertEqual(self.sender.call_args.kwargs["lang"], "en")
        html = self.html()
        self.assertIn("We sent you a new code", html)
        self.assertRedirects(self.client.get(reverse("portal:pending")), VERIFY, fetch_redirect_response=False)
        self.assertRedirects(self.post_code(self.last_code()), reverse("portal:pending"), fetch_redirect_response=False)

    def test_send_failure_still_creates_account_and_page_offers_resend(self):
        self.sender.side_effect = lightotp.LightOtpError("InsufficientBalance", 400, False)
        with self.assertLogs(LOGGER, level="ERROR"):
            resp = self.signup()
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.assertTrue(MerchantAccount.objects.filter(phone=GOOD_PHONE).exists())
        html = self.html()
        self.assertIn("temporarily unavailable", html)
        self.assertNotRegex(html, r'id="resend-btn"[^>]*disabled')
        # لما الخدمة ترجع، الزر بيشتغل
        self.sender.side_effect = self.sender.default_effect
        self.client.post(RESEND)
        self.assertEqual(len(PhoneOtp.objects.exclude(message_id="")), 1)

    def test_nothing_is_sent_for_invalid_input(self):
        self.signup(phone="+999999999999")
        self.signup(password="short")
        self.sender.assert_not_called()
        self.assertFalse(MerchantAccount.objects.exists())

    def test_invalid_numbers_per_phonenumbers_are_rejected_with_existing_message(self):
        for bad in ("+999999999999", "+12345678", "+966512345", "0501234567", "966501234567"):
            resp = self.signup(phone=bad)
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn("country code", resp.content.decode(), bad)
        self.assertFalse(MerchantAccount.objects.exists())

    def test_local_number_never_guessed_from_default_region(self):
        self.assertIsNone(portal_auth.normalize_phone("0501234567"))
        self.assertIsNone(portal_auth.normalize_phone("501234567"))
        self.assertEqual(portal_auth.normalize_phone("00966 50 123 4567"), GOOD_PHONE)

    def test_arabic_digits_are_normalized_before_sending(self):
        self.signup(phone="٠٠٩٦٦٥٠١٢٣٤٥٦٧")
        self.assertEqual(self.sender.call_args.kwargs["phone"], GOOD_PHONE)

    def test_rule_a_unverified_no_store_is_overwritten_in_place(self):
        attacker = self.client_class()
        self.set_lang("en", attacker)
        self.signup(client=attacker, name="Attacker", password="Attacker-Pass-777")
        acc = MerchantAccount.objects.get()
        PhoneOtp.objects.filter(account=acc).update(consumed_at=None)
        MerchantAccount.objects.filter(pk=acc.pk).update(
            failed_logins=3, locked_until=timezone.now() + timedelta(hours=1),
            created_at=timezone.now() - timedelta(days=3),
        )
        self.assertTrue(self.session_account_valid(attacker))
        self.shift(31)
        victim = self.client
        resp = self.signup(client=victim, name="Victim", password="Victim-Password-91")
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.assertEqual(MerchantAccount.objects.count(), 1)
        mine = MerchantAccount.objects.get()
        self.assertEqual(mine.pk, acc.pk, "نفس الصف")
        self.assertEqual(mine.name, "Victim")
        self.assertTrue(check_password("Victim-Password-91", mine.password))
        self.assertFalse(check_password("Attacker-Pass-777", mine.password))
        self.assertEqual(mine.failed_logins, 0)
        self.assertIsNone(mine.locked_until)
        self.assertLess((timezone.now() - mine.created_at).total_seconds(), 10, "بيعدّ من جديد للـ purge")
        self.assertFalse(mine.phone_verified)
        # جلسة المهاجم ماتت، والضحية شغّالة
        self.assertFalse(self.session_account_valid(attacker))
        self.assertTrue(self.session_account_valid(victim))
        # الكود القديم مبطّل، والجديد وحده يفتح
        old_otp, new_otp = PhoneOtp.objects.order_by("pk")
        self.assertIsNotNone(old_otp.consumed_at)
        self.assertIsNone(new_otp.consumed_at)
        self.assertEqual(self.post_code(self.last_code(), victim).status_code, 302)

    def test_rule_a_overwrite_keeps_the_cooldown_so_it_cannot_be_used_to_spam(self):
        self.signup()
        calls = self.sender.call_count
        other = self.client_class()
        self.set_lang("en", other)
        resp = self.signup(client=other)  # فورا بنفس الرقم: الحساب بينكتب فوقه والتهدئة سارية
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.assertEqual(self.sender.call_count, calls, "ما في رسالة تانية")
        html = self.html(client=other)
        self.assertIn("Please wait", html)
        self.assertRegex(html, r'data-seconds="(29|30)"')

    def test_rule_b_unverified_with_store_is_not_overwritten(self):
        owner = make_account(self.a, phone=GOOD_PHONE, password="Owner-Password-12", verified=False, name="Owner")
        resp = self.signup()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already registered", resp.content.decode())
        owner.refresh_from_db()
        self.assertEqual((owner.merchant, owner.name), (self.a, "Owner"))
        self.assertTrue(check_password("Owner-Password-12", owner.password))
        self.sender.assert_not_called()
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)

    def test_rule_b_verified_accounts_are_never_touched(self):
        for kw in ({"merchant": self.a}, {}):
            MerchantAccount.objects.all().delete()
            owner = self.new_account(verified=True, password="Owner-Password-12", name="Owner", **kw)
            resp = self.signup()
            self.assertEqual(resp.status_code, 400, kw)
            self.assertIn("already registered", resp.content.decode())
            owner.refresh_from_db()
            self.assertEqual(owner.name, "Owner")
            self.assertTrue(check_password("Owner-Password-12", owner.password))
        self.sender.assert_not_called()

    def test_inactive_unverified_account_is_not_resurrected(self):
        owner = self.new_account(is_active=False)
        self.assertEqual(self.signup().status_code, 400)
        owner.refresh_from_db()
        self.assertFalse(owner.is_active)

    def test_registering_cannot_hijack_a_store_binding_through_public_path(self):
        make_account(self.a, phone=GOOD_PHONE, password="Owner-Password-12", verified=False)
        self.signup(name="Mallory", password="Mallory-Password-5")
        self.assertEqual(MerchantAccount.objects.get().merchant, self.a)
        self.assertFalse(MerchantAccount.objects.filter(name="Mallory").exists())

    @override_settings(PORTAL_MAX_PENDING_ACCOUNTS=1)
    def test_pending_cap_counts_unverified_accounts(self):
        self.new_account(phone="+966500000001", verified=False)
        resp = self.signup(phone="+966500000002")
        self.assertEqual(resp.status_code, 503)
        self.sender.assert_not_called()

    def test_honeypot_sends_nothing(self):
        self.signup(website="x")
        self.sender.assert_not_called()


class InviteRegistrationTests(OtpBase):
    def test_creates_unverified_bound_account_sends_otp_and_redirects_to_verify(self):
        key = self.invite(self.a)
        resp = self.register(key)
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.assert_headers(resp)
        acc = MerchantAccount.objects.get()
        self.assertEqual(acc.merchant, self.a)
        self.assertFalse(acc.phone_verified)
        self.assertEqual(self.sender.call_count, 1)
        self.a.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, "", "الدعوة انستهلكت")
        self.assertRedirects(self.client.get(reverse("portal:home")), VERIFY, fetch_redirect_response=False)
        self.assertRedirects(self.post_code(self.last_code()), reverse("portal:home"), fetch_redirect_response=False)

    def test_send_failure_still_creates_account(self):
        self.sender.side_effect = lightotp.LightOtpError("TemplateIsRejected", 400, False)
        with self.assertLogs(LOGGER, level="ERROR"):
            resp = self.register(self.invite(self.a))
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.assertTrue(MerchantAccount.objects.filter(merchant=self.a).exists())
        self.assertIn("temporarily unavailable", self.html())

    def test_invalid_numbers_rejected_and_invite_kept(self):
        key = self.invite(self.a)
        for bad in ("+999999999999", "+12345678", "0501234567"):
            resp = self.register(key, phone=bad)
            self.assertEqual(resp.status_code, 400, bad)
            self.assertIn("country code", resp.content.decode())
        self.sender.assert_not_called()
        self.a.refresh_from_db()
        self.assertTrue(self.a.portal_key_hash)

    def test_rule_c_unverified_no_store_squatter_is_deleted_and_registration_proceeds(self):
        squatter = self.new_account(phone=GOOD_PHONE, name="Squatter")
        PhoneOtp.objects.create(account=squatter, phone=GOOD_PHONE, expires_at=timezone.now(), message_id="x")
        resp = self.register(self.invite(self.a))
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        self.assertFalse(MerchantAccount.objects.filter(pk=squatter.pk).exists())
        self.assertFalse(PhoneOtp.objects.filter(account_id=squatter.pk).exists(), "أكواده انحذفت معه")
        acc = MerchantAccount.objects.get()
        self.assertEqual((acc.merchant, acc.phone, acc.name), (self.a, GOOD_PHONE, "Ahmad"))

    def test_rule_c_unverified_account_of_a_different_store_blocks(self):
        other = make_account(self.b, phone=GOOD_PHONE, password="Owner-Password-12", verified=False, name="B-owner")
        key = self.invite(self.a)
        resp = self.register(key)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already used", resp.content.decode())
        other.refresh_from_db()
        self.assertEqual((other.merchant, other.name), (self.b, "B-owner"))
        self.assertTrue(MerchantAccount.objects.filter(pk=other.pk).exists())
        self.assertFalse(MerchantAccount.objects.filter(merchant=self.a).exists())
        self.a.refresh_from_db()
        self.assertTrue(self.a.portal_key_hash, "الدعوة ما بتنستهلك مع خطأ")
        self.sender.assert_not_called()

    def test_rule_c_verified_account_of_a_different_store_blocks(self):
        other = make_account(self.b, phone=GOOD_PHONE, password="Owner-Password-12", verified=True)
        resp = self.register(self.invite(self.a))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already used", resp.content.decode())
        self.assertTrue(MerchantAccount.objects.filter(pk=other.pk).exists())

    def test_rule_c_verified_pending_account_is_not_deleted(self):
        pending = self.new_account(phone=GOOD_PHONE, verified=True)
        resp = self.register(self.invite(self.a))
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(MerchantAccount.objects.filter(pk=pending.pk).exists())

    def test_squatter_deletion_is_rolled_back_when_registration_fails(self):
        squatter = self.new_account(phone=GOOD_PHONE)
        with mock.patch.object(MerchantAccount, "save", side_effect=IntegrityError):
            resp = self.register(self.invite(self.a))
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(MerchantAccount.objects.filter(pk=squatter.pk).exists())

    # --- reset path ---
    def test_reset_with_changed_phone_requires_new_otp_even_if_verified_before(self):
        old = make_account(self.a, phone=OTHER_PHONE, password="Old-Password-77", verified=True)
        old_client = self.client_class()
        self.sign_in(old, old_client)
        resp = self.register(self.invite(self.a), phone=GOOD_PHONE)
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        acc = MerchantAccount.objects.get()
        self.assertEqual((acc.pk, acc.phone), (old.pk, GOOD_PHONE))
        self.assertFalse(acc.phone_verified)
        self.assertIsNone(acc.phone_verified_at)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.sender.call_args.kwargs["phone"], GOOD_PHONE)
        self.assertRedirects(self.client.get(reverse("portal:home")), VERIFY, fetch_redirect_response=False)
        self.assertFalse(self.session_account_valid(old_client))

    def test_reset_with_same_verified_phone_stays_verified_and_sends_nothing(self):
        when = timezone.now() - timedelta(days=5)
        old = make_account(self.a, phone=GOOD_PHONE, password="Old-Password-77", verified=True)
        MerchantAccount.objects.filter(pk=old.pk).update(phone_verified_at=when)
        resp = self.register(self.invite(self.a), phone=GOOD_PHONE)
        self.assertRedirects(resp, VERIFY, fetch_redirect_response=False)
        acc = MerchantAccount.objects.get()
        self.assertTrue(acc.phone_verified)
        self.assertEqual(acc.phone_verified_at, when)
        self.assertTrue(check_password(PW, acc.password))
        self.sender.assert_not_called()
        # صفحة التحقق بتكمّل لحالها لصفحة المتجر
        self.assertRedirects(self.client.get(VERIFY), reverse("portal:home"), fetch_redirect_response=False)

    def test_reset_with_same_unverified_phone_gets_a_new_code_and_old_ones_die(self):
        old = make_account(self.a, phone=GOOD_PHONE, password="Old-Password-77", verified=False)
        self.send(old)
        old_code = self.last_code()
        self.shift(31)
        self.register(self.invite(self.a), phone=GOOD_PHONE)
        self.assertEqual(self.sender.call_count, 2)
        self.assertIsNotNone(PhoneOtp.objects.order_by("pk").first().consumed_at)
        self.assertEqual(self.post_code(old_code).status_code, 400)
        self.assertEqual(self.post_code(self.last_code()).status_code, 302)

    def test_reset_cannot_take_another_stores_phone(self):
        make_account(self.a, phone=OTHER_PHONE)
        make_account(self.b, phone=GOOD_PHONE, verified=False)
        self.assertEqual(self.register(self.invite(self.a), phone=GOOD_PHONE).status_code, 400)
        self.assertEqual(MerchantAccount.objects.get(merchant=self.a).phone, OTHER_PHONE)


# ---------- CSRF بشكل المتصفح ----------


@FAST_HASHERS
class OtpBrowserCsrfTests(TestCase):
    ORIGIN = "http://testserver"

    def setUp(self):
        cache.clear()
        self.sender = patch_otp_send(self)
        self.client = Client(enforce_csrf_checks=True)
        self.acc = make_pending_account(verified=False, phone=GOOD_PHONE, password=PW)
        _put_session(self.client, self.acc)
        portal_otp.send_code(self.acc, "ar", IP)
        self.code = sent_codes(self.sender)[-1]

    def post(self, url, data, token=None, origin=ORIGIN):
        data = dict(data)
        if token:
            data["csrfmiddlewaretoken"] = token
        extra = {"HTTP_ORIGIN": origin} if origin else {}
        return self.client.post(url, data, **extra)

    def test_verify_requires_csrf_token_and_same_origin(self):
        token = csrf_token(self.client.get(VERIFY).content.decode())
        data = {"code": self.code}
        self.assertEqual(self.post(VERIFY, data).status_code, 403)
        self.assertEqual(self.post(VERIFY, data, "bad" * 20).status_code, 403)
        self.assertEqual(self.post(VERIFY, data, token, origin="null").status_code, 403)
        self.assertEqual(self.post(VERIFY, data, token, origin="http://evil.example").status_code, 403)
        self.acc.refresh_from_db()
        self.assertFalse(self.acc.phone_verified)
        self.assertEqual(PhoneOtp.objects.get().attempts, 0, "المرفوض بـ CSRF ما بينعدّ")
        resp = self.post(VERIFY, data, token)
        self.assertEqual(resp.status_code, 302)
        self.acc.refresh_from_db()
        self.assertTrue(self.acc.phone_verified)

    def test_resend_requires_csrf_token_and_same_origin(self):
        token = csrf_token(self.client.get(VERIFY).content.decode())
        PhoneOtp.objects.update(created_at=F("created_at") - timedelta(seconds=40))
        calls = self.sender.call_count
        self.assertEqual(self.post(RESEND, {}).status_code, 403)
        self.assertEqual(self.post(RESEND, {}, token, origin="null").status_code, 403)
        self.assertEqual(self.post(RESEND, {}, token, origin="http://evil.example").status_code, 403)
        self.assertEqual(self.sender.call_count, calls, "ما بينرسل بدون CSRF صحيح")
        self.assertEqual(self.post(RESEND, {}, token).status_code, 302)
        self.assertEqual(self.sender.call_count, calls + 1)

    def test_verify_page_forms_all_carry_csrf_and_referrer_policy_is_same_origin(self):
        resp = self.client.get(VERIFY)
        self.assertEqual(resp["Referrer-Policy"], "same-origin")
        html = resp.content.decode()
        self.assertEqual(html.count("csrfmiddlewaretoken"), 3)  # تحقق + إعادة + خروج
        self.assertEqual(self.post(reverse("portal:logout"), {}).status_code, 403)

    def test_csrf_rejected_posts_do_not_count_toward_verify_throttle(self):
        for _ in range(portal_auth.IP_MAX_VERIFY_POSTS + 3):
            self.post(VERIFY, {"code": "000000"})
        token = csrf_token(self.client.get(VERIFY).content.decode())
        self.assertEqual(self.post(VERIFY, {"code": self.code}, token).status_code, 302)


# ---------- الستاف ----------


class StaffOtpTests(OtpBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)
        self.verified = self.new_account(phone="+966500000011", name="Vera", verified=True)
        self.unverified = self.new_account(phone="+966500000012", name="Una", verified=False)

    def dashboard(self):
        return self.client.get(reverse("apitokens:dashboard")).content.decode()

    def link(self, pk, merchant=None):
        return self.client.post(
            reverse("apitokens:portal-account-link", args=[pk]), {"merchant": (merchant or self.a).pk}
        )

    def test_shows_verified_date_state_and_last_otp_status(self):
        MerchantAccount.objects.filter(pk=self.verified.pk).update(phone_verified_at=timezone.make_aware(
            timezone.datetime(2026, 11, 5, 10, 0)))
        PhoneOtp.objects.create(account=self.unverified, phone=self.unverified.phone, expires_at=timezone.now(),
                                message_id="m1", message_status="Delivered", code_hash="a" * 64)
        PhoneOtp.objects.create(account=self.verified, phone=self.verified.phone, expires_at=timezone.now(),
                                send_error="HTTP 404: TemplateNotFound", consumed_at=timezone.now())
        html = self.dashboard()
        self.assertIn("Verified on 2026-11-05", html)
        self.assertIn("Not verified", html)
        self.assertIn("Delivered", html)
        self.assertIn("Send failed", html)
        self.assertNotIn("code_hash", html)
        self.assertNotIn("a" * 64, html)

    def test_account_without_any_code_says_so(self):
        self.assertIn("No code sent", self.dashboard())

    def test_failed_status_error_is_visible_to_staff_only_for_unverified(self):
        PhoneOtp.objects.create(account=self.unverified, phone=self.unverified.phone, expires_at=timezone.now(),
                                message_id="m1", message_status="Failed", send_error="131026 - Message undeliverable")
        self.assertIn("131026 - Message undeliverable", self.dashboard())

    def test_link_controls_only_for_verified_accounts(self):
        html = self.dashboard()
        verified_link = reverse("apitokens:portal-account-link", args=[self.verified.pk])
        unverified_link = reverse("apitokens:portal-account-link", args=[self.unverified.pk])
        self.assertIn(verified_link, html)
        self.assertNotIn(unverified_link, html)
        # الحذف للاثنين
        for acc in (self.verified, self.unverified):
            self.assertIn(reverse("apitokens:portal-account-delete", args=[acc.pk]), html)
        self.assertIn("cannot be linked yet", html)

    def test_only_unverified_pending_accounts_means_no_store_select(self):
        self.verified.delete()
        html = self.dashboard()
        self.assertNotIn('name="merchant"', html)
        self.assertNotIn("No stores without an account", html)

    def test_link_refuses_unverified_server_side(self):
        resp = self.link(self.unverified.pk)
        self.assertRedirects(resp, reverse("apitokens:dashboard"), fetch_redirect_response=False)
        self.unverified.refresh_from_db()
        self.assertIsNone(self.unverified.merchant)
        self.assertIn("has not verified its number", self.dashboard())

    def test_link_verified_still_works(self):
        self.link(self.verified.pk)
        self.verified.refresh_from_db()
        self.assertEqual(self.verified.merchant, self.a)

    def test_delete_unverified_works(self):
        resp = self.client.post(reverse("apitokens:portal-account-delete", args=[self.unverified.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(MerchantAccount.objects.filter(pk=self.unverified.pk).exists())

    def test_arabic_labels(self):
        self.set_lang("ar")
        html = self.dashboard()
        self.assertIn(TRANSLATIONS["portal_admin_pending_needs_verify"]["ar"], html)
        self.assertIn(TRANSLATIONS["portal_admin_pending_verified_on"]["ar"], html)


# ---------- أوامر الصيانة ----------


class PurgeCommandTests(OtpBase):
    def run_cmd(self, *args):
        out = StringIO()
        call_command("purge_unverified_accounts", *args, stdout=out)
        return out.getvalue()

    def age(self, account, hours):
        MerchantAccount.objects.filter(pk=account.pk).update(created_at=timezone.now() - timedelta(hours=hours))

    def test_deletes_only_old_unverified_without_store(self):
        old = self.new_account(phone="+966500000021")
        young = self.new_account(phone="+966500000022")
        old_verified = self.new_account(phone="+966500000023", verified=True)
        old_store = make_account(self.a, phone="+966500000024", verified=False)
        for acc in (old, old_verified, old_store):
            self.age(acc, 30)
        self.age(young, 23)
        PhoneOtp.objects.create(account=old, phone=old.phone, expires_at=timezone.now())
        out = self.run_cmd()
        self.assertIn("deleted 1 unverified", out)
        self.assertFalse(MerchantAccount.objects.filter(pk=old.pk).exists())
        self.assertFalse(PhoneOtp.objects.exists(), "الأكواد cascade")
        for kept in (young, old_verified, old_store):
            self.assertTrue(MerchantAccount.objects.filter(pk=kept.pk).exists())

    def test_hours_option(self):
        acc = self.new_account()
        self.age(acc, 3)
        self.assertIn("deleted 0", self.run_cmd())
        self.assertIn("deleted 1", self.run_cmd("--hours", "2"))
        self.assertFalse(MerchantAccount.objects.exists())

    def test_hours_zero_and_negative(self):
        self.new_account()
        self.assertIn("deleted 1", self.run_cmd("--hours=0"))
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.run_cmd("--hours", "-1")

    def test_nothing_to_delete(self):
        self.assertIn("deleted 0", self.run_cmd())


class CheckOtpStatusCommandTests(OtpBase):
    def setUp(self):
        super().setUp()
        self.acc = self.new_account()
        self.n = 0

    def otp(self, status="", message_id=None, hours_old=0, **kw):
        self.n += 1
        otp = PhoneOtp.objects.create(
            account=self.acc, phone=GOOD_PHONE, expires_at=timezone.now(),
            message_id=message_id if message_id is not None else f"mid-{self.n}", message_status=status, **kw,
        )
        PhoneOtp.objects.filter(pk=otp.pk).update(created_at=timezone.now() - timedelta(hours=hours_old))
        return otp

    def run_cmd(self, side_effect):
        out, err = StringIO(), StringIO()
        with mock.patch("apitokens.services.lightotp.check_status", side_effect=side_effect) as check:
            call_command("check_otp_status", stdout=out, stderr=err)
        return check, out.getvalue(), err.getvalue()

    def test_updates_status_from_lightotp(self):
        a, b = self.otp(""), self.otp("Pending")
        statuses = {a.message_id: "Sent", b.message_id: "Delivered"}
        check, out, err = self.run_cmd(lambda message_id: {"id": message_id, "messageStatus": statuses[message_id]})
        a.refresh_from_db(), b.refresh_from_db()
        self.assertEqual((a.message_status, b.message_status), ("Sent", "Delivered"))
        self.assertIn("checked=2 updated=2 errors=0", out)
        self.assertEqual(check.call_count, 2)

    def test_failure_fields_saved_to_send_error(self):
        otp = self.otp("Sent")
        data = {"messageStatus": "Failed", "failureCode": "131026", "failureReason": "Message undeliverable"}
        self.run_cmd(lambda message_id: data)
        otp.refresh_from_db()
        self.assertEqual(otp.message_status, "Failed")
        self.assertEqual(otp.send_error, "131026 - Message undeliverable")

    def test_failure_error_truncated_to_200(self):
        otp = self.otp("Pending")
        self.run_cmd(lambda message_id: {"messageStatus": "Failed", "failureCode": "x", "failureReason": "y" * 500})
        otp.refresh_from_db()
        self.assertEqual(len(otp.send_error), 200)

    def test_only_open_recent_rows_with_message_id_are_polled(self):
        wanted = [self.otp(""), self.otp("Pending"), self.otp("Sent")]
        self.otp("Delivered")
        self.otp("Read")
        self.otp("Failed")
        self.otp("Deleted")
        self.otp("Pending", hours_old=25)
        self.otp("Pending", message_id="")
        self.otp("Sent", message_id="console-9")
        check, out, err = self.run_cmd(lambda message_id: {"messageStatus": "Delivered"})
        polled = {call.kwargs["message_id"] for call in check.call_args_list}
        self.assertEqual(polled, {o.message_id for o in wanted})
        self.assertIn("checked=3", out)

    def test_per_row_errors_do_not_stop_the_rest(self):
        a, b, c = self.otp("Pending"), self.otp("Pending"), self.otp("Pending")
        def effect(message_id):
            if message_id == a.message_id:
                raise lightotp.LightOtpError("NetworkError", 0, True)
            if message_id == b.message_id:
                raise ValueError("bad json")
            return {"messageStatus": "Read"}
        check, out, err = self.run_cmd(effect)
        for o in (a, b, c):
            o.refresh_from_db()
        self.assertEqual((a.message_status, b.message_status, c.message_status), ("Pending", "Pending", "Read"))
        self.assertIn("checked=1 updated=1 errors=2", out)
        self.assertIn(f"otp #{a.pk}", err)
        self.assertNotIn(GOOD_PHONE, out + err)

    def test_unchanged_status_is_not_counted_as_update(self):
        self.otp("Pending")
        _, out, _ = self.run_cmd(lambda message_id: {"messageStatus": "Pending"})
        self.assertIn("checked=1 updated=0 errors=0", out)

    def test_not_configured_stops_cleanly(self):
        self.otp("Pending"), self.otp("Pending")
        check, out, err = self.run_cmd(lightotp.LightOtpNotConfigured("LIGHTOTP_API_KEY"))
        self.assertEqual(check.call_count, 1)
        self.assertIn("not configured", err)
        self.assertIn("errors=1", out)

    def test_no_rows_is_fine(self):
        check, out, _ = self.run_cmd(lambda message_id: {})
        check.assert_not_called()
        self.assertIn("checked=0 updated=0 errors=0", out)


# ---------- نصوص ونماذج ----------


class TextsAndModelTests(OtpBase):
    def test_every_otp_key_exists_in_both_languages(self):
        keys = [k for k in TRANSLATIONS if k.startswith("portal_otp_")]
        self.assertGreaterEqual(len(keys), 15)
        for key in keys:
            self.assertTrue(TRANSLATIONS[key]["ar"].strip() and TRANSLATIONS[key]["en"].strip(), key)
        for key in ("portal_otp_wait", "portal_otp_resend_in"):
            for lang in ("ar", "en"):
                self.assertIn("{n}", t(key, lang))

    def test_admin_keys_for_otp_exist(self):
        for key in ("portal_admin_pending_verified_on", "portal_admin_pending_otp_label", "portal_admin_pending_otp_none",
                    "portal_admin_pending_otp_failed", "portal_admin_pending_needs_verify",
                    "portal_admin_pending_err_not_verified"):
            self.assertTrue(TRANSLATIONS[key]["ar"] and TRANSLATIONS[key]["en"], key)

    def test_otps_cascade_with_the_account_and_default_fields(self):
        acc = self.new_account()
        self.send(acc)
        otp = PhoneOtp.objects.get()
        self.assertEqual(acc.otps.get(), otp)
        self.assertIsNone(MerchantAccount.objects.get(pk=acc.pk).phone_verified_at)
        acc.delete()
        self.assertFalse(PhoneOtp.objects.exists())

    def test_otp_page_never_lists_password_or_hash(self):
        acc, code = self.started()
        html = self.html()
        self.assertNotIn(acc.password, html)
        self.assertNotIn(PhoneOtp.objects.get().code_hash, html)
        self.assertNotIn(code, html)
