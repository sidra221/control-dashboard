"""اختبارات التسجيل العام (حساب معلّق بدون متجر) → صفحة الانتظار → ربط الستاف → الدخول العادي."""

from django.contrib.auth.hashers import check_password
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import portal_auth
from .i18n import TRANSLATIONS
from .models import ApiToken, MerchantAccount
from .portal_testing import (
    FAST_HASHERS, login_pending, login_portal, make_account, make_pending_account, patch_otp_send, sent_codes,
)
from .test_portal_auth import GOOD_PHONE, PW, PortalBase, csrf_token


def signup_url():
    return reverse("portal:register")


class SignupBase(PortalBase):
    def signup(self, client=None, **over):
        data = {"name": "Ahmad", "phone": GOOD_PHONE, "password": PW, "password_confirm": PW}
        data.update(over)
        return (client or self.client).post(signup_url(), data)


class SignupPageTests(SignupBase):
    def test_get_renders_form_with_headers_and_links(self):
        resp = self.client.get(signup_url())
        self.assertEqual(resp.status_code, 200)
        self.assert_headers(resp)
        html = resp.content.decode()
        self.assertIn('<body class="auth">', html)
        self.assertIn("csrfmiddlewaretoken", html)
        self.assertEqual(html.count('autocomplete="new-password"'), 2)
        self.assertIn("Already have an account? Sign in", html)
        self.assertIn(f'href="{reverse("portal:login")}"', html)
        # نفس مظهر auth: بدون لغة ولا لوغو ولا next
        self.assertNotIn('class="lang-switch"', html)
        self.assertNotIn('class="brand-mark"', html)
        self.assertNotIn('name="next"', html)
        self.assertIn('name="website"', html)  # honeypot
        self.assertIn('class="hp"', html)

    def test_phone_label_and_hint_say_whatsapp_on_both_registration_pages(self):
        key = self.invite(self.a)
        for lang, needle in (("en", "WhatsApp number"), ("ar", "رقم الواتساب")):
            self.set_lang(lang)
            for html in (
                self.client.get(signup_url()).content.decode(),
                self.client.get(self.enter_url(key)).content.decode(),
            ):
                self.assertIn(needle, html)
        self.set_lang("en")
        for html in (self.client.get(signup_url()).content.decode(), self.client.get(self.enter_url(key)).content.decode()):
            self.assertIn("Must be a WhatsApp number, with country code", html)

    def test_arabic_example_keeps_bidi_isolates(self):
        for key in ("portal_register_wa_phone_hint", "portal_register_err_phone", "portal_login_phone_hint"):
            self.assertIn("⁦+966501234567⁩", TRANSLATIONS[key]["ar"], key)

    def test_login_page_links_to_signup(self):
        html = self.client.get(reverse("portal:login")).content.decode()
        self.assertIn(f'href="{signup_url()}"', html)
        self.assertIn("Create an account", html)
        self.assertNotIn('class="lang-switch"', html)

    def test_both_languages(self):
        for lang, direction in (("ar", "rtl"), ("en", "ltr")):
            self.set_lang(lang)
            self.assertIn(f'lang="{lang}" dir="{direction}"', self.client.get(signup_url()).content.decode())

    def test_get_redirects_logged_in_accounts(self):
        login_pending(self.client)
        self.assertRedirects(self.client.get(signup_url()), reverse("portal:pending"), fetch_redirect_response=False)
        c2 = Client()
        login_portal(c2, self.a)
        self.assertRedirects(c2.get(signup_url()), reverse("portal:home"), fetch_redirect_response=False)

    def test_other_methods_not_allowed(self):
        self.assertEqual(self.client.put(signup_url()).status_code, 405)

    def test_translation_keys_exist_in_both_languages(self):
        keys = [k for k in TRANSLATIONS if k.startswith(("portal_signup_", "portal_pending_", "portal_admin_pending_"))]
        self.assertGreaterEqual(len(keys), 20)
        for key in keys:
            self.assertTrue(TRANSLATIONS[key]["ar"].strip() and TRANSLATIONS[key]["en"].strip(), key)


