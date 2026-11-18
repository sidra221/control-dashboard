"""اختبارات "نسيت كلمة المرور" لبوابة التاجر: طلب الكود بدون كشف الحسابات، الإرسال للحساب الموثّق الفعّال بس،
عدّ المحاولات والاستنفاد والانتهاء، نجاح الإعادة (كلمة المرور + القفل + الدخول + إبطال الجلسات القديمة)،
فصل أنواع الأكواد بالاتجاهين، CSRF، والـ throttle. الإرسال mock: ما في شبكة."""

import re
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.hashers import check_password
from django.core.cache import cache
from django.db.models import F
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from . import portal_auth, portal_otp
from .i18n import TRANSLATIONS, t
from .models import ApiToken, MerchantAccount, PhoneOtp
from .portal_testing import _put_session, make_pending_account, sent_codes
from .test_portal_auth import GOOD_PHONE, PW, PortalBase, strip_csrf

FORGOT = reverse("portal:password_forgot")
RESET = reverse("portal:password_reset")
HOME = reverse("portal:home")
NEW_PW = "Quartz-Lantern-7395"
OTHER_PHONE = "+966509999999"


def manual_otp(account, purpose, code="123456", **kw):
    """كود مفتوح بنوع معيّن بدون إرسال (لاختبارات فصل الأنواع)."""
    otp = PhoneOtp.objects.create(
        account=account, phone=account.phone, purpose=purpose, expires_at=timezone.now() + timedelta(minutes=10),
        message_id="manual", **kw,
    )
    otp.code_hash = portal_otp.hash_code(otp.pk, code)
    otp.save(update_fields=["code_hash"])
    return otp


@override_settings(PORTAL_RESET_MIN_SECONDS=0)
class ResetBase(PortalBase):
    def forgot(self, phone=GOOD_PHONE, client=None, **extra):
        return (client or self.client).post(FORGOT, {"phone": phone}, **extra)

    def reset(self, code, password=NEW_PW, confirm=None, client=None):
        data = {"code": code, "password": password, "password_confirm": password if confirm is None else confirm}
        return (client or self.client).post(RESET, data)

    def wrong_code(self, code):
        return "000000" if code != "000000" else "111111"


