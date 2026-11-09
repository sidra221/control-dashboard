"""اختبارات دخول بوابة التاجر: توليد المفتاح، الدخول، الجلسة، والفصل عن الستاف."""

import hashlib

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.urls import reverse

from . import portal_auth
from .models import ApiToken

PORTAL_HEADERS = {
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
}


def sha(key):
    return hashlib.sha256(key.encode()).hexdigest()


class PortalBase(TestCase):
    def setUp(self):
        self.a = ApiToken.objects.create(label="A", image_quota=100)
        self.b = ApiToken.objects.create(label="B", image_quota=100)
        self.staff = get_user_model().objects.create_user("staff", password="x", is_staff=True)

    def key_for(self, merchant):
        return portal_auth.generate_portal_key(merchant)

    def enter(self, key, client=None):
        return (client or self.client).get(reverse("portal:enter", args=[key]))

    def assert_headers(self, resp):
        for name, value in PORTAL_HEADERS.items():
            self.assertEqual(resp[name], value, name)


class KeyGenerationTests(PortalBase):
    def test_generate_stores_only_hash(self):
        key = self.key_for(self.a)
        self.a.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, sha(key))
        self.assertIsNotNone(self.a.portal_key_created_at)
        self.assertGreaterEqual(len(key), 40)
        self.assertNotEqual(self.a.portal_key_hash, key)
        # المفتاح الأصلي مو بأي عمود
        for field in ApiToken._meta.concrete_fields:
            self.assertNotEqual(getattr(self.a, field.attname), key)
        self.assertEqual(portal_auth.hash_portal_key(key), sha(key))

    def test_keys_are_unique_and_not_the_widget_token(self):
        k1 = self.key_for(self.a)
        k2 = self.key_for(self.a)
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k2, self.a.token)

    def test_revoke_clears(self):
        self.key_for(self.a)
        portal_auth.revoke_portal_key(self.a)
        self.a.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, "")
        self.assertIsNone(self.a.portal_key_created_at)


class EnterTests(PortalBase):
    def test_valid_key_logs_in_and_redirects(self):
        key = self.key_for(self.a)
        resp = self.enter(key)
        self.assertRedirects(resp, reverse("portal:home"), fetch_redirect_response=False)
        self.assert_headers(resp)
        session = self.client.session
        self.assertEqual(session["portal_merchant_id"], self.a.pk)
        self.assertEqual(session["portal_key_hash"], sha(key))
        self.assertNotIn(key, str(dict(session)))

    def test_session_key_is_cycled(self):
        key = self.key_for(self.a)
        self.client.get(reverse("portal:expired"))
        s = self.client.session
        s["marker"] = 1
        s.save()
        before = self.client.cookies["sessionid"].value
        self.enter(key)
        self.assertNotEqual(before, self.client.cookies["sessionid"].value)
        self.assertEqual(self.client.session["marker"], 1)

    def test_invalid_unknown_revoked_inactive_are_uniform_404(self):
        good = self.key_for(self.a)
        revoked = self.key_for(self.b)
        portal_auth.revoke_portal_key(self.b)
        inactive_key = self.key_for(ApiToken.objects.create(label="C", image_quota=1, is_active=False))
        bodies = set()
        for key in ["nope", "x" * 200, "ك", revoked, inactive_key, self.a.token, sha(good)]:
            resp = self.enter(key)
            self.assertEqual(resp.status_code, 404, key)
            self.assert_headers(resp)
            bodies.add(resp.content)
            self.assertNotIn("portal_merchant_id", self.client.session)
        self.assertEqual(len(bodies), 1)

    def test_widget_token_never_logs_in(self):
        self.key_for(self.a)
        self.assertEqual(self.enter(self.a.token).status_code, 404)

    def test_empty_hash_is_not_a_wildcard(self):
        # تاجر بدون مفتاح ما بيتطابق مع أي شي
        self.assertEqual(self.enter(sha("")).status_code, 404)
        self.assertEqual(self.enter(" ").status_code, 404)

    def test_post_not_allowed(self):
        key = self.key_for(self.a)
        resp = self.client.post(reverse("portal:enter", args=[key]))
        self.assertEqual(resp.status_code, 405)


def _ok_view(request):
    return HttpResponse(request.portal_merchant.label)