class SignupSuccessTests(SignupBase):
    def test_creates_unverified_account_logs_in_and_redirects_to_verify(self):
        resp = self.signup()
        self.assertRedirects(resp, reverse("portal:verify"), fetch_redirect_response=False)
        self.assert_headers(resp)
        acc = MerchantAccount.objects.get()
        self.assertIsNone(acc.merchant)
        self.assertEqual((acc.name, acc.phone), ("Ahmad", GOOD_PHONE))
        self.assertFalse(acc.phone_verified)
        self.assertTrue(acc.is_active)
        self.assertEqual(self.client.session[portal_auth.SESSION_ACCOUNT], acc.pk)
        self.assertEqual(self.client.session[portal_auth.SESSION_FINGERPRINT], portal_auth.session_fingerprint(acc))

    def test_password_is_hashed_never_plain(self):
        self.signup()
        stored = MerchantAccount.objects.get().password
        self.assertNotEqual(stored, PW)
        self.assertNotIn(PW, stored)
        self.assertTrue(check_password(PW, stored))

    def test_phone_normalized_and_name_stripped(self):
        self.signup(phone="٠٠٩٦٦ ٥٠ ١٢٣ ٤٥٦٧", name="  Ahmad  ")
        acc = MerchantAccount.objects.get()
        self.assertEqual((acc.phone, acc.name), (GOOD_PHONE, "Ahmad"))

    def test_session_key_cycled_and_staff_login_kept(self):
        self.client.force_login(self.staff)
        old = self.client.session.session_key
        self.signup()
        self.assertNotEqual(self.client.session.session_key, old)
        self.assertEqual(self.client.get(reverse("apitokens:dashboard")).status_code, 200)

    def test_no_store_gets_touched(self):
        self.signup()
        self.assertFalse(MerchantAccount.objects.filter(merchant__isnull=False).exists())

    def test_logged_in_post_creates_nothing(self):
        login_pending(self.client)
        resp = self.signup(phone="+966501111111")
        self.assertRedirects(resp, reverse("portal:pending"), fetch_redirect_response=False)
        self.assertEqual(MerchantAccount.objects.count(), 1)


class SignupValidationTests(SignupBase):
    def assert_rejected(self, field, **over):
        resp = self.signup(**over)
        self.assertEqual(resp.status_code, 400)
        self.assert_headers(resp)
        html = resp.content.decode()
        self.assertRegex(html, rf'id="{field}-msg">\s*<span class="field-error"')
        self.assertFalse(MerchantAccount.objects.exists())
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)
        return html

    def test_bad_phone_formats(self):
        for phone in ["0501234567", "501234567", "966501234567", "+966", "", "abc", "+0123456789"]:
            html = self.assert_rejected("phone", phone=phone)
            self.assertIn("country code", html)

    def test_arabic_digits_and_00_prefix_accepted(self):
        for phone in ["٠٠٩٦٦٥٠١٢٣٤٥٦٧", "00966501234567", "+٩٦٦٥٠١٢٣٤٥٦٧"]:
            MerchantAccount.objects.all().delete()
            resp = self.signup(client=Client(), phone=phone)
            self.assertEqual(resp.status_code, 302, phone)
            self.assertEqual(MerchantAccount.objects.get().phone, GOOD_PHONE)

    def test_empty_blank_and_long_name(self):
        for name in ["", "   ", "x" * 121]:
            self.assert_rejected("name", name=name)

    def test_duplicate_phone_of_pending_account(self):
        make_pending_account(phone=GOOD_PHONE)
        resp = self.signup()
        self.assertEqual(resp.status_code, 400)
        self.assertRegex(resp.content.decode(), r'id="phone-msg">\s*<span class="field-error"')
        self.assertEqual(MerchantAccount.objects.count(), 1)
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)

    def test_duplicate_phone_message_is_generic_and_translated(self):
        make_pending_account(phone=GOOD_PHONE)
        resp = self.signup()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already registered", resp.content.decode())
        self.set_lang("ar")
        self.assertIn("مسجّل من قبل", self.signup(client=self.client).content.decode())

    def test_duplicate_phone_belonging_to_account_with_store(self):
        make_account(self.b, phone=GOOD_PHONE)
        resp = self.signup()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already registered", resp.content.decode())
        self.assertEqual(MerchantAccount.objects.count(), 1)
        # ما انسرق الحساب ولا انغيّر
        self.assertEqual(MerchantAccount.objects.get().merchant, self.b)

    def test_weak_short_common_numeric_long_mismatch(self):
        cases = [
            ("password", "Ab1-xyz", "too short"),
            ("password", "password", "too common"),
            ("password", "qazwsxedc1", None),
            ("password", "12345678901", None),
            ("password", "x" * 129, "too long"),
        ]
        for field, pw, text in cases:
            html = self.assert_rejected(field, password=pw, password_confirm=pw)
            if text:
                self.assertIn(text, html)
        self.assert_rejected("password_confirm", password_confirm=PW + "x")

    def test_error_page_keeps_name_and_phone_but_never_passwords(self):
        html = self.assert_rejected("password", name="Layla", phone="050 123", password="short1", password_confirm="short1")
        self.assertIn('value="Layla"', html)
        self.assertIn('value="050 123"', html)
        self.assertNotIn("short1", html)
        html = self.assert_rejected("phone", phone="bad", password=PW, password_confirm=PW)
        self.assertNotIn(PW, html)

    def test_missing_fields_do_not_crash(self):
        resp = self.client.post(signup_url(), {})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(MerchantAccount.objects.exists())

    def test_django_english_messages_do_not_leak_into_arabic_ui(self):
        self.set_lang("ar")
        html = self.signup(password="password", password_confirm="password").content.decode()
        self.assertNotIn("This password is too common", html)