class ForgotRequestTests(ResetBase):
    def test_login_page_links_to_forgot(self):
        html = self.client.get(reverse("portal:login")).content.decode()
        self.assertIn(f'href="{FORGOT}"', html)
        self.assertIn(TRANSLATIONS["portal_reset_link"]["en"], html)
        self.assertNotIn("registration link", html)

    def test_get_forgot_page(self):
        resp = self.client.get(FORGOT)
        self.assertEqual(resp.status_code, 200)
        self.assert_headers(resp)
        self.assertIn('name="phone"', resp.content.decode())

    def test_reset_page_without_request_redirects_to_forgot(self):
        resp = self.client.get(RESET)
        self.assertRedirects(resp, FORGOT, fetch_redirect_response=False)
        resp = self.reset("123456")
        self.assertRedirects(resp, FORGOT, fetch_redirect_response=False)

    def test_no_enumeration_identical_responses(self):
        """رقم غير موجود، حساب غير موثّق، وحساب موثّق: نفس التحويل ونفس صفحة الإدخال حرفياً."""
        pages = []

        def run():
            client = Client()
            self.set_lang("en", client)
            resp = self.forgot(client=client)
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp["Location"], RESET)
            page = client.get(RESET)
            self.assertEqual(page.status_code, 200)
            pages.append(strip_csrf(page.content.decode()))

        run()  # ما في حساب
        account = self.account_a(verified=False)
        run()  # غير موثّق
        account.delete()
        cache.clear()
        self.account_a()
        run()  # موثّق
        self.assertEqual(pages[0], pages[1])
        self.assertEqual(pages[0], pages[2])
        self.assertEqual(self.sender.call_count, 1)  # الإرسال للموثّق بس

    def test_no_enumeration_even_when_send_fails_or_cools_down(self):
        account = self.account_a()
        with mock.patch.object(portal_otp, "_deliver", side_effect=RuntimeError("down")):
            resp = self.forgot()
        self.assertEqual(resp["Location"], RESET)
        # التهدئة (كود توثيق قريب على نفس الرقم) ما بتظهر للمستخدم
        portal_otp.send_code(account, "en", "10.0.0.1")
        resp = self.forgot()
        self.assertEqual(resp["Location"], RESET)

    def test_code_sent_only_for_active_verified_accounts(self):
        account = self.account_a()
        self.forgot()
        self.assertEqual(self.sender.call_count, 1)
        otp = PhoneOtp.objects.get()
        self.assertEqual(otp.purpose, PhoneOtp.PURPOSE_RESET)
        self.assertEqual(otp.account, account)
        self.assertRegex(sent_codes(self.sender)[0], r"^[0-9]{6}$")
        self.assertEqual(self.client.session[portal_auth.SESSION_RESET_ACCOUNT], account.pk)

    def test_no_code_for_unverified_inactive_or_disabled_store(self):
        cases = [
            lambda: self.account_a(verified=False),
            lambda: self.account_a(is_active=False),
        ]
        for make in cases:
            MerchantAccount.objects.all().delete()
            make()
            client = Client()
            self.forgot(client=client)
            self.assertNotIn(portal_auth.SESSION_RESET_ACCOUNT, client.session)
        MerchantAccount.objects.all().delete()
        self.account_a()
        ApiToken.objects.filter(pk=self.a.pk).update(is_active=False)
        self.forgot()
        self.assertEqual(self.sender.call_count, 0)
        self.assertFalse(PhoneOtp.objects.exists())

    def test_pending_verified_account_gets_code(self):
        make_pending_account(phone=GOOD_PHONE)
        self.forgot()
        self.assertEqual(self.sender.call_count, 1)

    def test_phone_normalized_like_login(self):
        self.account_a()
        self.forgot(phone="00966 50-123 ٤٥٦٧")
        self.assertEqual(self.sender.call_count, 1)

    def test_invalid_phone_is_a_field_error(self):
        resp = self.forgot(phone="hello")
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, TRANSLATIONS["portal_reset_err_phone"]["en"], status_code=400)
        self.assertNotIn(portal_auth.SESSION_RESET_STARTED, self.client.session)

    def test_forgot_get_prefills_last_phone(self):
        self.forgot()
        self.assertIn(GOOD_PHONE, self.client.get(FORGOT).content.decode())

    def test_logged_in_get_forgot_lands(self):
        account = self.account_a()
        _put_session(self.client, account)
        resp = self.client.get(FORGOT)
        self.assertEqual(resp.status_code, 302)

    def test_new_forgot_for_unknown_phone_clears_previous_account(self):
        self.account_a()
        self.forgot()
        self.assertIn(portal_auth.SESSION_RESET_ACCOUNT, self.client.session)
        self.forgot(phone=OTHER_PHONE)
        self.assertNotIn(portal_auth.SESSION_RESET_ACCOUNT, self.client.session)

    def test_response_time_is_padded(self):
        with override_settings(PORTAL_RESET_MIN_SECONDS=1.0), mock.patch("apitokens.portal_reset.time.sleep") as sleep:
            self.forgot(phone=OTHER_PHONE)
        sleep.assert_called_once()
        self.assertGreater(sleep.call_args.args[0], 0.5)


