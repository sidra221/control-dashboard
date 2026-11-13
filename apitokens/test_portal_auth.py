"""اختبارات دخول بوابة التاجر: دعوة (رابط لمرة وحدة) → تسجيل حساب (هاتف + كلمة مرور) → دخول.
بتغطي الدعوة، التسجيل والتحقق، إعادة التسجيل (reset)، الدخول، القفل والـ throttle، الجلسة
والفصل عن الستاف، CSRF بشكل المتصفح، وشاشة الستاف."""

import hashlib
import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.sessions.backends.db import SessionStore
from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import portal_auth
from .i18n import TRANSLATIONS
from .models import ApiToken, MerchantAccount
from .portal_auth import normalize_phone
from .portal_testing import DEFAULT_PASSWORD, FAST_HASHERS, login_portal, make_account

PORTAL_HEADERS = {
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
}
GOOD_PHONE = "+966501234567"
PW = "Zebra-Orchid-4821"
CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="[^"]+"')


def sha(key):
    return hashlib.sha256(key.encode()).hexdigest()


def strip_csrf(html):
    return CSRF_RE.sub("", html)


def csrf_token(html):
    return CSRF_RE.search(html).group(0).split('value="')[1].rstrip('"')


@FAST_HASHERS
class PortalBase(TestCase):
    def setUp(self):
        cache.clear()  # عدّاد الـ throttle بالـ LocMemCache بيضل بين الاختبارات
        self.a = ApiToken.objects.create(label="A", image_quota=100)
        self.b = ApiToken.objects.create(label="B", image_quota=100)
        self.staff = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        self.set_lang("en")  # الافتراضي عربي؛ الاختبارات بتفحص النص الإنجليزي إلا لما تقول غير هيك

    def invite(self, merchant):
        return portal_auth.generate_portal_key(merchant)

    def enter_url(self, key):
        return reverse("portal:enter", args=[key])

    def enter(self, key, client=None):
        return (client or self.client).get(self.enter_url(key))

    def form(self, **over):
        data = {"name": "Ahmad", "phone": GOOD_PHONE, "password": PW, "password_confirm": PW}
        data.update(over)
        return data

    def register(self, key, client=None, **over):
        return (client or self.client).post(self.enter_url(key), self.form(**over))

    def set_lang(self, lang, client=None):
        client = client or self.client
        session = client.session
        session["lang"] = lang
        session.save()

    def assert_headers(self, resp):
        for name, value in PORTAL_HEADERS.items():
            self.assertEqual(resp[name], value, name)

    def login_post(self, phone=GOOD_PHONE, password=PW, client=None, **extra):
        return (client or self.client).post(reverse("portal:login"), {"phone": phone, "password": password}, **extra)

    def account_a(self, **kw):
        return make_account(self.a, name="Ahmad", phone=GOOD_PHONE, password=PW, **kw)

    def portal_session_valid(self, client, merchant):
        req = RequestFactory().get("/x/")
        req.session = client.session
        resp = portal_auth.portal_required(_ok_view)(req)
        return resp.status_code == 200 and resp.content == merchant.label.encode()


def _ok_view(request):
    return HttpResponse(request.portal_merchant.label)


# ---------- الدعوة ----------


class KeyGenerationTests(PortalBase):
    def test_generate_stores_only_hash(self):
        key = self.invite(self.a)
        self.a.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, sha(key))
        self.assertIsNotNone(self.a.portal_key_created_at)
        self.assertGreaterEqual(len(key), 40)
        for field in ApiToken._meta.concrete_fields:
            self.assertNotEqual(getattr(self.a, field.attname), key)
        self.assertEqual(portal_auth.hash_portal_key(key), sha(key))

    def test_keys_are_unique_and_not_the_widget_token(self):
        k1, k2 = self.invite(self.a), self.invite(self.a)
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k2, self.a.token)

    def test_revoke_clears_invite(self):
        self.invite(self.a)
        portal_auth.revoke_portal_key(self.a)
        self.a.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, "")
        self.assertIsNone(self.a.portal_key_created_at)

    def test_revoke_deactivates_account_only_for_that_merchant(self):
        acc_a, acc_b = make_account(self.a), make_account(self.b)
        self.invite(self.a)
        portal_auth.revoke_portal_key(self.a)
        acc_a.refresh_from_db(), acc_b.refresh_from_db()
        self.assertFalse(acc_a.is_active)
        self.assertTrue(acc_b.is_active)

    def test_revoke_without_account_is_fine(self):
        portal_auth.revoke_portal_key(self.a)
        self.assertFalse(MerchantAccount.objects.exists())


