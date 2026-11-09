from django.conf import settings
from django.test import SimpleTestCase


class TryonPrivacyNoticeTests(SimpleTestCase):
    def test_privacy_page_has_tryon_section_in_both_languages(self):
        html = self.client.get("/privacy/").content.decode()
        self.assertIn('id="tryon"', html)
        self.assertIn("معالجة صور تجربة اللبس", html)
        self.assertIn('id="tryon-en"', html)
        self.assertIn("Try-on photo processing", html)
        # ما منوعد بشي مو مؤكد عن المزوّد.
        self.assertIn("14", html)
        self.assertNotIn("لا نحتفظ بها", html)

    def test_widget_shows_privacy_note_under_tryon_button(self):
        js = (settings.BASE_DIR / "apitokens" / "static" / "apitokens" / "embed.js").read_text()
        self.assertIn("tryonPrivacyNote()", js)
        self.assertIn("/privacy/#tryon", js)
        self.assertIn("سياسة خصوصية تجربة اللبس", js)
        self.assertIn("Try-On Privacy Policy", js)
        # التنبيه ما بيظهر لمنتج ما بيدعم التجربة.
        self.assertIn("rec.tryon_supported !== false) card.appendChild(tryonPrivacyNote())", js)