class ResetCodeTests(ResetBase):
    def start(self, **kw):
        account = self.account_a(**kw)
        self.forgot()
        return account, sent_codes(self.sender)[-1]

    def test_success_sets_password_clears_lock_logs_in_and_kills_old_sessions(self):
        account, code = self.start()
        MerchantAccount.objects.filter(pk=account.pk).update(
            failed_logins=3, locked_until=timezone.now() + timedelta(minutes=10)
        )
        old = Client()
        _put_session(old, MerchantAccount.objects.get(pk=account.pk))
        self.assertEqual(old.get(HOME).status_code, 302)
        self.assertNotEqual(old.get(HOME)["Location"], reverse("portal:login"))
        extra = manual_otp(account, PhoneOtp.PURPOSE_RESET, code="654321")  # كود إعادة أقدم لسا مفتوح
        PhoneOtp.objects.filter(pk=extra.pk).update(created_at=F("created_at") - timedelta(minutes=1))

        resp = self.reset(code)
        self.assertRedirects(resp, HOME, fetch_redirect_response=False)
        account.refresh_from_db()
        self.assertTrue(check_password(NEW_PW, account.password))
        self.assertFalse(check_password(PW, account.password))
        self.assertEqual(account.failed_logins, 0)
        self.assertIsNone(account.locked_until)
        self.assertFalse(PhoneOtp.objects.filter(purpose=PhoneOtp.PURPOSE_RESET, consumed_at__isnull=True).exists())
        extra.refresh_from_db()
        self.assertIsNotNone(extra.consumed_at)
        session = self.client.session
        self.assertEqual(session[portal_auth.SESSION_ACCOUNT], account.pk)
        self.assertEqual(session[portal_auth.SESSION_FINGERPRINT], portal_auth.session_fingerprint(account))
        for key in portal_auth._RESET_STATE_KEYS:
            self.assertNotIn(key, session)
        # الجلسة القديمة ماتت
        self.assertRedirects(old.get(HOME), reverse("portal:login"), fetch_redirect_response=False)
        # الدخول بالكلمة الجديدة بيشتغل والقديمة لا
        self.assertEqual(self.login_post(password=PW, client=Client()).status_code, 400)
        self.assertEqual(self.login_post(password=NEW_PW, client=Client()).status_code, 302)

    def test_code_cannot_be_reused(self):
        _, code = self.start()
        self.assertEqual(self.reset(code).status_code, 302)
        session = self.client.session
        session[portal_auth.SESSION_RESET_STARTED] = time.time()
        session[portal_auth.SESSION_RESET_ACCOUNT] = MerchantAccount.objects.get().pk
        session.save()
        resp = self.reset(code, password="Another-Pass-9921")
        self.assertEqual(resp.status_code, 400)

    def test_wrong_codes_counted_and_locked_after_five(self):
        account, code = self.start()
        wrong = self.wrong_code(code)
        for i in range(4):
            resp = self.reset(wrong)
            self.assertContains(resp, TRANSLATIONS["portal_reset_err_wrong"]["en"], status_code=400)
        resp = self.reset(wrong)
        self.assertContains(resp, TRANSLATIONS["portal_reset_err_exhausted"]["en"], status_code=400)
        self.assertEqual(PhoneOtp.objects.get(purpose=PhoneOtp.PURPOSE_RESET).attempts, 5)
        resp = self.reset(code)  # حتى الكود الصح ما عاد ينفع
        self.assertContains(resp, TRANSLATIONS["portal_reset_err_exhausted"]["en"], status_code=400)
        account.refresh_from_db()
        self.assertTrue(check_password(PW, account.password))
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)

    def test_malformed_code_not_counted(self):
        self.start()
        resp = self.reset("12ab")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PhoneOtp.objects.get().attempts, 0)

    def test_bad_password_does_not_burn_attempt(self):
        _, code = self.start()
        resp = self.reset(self.wrong_code(code), password="short")
        self.assertContains(resp, TRANSLATIONS["portal_register_err_pw_short"]["en"], status_code=400)
        resp = self.reset(self.wrong_code(code), password="password123")
        self.assertContains(resp, TRANSLATIONS["portal_register_err_pw_common"]["en"], status_code=400)
        resp = self.reset(code, confirm="Different-Pass-1234")
        self.assertContains(resp, TRANSLATIONS["portal_register_err_mismatch"]["en"], status_code=400)
        self.assertEqual(PhoneOtp.objects.get().attempts, 0)
        self.assertIsNone(PhoneOtp.objects.get().consumed_at)

    def test_expired_code(self):
        account, code = self.start()
        PhoneOtp.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        resp = self.reset(code)
        self.assertContains(resp, TRANSLATIONS["portal_reset_err_expired"]["en"], status_code=400)
        account.refresh_from_db()
        self.assertTrue(check_password(PW, account.password))

    def test_ttl_and_attempts_match_verification(self):
        self.start()
        otp = PhoneOtp.objects.get()
        self.assertAlmostEqual(
            (otp.expires_at - otp.created_at).total_seconds(), portal_otp.OTP_TTL.total_seconds(), delta=5
        )

    def test_account_disabled_after_code_sent(self):
        account, code = self.start()
        MerchantAccount.objects.filter(pk=account.pk).update(is_active=False)
        resp = self.reset(code)
        self.assertEqual(resp.status_code, 400)
        account.refresh_from_db()
        self.assertTrue(check_password(PW, account.password))

    def test_arabic_digits_accepted(self):
        _, code = self.start()
        arabic = code.translate({ord("0") + i: 0x0660 + i for i in range(10)})
        resp = self.reset(arabic)
        self.assertEqual(resp.status_code, 302)

    def test_pending_account_lands_on_pending(self):
        make_pending_account(phone=GOOD_PHONE)
        self.forgot()
        resp = self.reset(sent_codes(self.sender)[-1])
        self.assertRedirects(resp, reverse("portal:pending"), fetch_redirect_response=False)