class HoneypotTests(SignupBase):
    def test_filled_honeypot_creates_nothing_but_looks_like_success(self):
        resp = self.signup(website="http://spam.example")
        self.assertRedirects(resp, reverse("portal:verify"), fetch_redirect_response=False)
        self.sender.assert_not_called()
        self.assert_headers(resp)
        self.assertFalse(MerchantAccount.objects.exists())
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)
        # الصفحة التالية بتودي للدخول لأنه ما في جلسة
        self.assertRedirects(self.client.get(reverse("portal:verify")), reverse("portal:login"), fetch_redirect_response=False)

    def test_filled_honeypot_with_invalid_data_still_silent(self):
        resp = self.signup(website="x", name="", phone="bad")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(MerchantAccount.objects.exists())

    def test_empty_honeypot_is_fine(self):
        self.assertEqual(self.signup(website="").status_code, 302)
        self.assertEqual(MerchantAccount.objects.count(), 1)


class SignupThrottleTests(SignupBase):
    def test_11th_post_in_window_is_429_even_with_valid_data(self):
        for i in range(portal_auth.IP_MAX_REGISTER_POSTS):
            self.assertEqual(self.signup(client=Client(), phone=f"+96650000{i:04d}").status_code, 302)
        resp = self.signup(client=Client(), phone="+966509999999")
        self.assertEqual(resp.status_code, 429)
        self.assert_headers(resp)
        self.assertIn("محاولات كتير", resp.content.decode())
        self.assertEqual(MerchantAccount.objects.count(), portal_auth.IP_MAX_REGISTER_POSTS)

    def test_failed_and_honeypot_posts_count_too(self):
        for i in range(portal_auth.IP_MAX_REGISTER_POSTS):
            self.signup(name="" if i % 2 else "Ok", website="x" if i % 2 == 0 else "")
        self.assertEqual(self.signup().status_code, 429)

    def test_counter_is_separate_from_login_throttle(self):
        for _ in range(portal_auth.IP_MAX_LOGIN_POSTS + 1):
            self.login_post(password="nope-nope-1")
        self.assertEqual(self.signup().status_code, 302)

    def test_register_posts_do_not_consume_login_budget(self):
        make_account(self.a, phone=GOOD_PHONE, password=PW)
        for _ in range(portal_auth.IP_MAX_REGISTER_POSTS + 1):
            self.signup(client=Client(), name="")
        self.assertEqual(self.login_post().status_code, 302)

    def test_other_ip_not_affected_and_xff_ignored(self):
        for _ in range(portal_auth.IP_MAX_REGISTER_POSTS + 1):
            self.signup(client=Client(), name="")
        resp = self.client.post(signup_url(), self.form(), HTTP_X_FORWARDED_FOR="8.8.8.8")
        self.assertEqual(resp.status_code, 429)
        resp = self.client.post(signup_url(), self.form(), REMOTE_ADDR="10.9.9.9")
        self.assertEqual(resp.status_code, 302)

    def test_get_requests_not_counted(self):
        for _ in range(portal_auth.IP_MAX_REGISTER_POSTS + 5):
            self.assertEqual(self.client.get(signup_url()).status_code, 200)
        self.assertEqual(self.signup().status_code, 302)

    def test_window_expiry_resets(self):
        for _ in range(portal_auth.IP_MAX_REGISTER_POSTS + 1):
            self.signup(client=Client(), name="")
        cache.clear()  # متل انتهاء الـ TTL
        self.assertEqual(self.signup().status_code, 302)


class PendingCapTests(SignupBase):
    @override_settings(PORTAL_MAX_PENDING_ACCOUNTS=2)
    def test_cap_refuses_new_registrations_generically(self):
        make_pending_account(phone="+966500000001")
        make_pending_account(phone="+966500000002")
        resp = self.signup()
        self.assertEqual(resp.status_code, 503)
        self.assert_headers(resp)
        self.assertIn("temporarily unavailable", resp.content.decode())
        self.assertEqual(MerchantAccount.objects.count(), 2)

    @override_settings(PORTAL_MAX_PENDING_ACCOUNTS=2)
    def test_cap_counts_only_pending_not_linked_accounts(self):
        make_account(self.a, phone="+966500000001")
        make_account(self.b, phone="+966500000002")
        make_pending_account(phone="+966500000003")
        self.assertEqual(self.signup().status_code, 302)  # 1 معلّق → 2 بعد التسجيل
        self.assertEqual(self.signup(client=Client(), phone="+966500000004").status_code, 503)

    def test_default_cap_is_500(self):
        self.assertEqual(portal_auth._max_pending(), 500)

    @override_settings(PORTAL_MAX_PENDING_ACCOUNTS=1)
    def test_cap_does_not_block_invite_registration(self):
        make_pending_account(phone="+966500000001")
        key = self.invite(self.a)
        self.assertEqual(self.register(key).status_code, 302)