class InviteValidityTests(PortalBase):
    def test_valid_invite_renders_register_form(self):
        key = self.invite(self.a)
        resp = self.enter(key)
        self.assertEqual(resp.status_code, 200)
        self.assert_headers(resp)
        html = resp.content.decode()
        self.assertIn("<strong>A</strong>", html)
        self.assertIn('name="phone"', html)
        self.assertIn('name="password_confirm"', html)
        self.assertIn("csrfmiddlewaretoken", html)
        self.assertIn('name="robots" content="noindex,nofollow"', html)

    def test_get_does_not_log_in_nor_store_key_in_session(self):
        key = self.invite(self.a)
        self.enter(key)
        session = self.client.session
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, session)
        self.assertNotIn(key, str(dict(session)))
        self.assertNotIn(sha(key), str(dict(session)))
        self.assertRedirects(
            self.client.get(reverse("portal:home")), reverse("portal:login"), fetch_redirect_response=False
        )

    def test_post_keeps_key_out_of_session_too(self):
        key = self.invite(self.a)
        self.register(key)
        session = self.client.session
        self.assertNotIn(key, str(dict(session)))
        self.assertNotIn(sha(key), str(dict(session)))

    def test_invalid_expired_used_inactive_are_uniform_404(self):
        good = self.invite(self.a)
        used = self.invite(self.b)
        self.register(used, client=self.client_class())
        expired_merchant = ApiToken.objects.create(label="E", image_quota=1)
        expired_key = self.invite(expired_merchant)
        ApiToken.objects.filter(pk=expired_merchant.pk).update(
            portal_key_created_at=timezone.now() - timedelta(days=7, minutes=1)
        )
        inactive_key = self.invite(ApiToken.objects.create(label="C", image_quota=1, is_active=False))
        revoked_merchant = ApiToken.objects.create(label="R", image_quota=1)
        revoked_key = self.invite(revoked_merchant)
        portal_auth.revoke_portal_key(revoked_merchant)
        bodies = set()
        for key in ["nope", "x" * 200, "ك", used, expired_key, inactive_key, revoked_key, self.a.token, sha(good), " "]:
            for resp in (self.enter(key), self.client.post(self.enter_url(key), self.form())):
                self.assertEqual(resp.status_code, 404, key)
                self.assert_headers(resp)
                bodies.add(resp.content)
            self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)
        self.assertEqual(len(bodies), 1)

    def test_invite_valid_just_under_7_days_then_dead(self):
        key = self.invite(self.a)
        ApiToken.objects.filter(pk=self.a.pk).update(portal_key_created_at=timezone.now() - timedelta(days=6, hours=23))
        self.assertEqual(self.enter(key).status_code, 200)
        ApiToken.objects.filter(pk=self.a.pk).update(portal_key_created_at=timezone.now() - timedelta(days=7, hours=1))
        self.assertEqual(self.enter(key).status_code, 404)

    def test_hash_without_timestamp_is_not_valid(self):
        key = self.invite(self.a)
        ApiToken.objects.filter(pk=self.a.pk).update(portal_key_created_at=None)
        self.assertEqual(self.enter(key).status_code, 404)

    def test_widget_token_never_works(self):
        self.invite(self.a)
        self.assertEqual(self.enter(self.a.token).status_code, 404)

    def test_empty_hash_is_not_a_wildcard(self):
        self.assertEqual(self.enter(sha("")).status_code, 404)
        self.assertEqual(self.enter(" ").status_code, 404)

    def test_old_invite_dead_after_new_one(self):
        old, new = self.invite(self.a), self.invite(self.a)
        self.assertEqual(self.enter(old).status_code, 404)
        self.assertEqual(self.enter(new).status_code, 200)

    def test_other_methods_not_allowed(self):
        key = self.invite(self.a)
        self.assertEqual(self.client.put(self.enter_url(key)).status_code, 405)
        self.assertEqual(self.client.delete(self.enter_url(key)).status_code, 405)

    def test_disabling_merchant_kills_pending_invite(self):
        key = self.invite(self.a)
        ApiToken.objects.filter(pk=self.a.pk).update(is_active=False)
        self.assertEqual(self.enter(key).status_code, 404)


# ---------- التسجيل ----------


class RegistrationSuccessTests(PortalBase):
    def test_success_creates_account_consumes_invite_logs_in_and_redirects(self):
        key = self.invite(self.a)
        resp = self.register(key)
        self.assertRedirects(resp, reverse("portal:home"), fetch_redirect_response=False)
        self.assert_headers(resp)
        account = MerchantAccount.objects.get(merchant=self.a)
        self.assertEqual((account.name, account.phone), ("Ahmad", GOOD_PHONE))
        self.assertTrue(account.is_active)
        self.assertFalse(account.phone_verified)
        self.assertIsNotNone(account.last_login_at)
        self.a.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, "")
        self.assertIsNone(self.a.portal_key_created_at)
        session = self.client.session
        self.assertEqual(session[portal_auth.SESSION_ACCOUNT], account.pk)
        self.assertEqual(session[portal_auth.SESSION_FINGERPRINT], portal_auth.session_fingerprint(account))
        # والبيت ما بيعرف كلمة المرور بالجلسة
        self.assertNotIn(PW, str(dict(session)))
        self.assertTrue(self.portal_session_valid(self.client, self.a))

    def test_invite_is_single_use(self):
        key = self.invite(self.a)
        self.register(key)
        for client in (self.client, self.client_class()):
            self.assertEqual(self.enter(key, client=client).status_code, 404)
            self.assertEqual(self.register(key, client=client, phone="+966502222222").status_code, 404)
        self.assertEqual(MerchantAccount.objects.count(), 1)
        self.assertEqual(MerchantAccount.objects.get().phone, GOOD_PHONE)

    def test_then_home_goes_to_consent_screen(self):
        self.register(self.invite(self.a))
        resp = self.client.get(reverse("portal:home"))
        self.assertRedirects(resp, reverse("portal:accept"), fetch_redirect_response=False)

    def test_session_key_is_cycled_and_other_session_data_kept(self):
        key = self.invite(self.a)
        self.enter(key)
        s = self.client.session
        s["marker"] = 1
        s.save()
        before = self.client.cookies["sessionid"].value
        self.register(key)
        self.assertNotEqual(before, self.client.cookies["sessionid"].value)
        self.assertEqual(self.client.session["marker"], 1)

    def test_password_stored_hashed_not_plain(self):
        self.register(self.invite(self.a))
        stored = MerchantAccount.objects.get().password
        self.assertNotIn(PW, stored)
        self.assertTrue(check_password(PW, stored))
        self.assertFalse(check_password(PW + "x", stored))

    @override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"])
    def test_default_hasher_is_pbkdf2(self):
        self.register(self.invite(self.a))
        self.assertTrue(MerchantAccount.objects.get().password.startswith("pbkdf2_sha256$"))

    def test_phone_is_normalized_on_save_and_name_stripped(self):
        self.register(self.invite(self.a), phone="  00966 (50) 123-4567 ", name="  Ahmad Ali  ")
        account = MerchantAccount.objects.get()
        self.assertEqual((account.phone, account.name), (GOOD_PHONE, "Ahmad Ali"))

    def test_staff_login_survives_registration(self):
        self.client.force_login(self.staff)
        self.register(self.invite(self.a))
        self.assertEqual(self.client.get(reverse("apitokens:dashboard")).status_code, 200)

    def test_other_merchant_untouched(self):
        self.invite(self.b)
        self.register(self.invite(self.a))
        self.b.refresh_from_db()
        self.assertTrue(self.b.portal_key_hash)
        self.assertFalse(MerchantAccount.objects.filter(merchant=self.b).exists())


class PhoneNormalizationTests(TestCase):
    def test_valid_formats(self):
        cases = {
            "+966501234567": "+966501234567",
            "+966 50 123 4567": "+966501234567",
            "+966-50-123-4567": "+966501234567",
            "(+966) 50.123.4567": "+966501234567",
            "00966501234567": "+966501234567",
            "٠٠٩٦٦٥٠١٢٣٤٥٦٧": "+966501234567",
            "+٩٦٦٥٠١٢٣٤٥٦٧": "+966501234567",
            "+۹۶۶۵۰۱۲۳۴۵۶۷": "+966501234567",
            "‎+966 501 234 567‏": "+966501234567",
            "+12345678": "+12345678",
            "+123456789012345": "+123456789012345",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_phone(raw), expected, raw)

    def test_invalid_formats(self):
        for raw in [
            "", " ", "0501234567", "501234567", "966501234567", "+0966501234567", "00", "+", "+966",
            "+1234567", "+1234567890123456", "abc", "+966 5x1234567", "+966+501234567", "++966501234567",
            "+９６６５０１２３４５６７", "٠٥٠١٢٣٤٥٦٧", None, 12345678, "+966\n501234567x",
        ]:
            self.assertIsNone(normalize_phone(raw), repr(raw))


