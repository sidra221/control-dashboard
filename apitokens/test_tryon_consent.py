from django.conf import settings
from django.test import SimpleTestCase


class TryonPrivacyPolicyTests(SimpleTestCase):
    def test_privacy_page_has_tryon_section_in_both_languages(self):
        html = self.client.get("/privacy/").content.decode()
        self.assertIn('id="tryon"', html)
        self.assertIn("معالجة صور تجربة اللبس", html)
        self.assertIn('id="tryon-en"', html)
        self.assertIn("Try-on photo processing", html)
        # ما منوعد بشي مو مؤكد عن المزوّد.
        self.assertIn("14", html)
        self.assertNotIn("لا نحتفظ بها", html)

    def test_privacy_page_claims_no_shopper_consent_step(self):
        html = self.client.get("/privacy/").content.decode()
        # الموافقة صارت من التاجر (بالبوابة) مو من الزبون، فالصفحة ما لازم تدّعي تنبيه للزبون.
        self.assertNotIn("تنبيه يتضمن رابط هذه السياسة", html)
        self.assertNotIn("a notice linking to this policy", html)

    def test_widget_shows_no_shopper_privacy_notice(self):
        js = (settings.BASE_DIR / "apitokens" / "static" / "apitokens" / "embed.js").read_text()
        self.assertNotIn("tryonPrivacyNote", js)
        self.assertNotIn("tryon-note", js)