@FAST_HASHERS
class SignupCsrfTests(TestCase):
    ORIGIN = "http://testserver"

    def setUp(self):
        cache.clear()
        self.sender = patch_otp_send(self)
        self.client = Client(enforce_csrf_checks=True)

    def post(self, data, token=None, origin=ORIGIN):
        data = dict(data)
        if token:
            data["csrfmiddlewaretoken"] = token
        extra = {"HTTP_ORIGIN": origin} if origin else {}
        return self.client.post(signup_url(), data, **extra)

    def test_signup_requires_csrf(self):
        data = {"name": "Ahmad", "phone": GOOD_PHONE, "password": PW, "password_confirm": PW}
        token = csrf_token(self.client.get(signup_url()).content.decode())
        self.assertEqual(self.post(data).status_code, 403)
        self.assertEqual(self.post(data, "bad" * 20).status_code, 403)
        self.assertEqual(self.post(data, token, origin="null").status_code, 403)
        self.assertEqual(self.post(data, token, origin="http://evil.example").status_code, 403)
        self.assertFalse(MerchantAccount.objects.exists())
        resp = self.post(data, token)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("portal:verify"))
        self.assertTrue(MerchantAccount.objects.filter(merchant=None).exists())

    def test_csrf_rejected_posts_do_not_count_toward_throttle(self):
        for _ in range(portal_auth.IP_MAX_REGISTER_POSTS + 3):
            self.post({"name": "x"})
        token = csrf_token(self.client.get(signup_url()).content.decode())
        data = {"name": "Ahmad", "phone": GOOD_PHONE, "password": PW, "password_confirm": PW}
        self.assertEqual(self.post(data, token).status_code, 302)

    def test_pending_page_and_logout_require_csrf_and_referrer_policy(self):
        data = {"name": "Ahmad", "phone": GOOD_PHONE, "password": PW, "password_confirm": PW}
        token = csrf_token(self.client.get(signup_url()).content.decode())
        self.assertEqual(self.client.get(signup_url())["Referrer-Policy"], "same-origin")
        self.post(data, token)
        # الحساب غير موثّق: لازم الكود أول (صفحة الانتظار ما بتنفتح قبل)
        verify_page = self.client.get(reverse("portal:verify"))
        vtoken = csrf_token(verify_page.content.decode())
        self.assertEqual(self.post_to(reverse("portal:verify"), {"code": sent_codes(self.sender)[-1]}).status_code, 403)
        self.assertEqual(
            self.post_to(reverse("portal:verify"), {"code": sent_codes(self.sender)[-1]}, vtoken).status_code, 302
        )
        page = self.client.get(reverse("portal:pending"))
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        token = csrf_token(page.content.decode())
        self.assertEqual(self.post_to(reverse("portal:logout"), {}).status_code, 403)
        self.assertIn(portal_auth.SESSION_ACCOUNT, self.client.session)
        self.assertEqual(self.post_to(reverse("portal:logout"), {}, token).status_code, 302)
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)

    def post_to(self, url, data, token=None):
        data = dict(data)
        if token:
            data["csrfmiddlewaretoken"] = token
        return self.client.post(url, data, HTTP_ORIGIN=self.ORIGIN)


class PendingPageTests(SignupBase):
    def test_anonymous_redirects_to_login(self):
        resp = self.client.get(reverse("portal:pending"))
        self.assertRedirects(resp, reverse("portal:login"), fetch_redirect_response=False)
        self.assert_headers(resp)

    def test_pending_account_sees_message_logout_and_headers(self):
        login_pending(self.client, name="Layla")
        resp = self.client.get(reverse("portal:pending"))
        self.assertEqual(resp.status_code, 200)
        self.assert_headers(resp)
        html = resp.content.decode()
        self.assertIn("Activate your subscription", html)
        self.assertIn("Request subscription code", html)
        self.assertIn("Layla", html)
        self.assertIn(f'action="{reverse("portal:logout")}"', html)
        self.assertIn("csrfmiddlewaretoken", html)

    def test_arabic(self):
        login_pending(self.client)
        self.set_lang("ar")
        html = self.client.get(reverse("portal:pending")).content.decode()
        self.assertIn("فعّل اشتراكك", html)
        self.assertIn('dir="rtl"', html)

    def test_name_is_escaped(self):
        login_pending(self.client, name="<script>alert(1)</script>")
        html = self.client.get(reverse("portal:pending")).content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_linked_account_goes_home(self):
        login_portal(self.client, self.a)
        self.assertRedirects(self.client.get(reverse("portal:pending")), reverse("portal:home"), fetch_redirect_response=False)

    def test_deactivated_pending_account_is_kicked_out(self):
        acc = login_pending(self.client)
        MerchantAccount.objects.filter(pk=acc.pk).update(is_active=False)
        self.assertRedirects(self.client.get(reverse("portal:pending")), reverse("portal:login"), fetch_redirect_response=False)
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)

    def test_fingerprint_mismatch_is_kicked_out(self):
        acc = login_pending(self.client)
        MerchantAccount.objects.filter(pk=acc.pk).update(password="x" + acc.password)
        self.assertRedirects(self.client.get(reverse("portal:pending")), reverse("portal:login"), fetch_redirect_response=False)

    def test_deleted_account_is_kicked_out(self):
        acc = login_pending(self.client)
        acc.delete()
        self.assertRedirects(self.client.get(reverse("portal:pending")), reverse("portal:login"), fetch_redirect_response=False)

    def test_logout_works_for_pending_and_keeps_staff_session(self):
        self.client.force_login(self.staff)
        login_pending(self.client)
        resp = self.client.post(reverse("portal:logout"))
        self.assertRedirects(resp, reverse("portal:login"), fetch_redirect_response=False)
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)
        self.assertEqual(self.client.get(reverse("apitokens:dashboard")).status_code, 200)

    def test_only_get_post_semantics_no_state_change(self):
        login_pending(self.client)
        self.assertEqual(self.client.get(reverse("portal:pending")).status_code, 200)