class RegistrationValidationTests(PortalBase):
    def setUp(self):
        super().setUp()
        self.key = self.invite(self.a)

    def assert_rejected(self, field, **over):
        resp = self.register(self.key, **over)
        self.assertEqual(resp.status_code, 400)
        self.assert_headers(resp)
        html = resp.content.decode()
        self.assertIn('class="field-error"', html)
        self.assertFalse(MerchantAccount.objects.filter(merchant=self.a).exists())
        self.a.refresh_from_db()
        self.assertTrue(self.a.portal_key_hash, "الدعوة ما لازم تنستهلك مع خطأ")
        # الخطأ جنب الحقل المعني
        self.assertRegex(html, rf'id="{field}-msg">\s*<span class="field-error"')
        return html

    def test_phone_without_country_code_rejected_with_hint(self):
        for phone in ["0501234567", "501234567", "966501234567", "+966", ""]:
            html = self.assert_rejected("phone", phone=phone)
            self.assertIn("country code", html)
            self.assertIn("+966501234567", html)

    def test_phone_arabic_digits_and_00_prefix_accepted(self):
        for phone in ["٠٠٩٦٦٥٠١٢٣٤٥٦٧", "00966501234567", "+٩٦٦٥٠١٢٣٤٥٦٧"]:
            MerchantAccount.objects.all().delete()
            ApiToken.objects.filter(pk=self.a.pk).update(portal_key_hash=sha(self.key), portal_key_created_at=timezone.now())
            self.assertEqual(self.register(self.key, client=self.client_class(), phone=phone).status_code, 302, phone)
            self.assertEqual(MerchantAccount.objects.get().phone, GOOD_PHONE)

    def test_duplicate_phone_with_other_merchant_rejected(self):
        make_account(self.b, phone=GOOD_PHONE)
        html = self.assert_rejected("phone")
        self.assertIn("already used", html)

    def test_empty_and_blank_and_long_name_rejected(self):
        for name in ["", "   ", "x" * 121]:
            self.assert_rejected("name", name=name)

    def test_short_password(self):
        html = self.assert_rejected("password", password="Ab1!xyz", password_confirm="Ab1!xyz")
        self.assertIn("too short", html)

    def test_common_password(self):
        html = self.assert_rejected("password", password="password123", password_confirm="password123")
        self.assertIn("too common", html)

    def test_all_numeric_password_is_weak(self):
        html = self.assert_rejected("password", password="83749201566", password_confirm="83749201566")
        self.assertIn("too weak", html)

    def test_too_long_password(self):
        pw = "aB3$" * 40
        self.assert_rejected("password", password=pw, password_confirm=pw)

    def test_password_mismatch(self):
        self.assert_rejected("password_confirm", password_confirm=PW + "x")

    def test_several_errors_at_once_are_all_shown(self):
        resp = self.register(self.key, name="", phone="123", password="a", password_confirm="b")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.content.decode().count('class="field-error"'), 3)

    def test_error_page_keeps_name_and_phone_but_never_passwords(self):
        secret = "Qx-Secret-Pw-9917"
        resp = self.register(self.key, name="Layla", phone="0501234567", password=secret, password_confirm=secret + "z")
        html = resp.content.decode()
        self.assertIn('value="Layla"', html)
        self.assertIn('value="0501234567"', html)
        self.assertNotIn(secret, html)
        self.assertIn('type="password"', html)
        self.assertNotRegex(html, r'type="password"[^>]*value="[^"]')

    def test_django_english_messages_do_not_leak_into_arabic_ui(self):
        self.set_lang("ar")
        for pw in ["password123", "Ab1!xyz", "83749201566"]:
            html = self.register(self.key, password=pw, password_confirm=pw).content.decode()
            self.assertNotIn("This password", html)
            self.assertIn("كلمة المرور", html)

    def test_validation_error_is_not_an_invite_oracle(self):
        # دعوة غلط بتضل 404 حتى لو الحقول غلط
        self.assertEqual(self.client.post(self.enter_url("bad"), self.form(name="")).status_code, 404)

    def test_missing_fields_do_not_crash(self):
        self.assertEqual(self.client.post(self.enter_url(self.key), {}).status_code, 400)


class ResetPathTests(PortalBase):
    def test_reregistering_updates_reactivates_and_kills_old_sessions(self):
        old_client = self.client_class()
        old_acc = login_portal(old_client, self.a, name="Old", phone="+966500000099", password="Old-Password-77")
        self.assertTrue(self.portal_session_valid(old_client, self.a))
        MerchantAccount.objects.filter(pk=old_acc.pk).update(
            is_active=False, phone_verified=True, failed_logins=3, locked_until=timezone.now() + timedelta(hours=1)
        )

        key = self.invite(self.a)
        new_client = self.client_class()
        self.assertEqual(self.register(key, client=new_client).status_code, 302)

        self.assertEqual(MerchantAccount.objects.count(), 1)
        account = MerchantAccount.objects.get()
        self.assertEqual(account.pk, old_acc.pk)
        self.assertEqual((account.name, account.phone), ("Ahmad", GOOD_PHONE))
        self.assertTrue(account.is_active)
        self.assertFalse(account.phone_verified)
        self.assertEqual(account.failed_logins, 0)
        self.assertIsNone(account.locked_until)
        self.assertTrue(check_password(PW, account.password))
        self.assertFalse(check_password("Old-Password-77", account.password))
        # الجلسة القديمة ماتت، والجديدة شغّالة
        self.assertFalse(self.portal_session_valid(old_client, self.a))
        self.assertTrue(self.portal_session_valid(new_client, self.a))
        # ودخول بالقديم مرفوض، وبالجديد مقبول
        self.assertEqual(self.login_post("+966500000099", "Old-Password-77", client=self.client_class()).status_code, 400)
        self.assertEqual(self.login_post(client=self.client_class()).status_code, 302)

    def test_reset_with_same_password_still_kills_old_sessions(self):
        old_client = self.client_class()
        login_portal(old_client, self.a, phone=GOOD_PHONE, password=PW)
        self.register(self.invite(self.a))
        self.assertFalse(self.portal_session_valid(old_client, self.a))

    def test_page_warns_that_existing_account_gets_replaced(self):
        key = self.invite(self.a)
        self.assertNotIn("already has an account", self.enter(key).content.decode())
        make_account(self.a)
        self.assertIn("already has an account", self.enter(key).content.decode())

    def test_own_phone_can_be_kept_but_not_another_merchants(self):
        make_account(self.a, phone=GOOD_PHONE)
        make_account(self.b, phone="+966509999999")
        key = self.invite(self.a)
        self.assertEqual(self.register(key, phone="+966509999999").status_code, 400)
        self.assertEqual(self.register(key, phone=GOOD_PHONE).status_code, 302)


