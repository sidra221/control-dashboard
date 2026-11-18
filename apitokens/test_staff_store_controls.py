"""Staff store controls: per-store settings page, top-up / new-period actions, dashboard search and
filter, and the MerchantAccount admin (never showing the password hash)."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .i18n import TRANSLATIONS
from .models import ApiToken, MerchantAccount
from .portal_testing import FAST_HASHERS, login_portal, make_account


def make_store(label="Shop", **kw):
    kw.setdefault("image_quota", 100)
    return ApiToken.objects.create(label=label, **kw)


def valid_post(**overrides):
    data = {
        "label": "New Name",
        "image_quota": "250",
        "period": "weekly",
        "currency": "sar",
        "tryons_per_shopper": "5",
        "store_info": "Free shipping over 200 SAR.",
    }
    data.update(overrides)
    return data


def msgs(resp):
    return [str(m) for m in get_messages(resp.wsgi_request)]


@FAST_HASHERS
class StaffBase(TestCase):
    def setUp(self):
        self.store = make_store("Alpha Store", images_used=40)
        self.staff = get_user_model().objects.create_user("staff", password="x", is_staff=True)
        self.client.force_login(self.staff)
        session = self.client.session
        session["lang"] = "en"
        session.save()

    def settings_url(self, store=None):
        return reverse("apitokens:store-settings", args=[(store or self.store).pk])

    def topup_url(self):
        return reverse("apitokens:store-topup", args=[self.store.pk])

    def new_period_url(self):
        return reverse("apitokens:store-new-period", args=[self.store.pk])


class AccessTests(StaffBase):
    def urls(self):
        return [
            ("get", self.settings_url()),
            ("post", self.settings_url()),
            ("post", self.topup_url()),
            ("post", self.new_period_url()),
        ]

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        for method, url in self.urls():
            resp = getattr(self.client, method)(url, valid_post(amount="5"))
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn(reverse("admin:login"), resp["Location"])
        self.store.refresh_from_db()
        self.assertEqual((self.store.label, self.store.images_used), ("Alpha Store", 40))

    def test_merchant_portal_session_cannot_access(self):
        self.client.logout()
        login_portal(self.client, self.store)
        for method, url in self.urls():
            resp = getattr(self.client, method)(url, valid_post(amount="5"))
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn(reverse("admin:login"), resp["Location"])
        self.store.refresh_from_db()
        self.assertEqual((self.store.label, self.store.images_used), ("Alpha Store", 40))

    def test_non_staff_user_cannot_access(self):
        user = get_user_model().objects.create_user("plain", password="x")
        self.client.force_login(user)
        resp = self.client.get(self.settings_url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("admin:login"), resp["Location"])

    def test_actions_reject_get(self):
        self.assertEqual(self.client.get(self.topup_url()).status_code, 405)
        self.assertEqual(self.client.get(self.new_period_url()).status_code, 405)

    def test_unknown_store_404(self):
        self.assertEqual(self.client.get(reverse("apitokens:store-settings", args=[99999])).status_code, 404)

    def test_csrf_enforced(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        self.assertEqual(client.post(self.settings_url(), valid_post()).status_code, 403)
        self.assertEqual(client.post(self.new_period_url()).status_code, 403)

    def test_settings_page_renders_and_dashboard_links_it(self):
        resp = self.client.get(self.settings_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="Alpha Store"')
        self.assertContains(resp, reverse("apitokens:store-topup", args=[self.store.pk]))
        self.assertContains(resp, reverse("apitokens:store-new-period", args=[self.store.pk]))
        dash = self.client.get(reverse("apitokens:dashboard"))
        self.assertContains(dash, self.settings_url())


class SettingsSaveTests(StaffBase):
    def test_all_fields_saved_with_prg(self):
        resp = self.client.post(self.settings_url(), valid_post())
        self.assertRedirects(resp, self.settings_url(), fetch_redirect_response=False)
        self.store.refresh_from_db()
        self.assertEqual(self.store.label, "New Name")
        self.assertEqual(self.store.image_quota, 250)
        self.assertEqual(self.store.period, "weekly")
        self.assertEqual(self.store.currency, "SAR")
        self.assertEqual(self.store.tryons_per_shopper, 5)
        self.assertEqual(self.store.store_info, "Free shipping over 200 SAR.")
        self.assertEqual(self.store.images_used, 40)  # usage is untouched by a settings save
        self.assertIn(TRANSLATIONS["store_settings_msg_saved"]["en"], msgs(resp))

    def test_each_field_saved_individually(self):
        base = {
            "label": "Alpha Store", "image_quota": "100", "period": "monthly", "currency": "",
            "tryons_per_shopper": "3", "store_info": "",
        }
        cases = [
            ("label", "Renamed", "Renamed"),
            ("image_quota", "7", 7),
            ("period", "daily", "daily"),
            ("currency", " usd ", "USD"),
            ("currency", "", ""),
            ("tryons_per_shopper", "0", 0),
            ("tryons_per_shopper", "10", 10),
            ("store_info", "Returns within 14 days", "Returns within 14 days"),
        ]
        for field, raw, expected in cases:
            with self.subTest(field=field, raw=raw):
                data = dict(base, **{field: raw})
                resp = self.client.post(self.settings_url(), data)
                self.assertEqual(resp.status_code, 302)
                self.store.refresh_from_db()
                self.assertEqual(getattr(self.store, field), expected)
                base[field] = raw

    def test_unchanged_submission_reports_no_changes(self):
        self.store.currency = "SAR"
        self.store.save()
        data = valid_post(label="Alpha Store", image_quota="100", period="monthly", tryons_per_shopper="3", store_info="")
        resp = self.client.post(self.settings_url(), data)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(TRANSLATIONS["store_settings_msg_unchanged"]["en"], msgs(resp))

    def test_change_is_logged_with_staff_and_old_new_values(self):
        with self.assertLogs("apitokens.staff_store_controls", level="INFO") as logs:
            self.client.post(self.settings_url(), valid_post(store_info="secret policy text"))
        line = "\n".join(logs.output)
        self.assertIn(f"staff_user={self.staff.pk}", line)
        self.assertIn(f"store={self.store.pk}", line)
        self.assertIn("image_quota: 100 -> 250", line)
        self.assertIn("label: 'Alpha Store' -> 'New Name'", line)
        self.assertNotIn("secret policy text", line)  # store_info content is not logged

    def test_legacy_per_shopper_value_can_be_kept(self):
        ApiToken.objects.filter(pk=self.store.pk).update(tryons_per_shopper=7)
        resp = self.client.post(self.settings_url(), valid_post(tryons_per_shopper="7"))
        self.assertEqual(resp.status_code, 302)
        self.store.refresh_from_db()
        self.assertEqual(self.store.tryons_per_shopper, 7)


class SettingsValidationTests(StaffBase):
    def assert_rejected(self, key, **overrides):
        resp = self.client.post(self.settings_url(), valid_post(**overrides))
        self.assertEqual(resp.status_code, 400)
        expected = TRANSLATIONS[key]["en"].split("{")[0]
        self.assertTrue(any(m.startswith(expected) for m in msgs(resp)), msgs(resp))
        self.store.refresh_from_db()
        self.assertEqual(self.store.label, "Alpha Store")  # nothing saved on any error
        self.assertEqual(self.store.image_quota, 100)
        return resp

    def test_label_required(self):
        self.assert_rejected("store_settings_err_label_required", label="   ")

    def test_label_too_long(self):
        self.assert_rejected("store_settings_err_label_too_long", label="x" * 121)

    def test_bad_quotas(self):
        for raw in ("0", "-5", "abc", "", "1.5", "١٠", "1000001"):
            with self.subTest(raw=raw):
                self.assert_rejected("store_settings_err_quota", image_quota=raw)

    def test_bad_period(self):
        self.assert_rejected("store_settings_err_period", period="yearly")

    def test_bad_currency(self):
        for raw in ("US", "USDX", "12A", "ريال"):
            with self.subTest(raw=raw):
                self.assert_rejected("store_settings_err_currency", currency=raw)

    def test_bad_per_shopper(self):
        for raw in ("2", "-1", "", "unlimited"):
            with self.subTest(raw=raw):
                self.assert_rejected("store_settings_err_per_shopper", tryons_per_shopper=raw)

    def test_store_info_too_long(self):
        self.assert_rejected("store_settings_err_store_info_too_long", store_info="x" * 8001)

    def test_error_rerender_keeps_submitted_values(self):
        resp = self.assert_rejected("store_settings_err_period", period="yearly", label="Typed Name")
        self.assertContains(resp, 'value="Typed Name"', status_code=400)


class TopUpTests(StaffBase):
    def test_topup_lowers_images_used(self):
        with self.assertLogs("apitokens.staff_store_controls", level="INFO") as logs:
            resp = self.client.post(self.topup_url(), {"amount": "15"})
        self.assertRedirects(resp, self.settings_url(), fetch_redirect_response=False)
        self.store.refresh_from_db()
        self.assertEqual(self.store.images_used, 25)
        self.assertEqual(self.store.image_quota, 100)  # recurring quota unchanged
        self.assertIn("images_used: 40 -> 25", "\n".join(logs.output))
        self.assertIn(f"staff_user={self.staff.pk}", "\n".join(logs.output))

    def test_topup_never_below_zero(self):
        resp = self.client.post(self.topup_url(), {"amount": "500"})
        self.assertEqual(resp.status_code, 302)
        self.store.refresh_from_db()
        self.assertEqual(self.store.images_used, 0)
        self.assertTrue(any("(40)" in m for m in msgs(resp)))  # capped warning

    def test_topup_with_nothing_used(self):
        ApiToken.objects.filter(pk=self.store.pk).update(images_used=0)
        resp = self.client.post(self.topup_url(), {"amount": "5"})
        self.assertIn(TRANSLATIONS["store_settings_err_topup_nothing_used"]["en"], msgs(resp))

    def test_topup_on_expired_period_does_nothing(self):
        ApiToken.objects.filter(pk=self.store.pk).update(period_start=timezone.now() - timedelta(days=31))
        resp = self.client.post(self.topup_url(), {"amount": "5"})
        self.assertIn(TRANSLATIONS["store_settings_err_topup_period_expired"]["en"], msgs(resp))
        self.store.refresh_from_db()
        self.assertEqual(self.store.images_used, 40)

    def test_topup_invalid_amounts(self):
        for raw in ("0", "-3", "", "x", "2.5", "1000001"):
            with self.subTest(raw=raw):
                resp = self.client.post(self.topup_url(), {"amount": raw})
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(any(m.startswith("The number of images") for m in msgs(resp)))
        self.store.refresh_from_db()
        self.assertEqual(self.store.images_used, 40)


class NewPeriodTests(StaffBase):
    def test_new_period_resets_usage_and_start(self):
        old_start = timezone.now() - timedelta(days=10)
        ApiToken.objects.filter(pk=self.store.pk).update(period_start=old_start)
        before = timezone.now()
        with self.assertLogs("apitokens.staff_store_controls", level="INFO") as logs:
            resp = self.client.post(self.new_period_url())
        self.assertRedirects(resp, self.settings_url(), fetch_redirect_response=False)
        self.store.refresh_from_db()
        self.assertEqual(self.store.images_used, 0)
        self.assertGreaterEqual(self.store.period_start, before)
        self.assertIn("images_used: 40 -> 0", "\n".join(logs.output))
        self.assertIn(TRANSLATIONS["store_settings_msg_new_period"]["en"], msgs(resp))


class DashboardSearchTests(StaffBase):
    def setUp(self):
        super().setUp()
        self.beta = make_store("Beta Boutique")
        self.gamma = make_store("gamma shoes", is_active=False)

    def labels(self, **params):
        resp = self.client.get(reverse("apitokens:dashboard"), params)
        self.assertEqual(resp.status_code, 200)
        return {tok.label for tok in resp.context["tokens"]}, resp

    def test_no_filter_lists_all(self):
        labels, _ = self.labels()
        self.assertEqual(labels, {"Alpha Store", "Beta Boutique", "gamma shoes"})

    def test_search_label_case_insensitive(self):
        self.assertEqual(self.labels(q="BOUTIQUE")[0], {"Beta Boutique"})
        self.assertEqual(self.labels(q="Gamma")[0], {"gamma shoes"})

    def test_search_token_prefix_case_insensitive(self):
        prefix = self.beta.token[:8]
        self.assertIn("Beta Boutique", self.labels(q=prefix.swapcase())[0])
        self.assertIn("Beta Boutique", self.labels(q=prefix)[0])

    def test_status_filter(self):
        self.assertEqual(self.labels(status="active")[0], {"Alpha Store", "Beta Boutique"})
        self.assertEqual(self.labels(status="inactive")[0], {"gamma shoes"})
        self.assertEqual(len(self.labels(status="bogus")[0]), 3)

    def test_search_and_filter_combined_and_empty_state(self):
        self.assertEqual(self.labels(q="shoes", status="active")[0], set())
        _, resp = self.labels(q="nothing-matches")
        self.assertContains(resp, TRANSLATIONS["stores_search_empty"]["en"])

    def test_stats_ignore_filter(self):
        _, resp = self.labels(q="Beta")
        self.assertEqual(resp.context["stats"], {"total": 3, "active": 2, "images_used": 40})


@FAST_HASHERS
class MerchantAccountAdminTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("root", "root@example.com", "x")
        self.client.force_login(self.admin)
        self.store = make_store("Admin Shop")
        self.account = make_account(self.store, name="Owner Name", phone="+966512345678")

    def test_changelist_and_change_page_hide_hash(self):
        account = MerchantAccount.objects.get(pk=self.account.pk)
        self.assertTrue(account.password)
        changelist = self.client.get(reverse("admin:apitokens_merchantaccount_changelist"))
        self.assertEqual(changelist.status_code, 200)
        self.assertContains(changelist, "+966512345678")
        self.assertContains(changelist, "Owner Name")
        change = self.client.get(reverse("admin:apitokens_merchantaccount_change", args=[account.pk]))
        self.assertEqual(change.status_code, 200)
        for resp in (changelist, change):
            content = resp.content.decode()
            self.assertNotIn(account.password, content)
            self.assertNotIn(account.password.split("$")[-1], content)
        self.assertNotContains(change, 'name="password"')

    def test_search_by_phone_and_name(self):
        url = reverse("admin:apitokens_merchantaccount_changelist")
        self.assertContains(self.client.get(url, {"q": "512345"}), "Owner Name")
        self.assertContains(self.client.get(url, {"q": "owner"}), "+966512345678")
        self.assertNotContains(self.client.get(url, {"q": "nobody"}), "+966512345678")

    def test_saving_in_admin_keeps_password(self):
        before = MerchantAccount.objects.get(pk=self.account.pk).password
        resp = self.client.post(
            reverse("admin:apitokens_merchantaccount_change", args=[self.account.pk]),
            {"merchant": self.store.pk, "name": "Renamed", "phone": "+966512345678", "is_active": "on",
             "phone_verified": "on", "failed_logins": "0", "locked_until_0": "", "locked_until_1": ""},
        )
        self.assertEqual(resp.status_code, 302, resp.content[:2000])
        acc = MerchantAccount.objects.get(pk=self.account.pk)
        self.assertEqual(acc.name, "Renamed")
        self.assertEqual(acc.password, before)
