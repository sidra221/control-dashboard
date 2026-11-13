"""صفحة الاشتراك (حساب موثّق بلا متجر): طلب كود الاشتراك على واتساب، وتفعيله بينشئ متجر برصيد التجربة."""
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from . import portal_otp
from .models import ApiToken, MerchantAccount, PhoneOtp
from .portal_testing import _put_session, login_pending, patch_otp_send, sent_codes
from .services import lightotp
from .test_portal_auth import PortalBase

URL = "/portal/pending/"


@override_settings(PORTAL_OTP_BACKEND="lightotp", PORTAL_TRIAL_IMAGE_QUOTA=20)
class SubscribeTests(PortalBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.sender = patch_otp_send(self)
        self.account = login_pending(self.client, name="Layla")

    def request_code(self, name="متجر ليلى"):
        return self.client.post(URL, {"action": "request", "store_name": name})

    def activate(self, code):
        return self.client.post(URL, {"action": "activate", "code": code})

    def test_page_shows_features_plan_and_store_form(self):
        html = self.client.get(URL).content.decode()
        for text in ("AI shopping assistant", "Virtual try-on", "Salla, Zid and Shopify", "Stats dashboard",
                     "20 try-ons per month", 'name="store_name"', "Request subscription code"):
            self.assertIn(text, html)
        self.assertNotIn('name="code"', html)

    def test_request_sends_an_8_char_code_on_whatsapp_and_shows_code_form(self):
        resp = self.request_code()
        self.assertRedirects(resp, URL, fetch_redirect_response=False)
        codes = sent_codes(self.sender)
        self.assertEqual(len(codes), 1)
        self.assertRegex(codes[0], r"^[A-HJKMNP-Z2-9]{8}$")
        self.assertEqual(self.sender.call_args.kwargs["phone"], self.account.phone)
        otp = PhoneOtp.objects.get(account=self.account)
        self.assertEqual((otp.purpose, otp.store_name), (PhoneOtp.PURPOSE_SUBSCRIBE, "متجر ليلى"))
        self.assertNotIn(codes[0], otp.code_hash)
        html = self.client.get(URL).content.decode()
        self.assertIn('name="code"', html)
        self.assertIn("We sent your subscription code on WhatsApp to", html)
        self.assertNotIn('class="alert-ok"', html)  # بدون تكرار "أرسلنا" مرتين
        self.assertNotIn('class="alert-error"', html)  # والإرسال الناجح مو خطأ
        self.assertIn("متجر ليلى", html)

    def test_correct_code_creates_store_with_trial_quota_links_account_and_opens_dashboard(self):
        self.request_code()
        code = sent_codes(self.sender)[-1]
        resp = self.activate(f" {code[:4].lower()}-{code[4:]} ")  # مسافات وشرطة وحروف صغيرة مقبولة
        self.assertRedirects(resp, reverse("portal:home"), fetch_redirect_response=False)
        self.account.refresh_from_db()
        store = self.account.merchant
        self.assertIsNotNone(store)
        self.assertEqual((store.label, store.image_quota, store.period), ("متجر ليلى", 20, ApiToken.Period.MONTHLY))
        self.assertTrue(store.is_active)
        self.assertTrue(store.token)
        # نفس الجلسة: الرئيسية بتطلب الموافقة على السياسة أول
        self.assertRedirects(self.client.get(reverse("portal:home")), reverse("portal:accept"), fetch_redirect_response=False)
        # الصفحة بعدها بتحوّل عالرئيسية، والكود ما بيتستخدم مرتين
        self.assertRedirects(self.client.get(URL), reverse("portal:home"), fetch_redirect_response=False)
        self.assertRedirects(self.activate(code), reverse("portal:home"), fetch_redirect_response=False)
        self.assertEqual(ApiToken.objects.filter(label="متجر ليلى").count(), 1)

    def test_wrong_code_counts_and_locks_after_5(self):
        self.request_code()
        for _ in range(portal_otp.MAX_ATTEMPTS - 1):
            resp = self.activate("AAAAAAAA")
            self.assertEqual(resp.status_code, 400)
            self.assertIn("That code is incorrect", resp.content.decode())
        resp = self.activate("AAAAAAAA")
        self.assertIn("Too many wrong attempts", resp.content.decode())
        good = sent_codes(self.sender)[-1]
        self.assertIn("Too many wrong attempts", self.activate(good).content.decode())
        self.account.refresh_from_db()
        self.assertIsNone(self.account.merchant)

    def test_expired_code_is_refused(self):
        self.request_code()
        PhoneOtp.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        resp = self.activate(sent_codes(self.sender)[-1])
        self.assertIn("This code has expired", resp.content.decode())
        self.assertFalse(ApiToken.objects.filter(label="متجر ليلى").exists())

    def test_activate_without_request(self):
        resp = self.activate("ABCDEFGH")
        self.assertIn("There is no active code", resp.content.decode())

    def test_store_name_is_required(self):
        resp = self.request_code(name=" x ")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Enter your store name", resp.content.decode())
        self.assertEqual(self.sender.call_count, 0)

    def test_resend_respects_cooldown_and_new_code_replaces_old(self):
        self.request_code()
        self.request_code()  # تهدئة: ما في إرسال تاني
        self.assertEqual(self.sender.call_count, 1)
        self.assertIn("Please wait", self.client.get(URL).content.decode())
        PhoneOtp.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.request_code()
        self.assertEqual(self.sender.call_count, 2)
        first, second = sent_codes(self.sender)
        self.assertEqual(self.activate(first).status_code, 400)
        self.assertRedirects(self.activate(second), reverse("portal:home"), fetch_redirect_response=False)

    def test_subscription_code_does_not_touch_phone_verification_codes(self):
        verify = PhoneOtp.objects.create(account=self.account, phone=self.account.phone, expires_at=timezone.now() + timedelta(minutes=5))
        self.request_code()
        verify.refresh_from_db()
        self.assertIsNone(verify.consumed_at)  # كود من نوع تاني ما بيبطل
        # كود الاشتراك ما بينقبل ككود توثيق (بينحسب غلط على كود التوثيق المفتوح)
        self.assertEqual(portal_otp.verify_code(self.account, sent_codes(self.sender)[-1]), portal_otp.V_WRONG)

    def test_send_failure_shows_unavailable_and_creates_nothing(self):
        self.sender.side_effect = lightotp.LightOtpNotConfigured("LIGHTOTP_API_KEY")
        self.request_code()
        self.assertIn("temporarily unavailable", self.client.get(URL).content.decode())
        self.assertIn('name="store_name"', self.client.get(URL).content.decode())
        self.assertEqual(self.activate("ABCDEFGH").status_code, 400)
        self.account.refresh_from_db()
        self.assertIsNone(self.account.merchant)

    def test_linked_account_cannot_request_another_store(self):
        self.request_code()
        self.activate(sent_codes(self.sender)[-1])
        self.assertRedirects(self.request_code("متجر تاني"), reverse("portal:home"), fetch_redirect_response=False)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(ApiToken.objects.filter(portal_account=self.account).count(), 1)

    def test_unverified_account_is_sent_to_verify(self):
        MerchantAccount.objects.filter(pk=self.account.pk).update(phone_verified=False, phone_verified_at=None)
        self.assertRedirects(self.request_code(), reverse("portal:verify"), fetch_redirect_response=False)
        self.assertEqual(self.sender.call_count, 0)

    def test_requires_csrf(self):
        strict = Client(enforce_csrf_checks=True)
        _put_session(strict, self.account)
        self.assertEqual(strict.post(URL, {"action": "request", "store_name": "متجر ليلى"}).status_code, 403)
        self.assertEqual(strict.post(URL, {"action": "activate", "code": "ABCDEFGH"}).status_code, 403)
        self.assertEqual(self.sender.call_count, 0)

    def test_store_name_can_be_changed_after_a_code_was_sent(self):
        self.request_code("متجر ليلا")  # غلطة
        html = self.client.get(URL).content.decode()
        self.assertIn("Change store name", html)
        PhoneOtp.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.request_code("متجر ليلى")
        self.activate(sent_codes(self.sender)[-1])
        self.account.refresh_from_db()
        self.assertEqual(self.account.merchant.label, "متجر ليلى")

    def test_request_button_shows_the_shared_cooldown_right_after_phone_verification(self):
        PhoneOtp.objects.create(account=self.account, phone=self.account.phone, message_id="m-1",
                                consumed_at=timezone.now(), expires_at=timezone.now())
        html = self.client.get(URL).content.decode()
        self.assertRegex(html, r'data-countdown="[1-9]\d*"[^>]*data-ready="Request subscription code"[^>]*disabled')

    @override_settings(PORTAL_MAX_TRIAL_STORES_PER_DAY=1)
    def test_daily_trial_cap_stops_sending_and_activation(self):
        self.request_code()
        code = sent_codes(self.sender)[-1]
        other = MerchantAccount.objects.create(name="X", phone="+966500000999", password="x",
                                               phone_verified=True, phone_verified_at=timezone.now())
        other.merchant = ApiToken.objects.create(label="trial", image_quota=20)
        other.save(update_fields=["merchant"])
        resp = self.activate(code)
        self.assertIn("New subscriptions are temporarily unavailable", resp.content.decode())
        self.account.refresh_from_db()
        self.assertIsNone(self.account.merchant)
        PhoneOtp.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        self.request_code("متجر تاني")
        self.assertEqual(self.sender.call_count, 1)  # ما انبعت كود جديد
        # الستاف (created_by) ما بينحسب بالسقف، والقديم من 24 ساعة كمان
        ApiToken.objects.filter(pk=other.merchant.pk).update(created_at=timezone.now() - timedelta(hours=25))
        self.assertRedirects(self.activate(code), reverse("portal:home"), fetch_redirect_response=False)

    def test_arabic_page(self):
        self.set_lang("ar")
        html = self.client.get(URL).content.decode()
        self.assertIn("فعّل اشتراكك", html)
        self.assertIn("يتضمن الاشتراك 20 تجربة لبس شهرياً.", html)
        self.assertIn("اطلب كود الاشتراك", html)