# ---------- الدخول ----------


class LoginPageTests(PortalBase):
    def test_get_renders_form_with_proper_attributes(self):
        resp = self.client.get(reverse("portal:login"))
        self.assertEqual(resp.status_code, 200)
        self.assert_headers(resp)
        html = resp.content.decode()
        self.assertIn('name="robots" content="noindex,nofollow"', html)
        self.assertIn('autocomplete="tel"', html)
        self.assertIn('inputmode="tel"', html)
        self.assertIn('autocomplete="current-password"', html)
        self.assertIn('type="password"', html)
        self.assertIn('<label for="phone">', html)
        self.assertIn('<label for="password">', html)
        self.assertIn("csrfmiddlewaretoken", html)
        self.assertIn("Ask the store provider for a new registration link", html)
        # ما في next ولا زر لغة بصفحة الدخول (ما في سطح لـ open redirect)
        self.assertEqual(html.count('name="next"'), 0)

    def test_register_page_attributes(self):
        key = self.invite(self.a)
        html = self.enter(key).content.decode()
        self.assertEqual(html.count('autocomplete="new-password"'), 2)
        self.assertIn('autocomplete="name"', html)
        self.assertIn('autocomplete="tel"', html)
        self.assertIn('inputmode="tel"', html)
        for field in ("name", "phone", "password", "password_confirm"):
            self.assertIn(f'<label for="{field}">', html)

    def test_register_page_explains_store_and_phone_use(self):
        self.set_lang("en")
        html = self.enter(self.invite(self.a)).content.decode()
        self.assertIn("creating the account for the store", html)
        self.assertIn("<strong>A</strong>", html)
        self.assertIn("sign in", html)
        self.assertIn("WhatsApp", html)

    def test_both_languages_and_directions(self):
        for lang, direction in (("ar", "rtl"), ("en", "ltr")):
            self.set_lang(lang)
            self.assertIn(f'lang="{lang}" dir="{direction}"', self.client.get(reverse("portal:login")).content.decode())
            html = self.enter(self.invite(self.a)).content.decode()
            self.assertIn(f'lang="{lang}" dir="{direction}"', html)

    def test_auth_pages_have_no_language_switch_but_follow_the_session_language(self):
        key = self.invite(self.a)
        for url in (reverse("portal:login"), reverse("portal:enter", args=[key])):
            html = self.client.get(url).content.decode()
            self.assertNotIn('class="lang-switch"', html)
            self.assertNotIn('name="next"', html)
            self.assertNotIn('class="brand-mark"', html)
        session = self.client.session
        session["lang"] = "en"
        session.save()
        html = self.client.get(reverse("portal:login")).content.decode()
        self.assertIn('lang="en" dir="ltr"', html)

    def test_logged_in_get_redirects_home(self):
        login_portal(self.client, self.a)
        self.assertRedirects(
            self.client.get(reverse("portal:login")), reverse("portal:home"), fetch_redirect_response=False
        )

    def test_stale_session_get_shows_form(self):
        acc = login_portal(self.client, self.a)
        MerchantAccount.objects.filter(pk=acc.pk).update(is_active=False)
        self.assertEqual(self.client.get(reverse("portal:login")).status_code, 200)

    def test_other_methods_not_allowed(self):
        self.assertEqual(self.client.put(reverse("portal:login")).status_code, 405)