class PendingAccessControlTests(SignupBase):
    def test_portal_required_redirects_pending_to_pending_and_never_runs_view(self):
        login_pending(self.client)
        for name in ("portal:home", "portal:accept"):
            resp = self.client.get(reverse(name))
            self.assertRedirects(resp, reverse("portal:pending"), fetch_redirect_response=False)
            self.assert_headers(resp)
        resp = self.client.post(reverse("portal:accept"), {"agree": "1"})
        self.assertRedirects(resp, reverse("portal:pending"), fetch_redirect_response=False)

    def test_decorator_does_not_run_wrapped_view_for_pending(self):
        from django.http import HttpResponse
        from django.test import RequestFactory
        from django.contrib.sessions.backends.db import SessionStore

        acc = make_pending_account()
        req = RequestFactory().get("/x/")
        req.session = SessionStore()
        req.session[portal_auth.SESSION_ACCOUNT] = acc.pk
        req.session[portal_auth.SESSION_FINGERPRINT] = portal_auth.session_fingerprint(acc)
        called = []
        view = portal_auth.portal_required(lambda r: called.append(1) or HttpResponse("secret"))
        resp = view(req)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("portal:pending"))
        self.assertEqual(called, [])

    def test_pending_cannot_reach_staff_pages(self):
        login_pending(self.client)
        self.assertNotEqual(self.client.get(reverse("apitokens:dashboard")).status_code, 200)

    def test_login_of_pending_account_goes_to_pending(self):
        make_pending_account(phone=GOOD_PHONE, password=PW)
        resp = self.login_post()
        self.assertRedirects(resp, reverse("portal:pending"), fetch_redirect_response=False)
        self.assert_headers(resp)
        acc = MerchantAccount.objects.get()
        self.assertEqual(self.client.session[portal_auth.SESSION_ACCOUNT], acc.pk)
        self.assertIsNotNone(acc.last_login_at)

    def test_login_get_with_pending_session_redirects_to_pending(self):
        login_pending(self.client)
        self.assertRedirects(self.client.get(reverse("portal:login")), reverse("portal:pending"), fetch_redirect_response=False)

    def test_pending_login_wrong_password_generic_and_locks_after_five(self):
        acc = make_pending_account(phone=GOOD_PHONE, password=PW)
        for _ in range(portal_auth.MAX_FAILED_LOGINS):
            resp = self.login_post(password="nope-nope-1")
            self.assertEqual(resp.status_code, 400)
            self.assertIn("incorrect", resp.content.decode())
        acc.refresh_from_db()
        self.assertIsNotNone(acc.locked_until)
        self.assertEqual(self.login_post().status_code, 429)

    def test_inactive_pending_account_cannot_login(self):
        make_pending_account(phone=GOOD_PHONE, password=PW, is_active=False)
        self.assertEqual(self.login_post().status_code, 400)

    def test_login_ip_throttle_unchanged_for_pending(self):
        make_pending_account(phone=GOOD_PHONE, password=PW)
        for _ in range(portal_auth.IP_MAX_LOGIN_POSTS):
            self.login_post(password="nope-nope-1", client=Client())
        self.assertEqual(self.login_post().status_code, 429)

    def test_signup_then_logout_then_login_roundtrip(self):
        self.signup()
        self.verify_last_code()
        self.client.post(reverse("portal:logout"))
        resp = self.login_post()
        self.assertRedirects(resp, reverse("portal:pending"), fetch_redirect_response=False)


