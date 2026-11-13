from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from . import portal_auth


class ClientIpTests(SimpleTestCase):
    def req(self, **meta):
        return RequestFactory().post("/portal/login/", **meta)

    def test_default_ignores_spoofable_headers(self):
        r = self.req(REMOTE_ADDR="10.0.0.1", HTTP_X_REAL_IP="1.2.3.4", HTTP_X_FORWARDED_FOR="5.6.7.8")
        self.assertEqual(portal_auth._client_ip(r), "10.0.0.1")

    @override_settings(PORTAL_CLIENT_IP_HEADER="HTTP_X_REAL_IP")
    def test_configured_header_is_used(self):
        r = self.req(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="1.2.3.4")
        self.assertEqual(portal_auth._client_ip(r), "1.2.3.4")

    @override_settings(PORTAL_CLIENT_IP_HEADER="HTTP_X_FORWARDED_FOR")
    def test_only_the_last_hop_is_trusted(self):
        r = self.req(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="9.9.9.9, 1.2.3.4")
        self.assertEqual(portal_auth._client_ip(r), "1.2.3.4")

    @override_settings(PORTAL_CLIENT_IP_HEADER="HTTP_X_REAL_IP")
    def test_missing_header_falls_back_to_remote_addr(self):
        self.assertEqual(portal_auth._client_ip(self.req(REMOTE_ADDR="10.0.0.1")), "10.0.0.1")

    @override_settings(PORTAL_CLIENT_IP_HEADER="HTTP_X_REAL_IP")
    def test_throttle_is_per_client_not_global_behind_a_proxy(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for _ in range(portal_auth.IP_MAX_LOGIN_POSTS):
            self.assertFalse(portal_auth._ip_throttled(self.req(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="1.1.1.1")))
        self.assertTrue(portal_auth._ip_throttled(self.req(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="1.1.1.1")))
        # زائر تاني خلف نفس البروكسي ما بيتأثر
        self.assertFalse(portal_auth._ip_throttled(self.req(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="2.2.2.2")))