class LoginTests(PortalBase):
    def test_success_sets_session_resets_counter_and_redirects_home(self):
        acc = self.account_a(failed_logins=3)
        self.client.get(reverse("portal:login"))
        s = self.client.session
        s["marker"] = 1
        s.save()
        before = self.client.cookies["sessionid"].value
        resp = self.login_post()
        self.assertRedirects(resp, reverse("portal:home"), fetch_redirect_response=False)
        self.assert_headers(resp)
        acc.refresh_from_db()
        self.assertEqual(acc.failed_logins, 0)
        self.assertIsNone(acc.locked_until)
        self.assertIsNotNone(acc.last_login_at)
        session = self.client.session
        self.assertEqual(session[portal_auth.SESSION_ACCOUNT], acc.pk)
        self.assertEqual(session[portal_auth.SESSION_FINGERPRINT], portal_auth.session_fingerprint(acc))
        self.assertNotEqual(before, self.client.cookies["sessionid"].value)
        self.assertEqual(session["marker"], 1)
        self.assertNotIn(PW, str(dict(session)))
        self.assertNotIn(acc.password, str(dict(session)))
        self.assertTrue(self.portal_session_valid(self.client, self.a))

    def test_phone_typed_in_other_formats_logs_in(self):
        self.account_a()
        for phone in ["00966 50 123 4567", "٠٠٩٦٦٥٠١٢٣٤٥٦٧", "+966-50-123-4567"]:
            self.assertEqual(self.login_post(phone=phone, client=self.client_class()).status_code, 302, phone)

    def test_no_next_param_open_redirect(self):
        self.account_a()
        resp = self.client.post(
            reverse("portal:login") + "?next=http://evil.example/",
            {"phone": GOOD_PHONE, "password": PW, "next": "http://evil.example/"},
        )
        self.assertEqual(resp["Location"], reverse("portal:home"))

    def test_generic_failure_identical_for_unknown_phone_and_wrong_password(self):
        self.account_a()
        wrong_pw = self.login_post(password="Wrong-Password-1")
        unknown = self.login_post(phone="+966509998888", password="Wrong-Password-1")
        self.assertEqual(wrong_pw.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("The phone number or password is incorrect.", wrong_pw.content.decode())
        self.assertEqual(
            strip_csrf(wrong_pw.content.decode()).replace(f'value="{GOOD_PHONE}"', 'value="P"'),
            strip_csrf(unknown.content.decode()).replace('value="+966509998888"', 'value="P"'),
        )
        for resp in (wrong_pw, unknown):
            self.assert_headers(resp)
            self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)

    def test_failure_message_is_translated(self):
        self.set_lang("ar")
        html = self.login_post(phone="+966509998888").content.decode()
        self.assertIn("رقم الهاتف أو كلمة المرور غير صحيحة", html)
        self.assertNotIn("incorrect", html)

    def test_malformed_phone_and_empty_fields_get_same_generic_failure(self):
        self.account_a()

        def norm(resp):
            return re.sub(r'(name="phone" value=")[^"]*"', r'\1P"', strip_csrf(resp.content.decode()))

        reference = norm(self.login_post(phone="+966509998888"))
        for phone, pw in [("abc", PW), ("", ""), ("0501234567", PW), (GOOD_PHONE, "")]:
            resp = self.login_post(phone=phone, password=pw)
            self.assertEqual(resp.status_code, 400, (phone, pw))
            self.assertEqual(norm(resp), reference, (phone, pw))

    def test_failed_login_keeps_phone_but_never_echoes_password(self):
        self.account_a()
        html = self.login_post(password="Wrong-Password-1").content.decode()
        self.assertIn(f'value="{GOOD_PHONE}"', html)
        self.assertNotIn("Wrong-Password-1", html)

    def test_inactive_account_and_inactive_merchant_get_generic_failure(self):
        acc = self.account_a()
        ok_body = strip_csrf(self.login_post(password="Wrong-Password-1").content.decode())
        MerchantAccount.objects.filter(pk=acc.pk).update(is_active=False)
        resp = self.login_post()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(strip_csrf(resp.content.decode()), ok_body)
        MerchantAccount.objects.filter(pk=acc.pk).update(is_active=True)
        ApiToken.objects.filter(pk=self.a.pk).update(is_active=False)
        resp = self.login_post()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(strip_csrf(resp.content.decode()), ok_body)
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)

    def test_unknown_phone_still_does_a_password_hash_check(self):
        with mock.patch.object(portal_auth, "check_password", wraps=check_password) as spy:
            self.login_post(phone="+966509998888", password="whatever-1")
            self.login_post(phone="garbage", password="whatever-1")
        self.assertEqual(spy.call_count, 2)
        for call in spy.call_args_list:
            self.assertEqual(call.args[1], portal_auth._dummy_hash())

    def test_dummy_hash_is_a_real_hash_of_unknown_password(self):
        dummy = portal_auth._dummy_hash()
        self.assertNotEqual(dummy, "")
        self.assertFalse(check_password(PW, dummy))
        self.assertFalse(check_password("", dummy))


class LockoutTests(PortalBase):
    def bad_logins(self, n, client=None):
        for _ in range(n):
            self.login_post(password="Wrong-Password-1", client=client or self.client_class())

    def test_five_failures_lock_and_correct_password_is_refused(self):
        acc = self.account_a()
        self.bad_logins(4)
        acc.refresh_from_db()
        self.assertEqual(acc.failed_logins, 4)
        self.assertIsNone(acc.locked_until)
        self.bad_logins(1)
        acc.refresh_from_db()
        self.assertIsNotNone(acc.locked_until)
        self.assertGreater(acc.locked_until, timezone.now() + timedelta(minutes=14))
        self.assertLess(acc.locked_until, timezone.now() + timedelta(minutes=16))
        locked_until = acc.locked_until
        resp = self.login_post()  # الكلمة الصح
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Too many attempts", resp.content.decode())
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)
        self.assert_headers(resp)
        acc.refresh_from_db()
        self.assertEqual(acc.locked_until, locked_until, "المحاولات أثناء القفل ما بتمدّده")

    def test_lock_message_is_translated_and_has_no_account_details(self):
        self.account_a()
        self.bad_logins(5)
        self.set_lang("ar")
        html = self.login_post().content.decode()
        self.assertIn("محاولات كتير", html)
        self.assertNotIn("Too many", html)

    def test_failures_from_different_sessions_accumulate(self):
        acc = self.account_a()
        self.bad_logins(5)  # كل محاولة من كلينت جديد
        acc.refresh_from_db()
        self.assertIsNotNone(acc.locked_until)

    def test_success_resets_counter(self):
        acc = self.account_a()
        self.bad_logins(4)
        self.assertEqual(self.login_post(client=self.client_class()).status_code, 302)
        acc.refresh_from_db()
        self.assertEqual(acc.failed_logins, 0)
        self.bad_logins(4)
        acc.refresh_from_db()
        self.assertIsNone(acc.locked_until)
        self.assertEqual(self.login_post(client=self.client_class()).status_code, 302)

    def test_lock_expires(self):
        acc = self.account_a()
        self.bad_logins(5)
        MerchantAccount.objects.filter(pk=acc.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.login_post().status_code, 302)
        acc.refresh_from_db()
        self.assertIsNone(acc.locked_until)
        self.assertEqual(acc.failed_logins, 0)

    def test_after_expiry_a_fresh_set_of_attempts_starts(self):
        acc = self.account_a()
        self.bad_logins(5)
        MerchantAccount.objects.filter(pk=acc.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        self.bad_logins(1)
        acc.refresh_from_db()
        self.assertEqual(acc.failed_logins, 1)
        self.assertEqual(self.login_post().status_code, 302)

    def test_lock_is_per_account(self):
        self.account_a()
        make_account(self.b, phone="+966508888888")
        self.bad_logins(5)
        self.assertEqual(self.login_post("+966508888888", DEFAULT_PASSWORD).status_code, 302)


class IpThrottleTests(PortalBase):
    def post_unknown(self, **extra):
        return self.login_post(phone="+966509998888", password="x-y-z-1", client=self.client_class(), **extra)

    def test_21st_post_in_window_is_throttled_even_with_good_credentials(self):
        self.account_a()
        for i in range(20):
            self.assertEqual(self.post_unknown().status_code, 400, i)
        resp = self.login_post()
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Too many attempts", resp.content.decode())
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, resp.wsgi_request.session)

    def test_throttle_message_matches_lock_message(self):
        self.account_a()
        for _ in range(20):
            self.post_unknown()
        throttled = strip_csrf(self.post_unknown().content.decode()).replace("+966509998888", "P")
        make_account(self.b, phone="+966508888888")
        for _ in range(5):
            self.login_post("+966508888888", "Wrong-Password-1", client=self.client_class(), REMOTE_ADDR="10.9.9.9")
        locked = self.login_post("+966508888888", DEFAULT_PASSWORD, client=self.client_class(), REMOTE_ADDR="10.9.9.9")
        self.assertEqual(throttled, strip_csrf(locked.content.decode()).replace("+966508888888", "P"))

    def test_other_ip_not_affected(self):
        self.account_a()
        for _ in range(21):
            self.post_unknown()
        self.assertEqual(self.login_post(client=self.client_class(), REMOTE_ADDR="10.1.2.3").status_code, 302)

    def test_x_forwarded_for_is_ignored(self):
        self.account_a()
        for i in range(20):
            self.post_unknown(HTTP_X_FORWARDED_FOR=f"9.9.9.{i}")
        resp = self.login_post(client=self.client_class(), HTTP_X_FORWARDED_FOR="8.8.8.8")
        self.assertEqual(resp.status_code, 429)

    def test_throttle_window_resets(self):
        self.account_a()
        for _ in range(21):
            self.post_unknown()
        cache.clear()  # بديل لانتهاء الـ 10 دقايق
        self.assertEqual(self.login_post(client=self.client_class()).status_code, 302)

    def test_get_requests_are_not_counted(self):
        self.account_a()
        for _ in range(30):
            self.client.get(reverse("portal:login"))
        self.assertEqual(self.login_post(client=self.client_class()).status_code, 302)


