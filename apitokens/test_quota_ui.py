"""واجهة الرصيد: بطاقة الرصيد وحد الزبون ببوابة التاجر، وأعمدة الرصيد واستهلاك الشات عند الستاف."""
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from . import portal_auth
from .i18n import TRANSLATIONS
from .models import ApiToken, LlmUsage
from .portal_auth import PORTAL_TERMS_VERSION
from .portal_testing import FAST_HASHERS, login_portal, make_account


def make_merchant(label="Shop A", quota=100, **kw):
    kw.setdefault("terms_accepted_version", PORTAL_TERMS_VERSION)
    return ApiToken.objects.create(label=label, image_quota=quota, **kw)


def csrf_token(html):
    return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html).group(1)


@FAST_HASHERS
class PortalCase(TestCase):
    def get_home(self, lang="en", client=None, **params):
        client = client or self.client
        session = client.session
        session["lang"] = lang
        session.save()
        return client.get(reverse("portal:home"), params)

    def html(self, lang="en"):
        resp = self.get_home(lang)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()


class QuotaCardTests(PortalCase):
    def test_fresh_store_shows_full_balance_without_banner(self):
        m = make_merchant(quota=100, period=ApiToken.Period.MONTHLY)
        login_portal(self.client, m)
        html = self.html()
        self.assertIn('<div class="quota-n"><bdi>100</bdi><span class="quota-of"> / <bdi>100</bdi></span></div>', html)
        self.assertIn('aria-valuenow="0"', html)
        self.assertIn("Used 0 of 100 (0%)", html)
        self.assertNotIn('id="quota-banner"', html)
        self.assertIn("Your credit renews on", html)
        expected = (m.period_start + timedelta(days=30)).astimezone(timezone.get_current_timezone()).strftime("%Y-%m-%d")
        self.assertIn(expected, html)
        self.assertIn("shared by all of your shoppers", html)

    def test_eighty_percent_shows_running_low_banner(self):
        m = make_merchant(quota=100, images_used=80)
        login_portal(self.client, m)
        html = self.html()
        self.assertIn("<bdi>20</bdi>", html)
        self.assertIn('aria-valuenow="80"', html)
        self.assertIn('class="quota-banner low"', html)
        self.assertIn("Your credit is running low", html)
        self.assertIn("used 80% of your credit and 20 are left", html)
        self.assertNotIn("is used up", html)

    def test_just_below_threshold_has_no_banner(self):
        m = make_merchant(quota=100, images_used=79)
        login_portal(self.client, m)
        self.assertNotIn('id="quota-banner"', self.html())

    def test_exhausted_shows_strong_banner_chat_keeps_working(self):
        m = make_merchant(quota=100, images_used=100)
        login_portal(self.client, m)
        html = self.html()
        self.assertIn('class="quota-banner exhausted"', html)
        self.assertIn('role="alert"', html)
        self.assertIn("Your try-on credit is used up", html)
        self.assertIn("Try-on is paused for your shoppers until your credit renews", html)
        self.assertIn("chat", html.split('id="quota-banner"')[1].split("</div>")[0])
        self.assertIn('<div class="quota-n"><bdi>0</bdi>', html)
        self.assertIn('aria-valuenow="100"', html)

    def test_expired_period_renews_with_next_use_and_shows_full_balance(self):
        m = make_merchant(quota=100, images_used=100, period=ApiToken.Period.MONTHLY)
        ApiToken.objects.filter(pk=m.pk).update(period_start=timezone.now() - timedelta(days=40))
        login_portal(self.client, m)
        html = self.html()
        self.assertIn('<div class="quota-n"><bdi>100</bdi>', html)
        self.assertIn("renews in full with the next try-on", html)
        self.assertNotIn("Your credit renews on", html)
        self.assertNotIn('id="quota-banner"', html)
        self.assertIn("Used 0 of 100 (0%)", html)

    def test_daily_cap_shown_for_multi_day_periods(self):
        m = make_merchant(quota=100, period=ApiToken.Period.MONTHLY)
        login_portal(self.client, m)
        html = self.html()
        self.assertIn("Today&#x27;s try-ons (last 24 hours): 0 of 20", html)
        self.assertIn('id="quota-daily"', html)

    def test_daily_pool_has_no_daily_line(self):
        m = make_merchant(quota=100, period=ApiToken.Period.DAILY)
        login_portal(self.client, m)
        html = self.html()
        self.assertNotIn('id="quota-daily"', html)
        self.assertNotIn("try-ons (last 24 hours)", html)

    def test_reserved_credit_counts_against_remaining(self):
        from .models import QuotaReservation

        m = make_merchant(quota=100, images_used=10)
        QuotaReservation.objects.create(merchant=m, expires_at=timezone.now() + timedelta(minutes=3))
        login_portal(self.client, m)
        self.assertIn('<div class="quota-n"><bdi>89</bdi>', self.html())

    def test_arabic_page_uses_arabic_copy(self):
        m = make_merchant(quota=100, images_used=100)
        login_portal(self.client, m)
        html = self.html("ar")
        self.assertIn("خلص رصيد تجربة اللبس", html)
        self.assertIn("رصيدك حوض واحد مشترك بين كل زبائن متجرك", html)
        self.assertIn("المساعد الذكي (الشات) بيضل شغّال", html)

    def test_merchants_never_see_each_others_numbers(self):
        a = make_merchant("Shop A", quota=100, images_used=7)
        b = make_merchant("Shop B Secret", quota=7654, images_used=4321, tryons_per_shopper=10)
        login_portal(self.client, a)
        html = self.html()
        self.assertIn("Used 7 of 100", html)
        for leaked in ("7654", "4321", "Shop B Secret", "3333"):
            self.assertNotIn(leaked, html)
        # حتى لو حاول يمرّر merchant/pk بالرابط
        resp = self.client.get(reverse("portal:home"), {"merchant": b.pk, "pk": b.pk, "id": b.pk})
        self.assertNotIn("7654", resp.content.decode())
        self.assertIn("Used 7 of 100", resp.content.decode())

    def test_no_internal_alert_or_staff_data_in_page(self):
        m = make_merchant(quota=100, images_used=90)
        LlmUsage.objects.create(merchant=m, day=timezone.localdate(), calls=9, input_tokens=111111, output_tokens=222222)
        login_portal(self.client, m)
        html = self.html()
        for hidden in ("quota_alert", "LlmUsage", "333333", "111111", "222222", "LIGHTOTP"):
            self.assertNotIn(hidden, html)

    def test_home_headers_stay_hardened(self):
        login_portal(self.client, make_merchant())
        resp = self.get_home()
        self.assertEqual(resp["Referrer-Policy"], "same-origin")
        self.assertEqual(resp["Cache-Control"], "no-store")


