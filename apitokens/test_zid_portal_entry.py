"""زر "افتح لوحة متجرك" بصفحة تطبيق زد: الحساب بينربط بمتجر زد نفسه، بلا متجر تجريبي مكرر."""
from django.test import TestCase
from django.urls import reverse

from . import portal_auth
from .models import ApiToken, MerchantAccount, ZidConnection
from .portal_testing import make_account
from .zid_views import CONNECTION_SESSION_KEY


class ZidPortalEntryTests(TestCase):
    def setUp(self):
        self.m = ApiToken.objects.create(label="متجر زد", image_quota=20)
        self.c = ZidConnection.objects.create(merchant=self.m, store_id="777", store_url="https://x.zid.store/")

    def login_zid(self, connection=None):
        session = self.client.session
        session[CONNECTION_SESSION_KEY] = (connection or self.c).pk
        session.save()

    def test_app_page_shows_the_button(self):
        self.login_zid()
        html = self.client.get(reverse("zid-app")).content.decode()
        self.assertIn(reverse("zid-portal-entry"), html)
        self.assertIn("افتح لوحة متجرك", html)

    def test_without_account_opens_registration_bound_to_this_store(self):
        self.login_zid()
        resp = self.client.get(reverse("zid-portal-entry"))
        self.assertEqual(resp.status_code, 302)
        key = resp["Location"].rstrip("/").split("/")[-1]
        self.assertEqual(portal_auth._invite_merchant(key), self.m)
        page = self.client.get(resp["Location"])
        self.assertEqual(page.status_code, 200)  # صفحة التسجيل للمتجر نفسه، مش متجر جديد
        self.assertEqual(ApiToken.objects.count(), 1)

    def test_with_account_goes_to_login(self):
        make_account(self.m, phone="+966500000444")
        self.login_zid()
        self.assertRedirects(self.client.get(reverse("zid-portal-entry")), reverse("portal:login"),
                             fetch_redirect_response=False)
        self.assertEqual(self.m.portal_key_hash, ApiToken.objects.get(pk=self.m.pk).portal_key_hash)

    def test_without_zid_session_goes_to_install(self):
        self.assertRedirects(self.client.get(reverse("zid-portal-entry")), reverse("zid-install"),
                             fetch_redirect_response=False)

    def test_inactive_store_or_connection_refused(self):
        self.login_zid()
        ApiToken.objects.filter(pk=self.m.pk).update(is_active=False)
        self.assertRedirects(self.client.get(reverse("zid-portal-entry")), reverse("zid-install"),
                             fetch_redirect_response=False)
        self.assertFalse(MerchantAccount.objects.exists())
