"""اختبارات صفحة بوابة التاجر (portal_views.home): العزل بين التجار، المحتوى الممنوع،
الفلاتر، الحالة الفاضية، واللغتين. الدخول بالاختبار عبر الجلسة مباشرة (متل `enter`)."""

import hashlib
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .i18n import TRANSLATIONS
from .i18n_portal import PORTAL_TRANSLATIONS
from .models import ApiToken, Conversation, Product, TryOnEvent


def make_merchant(label, key, **kw):
    return ApiToken.objects.create(
        label=label, image_quota=100, portal_key_hash=hashlib.sha256(key.encode()).hexdigest(), **kw,
    )


def tryon(merchant, sku, session, hours_ago=2, success=True, error=""):
    ev = TryOnEvent.objects.create(merchant=merchant, session_id=session, sku=sku, success=success, error=error)
    TryOnEvent.objects.filter(pk=ev.pk).update(created_at=timezone.now() - timedelta(hours=hours_ago))


def order(merchant, session, total, order_id, hours_ago=1):
    return Conversation.objects.create(
        merchant=merchant, session_id=session, order_id=order_id, order_total=Decimal(total),
        order_at=timezone.now() - timedelta(hours=hours_ago), recommended_skus=["x"], buy_clicks=1,
    )


class PortalBase(TestCase):
    def login(self, merchant, key):
        session = self.client.session
        session["portal_merchant_id"] = merchant.pk
        session["portal_key_hash"] = hashlib.sha256(key.encode()).hexdigest()
        session.save()

    def get(self, lang="en", **params):
        session = self.client.session
        session["lang"] = lang
        session.save()
        return self.client.get(reverse("portal:home"), params)


class PortalAuthGateTests(PortalBase):
    def test_unauthenticated_redirects_to_expired(self):
        resp = self.client.get(reverse("portal:home"))
        self.assertRedirects(resp, reverse("portal:expired"), fetch_redirect_response=False)

    def test_revoked_key_redirects(self):
        merchant = make_merchant("Shop A", "key-a")
        self.login(merchant, "key-a")
        ApiToken.objects.filter(pk=merchant.pk).update(portal_key_hash="")
        resp = self.client.get(reverse("portal:home"))
        self.assertEqual(resp.status_code, 302)


class PortalIsolationTests(PortalBase):
    def setUp(self):
        self.a = make_merchant("Alpha Boutique", "key-a", currency="SAR")
        self.b = make_merchant("Bravo Fashion", "key-b", currency="USD")
        Product.objects.create(merchant=self.a, sku="SKU-ALPHA", title="Alpha Dress", price=10, image_url="http://x/a.jpg")
        Product.objects.create(merchant=self.b, sku="SKU-BRAVO", title="Bravo Jacket", price=10, image_url="http://x/b.jpg")
        for i in range(2):
            tryon(self.a, "SKU-ALPHA", f"alpha-sess-{i}")
        order(self.a, "alpha-sess-0", "111.00", "oa1")
        for i in range(7):
            tryon(self.b, "SKU-BRAVO", f"bravo-sess-{i}")
        order(self.b, "bravo-sess-0", "987.65", "ob1")
        order(self.b, "bravo-sess-1", "432.10", "ob2")

    def test_a_sees_only_own_data(self):
        self.login(self.a, "key-a")
        resp = self.get()
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Alpha Boutique", html)
        self.assertIn("Alpha Dress", html)
        self.assertIn("111.00 SAR", html)
        for leaked in ("Bravo Fashion", "Bravo Jacket", "SKU-BRAVO", "987.65", "432.10", "USD", "bravo-sess"):
            self.assertNotIn(leaked, html)
        self.assertEqual(resp.context["stats"]["totals"]["tryons"], 2)
        self.assertEqual(resp.context["conv"]["orders"], 1)

    def test_b_sees_only_own_data(self):
        self.login(self.b, "key-b")
        html = self.get().content.decode()
        self.assertIn("Bravo Jacket", html)
        self.assertEqual(self.get().context["stats"]["totals"]["tryons"], 7)
        for leaked in ("Alpha Boutique", "Alpha Dress", "SKU-ALPHA", "111.00", "SAR"):
            self.assertNotIn(leaked, html)

    def test_other_merchant_id_in_query_is_ignored(self):
        self.login(self.a, "key-a")
        resp = self.get(pk=self.b.pk, merchant=self.b.pk, merchant_id=self.b.pk, sku="SKU-BRAVO")
        html = resp.content.decode()
        self.assertIn("Alpha Boutique", html)
        self.assertNotIn("Bravo", html)
        self.assertEqual(resp.context["merchant"].pk, self.a.pk)

    def test_session_with_wrong_hash_is_rejected(self):
        session = self.client.session
        session["portal_merchant_id"] = self.b.pk
        session["portal_key_hash"] = hashlib.sha256(b"key-a").hexdigest()  # مفتاح التاجر A مع id تاجر B
        session.save()
        resp = self.client.get(reverse("portal:home"))
        self.assertRedirects(resp, reverse("portal:expired"), fetch_redirect_response=False)