class DecoyTests(ResetBase):
    """رقم بلا حساب مؤهّل: نفس تسلسل الردود (غلط ×4، استنفاد، انتهاء)."""

    def test_unknown_phone_mimics_attempt_limit(self):
        self.forgot(phone=OTHER_PHONE)
        for i in range(4):
            self.assertContains(self.reset("123456"), TRANSLATIONS["portal_reset_err_wrong"]["en"], status_code=400)
        self.assertContains(self.reset("123456"), TRANSLATIONS["portal_reset_err_exhausted"]["en"], status_code=400)
        self.assertContains(self.reset("123456"), TRANSLATIONS["portal_reset_err_exhausted"]["en"], status_code=400)

    def test_unknown_phone_mimics_expiry(self):
        self.forgot(phone=OTHER_PHONE)
        session = self.client.session
        session[portal_auth.SESSION_RESET_STARTED] = time.time() - portal_otp.OTP_TTL.total_seconds() - 1
        session.save()
        self.assertContains(self.reset("123456"), TRANSLATIONS["portal_reset_err_expired"]["en"], status_code=400)

    def test_unknown_and_real_wrong_code_pages_identical(self):
        self.forgot()  # بلا حساب
        unknown = strip_csrf(self.reset("000001").content.decode())
        self.account_a()
        cache.clear()
        client = Client()
        self.set_lang("en", client)
        self.forgot(client=client)
        code = sent_codes(self.sender)[-1]
        real = strip_csrf(self.reset(self.wrong_code(code) if code != "000001" else "000002", client=client)
                          .content.decode())
        self.assertEqual(unknown, real)


