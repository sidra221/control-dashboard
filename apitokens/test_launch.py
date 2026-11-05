"""Launch-readiness tests: production SECRET_KEY check, consent-aware widget storage, typing cap.

The widget tests run embed.js inside a Node `vm` sandbox with a tiny fake DOM (a Proxy element),
fake fetch and in-memory localStorage/sessionStorage. They are skipped (loudly) when `node` is missing.
"""

import json
import re
import secrets
import shutil
import subprocess
from io import StringIO
from pathlib import Path
from unittest import mock, skipUnless

from django.apps import apps
from django.core import checks
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.test import SimpleTestCase, TestCase, override_settings

from .apps import check_debug_on_public_host, check_secret_key, secret_key_problem

STRONG_KEY = secrets.token_urlsafe(64)
EMBED_JS = Path(__file__).resolve().parent / "static" / "apitokens" / "embed.js"


def _check_with(debug, key, startup_debug=False, hosts=None):
    """يشغّل فحص الـ SECRET_KEY كأنه إنتاج: DEBUG الحالي + DEBUG وقت الإقلاع."""
    overrides = {"DEBUG": debug, "SECRET_KEY": key}
    if hosts is not None:
        overrides["ALLOWED_HOSTS"] = hosts
    with override_settings(**overrides):
        with mock.patch.object(apps.get_app_config("apitokens"), "startup_debug", startup_debug):
            return check_secret_key(None), check_debug_on_public_host(None)


class SecretKeyCheckTests(SimpleTestCase):
    def test_strong_key_passes_in_production(self):
        errors, warnings = _check_with(False, STRONG_KEY)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_weak_keys_are_rejected_in_production(self):
        weak = {
            "django-insecure prefix": "django-insecure-" + STRONG_KEY,
            "change-me": "change-me",
            "change-me upper": "CHANGE-ME",
            "contains change-me": STRONG_KEY + "-change-me-" + STRONG_KEY,
            "contains changeme": "xK9" * 20 + "ChangeMe" + "qZ7" * 5,
            "contains secret": STRONG_KEY + "My_Secret" + STRONG_KEY,
            "too short": STRONG_KEY[:49],
            "few distinct": "ab" * 40,
            "few distinct 11": "abcdefghijk" * 6,
        }
        for label, key in weak.items():
            with self.subTest(label):
                errors, _ = _check_with(False, key)
                self.assertEqual([e.id for e in errors], ["apitokens.E003"], key)
                self.assertIsInstance(errors[0], checks.Error)

    def test_boundaries(self):
        # 50 chars with 12 distinct passes; 49 chars or 11 distinct fails.
        ok = "abcdefghijkl" * 5  # 60 chars, 12 distinct
        self.assertIsNone(secret_key_problem(ok))
        self.assertIsNone(secret_key_problem(STRONG_KEY[:50]) if "secret" not in STRONG_KEY[:50].lower() else None)
        self.assertIsNotNone(secret_key_problem("abcdefghijk" * 6))
        self.assertIsNotNone(secret_key_problem(None))
        self.assertIsNotNone(secret_key_problem(""))

    def test_error_explains_identity_forgery_and_generation(self):
        errors, _ = _check_with(False, "change-me")
        text = errors[0].msg + " " + errors[0].hint
        self.assertIn("forge", text)
        self.assertIn("identit", text)
        self.assertIn("secrets.token_urlsafe(64)", text)

    def test_not_enforced_in_debug(self):
        errors, _ = _check_with(True, "django-insecure-dev")
        self.assertEqual(errors, [])

    def test_not_enforced_under_test_runner_with_debug_start(self):
        # Django's test runner flips settings.DEBUG to False, but the app started in debug.
        errors, _ = _check_with(False, "django-insecure-dev", startup_debug=True)
        self.assertEqual(errors, [])

    def test_current_test_settings_do_not_fail_checks(self):
        # The runner itself runs all checks before the suite (and would abort on E003); here we assert
        # our two checks are quiet with the settings this suite is running under (DEBUG flipped off).
        self.assertEqual(check_secret_key(None), [])
        self.assertEqual([m for m in check_debug_on_public_host(None) if m.is_serious()], [])

    def test_debug_with_public_host_warns(self):
        _, warnings = _check_with(True, "django-insecure-dev", hosts=["localhost", "shop.example.com"])
        self.assertEqual([w.id for w in warnings], ["apitokens.W002"])
        self.assertIn("shop.example.com", warnings[0].msg)
        self.assertIsInstance(warnings[0], checks.Warning)

    def test_debug_with_local_hosts_does_not_warn(self):
        _, warnings = _check_with(True, "x", hosts=["localhost", "127.0.0.1", "[::1]", "dev.localhost"])
        self.assertEqual(warnings, [])

    def test_no_debug_warning_in_production(self):
        _, warnings = _check_with(False, STRONG_KEY, hosts=["shop.example.com"])
        self.assertEqual(warnings, [])