class StaffPendingTests(SignupBase):
    def setUp(self):
        super().setUp()
        self.pending = make_pending_account(name="Layla Pending", phone="+966500000009", password="Staff-Must-Not-See-1")

    def link_url(self, pk=None):
        return reverse("apitokens:portal-account-link", args=[pk or self.pending.pk])

    def delete_url(self, pk=None):
        return reverse("apitokens:portal-account-delete", args=[pk or self.pending.pk])

    def dashboard(self):
        return self.client.get(reverse("apitokens:dashboard")).content.decode()

    def link(self, merchant=None, pk=None):
        merchant = merchant or self.a
        return self.client.post(self.link_url(pk), {"merchant": merchant.pk})

    # --- العرض ---
    def test_section_hidden_without_pending_accounts(self):
        self.client.force_login(self.staff)
        self.pending.delete()
        html = self.dashboard()
        self.assertNotIn("Pending accounts", html)
        self.assertNotIn("portal-account", html)

    def test_section_shows_account_details_and_free_stores_only(self):
        self.client.force_login(self.staff)
        make_account(self.b, phone="+966500000010")  # B إله حساب → ما بيظهر بالقائمة
        html = self.dashboard()
        self.assertIn("Pending accounts", html)
        self.assertIn("Layla Pending", html)
        self.assertIn("+966500000009", html)
        self.assertIn("Verified on", html)
        self.assertIn(self.link_url(), html)
        self.assertIn(self.delete_url(), html)
        self.assertIn('<option value="%d">A (#%d)</option>' % (self.a.pk, self.a.pk), html)
        self.assertNotIn('<option value="%d">' % self.b.pk, html)

    def test_inactive_store_not_offered(self):
        self.client.force_login(self.staff)
        ApiToken.objects.filter(pk=self.a.pk).update(is_active=False)
        self.assertNotIn('<option value="%d">' % self.a.pk, self.dashboard())

    def test_no_free_stores_shows_note_and_no_select(self):
        self.client.force_login(self.staff)
        make_account(self.a, phone="+966500000011")
        make_account(self.b, phone="+966500000012")
        html = self.dashboard()
        self.assertIn("No stores without an account", html)
        self.assertNotIn('name="merchant"', html)
        self.assertIn(self.delete_url(), html)

    def test_arabic(self):
        self.client.force_login(self.staff)
        self.set_lang("ar")
        html = self.dashboard()
        self.assertIn("حسابات بانتظار الربط", html)
        self.assertIn("موثّق بتاريخ", html)

    def test_never_leaks_hash_or_password(self):
        self.client.force_login(self.staff)
        html = self.dashboard()
        self.assertNotIn(self.pending.password, html)
        self.assertNotIn("Staff-Must-Not-See-1", html)
        self.assertNotIn("md5$", html)

    def test_name_is_escaped(self):
        self.client.force_login(self.staff)
        MerchantAccount.objects.filter(pk=self.pending.pk).update(name="<b>x</b>")
        self.assertNotIn("<b>x</b>", self.dashboard())

    # --- الربط ---
    def test_link_success(self):
        self.client.force_login(self.staff)
        resp = self.link()
        self.assertRedirects(resp, reverse("apitokens:dashboard"), fetch_redirect_response=False)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.merchant, self.a)
        self.assertTrue(self.pending.phone_verified)
        html = self.client.get(reverse("apitokens:dashboard")).content.decode()
        self.assertIn("Account linked to the store", html)
        self.assertNotIn("Pending accounts", html)
        # بيظهر بسطر حالة المتجر متل حسابات الدعوة
        self.assertIn("Layla Pending", html)
        self.assertIn("never logged in", html)

    def test_link_refused_when_store_already_has_account(self):
        self.client.force_login(self.staff)
        other = make_account(self.a, phone="+966500000010")
        resp = self.link()
        self.assertEqual(resp.status_code, 302)
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.merchant)
        other.refresh_from_db()
        self.assertEqual(other.merchant, self.a)
        self.assertIn("already has an account", self.dashboard())

    def test_double_link_second_call_refused(self):
        self.client.force_login(self.staff)
        p2 = make_pending_account(name="Second", phone="+966500000020")
        self.link(pk=self.pending.pk)
        self.link(pk=p2.pk)
        p2.refresh_from_db()
        self.assertIsNone(p2.merchant)
        self.assertEqual(MerchantAccount.objects.filter(merchant=self.a).count(), 1)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.merchant, self.a)

    def test_integrity_race_is_reported_not_500(self):
        from unittest import mock

        self.client.force_login(self.staff)
        from django.db import IntegrityError

        with mock.patch.object(MerchantAccount, "save", side_effect=IntegrityError):
            resp = self.link()
        self.assertEqual(resp.status_code, 302)
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.merchant)
        self.assertIn("already has an account", self.dashboard())

    def test_link_refused_for_non_pending_account(self):
        self.client.force_login(self.staff)
        linked = make_account(self.b, phone="+966500000030")
        resp = self.link(merchant=self.a, pk=linked.pk)
        self.assertEqual(resp.status_code, 302)
        linked.refresh_from_db()
        self.assertEqual(linked.merchant, self.b)  # ما انسرق ولا انتقل
        self.assertIn("not pending", self.dashboard())

    def test_link_refused_for_inactive_store(self):
        self.client.force_login(self.staff)
        ApiToken.objects.filter(pk=self.a.pk).update(is_active=False)
        self.link()
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.merchant)

    def test_link_unknown_ids_and_bad_input(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self.link_url(99999), {"merchant": self.a.pk}).status_code, 404)
        self.assertEqual(self.client.post(self.link_url(), {"merchant": 99999}).status_code, 404)
        for bad in ("", "abc", "-1", "1.5", "١"):
            self.assertEqual(self.client.post(self.link_url(), {"merchant": bad}).status_code, 400, bad)
        self.assertEqual(self.client.post(self.link_url(), {}).status_code, 400)
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.merchant)

    def test_link_requires_staff_and_post(self):
        resp = self.link()
        self.assertEqual(resp.status_code, 302)
        self.assertIn("login", resp["Location"])
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.merchant)
        # تاجر مسجّل دخول للبوابة مش ستاف
        login_portal(self.client, self.b)
        self.link()
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.merchant)
        # ستاف عادي (مش is_staff)
        nonstaff = self.staff.__class__.objects.create_user("plain", password="x")
        c = Client()
        c.force_login(nonstaff)
        c.post(self.link_url(), {"merchant": self.a.pk})
        self.pending.refresh_from_db()
        self.assertIsNone(self.pending.merchant)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.link_url()).status_code, 405)

    def test_link_requires_csrf(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.staff)
        resp = c.post(self.link_url(), {"merchant": self.a.pk}, HTTP_ORIGIN="http://testserver")
        self.assertEqual(resp.status_code, 403)
        resp = c.post(self.delete_url(), {}, HTTP_ORIGIN="http://testserver")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(MerchantAccount.objects.filter(pk=self.pending.pk, merchant=None).exists())

    # --- الحذف ---
    def test_delete_pending(self):
        self.client.force_login(self.staff)
        resp = self.client.post(self.delete_url())
        self.assertRedirects(resp, reverse("apitokens:dashboard"), fetch_redirect_response=False)
        self.assertFalse(MerchantAccount.objects.filter(pk=self.pending.pk).exists())
        self.assertIn("Account deleted", self.dashboard())

    def test_delete_refuses_linked_account(self):
        self.client.force_login(self.staff)
        linked = make_account(self.a, phone="+966500000030")
        resp = self.client.post(self.delete_url(linked.pk))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(MerchantAccount.objects.filter(pk=linked.pk).exists())
        self.assertIn("not pending", self.dashboard())

    def test_delete_unknown_staff_and_method(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self.delete_url(99999)).status_code, 404)
        self.assertEqual(self.client.get(self.delete_url()).status_code, 405)
        c = Client()
        self.assertEqual(c.post(self.delete_url()).status_code, 302)
        self.assertTrue(MerchantAccount.objects.filter(pk=self.pending.pk).exists())

    def test_deleted_account_session_dies_and_phone_is_free_again(self):
        c = Client()
        acc = login_pending(c, phone="+966500000040")
        self.client.force_login(self.staff)
        self.client.post(self.delete_url(acc.pk))
        self.assertRedirects(c.get(reverse("portal:pending")), reverse("portal:login"), fetch_redirect_response=False)
        self.assertEqual(self.signup(client=Client(), phone="+966500000040").status_code, 302)

    # --- بعد الربط ---
    def test_after_link_merchant_sees_consent_then_home(self):
        c = Client()
        acc = login_pending(c, phone="+966500000050")
        self.assertRedirects(c.get(reverse("portal:home")), reverse("portal:pending"), fetch_redirect_response=False)
        self.client.force_login(self.staff)
        self.link(pk=acc.pk)
        self.assertRedirects(c.get(reverse("portal:pending")), reverse("portal:home"), fetch_redirect_response=False)
        self.assertRedirects(c.get(reverse("portal:home")), reverse("portal:accept"), fetch_redirect_response=False)
        self.assertEqual(c.get(reverse("portal:accept")).status_code, 200)
        self.assertRedirects(c.post(reverse("portal:accept"), {"agree": "1"}), reverse("portal:home"), fetch_redirect_response=False)
        self.assertEqual(c.get(reverse("portal:home")).status_code, 200)

    def test_after_link_login_goes_home_and_consent_keeps_if_already_accepted(self):
        make_pending_account(phone=GOOD_PHONE, password=PW)
        acc = MerchantAccount.objects.get(phone=GOOD_PHONE)
        portal_auth.accept_terms(self.a)
        self.client.force_login(self.staff)
        self.link(pk=acc.pk)
        c = Client()
        self.assertRedirects(self.login_post(client=c), reverse("portal:home"), fetch_redirect_response=False)
        self.assertEqual(c.get(reverse("portal:home")).status_code, 200)

    def test_revoke_deactivates_staff_linked_account(self):
        c = Client()
        acc = login_pending(c, phone="+966500000060")
        self.client.force_login(self.staff)
        self.link(pk=acc.pk)
        portal_auth.accept_terms(self.a)
        self.assertEqual(c.get(reverse("portal:home")).status_code, 200)
        self.client.post(reverse("apitokens:portal-link", args=[self.a.pk]), {"action": "revoke"})
        acc.refresh_from_db()
        self.assertFalse(acc.is_active)
        self.assertRedirects(c.get(reverse("portal:home")), reverse("portal:login"), fetch_redirect_response=False)

    def test_linked_account_shows_in_store_status_and_buttons(self):
        self.client.force_login(self.staff)
        self.link()
        html = self.dashboard()
        self.assertIn("Account:", html)
        self.assertEqual(html.count('value="revoke"'), 1)

    def test_cross_merchant_isolation_after_link(self):
        c = Client()
        acc = login_pending(c, phone="+966500000070")
        self.client.force_login(self.staff)
        self.link(pk=acc.pk)
        portal_auth.accept_terms(self.a)
        html = c.get(reverse("portal:home")).content.decode()
        self.assertIn(self.a.label, html)
        self.assertNotIn("<h1>B</h1>", html)


