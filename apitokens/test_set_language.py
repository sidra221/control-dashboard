from django.test import TestCase


class SetLanguageRedirectTests(TestCase):
    def test_internal_next_is_kept_and_language_set(self):
        r = self.client.post("/set-language/", {"lang": "en", "next": "/portal/?range=7"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/portal/?range=7")
        self.assertEqual(self.client.session["lang"], "en")

    def test_external_or_protocol_relative_next_falls_back_to_root(self):
        for bad in ("//evil.com", "/\\evil.com", "https://evil.com/x", "javascript:alert(1)", "///evil.com"):
            r = self.client.post("/set-language/", {"lang": "ar", "next": bad})
            self.assertEqual(r["Location"], "/", bad)
