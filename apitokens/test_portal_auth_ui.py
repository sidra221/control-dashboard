"""تخطيط صفحات الدخول والتسجيل: موسّط بدون هيدر لوحة التحكم، وزر إظهار كلمة المرور."""

from django.test import TestCase
from django.urls import reverse

from .models import ApiToken
from .portal_auth import generate_portal_key
from .portal_testing import login_portal


class AuthPagesLayoutTests(TestCase):
    def test_login_page_uses_the_centered_auth_layout(self):
        html = self.client.get(reverse("portal:login")).content.decode()
        self.assertIn('<body class="auth">', html)
        self.assertIn("auth-brand", html)
        self.assertNotIn("<header>", html)  # هيدر اللوحة مو هون
        self.assertIn('class="pw-toggle"', html)
        self.assertIn('data-for="password"', html)

    def test_login_phone_has_placeholder_autofocus_and_ltr_dir(self):
        html = self.client.get(reverse("portal:login")).content.decode()
        self.assertIn('placeholder="+966 50 123 4567"', html)
        self.assertIn("autofocus", html)
        self.assertIn('dir="ltr"', html)
        self.assertIn('autocomplete="current-password"', html)

    def test_register_page_has_toggle_on_both_password_fields(self):
        merchant = ApiToken.objects.create(label="Shop", image_quota=10)
        key = generate_portal_key(merchant)
        html = self.client.get(reverse("portal:enter", args=[key])).content.decode()
        self.assertIn('<body class="auth">', html)
        self.assertIn('data-for="password"', html)
        self.assertIn('data-for="password_confirm"', html)
        self.assertNotIn("<header>", html)

    def test_password_toggle_labels_are_translated(self):
        for lang, show in (("ar", "إظهار كلمة المرور"), ("en", "Show password")):
            session = self.client.session
            session["lang"] = lang
            session.save()
            html = self.client.get(reverse("portal:login")).content.decode()
            self.assertIn(show, html)

    def test_dashboard_pages_keep_the_regular_header(self):
        merchant = ApiToken.objects.create(label="Shop", image_quota=10)
        login_portal(self.client, merchant)
        html = self.client.get(reverse("portal:accept")).content.decode()
        self.assertIn("<header>", html)
        self.assertNotIn('<body class="auth">', html)