class InviteFlowWithPendingTests(SignupBase):
    def test_invite_binds_directly_and_still_works(self):
        key = self.invite(self.a)
        resp = self.register(key)
        self.assertRedirects(resp, reverse("portal:verify"), fetch_redirect_response=False)
        acc = MerchantAccount.objects.get()
        self.assertEqual(acc.merchant, self.a)
        self.assertFalse(acc.phone_verified)
        self.assertRedirects(self.verify_last_code(), reverse("portal:home"), fetch_redirect_response=False)

    def test_invite_cannot_take_phone_of_a_pending_account(self):
        pending = make_pending_account(phone=GOOD_PHONE, password="Other-Secret-9911")
        key = self.invite(self.a)
        resp = self.register(key)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already used", resp.content.decode())
        pending.refresh_from_db()
        self.assertIsNone(pending.merchant)
        self.assertTrue(check_password("Other-Secret-9911", pending.password))
        self.assertFalse(MerchantAccount.objects.filter(merchant=self.a).exists())
        self.a.refresh_from_db()
        self.assertTrue(self.a.portal_key_hash, "الدعوة ما بتنستهلك مع خطأ")

    def test_invite_cannot_take_phone_of_another_stores_account(self):
        make_account(self.b, phone=GOOD_PHONE)
        resp = self.register(self.invite(self.a))
        self.assertEqual(resp.status_code, 400)

    def test_invite_reset_keeps_own_phone(self):
        make_account(self.a, phone=GOOD_PHONE, password="Old-Password-123x")
        resp = self.register(self.invite(self.a))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(MerchantAccount.objects.count(), 1)

    def test_invite_phone_race_with_pending_is_a_clean_error(self):
        from unittest import mock

        from django.db import IntegrityError

        key = self.invite(self.a)
        with mock.patch.object(MerchantAccount, "save", side_effect=IntegrityError):
            resp = self.register(key)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already used", resp.content.decode())

    def test_invite_page_still_has_no_honeypot_or_language_switch(self):
        html = self.client.get(self.enter_url(self.invite(self.a))).content.decode()
        self.assertNotIn('name="website"', html)
        self.assertNotIn('class="lang-switch"', html)