class ShopperLimitFormTests(PortalCase):
    def setUp(self):
        self.m = make_merchant("Shop A", tryons_per_shopper=3)
        self.other = make_merchant("Shop B", tryons_per_shopper=3)
        login_portal(self.client, self.m)
        self.url = reverse("portal:settings")

    def post(self, value, **extra):
        return self.client.post(self.url, {"tryons_per_shopper": value, **extra})

    def limit(self, merchant=None):
        merchant = merchant or self.m
        merchant.refresh_from_db()
        return merchant.tryons_per_shopper

    def test_route_is_under_portal(self):
        self.assertEqual(self.url, "/portal/settings/")

    def test_every_valid_choice_is_saved(self):
        for value in (1, 3, 5, 10, 0, 5):
            resp = self.post(str(value))
            self.assertRedirects(resp, reverse("portal:home") + "?saved=1", fetch_redirect_response=False)
            self.assertEqual(self.limit(), value)

    def test_invalid_values_change_nothing_and_flag_error(self):
        for bad in ("7", "-1", "abc", "", "3.0", "11", "٣", " 5", "5 ", "1e1", "99999999999", "null"):
            resp = self.post(bad)
            self.assertRedirects(resp, reverse("portal:home") + "?saved=error", fetch_redirect_response=False, msg_prefix=repr(bad))
            self.assertEqual(self.limit(), 3, repr(bad))

    def test_missing_field_is_an_error(self):
        resp = self.client.post(self.url, {})
        self.assertRedirects(resp, reverse("portal:home") + "?saved=error", fetch_redirect_response=False)
        self.assertEqual(self.limit(), 3)

    def test_only_own_merchant_changes_even_with_spoofed_params(self):
        resp = self.client.post(
            self.url,
            {"tryons_per_shopper": "10", "merchant": self.other.pk, "pk": self.other.pk, "id": self.other.pk,
             "token": self.other.token, "image_quota": "999999", "images_used": "0"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.limit(), 10)
        self.assertEqual(self.limit(self.other), 3)
        self.m.refresh_from_db()
        self.assertEqual(self.m.image_quota, 100)

    def test_save_does_not_overwrite_concurrent_usage(self):
        # صفحة قديمة بالذاكرة؛ الاستهلاك زاد بالقاعدة. الحفظ لازم ما يرجّع الرقم القديم.
        ApiToken.objects.filter(pk=self.m.pk).update(images_used=42)
        self.post("5")
        self.m.refresh_from_db()
        self.assertEqual((self.m.images_used, self.m.tryons_per_shopper), (42, 5))

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.limit(), 3)

    def test_unauthenticated_redirects_to_login_and_changes_nothing(self):
        anon = Client()
        resp = anon.post(self.url, {"tryons_per_shopper": "10"})
        self.assertRedirects(resp, reverse("portal:login"), fetch_redirect_response=False)
        self.assertEqual(self.limit(), 3)
        resp = anon.get(self.url)
        self.assertRedirects(resp, reverse("portal:login"), fetch_redirect_response=False)

    def test_unverified_account_goes_to_verify(self):
        c = Client()
        m = make_merchant("Unverified")
        login_portal(c, m, verified=False)
        resp = c.post(self.url, {"tryons_per_shopper": "10"})
        self.assertRedirects(resp, reverse("portal:verify"), fetch_redirect_response=False)
        m.refresh_from_db()
        self.assertEqual(m.tryons_per_shopper, 3)

    def test_terms_gate_applies(self):
        c = Client()
        m = make_merchant("No terms", terms_accepted_version="")
        login_portal(c, m)
        resp = c.post(self.url, {"tryons_per_shopper": "10"})
        self.assertRedirects(resp, reverse("portal:accept"), fetch_redirect_response=False)
        m.refresh_from_db()
        self.assertEqual(m.tryons_per_shopper, 3)

    def test_revoked_account_cannot_post(self):
        portal_auth.revoke_portal_key(self.m)
        resp = self.post("10")
        self.assertRedirects(resp, reverse("portal:login"), fetch_redirect_response=False)
        self.assertEqual(self.limit(), 3)

    def test_response_headers_hardened(self):
        resp = self.post("5")
        self.assertEqual(resp["Referrer-Policy"], "same-origin")
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_form_lists_choices_with_current_selected(self):
        ApiToken.objects.filter(pk=self.m.pk).update(tryons_per_shopper=5)
        html = self.html()
        self.assertIn('action="/portal/settings/"', html)
        self.assertIn('name="csrfmiddlewaretoken"', html)
        for value in ("1", "3", "5", "10", "0"):
            self.assertRegex(html, rf'<option value="{value}"[^>]*>')
        self.assertIn('<option value="5" selected>5</option>', html)
        self.assertNotIn('<option value="3" selected>', html)
        self.assertIn('<option value="0">Unlimited</option>', html)
        self.assertIn("how many successful try-ons a single shopper can make in 24 hours", html)

    def test_unlimited_is_preselected_when_zero(self):
        ApiToken.objects.filter(pk=self.m.pk).update(tryons_per_shopper=0)
        self.assertIn('<option value="0" selected>Unlimited</option>', self.html())

    def test_value_outside_list_is_shown_disabled_not_submittable(self):
        ApiToken.objects.filter(pk=self.m.pk).update(tryons_per_shopper=2)
        html = self.html()
        self.assertIn('<option value="2" selected disabled>2</option>', html)

    def test_saved_flag_shows_confirmation_only_when_asked(self):
        self.assertNotIn('id="quota-saved"', self.html())
        resp = self.get_home(saved="1")
        self.assertContains(resp, 'id="quota-saved"')
        self.assertContains(resp, "Setting saved.")
        self.assertNotContains(resp, 'id="quota-save-error"')

    def test_error_flag_shows_error(self):
        resp = self.get_home(saved="error")
        self.assertContains(resp, 'id="quota-save-error"')
        self.assertContains(resp, "nothing was changed")
        self.assertNotContains(resp, 'id="quota-saved"')

    def test_unknown_saved_flag_is_ignored_and_not_reflected(self):
        resp = self.get_home(saved="<script>alert(1)</script>")
        self.assertNotContains(resp, "<script>alert(1)</script>")
        self.assertNotContains(resp, 'id="quota-saved"')
        self.assertNotContains(resp, 'id="quota-save-error"')

    def test_no_django_messages_framework_used(self):
        self.post("5")
        resp = self.client.get(reverse("portal:home"))
        self.assertEqual(list(resp.context["messages"]) if "messages" in resp.context else [], [])