# ---------- portal_required والجلسة ----------


class PortalRequiredTests(PortalBase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.view = portal_auth.portal_required(_ok_view)

    def request(self, **session):
        req = self.factory.get("/x/")
        req.session = SessionStore()
        req.session.update(session)
        return req

    def test_no_session_redirects_to_login_not_expired(self):
        resp = self.view(self.request())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("portal:login"))
        self.assert_headers(resp)

    def test_valid_session_runs_view_with_headers_and_sets_account(self):
        acc = make_account(self.a)
        seen = {}

        def view(request):
            seen["account"], seen["merchant"] = request.portal_account, request.portal_merchant
            return HttpResponse("ok")

        req = self.request(**{portal_auth.SESSION_ACCOUNT: acc.pk, portal_auth.SESSION_FINGERPRINT: portal_auth.session_fingerprint(acc)})
        resp = portal_auth.portal_required(view)(req)
        self.assertEqual(resp.content, b"ok")
        self.assert_headers(resp)
        self.assertEqual((seen["account"].pk, seen["merchant"].pk), (acc.pk, self.a.pk))

    def test_bad_session_values_are_cleared_and_other_keys_kept(self):
        acc = make_account(self.a)
        fp = portal_auth.session_fingerprint(acc)
        for aid, f in [
            ("1", fp), (True, fp), (acc.pk, ""), (acc.pk, "zz"), (acc.pk, "ك"), (99999, fp), (None, None),
            (acc.pk, 123), (acc.pk, None), (str(acc.pk), fp), (acc.pk, fp.upper()),
        ]:
            req = self.request(**{portal_auth.SESSION_ACCOUNT: aid, portal_auth.SESSION_FINGERPRINT: f, "other": "keep"})
            resp = self.view(req)
            self.assertEqual(resp.status_code, 302, (aid, f))
            self.assertEqual(resp["Location"], reverse("portal:login"))
            self.assertNotIn(portal_auth.SESSION_ACCOUNT, req.session)
            self.assertNotIn(portal_auth.SESSION_FINGERPRINT, req.session)
            self.assertEqual(req.session["other"], "keep")

    def test_old_style_session_keys_grant_nothing(self):
        self.invite(self.a)
        req = self.request(portal_merchant_id=self.a.pk, portal_key_hash=self.a.portal_key_hash)
        self.assertEqual(self.view(req).status_code, 302)

    def test_fingerprint_of_other_account_rejected(self):
        acc_a, acc_b = make_account(self.a), make_account(self.b)
        req = self.request(**{portal_auth.SESSION_ACCOUNT: acc_a.pk, portal_auth.SESSION_FINGERPRINT: portal_auth.session_fingerprint(acc_b)})
        self.assertEqual(self.view(req).status_code, 302)

    def test_fingerprint_is_keyed_hmac_not_plain_hash(self):
        acc = make_account(self.a)
        fp = portal_auth.session_fingerprint(acc)
        self.assertNotEqual(fp, sha(acc.password))
        self.assertNotIn(acc.password, fp)
        self.assertEqual(len(fp), 64)
        with override_settings(SECRET_KEY="another-secret-key-for-test"):
            self.assertNotEqual(portal_auth.session_fingerprint(acc), fp)


