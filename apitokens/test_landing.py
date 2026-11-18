from django.test import TestCase, override_settings
from django.urls import reverse


class LandingPageTests(TestCase):
    def test_public_page_renders_without_login(self):
        res = self.client.get(reverse("landing"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "PEPLO")
        self.assertContains(res, 'lang="ar"')

    def test_primary_links(self):
        res = self.client.get(reverse("landing"))
        self.assertContains(res, reverse("portal:register"))
        self.assertContains(res, reverse("portal:login"))
        self.assertContains(res, reverse("privacy"))

    @override_settings(SUPPORT_WHATSAPP="966530307054")
    def test_whatsapp_link(self):
        res = self.client.get(reverse("landing"))
        self.assertContains(res, "https://wa.me/966530307054")

    @override_settings(SUPPORT_WHATSAPP="")
    def test_no_whatsapp_link_when_not_configured(self):
        res = self.client.get(reverse("landing"))
        self.assertNotContains(res, "wa.me")

    @override_settings(DEMO_STORE_KEY="")
    def test_demo_button_hidden_without_demo_store(self):
        res = self.client.get(reverse("landing"))
        self.assertNotContains(res, reverse("demo-home"))

    @override_settings(DEMO_STORE_KEY="some-token")
    def test_demo_button_shown_with_demo_store(self):
        res = self.client.get(reverse("landing"))
        self.assertContains(res, reverse("demo-home"))

    def test_tryon_comparison_and_assets(self):
        res = self.client.get(reverse("landing"))
        self.assertContains(res, "data-ba")
        for name in ("tryon-before.webp", "tryon-after.webp", "product-red-dress.webp", "landing.js", "landing.css"):
            self.assertContains(res, name)

    def test_static_assets_exist(self):
        from pathlib import Path

        base = Path(__file__).parent / "static" / "apitokens" / "landing"
        for name in ("tryon-before.webp", "tryon-after.webp", "product-red-dress.webp", "peplo-mark-cream.webp",
                     "favicon.png", "apple-touch-icon.png", "og.png", "landing.js", "landing.css"):
            self.assertTrue((base / name).is_file(), name)

    def test_post_not_allowed(self):
        self.assertEqual(self.client.post(reverse("landing")).status_code, 405)