class PortalRequiredTests(PortalBase):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()
        self.view = portal_auth.portal_required(_ok_view)

    def request(self, **session):
        req = self.factory.get("/x/")
        req.session = SessionStore()
        req.session.update(session)
        return req

    def test_no_session_redirects_to_expired(self):
        resp = self.view(self.request())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("portal:expired"))
        self.assert_headers(resp)

    def test_valid_session_runs_view_with_headers(self):
        key = self.key_for(self.a)
        req = self.request(portal_merchant_id=self.a.pk, portal_key_hash=sha(key))
        resp = self.view(req)
        self.assertEqual(resp.content, b"A")
        self.assert_headers(resp)

    def test_bad_session_values_are_cleared(self):
        key = self.key_for(self.a)
        for mid, h in [("1", sha(key)), (self.a.pk, ""), (self.a.pk, "zz"), (self.a.pk, "ك"), (99999, sha(key)), (None, None)]:
            req = self.request(portal_merchant_id=mid, portal_key_hash=h, other="keep")
            resp = self.view(req)
            self.assertEqual(resp.status_code, 302, (mid, h))
            self.assertNotIn("portal_merchant_id", req.session)
            self.assertNotIn("portal_key_hash", req.session)
            self.assertEqual(req.session["other"], "keep")

    def test_hash_of_other_merchant_rejected(self):
        self.key_for(self.a)
        kb = self.key_for(self.b)
        resp = self.view(self.request(portal_merchant_id=self.a.pk, portal_key_hash=sha(kb)))
        self.assertEqual(resp.status_code, 302)


class SessionLifecycleTests(PortalBase):
    def run_view(self):
        req = RequestFactory().get("/x/")
        req.session = self.client.session
        return portal_auth.portal_required(_ok_view)(req)

    def test_reuse_then_regenerate_logs_out(self):
        self.enter(self.key_for(self.a))
        self.assertEqual(self.run_view().status_code, 200)
        self.key_for(self.a)
        self.assertEqual(self.run_view().status_code, 302)

    def test_revoke_logs_out(self):
        self.enter(self.key_for(self.a))
        portal_auth.revoke_portal_key(self.a)
        self.assertEqual(self.run_view().status_code, 302)

    def test_disabled_merchant_logs_out_and_reenable_needs_new_session(self):
        self.enter(self.key_for(self.a))
        self.a.is_active = False
        self.a.save(update_fields=["is_active"])
        self.assertEqual(self.run_view().status_code, 302)

    def test_old_key_dead_new_key_works(self):
        old = self.key_for(self.a)
        new = self.key_for(self.a)
        self.assertEqual(self.enter(old).status_code, 404)
        self.assertEqual(self.enter(new).status_code, 302)

    def test_cross_merchant_isolation(self):
        ka = self.key_for(self.a)
        kb = self.key_for(self.b)
        self.enter(ka)
        self.assertEqual(self.run_view().content, b"A")
        other = self.client_class()
        self.enter(kb, client=other)
        req = RequestFactory().get("/x/")
        req.session = other.session
        self.assertEqual(portal_auth.portal_required(_ok_view)(req).content, b"B")
        # الكلينت الأول لسا A
        self.assertEqual(self.run_view().content, b"A")
        # مفتاح A مرمّى ببيانات B ما بيمشي
        s = self.client.session
        s["portal_merchant_id"] = self.b.pk
        s.save()
        self.assertEqual(self.run_view().status_code, 302)


class StaffSeparationTests(PortalBase):
    def test_merchant_session_cannot_access_staff_pages(self):
        self.enter(self.key_for(self.a))
        for url in [reverse("apitokens:dashboard"), reverse("apitokens:catalog", args=[self.a.pk]),
                    reverse("apitokens:tryon-stats", args=[self.a.pk])]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertIn("/admin/login/", resp["Location"])
        resp = self.client.post(reverse("apitokens:portal-link", args=[self.b.pk]), {"action": "generate"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])
        self.b.refresh_from_db()
        self.assertEqual(self.b.portal_key_hash, "")

    def test_staff_login_does_not_grant_portal_access(self):
        self.client.force_login(self.staff)
        self.key_for(self.a)
        self.assertEqual(portal_auth.portal_required(_ok_view)(self._req()).status_code, 302)

    def _req(self):
        req = RequestFactory().get("/x/")
        req.session = self.client.session
        return req

    def test_enter_keeps_staff_login(self):
        self.client.force_login(self.staff)
        self.enter(self.key_for(self.a))
        self.assertEqual(self.client.get(reverse("apitokens:dashboard")).status_code, 200)
        self.assertEqual(self.client.session["portal_merchant_id"], self.a.pk)

    def test_logout_only_drops_portal_keys(self):
        self.client.force_login(self.staff)
        self.enter(self.key_for(self.a))
        s = self.client.session
        s["lang"] = "en"
        s.save()
        resp = self.client.post(reverse("portal:logout"))
        self.assertRedirects(resp, reverse("portal:expired"), fetch_redirect_response=False)
        self.assert_headers(resp)
        s = self.client.session
        self.assertNotIn("portal_merchant_id", s)
        self.assertNotIn("portal_key_hash", s)
        self.assertEqual(s["lang"], "en")
        self.assertEqual(self.client.get(reverse("apitokens:dashboard")).status_code, 200)

    def test_logout_requires_post(self):
        self.assertEqual(self.client.get(reverse("portal:logout")).status_code, 405)


