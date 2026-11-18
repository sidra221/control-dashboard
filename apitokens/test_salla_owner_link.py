"""صاحب متجر سلة بيسجّل بالبوابة بنفس جواله ← بينربط بمتجره بدل متجر تجريبي جديد."""
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from .models import ApiToken, MerchantAccount, SallaConnection
from .portal_testing import login_pending, make_account
from .services import salla as salla_service

PHONE = "+966511112222"


class SallaOwnerPhoneTests(TestCase):
    def test_user_info_mobile_is_saved_in_e164(self):
        m = ApiToken.objects.create(label="Salla 1", image_quota=20)
        c = SallaConnection.objects.create(merchant=m, salla_merchant_id="1", access_token="t")
        for raw, want in (("+966511112222", PHONE), ("966511112222", PHONE), ("0511112222", PHONE), ("", ""), ("abc", "")):
            body = {"data": {"mobile": raw, "merchant": {"name": "متجر", "domain": "https://s.salla.sa"}}}
            with mock.patch.object(salla_service.requests, "get") as get:
                get.return_value.json.return_value = body
                get.return_value.raise_for_status.return_value = None
                salla_service.fill_store_info(c)
            c.refresh_from_db()
            self.assertEqual(c.owner_phone, want, raw)
        self.assertEqual(c.store_name, "متجر")


class SallaAutoLinkTests(TestCase):
    def setUp(self):
        self.store = ApiToken.objects.create(label="متجر سلة", image_quota=20)
        self.conn = SallaConnection.objects.create(merchant=self.store, salla_merchant_id="9", owner_phone=PHONE)

    def test_verified_account_with_owner_phone_is_linked_instead_of_trial(self):
        acc = login_pending(self.client, phone=PHONE)
        resp = self.client.get(reverse("portal:pending"))
        self.assertRedirects(resp, reverse("portal:home"), fetch_redirect_response=False)
        acc.refresh_from_db()
        self.assertEqual(acc.merchant, self.store)
        self.assertEqual(ApiToken.objects.count(), 1)  # ما انعمل متجر تجريبي

    def test_other_phone_gets_the_normal_subscription_page(self):
        acc = login_pending(self.client, phone="+966533334444")
        self.assertEqual(self.client.get(reverse("portal:pending")).status_code, 200)
        acc.refresh_from_db()
        self.assertIsNone(acc.merchant)

    def test_store_that_already_has_an_account_is_not_taken(self):
        make_account(self.store, phone="+966599990000")
        acc = login_pending(self.client, phone=PHONE)
        self.assertEqual(self.client.get(reverse("portal:pending")).status_code, 200)
        acc.refresh_from_db()
        self.assertIsNone(acc.merchant)

    def test_inactive_connection_or_store_not_linked(self):
        SallaConnection.objects.filter(pk=self.conn.pk).update(is_active=False)
        acc = login_pending(self.client, phone=PHONE)
        self.client.get(reverse("portal:pending"))
        acc.refresh_from_db()
        self.assertIsNone(acc.merchant)

    def test_unverified_account_is_never_linked(self):
        acc = login_pending(self.client, phone=PHONE)
        MerchantAccount.objects.filter(pk=acc.pk).update(phone_verified=False, phone_verified_at=None)
        self.client.get(reverse("portal:pending"))
        acc.refresh_from_db()
        self.assertIsNone(acc.merchant)
