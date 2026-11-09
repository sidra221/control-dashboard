"""موافقة التاجر على سياسة الخصوصية: بوابة قبل لوحة المتجر، وتسجيل النسخة والوقت."""

import hashlib

from django.test import Client, TestCase
from django.urls import reverse

from .models import ApiToken
from .portal_auth import PORTAL_TERMS_VERSION


def make(label="Shop", key="k", **kw):
    return ApiToken.objects.create(
        label=label, image_quota=10, portal_key_hash=hashlib.sha256(key.encode()).hexdigest(), **kw,
    )


class ConsentBase(TestCase):
    def login(self, merchant, key="k"):
        session = self.client.session
        session["portal_merchant_id"] = merchant.pk
        session["portal_key_hash"] = hashlib.sha256(key.encode()).hexdigest()
        session.save()


class ConsentGateTests(ConsentBase):
    def test_home_redirects_to_accept_until_agreed(self):
        m = make()
        self.login(m)
        r = self.client.get(reverse("portal:home"))
        self.assertRedirects(r, reverse("portal:accept"), fetch_redirect_response=False)

    def test_old_version_asks_again(self):
        m = make(terms_accepted_version="2020-01-01")
        self.login(m)
        r = self.client.get(reverse("portal:home"))
        self.assertRedirects(r, reverse("portal:accept"), fetch_redirect_response=False)

    def test_accepted_version_reaches_home(self):
        m = make(terms_accepted_version=PORTAL_TERMS_VERSION)
        self.login(m)
        self.assertEqual(self.client.get(reverse("portal:home")).status_code, 200)

    def test_accept_page_shows_policy_link_checkbox_and_hardened_headers(self):
        self.login(make())
        r = self.client.get(reverse("portal:accept"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('href="/privacy/"', html)
        self.assertIn('name="agree"', html)
        self.assertIn(PORTAL_TERMS_VERSION, html)
        self.assertEqual(r["Cache-Control"], "no-store")
        self.assertEqual(r["Referrer-Policy"], "same-origin")

    def test_accept_requires_a_session(self):
        r = self.client.get(reverse("portal:accept"))
        self.assertRedirects(r, reverse("portal:expired"), fetch_redirect_response=False)
        r = self.client.post(reverse("portal:accept"), {"agree": "1"})
        self.assertRedirects(r, reverse("portal:expired"), fetch_redirect_response=False)


class AcceptTests(ConsentBase):
    def test_post_with_checkbox_records_version_and_time_then_home(self):
        m = make()
        self.login(m)
        r = self.client.post(reverse("portal:accept"), {"agree": "1"})
        self.assertRedirects(r, reverse("portal:home"), fetch_redirect_response=False)
        m.refresh_from_db()
        self.assertEqual(m.terms_accepted_version, PORTAL_TERMS_VERSION)
        self.assertIsNotNone(m.terms_accepted_at)
        self.assertEqual(self.client.get(reverse("portal:home")).status_code, 200)

    def test_post_without_checkbox_does_not_record(self):
        m = make()
        self.login(m)
        for data in ({}, {"agree": ""}, {"agree": "0"}, {"agree": "yes"}):
            r = self.client.post(reverse("portal:accept"), data)
            self.assertEqual(r.status_code, 400)
        m.refresh_from_db()
        self.assertEqual(m.terms_accepted_version, "")
        self.assertIsNone(m.terms_accepted_at)

    def test_accept_page_when_already_accepted_goes_home(self):
        m = make(terms_accepted_version=PORTAL_TERMS_VERSION)
        self.login(m)
        r = self.client.get(reverse("portal:accept"))
        self.assertRedirects(r, reverse("portal:home"), fetch_redirect_response=False)

    def test_acceptance_is_per_merchant(self):
        a, b = make("A", "ka"), make("B", "kb")
        self.login(a, "ka")
        self.client.post(reverse("portal:accept"), {"agree": "1"})
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.terms_accepted_version, PORTAL_TERMS_VERSION)
        self.assertEqual(b.terms_accepted_version, "")

    def test_regenerating_link_keeps_acceptance(self):
        from .portal_auth import generate_portal_key

        m = make()
        self.login(m)
        self.client.post(reverse("portal:accept"), {"agree": "1"})
        m.refresh_from_db()
        generate_portal_key(m)
        m.refresh_from_db()
        self.assertEqual(m.terms_accepted_version, PORTAL_TERMS_VERSION)
        self.assertTrue(m.terms_accepted_at)


class StaffVisibilityTests(TestCase):
    def test_staff_dashboard_shows_acceptance_status(self):
        from django.contrib.auth import get_user_model

        staff = get_user_model().objects.create_superuser("s", "s@example.com", "pw-12345-x")
        self.client.force_login(staff)
        make("Pending", "k1")
        make("Done", "k2", terms_accepted_version=PORTAL_TERMS_VERSION)
        ApiToken.objects.filter(label="Done").update(terms_accepted_at="2026-11-09T10:00:00Z")
        html = self.client.get(reverse("apitokens:dashboard")).content.decode()
        self.assertIn("ما وافق على السياسة بعد", html)
        self.assertIn("وافق على السياسة بتاريخ 2026-11-09", html)


class BrowserCsrfRegressionTests(TestCase):
    """المتصفح بيبعت Origin بكل POST. مع Referrer-Policy: no-referrer بيصير `null` وجانغو بيرفض."""

    def _session_client(self, merchant, key="k"):
        client = Client(enforce_csrf_checks=True)
        session = client.session
        session["portal_merchant_id"] = merchant.pk
        session["portal_key_hash"] = hashlib.sha256(key.encode()).hexdigest()
        session.save()
        return client

    def _csrf_token(self, client):
        html = client.get(reverse("portal:accept")).content.decode()
        marker = 'name="csrfmiddlewaretoken" value="'
        return html.split(marker)[1].split('"')[0]

    def test_same_origin_post_is_accepted_with_enforced_csrf(self):
        m = make()
        client = self._session_client(m)
        token = self._csrf_token(client)
        r = client.post(
            reverse("portal:accept"), {"agree": "1", "csrfmiddlewaretoken": token}, HTTP_ORIGIN="http://testserver",
        )
        self.assertEqual(r.status_code, 302)
        m.refresh_from_db()
        self.assertEqual(m.terms_accepted_version, PORTAL_TERMS_VERSION)

    def test_null_origin_is_rejected_so_policy_must_not_strip_origin(self):
        client = self._session_client(make())
        token = self._csrf_token(client)
        r = client.post(
            reverse("portal:accept"), {"agree": "1", "csrfmiddlewaretoken": token}, HTTP_ORIGIN="null",
        )
        self.assertEqual(r.status_code, 403)

    def test_portal_pages_never_send_no_referrer(self):
        client = self._session_client(make())
        for url in (reverse("portal:accept"), reverse("portal:expired")):
            self.assertNotEqual(client.get(url)["Referrer-Policy"], "no-referrer")