class StaffPortalLinkViewTests(PortalBase):
    def url(self, pk=None):
        return reverse("apitokens:portal-link", args=[pk or self.a.pk])

    def test_requires_staff(self):
        resp = self.client.post(self.url(), {"action": "generate"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])
        self.client.force_login(get_user_model().objects.create_user("plain", password="x"))
        resp = self.client.post(self.url(), {"action": "generate"})
        self.assertIn("/admin/login/", resp["Location"])
        self.a.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, "")

    def test_requires_post(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url()).status_code, 405)

    def test_unknown_action_and_merchant(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self.url(), {"action": "x"}).status_code, 400)
        self.assertEqual(self.client.post(self.url(99999), {"action": "generate"}).status_code, 404)

    def test_generate_shows_link_once_and_it_works(self):
        self.client.force_login(self.staff)
        resp = self.client.post(self.url(), {"action": "generate"})
        self.assertRedirects(resp, reverse("apitokens:dashboard"), fetch_redirect_response=False)
        self.a.refresh_from_db()
        self.assertTrue(self.a.portal_key_hash)
        first = self.client.get(reverse("apitokens:dashboard"))
        body = first.content.decode()
        self.assertIn('id="fresh-portal-link"', body)
        start = body.index("http://testserver/portal/enter/")
        link = body[start:body.index('"', start)]
        self.assertEqual(sha(link.split("/portal/enter/")[1].strip("/")), self.a.portal_key_hash)
        second = self.client.get(reverse("apitokens:dashboard"))
        self.assertNotIn("/portal/enter/", second.content.decode())
        self.assertNotIn("new_portal_link", self.client.session)
        # الرابط شغّال من كلينت تاني
        other = self.client_class()
        self.assertEqual(other.get(link.replace("http://testserver", "")).status_code, 302)

    def test_dashboard_never_leaks_hash(self):
        self.client.force_login(self.staff)
        self.client.post(self.url(), {"action": "generate"})
        self.a.refresh_from_db()
        for _ in range(2):
            self.assertNotIn(self.a.portal_key_hash, self.client.get(reverse("apitokens:dashboard")).content.decode())

    def test_regenerate_invalidates_old_and_revoke_clears(self):
        self.client.force_login(self.staff)
        old = self.key_for(self.a)
        self.client.post(self.url(), {"action": "generate"})
        self.assertEqual(self.client.get(reverse("portal:enter", args=[old])).status_code, 404)
        self.client.post(self.url(), {"action": "revoke"})
        self.a.refresh_from_db()
        self.assertEqual(self.a.portal_key_hash, "")
        self.assertNotIn("new_portal_link", self.client.session)

    def test_revoke_button_only_when_key_exists(self):
        self.client.force_login(self.staff)
        html = self.client.get(reverse("apitokens:dashboard")).content.decode()
        self.assertNotIn('value="revoke"', html)
        self.key_for(self.a)
        html = self.client.get(reverse("apitokens:dashboard")).content.decode()
        self.assertEqual(html.count('value="revoke"'), 1)
        self.assertEqual(html.count('value="generate"'), 2)


class ExpiredPageTests(PortalBase):
    def test_expired_is_403_static(self):
        resp = self.client.get(reverse("portal:expired"))
        self.assertEqual(resp.status_code, 403)
        self.assert_headers(resp)
        body = resp.content.decode()
        self.assertIn("invalid or has expired", body)
        self.assertIn("dir=\"rtl\"", body)
        self.assertNotIn("portal_merchant_id", body)

    def test_expired_shows_no_merchant_data(self):
        self.enter(self.key_for(self.a))
        body = self.client.get(reverse("portal:expired")).content.decode()
        self.assertNotIn(self.a.label + "<", body)
        self.assertNotIn(self.a.token, body)