class ModelTests(TestCase):
    def test_str_copes_with_no_merchant(self):
        acc = make_pending_account(name="Layla", phone="+966500000001")
        self.assertIn("Layla", str(acc))
        merchant = ApiToken.objects.create(label="Shop", image_quota=1)
        acc.merchant = merchant
        acc.save()
        self.assertIn("Shop", str(acc))

    def test_many_accounts_without_store_allowed_but_one_account_per_store(self):
        make_pending_account(phone="+966500000001")
        make_pending_account(phone="+966500000002")
        merchant = ApiToken.objects.create(label="Shop", image_quota=1)
        from django.db import IntegrityError, transaction

        MerchantAccount.objects.create(merchant=merchant, name="x", phone="+966500000004", password="!")
        with self.assertRaises(IntegrityError), transaction.atomic():
            MerchantAccount.objects.create(merchant=merchant, name="y", phone="+966500000005", password="!")

    def test_deleting_store_cascades_its_account_only(self):
        merchant = ApiToken.objects.create(label="Shop", image_quota=1)
        MerchantAccount.objects.create(merchant=merchant, name="x", phone="+966500000004", password="!")
        pending = make_pending_account(phone="+966500000001")
        merchant.delete()
        self.assertEqual(list(MerchantAccount.objects.values_list("pk", flat=True)), [pending.pk])