class PurposeIsolationTests(ResetBase):
    def test_reset_code_does_not_verify_phone(self):
        account = self.account_a(verified=False)
        _put_session(self.client, account)
        manual_otp(account, PhoneOtp.PURPOSE_RESET, code="246810")
        resp = self.client.post(reverse("portal:verify"), {"code": "246810"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(portal_otp.verify_code(account, "246810"), portal_otp.V_NONE)
        account.refresh_from_db()
        self.assertFalse(account.phone_verified)

    def test_reset_code_does_not_activate_subscription(self):
        account = make_pending_account(phone=GOOD_PHONE)
        self.forgot()
        code = sent_codes(self.sender)[-1]
        status, merchant = portal_otp.activate_subscription(account, code)
        self.assertEqual((status, merchant), (portal_otp.V_NONE, None))
        account.refresh_from_db()
        self.assertIsNone(account.merchant_id)

    def test_verify_code_does_not_reset_password(self):
        account = self.account_a()
        self.forgot(phone=OTHER_PHONE)  # بداية جلسة إعادة
        session = self.client.session
        session[portal_auth.SESSION_RESET_ACCOUNT] = account.pk
        session.save()
        manual_otp(account, PhoneOtp.PURPOSE_VERIFY, code="135791")
        resp = self.reset("135791")
        self.assertContains(resp, TRANSLATIONS["portal_reset_err_expired"]["en"], status_code=400)
        self.assertEqual(portal_otp.reset_password(account, "135791", "x"), portal_otp.V_NONE)
        account.refresh_from_db()
        self.assertTrue(check_password(PW, account.password))
        self.assertIsNone(PhoneOtp.objects.get().consumed_at)

    def test_subscribe_code_does_not_reset_password(self):
        account = make_pending_account(phone=GOOD_PHONE)
        otp = manual_otp(account, PhoneOtp.PURPOSE_SUBSCRIBE, code="ABCDEFGH", store_name="S")
        self.assertEqual(portal_otp.reset_password(account, "ABCDEFGH", "x"), portal_otp.V_NONE)
        otp.refresh_from_db()
        self.assertEqual(otp.attempts, 0)

    def test_reset_send_keeps_other_purposes_open(self):
        account = make_pending_account(phone=GOOD_PHONE)
        sub = manual_otp(account, PhoneOtp.PURPOSE_SUBSCRIBE, code="ABCDEFGH", store_name="S")
        PhoneOtp.objects.update(created_at=F("created_at") - timedelta(hours=7))  # خارج التهدئة
        self.forgot()
        sub.refresh_from_db()
        self.assertIsNone(sub.consumed_at)

    def test_cooldown_shared_with_verification(self):
        account = self.account_a()
        manual_otp(account, PhoneOtp.PURPOSE_VERIFY)  # إرسال ناجح قبل لحظة (message_id)
        self.forgot()
        self.assertEqual(self.sender.call_count, 0)
        self.assertFalse(PhoneOtp.objects.filter(purpose=PhoneOtp.PURPOSE_RESET).exists())


class CsrfAndThrottleTests(ResetBase):
    def test_csrf_required(self):
        self.account_a()
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.forgot(client=client).status_code, 403)
        self.assertEqual(self.sender.call_count, 0)
        # مع التوكن بيشتغل
        html = client.get(FORGOT).content.decode()
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html).group(1)
        resp = client.post(FORGOT, {"phone": GOOD_PHONE, "csrfmiddlewaretoken": token})
        self.assertEqual(resp.status_code, 302)
        code = sent_codes(self.sender)[-1]
        self.assertEqual(client.post(RESET, {"code": code, "password": NEW_PW, "password_confirm": NEW_PW}).status_code, 403)

    def test_forgot_ip_throttle(self):
        self.account_a()
        for _ in range(portal_auth.IP_MAX_FORGOT_POSTS):
            self.assertEqual(self.forgot(phone=OTHER_PHONE).status_code, 302)
        resp = self.forgot()
        self.assertContains(resp, TRANSLATIONS["portal_reset_err_throttled"]["en"], status_code=429)
        self.assertEqual(self.sender.call_count, 0)

    def test_reset_ip_throttle(self):
        _ = self.account_a()
        self.forgot()
        code = sent_codes(self.sender)[-1]
        for _ in range(portal_auth.IP_MAX_RESET_POSTS):
            self.reset("12")  # صيغة غلط: ما بتنعدّ عالكود
        resp = self.reset(code)
        self.assertContains(resp, TRANSLATIONS["portal_reset_err_throttled"]["en"], status_code=429)
        self.assertTrue(check_password(PW, MerchantAccount.objects.get().password))


class ResetTextTests(ResetBase):
    def test_all_reset_keys_have_both_languages(self):
        keys = [k for k in TRANSLATIONS if k.startswith("portal_reset_")]
        self.assertGreater(len(keys), 10)
        for key in keys:
            self.assertTrue(TRANSLATIONS[key]["ar"].strip() and TRANSLATIONS[key]["en"].strip(), key)

    def test_login_hint_points_to_reset_flow(self):
        self.assertNotIn("رابط", t("portal_login_forgot", "ar"))
        self.assertIn("واتساب", t("portal_login_forgot", "ar"))

    def test_arabic_reset_page_renders(self):
        self.set_lang("ar")
        self.forgot(phone=OTHER_PHONE)
        html = self.client.get(RESET).content.decode()
        self.assertIn(TRANSLATIONS["portal_reset_title"]["ar"], html)