@FAST_HASHERS
class ShopperLimitBrowserCsrfTests(TestCase):
    """المتصفح بيبعت Origin بكل POST: CSRF لازم يكون مفروض على نموذج الإعداد."""

    ORIGIN = "http://testserver"

    def setUp(self):
        self.m = make_merchant("Shop A", tryons_per_shopper=3)
        self.client = Client(enforce_csrf_checks=True)
        login_portal(self.client, self.m)
        self.url = reverse("portal:settings")
        self.token = csrf_token(self.client.get(reverse("portal:home")).content.decode())

    def post(self, token=None, origin=ORIGIN, value="10"):
        data = {"tryons_per_shopper": value}
        if token:
            data["csrfmiddlewaretoken"] = token
        extra = {"HTTP_ORIGIN": origin} if origin else {}
        return self.client.post(self.url, data, **extra)

    def limit(self):
        self.m.refresh_from_db()
        return self.m.tryons_per_shopper

    def test_same_origin_with_token_is_accepted(self):
        resp = self.post(self.token)
        self.assertRedirects(resp, reverse("portal:home") + "?saved=1", fetch_redirect_response=False)
        self.assertEqual(self.limit(), 10)

    def test_missing_or_bad_token_is_rejected(self):
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.post("bad" * 20).status_code, 403)
        self.assertEqual(self.limit(), 3)

    def test_null_and_foreign_origin_rejected(self):
        self.assertEqual(self.post(self.token, origin="null").status_code, 403)
        self.assertEqual(self.post(self.token, origin="http://evil.example").status_code, 403)
        self.assertEqual(self.limit(), 3)


class StaffDashboardQuotaTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(self.staff)

    def html(self, lang="en"):
        session = self.client.session
        session["lang"] = lang
        session.save()
        resp = self.client.get(reverse("apitokens:dashboard"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def row(self, html, label):
        rows = re.findall(r"<tr>.*?</tr>", html, flags=re.S)
        return next(r for r in rows if f"<td>{label}</td>" in r)

    def test_columns_and_per_store_numbers(self):
        m = make_merchant("Alpha", quota=200, images_used=50, tryons_per_shopper=5, period=ApiToken.Period.MONTHLY)
        make_merchant("Beta", quota=40, images_used=40, tryons_per_shopper=0)
        html = self.html()
        self.assertIn("<th>Credit</th>", html)
        self.assertIn("<th>Chat (last 30 days)</th>", html)
        a = self.row(html, "Alpha")
        self.assertIn("50 of 200 used", a)
        self.assertIn("150 left", a)
        self.assertIn("5 / day", a)
        self.assertIn((m.period_start + timedelta(days=30)).astimezone(timezone.get_current_timezone()).strftime("%Y-%m-%d"), a)
        b = self.row(html, "Beta")
        self.assertIn("40 of 40 used", b)
        self.assertIn("0 left", b)
        self.assertIn("Unlimited", b)

    def test_expired_period_counts_as_unused(self):
        m = make_merchant("Old", quota=30, images_used=30, period=ApiToken.Period.WEEKLY)
        ApiToken.objects.filter(pk=m.pk).update(period_start=timezone.now() - timedelta(days=9))
        row = self.row(self.html(), "Old")
        self.assertIn("0 of 30 used", row)
        self.assertIn("30 left", row)
        self.assertIn("Renews on next use", row)

    def test_chat_usage_is_summed_over_last_30_days_only(self):
        m = make_merchant("Alpha")
        other = make_merchant("Beta")
        today = timezone.localdate()
        LlmUsage.objects.create(merchant=m, day=today, calls=10, input_tokens=1000, output_tokens=500)
        LlmUsage.objects.create(merchant=m, day=today - timedelta(days=20), calls=5, input_tokens=200, output_tokens=100)
        LlmUsage.objects.create(merchant=m, day=today - timedelta(days=45), calls=999, input_tokens=9999999, output_tokens=1)
        LlmUsage.objects.create(merchant=other, day=today, calls=3, input_tokens=0, output_tokens=0)
        html = self.html()
        a = self.row(html, "Alpha")
        self.assertIn("15 calls", a)
        self.assertIn("1800 tokens", a)
        self.assertNotIn("999", a)
        b = self.row(html, "Beta")
        self.assertIn("3 calls", b)
        self.assertNotIn("tokens", b)

    def test_store_without_chat_usage_says_so(self):
        make_merchant("Quiet")
        self.assertIn("No usage", self.row(self.html(), "Quiet"))

    def test_arabic_labels(self):
        make_merchant("Alpha")
        html = self.html("ar")
        self.assertIn("<th>الرصيد</th>", html)
        self.assertIn("الشات (آخر 30 يوم)", html)

    def test_existing_buttons_still_render(self):
        m = make_merchant("Alpha")
        html = self.row(self.html(), "Alpha")
        for name in ("apitokens:catalog", "apitokens:discount-codes", "apitokens:tryon-stats", "apitokens:toggle",
                     "apitokens:portal-link"):
            self.assertIn(reverse(name, args=[m.pk]), html)

    def test_query_count_does_not_grow_with_stores(self):
        counter = iter(range(1000))

        def build(n):
            for _ in range(n):
                i = next(counter)
                s = make_merchant(f"Store {i}")
                LlmUsage.objects.create(merchant=s, day=timezone.localdate(), calls=1, input_tokens=1, output_tokens=1)
                make_account(s, phone=f"+96655000{i:04d}")

        build(1)
        baseline = self._count()
        build(6)
        self.assertEqual(self._count(), baseline)

    def _count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(reverse("apitokens:dashboard")).status_code, 200)
        return len(ctx)


class QuotaStringsTests(TestCase):
    def test_every_quota_key_has_both_languages_and_same_placeholders(self):
        keys = [k for k in TRANSLATIONS if k.startswith(("portal_quota_", "portal_admin_quota_"))]
        self.assertGreaterEqual(len(keys), 30)
        for key in keys:
            entry = TRANSLATIONS[key]
            self.assertTrue(entry.get("ar") and entry.get("en"), key)
            self.assertEqual(sorted(re.findall(r"\{\w+\}", entry["ar"])), sorted(re.findall(r"\{\w+\}", entry["en"])), key)