class SessionLifecycleTests(PortalBase):
    def logged_in(self, merchant=None, **kw):
        merchant = merchant or self.a
        client = self.client_class()
        account = login_portal(client, merchant, **kw)
        self.assertTrue(self.portal_session_valid(client, merchant))
        return client, account

    def test_password_change_logs_out(self):
        client, acc = self.logged_in()
        acc.password = make_password("Brand-New-Pass-55")
        acc.save(update_fields=["password"])
        self.assertFalse(self.portal_session_valid(client, self.a))
        self.assertRedirects(
            client.get(reverse("portal:home")), reverse("portal:login"), fetch_redirect_response=False
        )

    def test_account_deactivation_logs_out(self):
        client, acc = self.logged_in()
        MerchantAccount.objects.filter(pk=acc.pk).update(is_active=False)
        self.assertFalse(self.portal_session_valid(client, self.a))

    def test_merchant_disabled_logs_out_and_reenable_restores_nothing_new_needed(self):
        client, _ = self.logged_in()
        ApiToken.objects.filter(pk=self.a.pk).update(is_active=False)
        self.assertFalse(self.portal_session_valid(client, self.a))

    def test_account_deleted_logs_out(self):
        client, acc = self.logged_in()
        acc.delete()
        self.assertFalse(self.portal_session_valid(client, self.a))

    def test_revoke_logs_out_and_login_refused(self):
        client, _ = self.logged_in(phone=GOOD_PHONE, password=PW)
        self.invite(self.a)
        portal_auth.revoke_portal_key(self.a)
        self.assertFalse(self.portal_session_valid(client, self.a))
        self.assertEqual(self.login_post(client=self.client_class()).status_code, 400)

    def test_new_invite_alone_does_not_log_out_existing_session(self):
        client, _ = self.logged_in()
        self.invite(self.a)
        self.assertTrue(self.portal_session_valid(client, self.a))

    def test_revoke_then_new_invite_then_register_reactivates(self):
        make_account(self.a, phone=GOOD_PHONE)
        portal_auth.revoke_portal_key(self.a)
        key = self.invite(self.a)
        self.assertEqual(self.register(key).status_code, 302)
        self.assertTrue(MerchantAccount.objects.get().is_active)
        self.assertEqual(self.login_post(client=self.client_class()).status_code, 302)

    def test_cross_merchant_isolation(self):
        client_a, acc_a = self.logged_in(self.a)
        client_b, acc_b = self.logged_in(self.b, phone="+966508888888")
        self.assertTrue(self.portal_session_valid(client_a, self.a))
        self.assertTrue(self.portal_session_valid(client_b, self.b))
        # تبديل id الحساب بجلسة A لحساب B ببصمة A → مرفوض
        s = client_a.session
        s[portal_auth.SESSION_ACCOUNT] = acc_b.pk
        s.save()
        self.assertFalse(self.portal_session_valid(client_a, self.a))
        self.assertTrue(self.portal_session_valid(client_b, self.b))

    def test_account_phone_is_unique_at_db_level(self):
        from django.db import IntegrityError, transaction

        make_account(self.a, phone=GOOD_PHONE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_account(self.b, phone=GOOD_PHONE)


class StaffSeparationTests(PortalBase):
    def test_merchant_session_cannot_access_staff_pages(self):
        login_portal(self.client, self.a)
        for url in [reverse("apitokens:dashboard"), reverse("apitokens:catalog", args=[self.a.pk]),
                    reverse("apitokens:tryon-stats", args=[self.a.pk])]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn("/admin/login/", resp["Location"])
        resp = self.client.post(reverse("apitokens:portal-link", args=[self.b.pk]), {"action": "generate"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])
        self.b.refresh_from_db()
        self.assertEqual(self.b.portal_key_hash, "")

    def test_staff_login_does_not_grant_portal_access(self):
        self.client.force_login(self.staff)
        make_account(self.a)
        self.assertRedirects(
            self.client.get(reverse("portal:home")), reverse("portal:login"), fetch_redirect_response=False
        )

    def test_portal_login_keeps_staff_login(self):
        self.client.force_login(self.staff)
        self.account_a()
        self.assertEqual(self.login_post().status_code, 302)
        self.assertEqual(self.client.get(reverse("apitokens:dashboard")).status_code, 200)

    def test_logout_only_drops_portal_keys_and_goes_to_login(self):
        self.client.force_login(self.staff)
        login_portal(self.client, self.a)
        s = self.client.session
        s["lang"] = "en"
        s.save()
        resp = self.client.post(reverse("portal:logout"))
        self.assertRedirects(resp, reverse("portal:login"), fetch_redirect_response=False)
        self.assert_headers(resp)
        s = self.client.session
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, s)
        self.assertNotIn(portal_auth.SESSION_FINGERPRINT, s)
        self.assertEqual(s["lang"], "en")
        self.assertEqual(self.client.get(reverse("apitokens:dashboard")).status_code, 200)
        self.assertRedirects(
            self.client.get(reverse("portal:home")), reverse("portal:login"), fetch_redirect_response=False
        )

    def test_logout_requires_post(self):
        self.assertEqual(self.client.get(reverse("portal:logout")).status_code, 405)

    def test_expired_route_is_gone(self):
        self.assertEqual(self.client.get("/portal/expired/").status_code, 404)


# ---------- CSRF بشكل المتصفح ----------


@FAST_HASHERS
class BrowserCsrfTests(TestCase):
    """المتصفح بيبعت Origin بكل POST. بنتأكد إنه CSRF مفروض بكل نماذج البوابة وإن same-origin بيمشي."""

    ORIGIN = "http://testserver"

    def setUp(self):
        cache.clear()
        self.merchant = ApiToken.objects.create(label="A", image_quota=1)
        self.client = Client(enforce_csrf_checks=True)

    def post(self, url, data, token=None, origin=ORIGIN):
        data = dict(data)
        if token:
            data["csrfmiddlewaretoken"] = token
        extra = {"HTTP_ORIGIN": origin} if origin else {}
        return self.client.post(url, data, **extra)

    def test_login_requires_csrf_token(self):
        make_account(self.merchant, phone=GOOD_PHONE, password=PW)
        url = reverse("portal:login")
        token = csrf_token(self.client.get(url).content.decode())
        creds = {"phone": GOOD_PHONE, "password": PW}
        self.assertEqual(self.post(url, creds).status_code, 403)
        self.assertEqual(self.post(url, creds, token="bad" * 20).status_code, 403)
        self.assertEqual(self.post(url, creds, token, origin="null").status_code, 403)
        self.assertEqual(self.post(url, creds, token, origin="http://evil.example").status_code, 403)
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)
        resp = self.post(url, creds, token)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("portal:home"))

    def test_failed_login_does_not_count_when_csrf_rejected(self):
        acc = make_account(self.merchant, phone=GOOD_PHONE, password=PW)
        url = reverse("portal:login")
        self.client.get(url)
        for _ in range(6):
            self.post(url, {"phone": GOOD_PHONE, "password": "nope-nope-1"})
        acc.refresh_from_db()
        self.assertEqual(acc.failed_logins, 0)

    def test_register_requires_csrf_token(self):
        key = portal_auth.generate_portal_key(self.merchant)
        url = reverse("portal:enter", args=[key])
        token = csrf_token(self.client.get(url).content.decode())
        data = {"name": "Ahmad", "phone": GOOD_PHONE, "password": PW, "password_confirm": PW}
        self.assertEqual(self.post(url, data).status_code, 403)
        self.assertEqual(self.post(url, data, token, origin="null").status_code, 403)
        self.assertFalse(MerchantAccount.objects.exists())
        self.merchant.refresh_from_db()
        self.assertTrue(self.merchant.portal_key_hash)
        resp = self.post(url, data, token)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(MerchantAccount.objects.filter(merchant=self.merchant).exists())

    def test_logout_and_accept_require_csrf_token(self):
        login_portal(self.client, self.merchant)
        token = csrf_token(self.client.get(reverse("portal:accept")).content.decode())
        self.assertEqual(self.post(reverse("portal:logout"), {}).status_code, 403)
        self.assertEqual(self.post(reverse("portal:accept"), {"agree": "1"}).status_code, 403)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.terms_accepted_version, "")
        self.assertIn(portal_auth.SESSION_ACCOUNT, self.client.session)
        self.assertEqual(self.post(reverse("portal:accept"), {"agree": "1"}, token).status_code, 302)
        self.merchant.refresh_from_db()
        self.assertEqual(self.merchant.terms_accepted_version, portal_auth.PORTAL_TERMS_VERSION)
        self.assertEqual(self.post(reverse("portal:logout"), {}, token).status_code, 302)
        self.assertNotIn(portal_auth.SESSION_ACCOUNT, self.client.session)

    def test_login_and_register_pages_never_send_no_referrer(self):
        key = portal_auth.generate_portal_key(self.merchant)
        for url in (reverse("portal:login"), reverse("portal:enter", args=[key])):
            self.assertEqual(self.client.get(url)["Referrer-Policy"], "same-origin")