class PortalContentTests(PortalBase):
    def setUp(self):
        self.m = make_merchant("Gamma Store", "key-g", currency="AED")
        self.login(self.m, "key-g")
        Product.objects.create(merchant=self.m, sku="G-1", title="Gamma Coat", price=5, image_url="http://x/g.jpg")
        # 3 تجارب بدون طلب → شارة "بلا طلبات"
        for i in range(3):
            tryon(self.m, "G-1", f"sess-hidden-{i:04d}")
        # تجربة فاشلة بنص خطأ — ما لازم يظهر أبداً
        tryon(self.m, "G-1", "sess-failed-0001", success=False, error="SECRET_PROVIDER_FAILURE_42")
        Conversation.objects.create(merchant=self.m, session_id="sess-hidden-0000", recommended_skus=["G-1"])

    def test_forbidden_content_absent(self):
        for lang in ("en", "ar"):
            html = self.get(lang).content.decode()
            self.assertNotIn("SECRET_PROVIDER_FAILURE_42", html)
            self.assertNotIn("sess-hidden", html)
            self.assertNotIn("sess-failed", html)
            self.assertNotIn(self.m.token, html)
            self.assertNotIn("/admin", html)
            self.assertNotIn("image_quota", html)
            self.assertNotIn("failed try-ons", html)
            self.assertNotIn("تجربة فاشلة", html)
            self.assertNotIn("Recent try-ons", html)
            self.assertNotIn("<code>", html)

    def test_failed_count_not_in_context_leaking_to_page(self):
        # الفاشلة ما بتنحسب بتجارب البوابة وما بتنعرض
        resp = self.get()
        self.assertEqual(resp.context["stats"]["totals"]["tryons"], 3)

    def test_explanation_hint_and_chart_present(self):
        html = self.get("en").content.decode()
        self.assertIn("not proof", html)
        self.assertIn("Gamma Coat", html)
        self.assertIn("No orders", html)
        self.assertIn("<svg", html)
        self.assertIn('name="robots" content="noindex,nofollow"', html)
        self.assertIn(reverse("portal:logout"), html)
        self.assertIn("csrfmiddlewaretoken", html)

    def test_headers_hardened(self):
        resp = self.get()
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertEqual(resp["Referrer-Policy"], "no-referrer")

    def test_language_toggle_returns_to_portal_page(self):
        html = self.get("en", range="7", window="14").content.decode()
        self.assertIn(reverse("apitokens:set-language"), html)
        self.assertIn('name="next" value="/portal/?range=7&amp;window=14"', html)

    def test_both_languages_render_translated(self):
        en = self.get("en").content.decode()
        ar = self.get("ar").content.decode()
        self.assertIn('lang="en" dir="ltr"', en)
        self.assertIn('lang="ar" dir="rtl"', ar)
        self.assertIn("Total try-ons", en)
        self.assertIn(PORTAL_TRANSLATIONS["portal_home_stat_total"]["ar"], ar)
        self.assertNotIn("Total try-ons", ar)

    def test_all_portal_keys_translated_and_registered(self):
        for key, value in PORTAL_TRANSLATIONS.items():
            self.assertTrue(key.startswith("portal_"))
            self.assertTrue(value["ar"] and value["en"], key)
            self.assertIn(key, TRANSLATIONS)


class PortalFilterTests(PortalBase):
    def setUp(self):
        self.m = make_merchant("Delta", "key-d")
        self.login(self.m, "key-d")
        tryon(self.m, "D-1", "s-1")

    def test_junk_params_fall_back_to_defaults(self):
        resp = self.get(range="banana", window="9999")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["range_key"], "30")
        self.assertEqual(resp.context["window_days"], 7)
        resp = self.get(range=["1", "2"], window="-3")
        self.assertEqual(resp.context["range_key"], "30")
        self.assertEqual(resp.context["window_days"], 7)

    def test_valid_params_selected(self):
        resp = self.get(range="period", window="30")
        self.assertEqual(resp.context["range_key"], "period")
        self.assertEqual(resp.context["window_days"], 30)
        html = resp.content.decode()
        self.assertIn('<option value="period" selected>', html)
        self.assertIn('<option value="30" selected>', html)

    def test_range_7_excludes_older_tryons(self):
        tryon(self.m, "D-1", "s-old", hours_ago=24 * 20)
        self.assertEqual(self.get(range="7").context["stats"]["totals"]["tryons"], 1)
        self.assertEqual(self.get(range="30").context["stats"]["totals"]["tryons"], 2)

    def test_attribution_window_changes_revenue(self):
        tryon(self.m, "D-1", "s-win", hours_ago=24 * 10)
        order(self.m, "s-win", "50.00", "ow1", hours_ago=1)
        self.assertEqual(self.get(window="7").context["stats"]["totals"]["attributed_orders"], 0)
        self.assertEqual(self.get(window="14").context["stats"]["totals"]["attributed_orders"], 1)


class PortalEmptyTests(PortalBase):
    def test_empty_merchant_renders_empty_state(self):
        m = make_merchant("Empty Shop", "key-e")
        self.login(m, "key-e")
        for lang in ("en", "ar"):
            resp = self.get(lang)
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.context["has_data"])
            self.assertIn(PORTAL_TRANSLATIONS["portal_home_empty_text"][lang], resp.content.decode())
            self.assertIn("Empty Shop", resp.content.decode())

    def test_only_failed_tryons_counts_as_empty(self):
        m = make_merchant("Fail Shop", "key-f")
        self.login(m, "key-f")
        tryon(m, "F-1", "s", success=False, error="boom-text")
        resp = self.get()
        self.assertFalse(resp.context["has_data"])
        self.assertNotIn("boom-text", resp.content.decode())

    def test_data_outside_range_still_shows_dashboard_with_zeros(self):
        m = make_merchant("Old Shop", "key-o")
        self.login(m, "key-o")
        tryon(m, "O-1", "s", hours_ago=24 * 90)
        resp = self.get()
        self.assertTrue(resp.context["has_data"])
        self.assertEqual(resp.context["stats"]["totals"]["tryons"], 0)
        self.assertIn("No try-ons in this period yet", resp.content.decode())