class SecretKeyCommandTests(TestCase):
    """`manage.py check` end to end (the check framework touches the DB for model checks)."""

    def test_check_deploy_style_run_does_not_crash(self):
        with override_settings(DEBUG=False, SECRET_KEY=STRONG_KEY):
            with mock.patch.object(apps.get_app_config("apitokens"), "startup_debug", False):
                out = StringIO()
                call_command("check", deploy=True, stdout=out, stderr=StringIO())  # warnings are fine
        self.assertNotIn("E003", out.getvalue())

    def test_check_command_fails_with_weak_key_in_production(self):
        with override_settings(DEBUG=False, SECRET_KEY="change-me"):
            with mock.patch.object(apps.get_app_config("apitokens"), "startup_debug", False):
                with self.assertRaises(SystemCheckError) as ctx:
                    call_command("check", stdout=StringIO(), stderr=StringIO())
        self.assertIn("apitokens.E003", str(ctx.exception))


# ---------------------------------------------------------------- widget (Node sandbox)

NODE = shutil.which("node")

HARNESS = r"""
const vm = require("vm");
const fs = require("fs");

const src = fs.readFileSync(process.env.EMBED_JS, "utf8");
const STORE_KEY = "storekey_1234567890abcdef";
const V2KEY = "ctl_w2_" + STORE_KEY.slice(0, 16);

function makeEl() {
  const own = {
    classList: { contains: () => false, add() {}, remove() {}, toggle() {} },
    style: {}, dataset: {}, files: [],
  };
  return new Proxy(function () {}, {
    get(t, k) {
      if (k in own) return own[k];
      if (k === Symbol.toPrimitive) return () => "";
      return () => makeEl();
    },
    set(t, k, v) { own[k] = v; return true; },
  });
}

function makeStorage(initial) {
  const m = new Map(Object.entries(initial || {}));
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)); },
    removeItem: (k) => { m.delete(k); },
    dump: () => Object.fromEntries(m),
  };
}

const tick = () => new Promise((r) => setImmediate(r));

async function load(opts) {
  opts = opts || {};
  const local = makeStorage(opts.local);
  const session = makeStorage(opts.session);
  const calls = [];
  const attrs = { "data-store-key": STORE_KEY };
  if (opts.consent) attrs["data-consent"] = opts.consent;
  const script = { getAttribute: (n) => (n in attrs ? attrs[n] : null), src: "https://dash.example.com/static/apitokens/embed.js" };
  const bootstrap = opts.bootstrap || {
    ok: true, engine: "v2", returning: false, session_token: "sess1", shopper_token: "shop1", session_id: "sid1", messages: [],
  };
  const fetchStub = (url, init) => {
    const path = url.replace("https://dash.example.com", "");
    const body = init && init.body ? JSON.parse(init.body) : null;
    calls.push({ path, body });
    if (path === "/widget/v2/forget/" && opts.forgetFails) return Promise.reject(new Error("offline"));
    let data = { ok: true };
    if (path === "/widget/v2/bootstrap/") data = bootstrap;
    return Promise.resolve({ json: () => Promise.resolve(data) });
  };
  const ctx = {
    localStorage: local, sessionStorage: session,
    location: { href: "https://shop.example.com/p/1" },
    crypto: require("crypto").webcrypto,
    document: { currentScript: script, createElement: () => makeEl(), body: makeEl() },
    fetch: fetchStub, console, URL,
    setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {},
  };
  ctx.window = ctx; // window.X and bare X are the same global, like a browser
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  for (let i = 0; i < 5; i++) await tick();
  return { ctx, local, session, calls, W: () => ctx.ControlWidget };
}

function summary(h) {
  return { local: h.local.dump(), session: h.session.dump() };
}

(async () => {
  const out = {};
  const tokens = (s) => (s[V2KEY] ? JSON.parse(s[V2KEY]) : null);

  // 1. no attribute: exactly as before (localStorage)
  let h = await load({});
  out.legacy = { ...summary(h), hasConsent: h.W().hasConsent(), grant: h.W().grantConsent() };

  // 2. required, no consent yet: sessionStorage only, nothing in localStorage
  h = await load({ consent: "required" });
  out.required = { ...summary(h), hasConsent: h.W().hasConsent(), bootstrapBody: h.calls[0].body };

  // 3. grant moves tokens to localStorage and persists the flag
  h = await load({ consent: "required" });
  const granted = h.W().grantConsent();
  out.granted = { ...summary(h), ret: granted, hasConsent: h.W().hasConsent() };

  // 4. returning visitor with stored consent: reads localStorage state
  h = await load({
    consent: "required",
    local: { [V2KEY + "_consent"]: "1", [V2KEY]: JSON.stringify({ session_token: "oldS", shopper_token: "oldH", session_id: "oldId" }) },
  });
  out.returning = { hasConsent: h.W().hasConsent(), bootstrapBody: h.calls[0].body };

  // 5. required but NO consent flag: stale localStorage state must be ignored
  h = await load({
    consent: "required",
    local: { [V2KEY]: JSON.stringify({ session_token: "oldS", shopper_token: "oldH", session_id: "oldId" }) },
  });
  out.staleIgnored = { hasConsent: h.W().hasConsent(), bootstrapBody: h.calls[0].body, ...summary(h) };

  // 6. revoke after grant: forget call with the shopper token, everything cleared, back to session mode
  h = await load({ consent: "required" });
  h.W().grantConsent();
  const revokeRet = await h.W().revokeConsent();
  const forgetCall = h.calls.find((c) => c.path === "/widget/v2/forget/");
  out.revoked = { ...summary(h), ret: revokeRet, hasConsent: h.W().hasConsent(), forgetBody: forgetCall && forgetCall.body };

  // 7. revoke when the server is unreachable: persistence stops, tokens stay in the session for retry
  h = await load({ consent: "required", forgetFails: true });
  h.W().grantConsent();
  const failRet = await h.W().revokeConsent();
  out.revokeOffline = { ...summary(h), ret: failRet, hasConsent: h.W().hasConsent() };

  // 8. forget() in session mode clears sessionStorage too; consent flag untouched
  h = await load({ consent: "required" });
  const forgetRet = await h.W().forget();
  out.forgetSession = { ...summary(h), ret: forgetRet };

  // 9. v1 merchant: nothing persisted anywhere
  h = await load({ consent: "required", bootstrap: { ok: true, engine: "v1" } });
  out.v1 = summary(h);
  h = await load({ bootstrap: { ok: true, engine: "v1" } });
  out.v1NoAttr = summary(h);

  // 10. non-required revoke still forgets (server delete + clear)
  h = await load({});
  const r10 = await h.W().revokeConsent();
  out.legacyRevoke = { ...summary(h), ret: r10, forgetCalled: h.calls.some((c) => c.path === "/widget/v2/forget/") };

  // 11. public API surface
  h = await load({});
  out.api = Object.keys(h.W()).sort();

  process.stdout.write(JSON.stringify({ V2KEY, out }));
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""


@skipUnless(NODE, "node is not installed: widget storage routing tests skipped (run them where node exists)")
class WidgetConsentTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        proc = subprocess.run(
            [NODE, "-e", HARNESS],
            env={"EMBED_JS": str(EMBED_JS), "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            raise AssertionError(f"node harness failed:\n{proc.stderr}\n{proc.stdout}")
        data = json.loads(proc.stdout)
        cls.key = data["V2KEY"]
        cls.out = data["out"]

    def tokens(self, storage):
        raw = storage.get(self.key)
        return json.loads(raw) if raw else None

    def test_syntax(self):
        proc = subprocess.run([NODE, "--check", str(EMBED_JS)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_without_attribute_uses_local_storage_as_before(self):
        o = self.out["legacy"]
        self.assertEqual(self.tokens(o["local"])["shopper_token"], "shop1")
        self.assertEqual(self.tokens(o["local"])["session_token"], "sess1")
        self.assertIsNone(self.tokens(o["session"]))
        self.assertNotIn(self.key + "_consent", o["local"])
        self.assertTrue(o["hasConsent"])
        self.assertTrue(o["grant"])

    def test_required_without_consent_is_session_only(self):
        o = self.out["required"]
        self.assertEqual(o["local"], {})
        self.assertEqual(self.tokens(o["session"])["shopper_token"], "shop1")
        self.assertEqual(self.tokens(o["session"])["session_id"], "sid1")
        self.assertFalse(o["hasConsent"])

    def test_grant_moves_tokens_to_local_storage(self):
        o = self.out["granted"]
        self.assertTrue(o["ret"])
        self.assertTrue(o["hasConsent"])
        self.assertEqual(o["local"][self.key + "_consent"], "1")
        self.assertEqual(self.tokens(o["local"])["shopper_token"], "shop1")
        self.assertIsNone(self.tokens(o["session"]))

    def test_consented_returning_visitor_reuses_stored_identity(self):
        o = self.out["returning"]
        self.assertTrue(o["hasConsent"])
        self.assertEqual(o["bootstrapBody"]["shopper_token"], "oldH")

    def test_stale_local_state_ignored_without_consent(self):
        o = self.out["staleIgnored"]
        self.assertFalse(o["hasConsent"])
        self.assertIsNone(o["bootstrapBody"]["shopper_token"])

    def test_revoke_forgets_and_returns_to_session_mode(self):
        o = self.out["revoked"]
        self.assertTrue(o["ret"])
        self.assertFalse(o["hasConsent"])
        self.assertEqual(o["forgetBody"], {"shopper_token": "shop1"})
        self.assertEqual(o["local"], {})
        self.assertIsNone(self.tokens(o["session"]))

    def test_revoke_offline_stops_persistence_and_keeps_tokens_in_session(self):
        o = self.out["revokeOffline"]
        self.assertFalse(o["ret"])
        self.assertFalse(o["hasConsent"])
        self.assertEqual(o["local"], {})
        self.assertEqual(self.tokens(o["session"])["shopper_token"], "shop1")

    def test_forget_clears_session_storage_too(self):
        o = self.out["forgetSession"]
        self.assertTrue(o["ret"])
        self.assertIsNone(self.tokens(o["session"]))
        self.assertEqual(o["local"], {})

    def test_v1_merchants_persist_nothing(self):
        self.assertEqual(self.out["v1"], {"local": {}, "session": {}})
        self.assertEqual(self.out["v1NoAttr"], {"local": {}, "session": {}})

    def test_revoke_without_attribute_still_forgets(self):
        o = self.out["legacyRevoke"]
        self.assertTrue(o["forgetCalled"])
        self.assertTrue(o["ret"])
        self.assertEqual(o["local"], {})

    def test_public_api(self):
        self.assertEqual(self.out["api"], ["forget", "grantConsent", "hasConsent", "open", "revokeConsent"])


class WidgetTypingTests(SimpleTestCase):
    def test_typing_animation_capped_near_1_2_seconds(self):
        src = EMBED_JS.read_text(encoding="utf-8")
        self.assertIn("Math.max(4, Math.min(14, 1200 / chars.length))", src)
        self.assertNotIn("3000 / chars.length", src)
        self.assertIn("reduceMotion", src)
        # worst case: delay = 14ms/char only applies up to 85 chars (1.2 s); longer replies speed up to 4ms/char.
        match = re.search(r"Math\.max\((\d+), Math\.min\((\d+), (\d+) / chars\.length\)\)", src)
        lo, hi, budget = map(int, match.groups())
        for n in (1, 30, 85, 300, 600):
            self.assertLessEqual(n * max(lo, min(hi, budget / n)), max(budget, n * lo) + 1e-6)
        self.assertLessEqual(300 * max(lo, min(hi, budget / 300)), 1300)