# ---------- شاشة الستاف ----------


class StaffPortalLinkViewTests(PortalBase):
    def url(self, pk=None):
        return reverse("apitokens:portal-link", args=[pk or self.a.pk])

    def dashboard(self):
        return self.client.get(reverse("apitokens:dashboard")).content.decode()

    def test_requires_staff(self):
        resp = self.client.post(self.url(), {"action": "generate"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])
        self.client.force_login(get_user_model().objects.create_user("plain", password="x"))
        resp = self.client.post(self.url(), {"action": "generate"})
        self.assertIn("/admin/login/", resp["Location"])
        self.a.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, "")

    def test_requires_post(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url()).status_code, 405)

    def test_unknown_action_and_merchant(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self.url(), {"action": "x"}).status_code, 400)
        self.assertEqual(self.client.post(self.url(99999), {"action": "generate"}).status_code, 404)

    def test_generate_shows_link_once_and_it_opens_registration(self):
        self.client.force_login(self.staff)
        resp = self.client.post(self.url(), {"action": "generate"})
        self.assertRedirects(resp, reverse("apitokens:dashboard"), fetch_redirect_response=False)
        self.a.refresh_from_db()
        self.assertTrue(self.a.portal_key_hash)
        body = self.dashboard()
        self.assertIn('id="fresh-portal-link"', body)
        start = body.index("http://testserver/portal/enter/")
        link = body[start:body.index('"', start)]
        self.assertEqual(sha(link.split("/portal/enter/")[1].strip("/")), self.a.portal_key_hash)
        second = self.dashboard()
        self.assertNotIn("/portal/enter/", second)
        self.assertNotIn("new_portal_link", self.client.session)
        other = self.client_class()
        resp = other.get(link.replace("http://testserver", ""))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("csrfmiddlewaretoken", resp.content.decode())

    def test_generated_text_says_single_use_7_days(self):
        self.client.force_login(self.staff)
        self.client.post(self.url(), {"action": "generate"})
        body = self.dashboard()
        self.assertIn("Single-use, valid for 7 days", body)

    def test_regenerate_invalidates_old_and_revoke_clears_and_disables(self):
        self.client.force_login(self.staff)
        old = self.invite(self.a)
        self.client.post(self.url(), {"action": "generate"})
        self.assertEqual(self.client.get(self.enter_url(old)).status_code, 404)
        acc = make_account(self.a)
        self.client.post(self.url(), {"action": "revoke"})
        self.a.refresh_from_db()
        acc.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, "")
        self.assertFalse(acc.is_active)
        self.assertNotIn("new_portal_link", self.client.session)

    def test_buttons_follow_state(self):
        self.client.force_login(self.staff)
        html = self.dashboard()
        self.assertNotIn('value="revoke"', html)
        self.assertEqual(html.count('value="generate"'), 2)
        self.invite(self.a)
        self.assertEqual(self.dashboard().count('value="revoke"'), 1)
        portal_auth.revoke_portal_key(self.a)
        self.assertNotIn('value="revoke"', self.dashboard())  # لا دعوة ولا حساب فعّال
        make_account(self.a)
        self.assertEqual(self.dashboard().count('value="revoke"'), 1)

    def test_status_lines_for_every_state(self):
        self.client.force_login(self.staff)
        self.assertIn("No invite", self.dashboard())
        key = self.invite(self.a)
        until = timezone.localtime(self.a.portal_key_created_at + timedelta(days=7)).strftime("%Y-%m-%d")
        self.a.refresh_from_db()
        until = timezone.localtime(self.a.portal_key_created_at + timedelta(days=7)).strftime("%Y-%m-%d")
        self.assertIn(f"Invite pending (valid until {until})", self.dashboard())
        ApiToken.objects.filter(pk=self.a.pk).update(portal_key_created_at=timezone.now() - timedelta(days=8))
        html = self.dashboard()
        self.assertIn("Invite expired", html)
        self.assertNotIn("Invite pending", html)
        self.assertNotIn(key, html)

    def test_account_status_lines(self):
        self.client.force_login(self.staff)
        acc = make_account(self.a, name="Layla Owner", phone=GOOD_PHONE)
        html = self.dashboard()
        self.assertIn("Layla Owner", html)
        self.assertIn(GOOD_PHONE, html)
        self.assertIn("never logged in", html)
        self.assertIn("Has not agreed to the policy yet", html)
        when = timezone.now() - timedelta(days=2)
        MerchantAccount.objects.filter(pk=acc.pk).update(last_login_at=when)
        self.assertIn("last login " + timezone.localtime(when).strftime("%Y-%m-%d"), self.dashboard())
        MerchantAccount.objects.filter(pk=acc.pk).update(is_active=False)
        html = self.dashboard()
        self.assertIn("Account disabled", html)
        self.assertNotIn("last login", html)

    def test_status_lines_in_arabic(self):
        self.client.force_login(self.staff)
        self.set_lang("ar")
        self.assertIn("ما في دعوة", self.dashboard())
        self.invite(self.a)
        self.assertIn("دعوة بانتظار التسجيل", self.dashboard())

    def test_dashboard_never_leaks_hash_key_or_password(self):
        self.client.force_login(self.staff)
        self.client.post(self.url(), {"action": "generate"})
        self.a.refresh_from_db()
        acc = make_account(self.a, password="Staff-Must-Not-See-1")
        for _ in range(2):
            html = self.dashboard()
            self.assertNotIn(self.a.portal_key_hash, html)
            self.assertNotIn(acc.password, html)
            self.assertNotIn("Staff-Must-Not-See-1", html)
            self.assertNotIn("md5$", html)
            self.assertNotIn("pbkdf2", html)


class TranslationTests(TestCase):
    def test_login_and_register_keys_exist_in_both_languages(self):
        keys = [k for k in TRANSLATIONS if k.startswith(("portal_login_", "portal_register_"))]
        self.assertGreaterEqual(len(keys), 20)
        for key in keys:
            self.assertTrue(TRANSLATIONS[key]["ar"].strip() and TRANSLATIONS[key]["en"].strip(), key)
