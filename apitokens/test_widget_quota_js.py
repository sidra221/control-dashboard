"""Static + small behaviour checks for the widget's per-shopper quota handling (embed.js).

The behaviour test runs the real embed.js inside Node's built-in ``vm`` with minimal DOM stubs;
it is skipped when Node isn't installed. A real-browser check is still the final word.
"""

import json
import unittest
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

EMBED = Path(settings.BASE_DIR) / "apitokens" / "static" / "apitokens" / "embed.js"
NODE = shutil.which("node")


def _js():
    return EMBED.read_text(encoding="utf-8")


class WidgetShopperIdStaticTests(SimpleTestCase):
    def setUp(self):
        self.js = _js()

    def test_shopper_id_storage_key_and_validation(self):
        self.assertIn('"cw_shopper_" + cleanId(STORE_KEY).slice(0, 8)', self.js)
        self.assertIn("/^[A-Za-z0-9_-]{8,64}$/", self.js)
        self.assertIn("crypto.randomUUID", self.js)
        self.assertIn("var SHOPPER_ID = (function () {", self.js)

    def test_every_storage_access_is_wrapped_in_try_catch(self):
        start = self.js.index("var SHOPPER_ID = (function () {")
        end = self.js.index("var shownSkus", start)
        block = self.js[start:end]
        for call in ("localStorage.getItem", "localStorage.setItem"):
            idx = block.index(call)
            before = block[:idx]
            self.assertGreater(before.rfind("try {"), before.rfind("} catch"), f"{call} is not inside try/catch")
        # And no other storage access anywhere in the widget outside that block.
        rest = "\n".join(
            line for line in (self.js[:start] + self.js[end:]).splitlines() if not line.lstrip().startswith("//")
        )
        self.assertNotIn("localStorage", rest)
        self.assertNotIn("sessionStorage", rest)

    def test_shopper_id_is_sent_with_all_four_request_kinds(self):
        js = self.js
        # chat body
        chat = js[js.index('"/widget/chat/"'):]
        self.assertIn("shopper_id: SHOPPER_ID", chat[: chat.index(".then(readJson)")])
        # page-product body
        pp = js[js.index('"/widget/page-product/"'):]
        self.assertIn("shopper_id: SHOPPER_ID", pp[: pp.index(".then(readJson)")])
        # event body
        ev = js[js.index('"/widget/event/"'):]
        self.assertIn("shopper_id: SHOPPER_ID", ev[: ev.index("keepalive")])
        # try-on multipart field
        self.assertIn('form.append("shopper_id", SHOPPER_ID)', js)


class WidgetQuotaHandlingStaticTests(SimpleTestCase):
    def setUp(self):
        self.js = _js()

    def test_each_new_error_code_is_handled_by_name(self):
        for code in ("shopper_limit_reached", "store_daily_limit", "rate_limited", "chat_limit_reached", "quota_exceeded"):
            self.assertIn(f'"{code}"', self.js, code)
        # quota_exceeded keeps its original message
        self.assertIn("خلص رصيد الصور المسموح لهذي الفترة.", self.js)

    def test_json_bodies_are_read_regardless_of_http_status(self):
        # Only readJson may call r.json(); 429 bodies are parsed there.
        self.assertEqual(len(re.findall(r"\br\.json\(\)", self.js)), 1)
        self.assertEqual(self.js.count(".then(readJson)"), 3)
        self.assertIn("r.status === 429", self.js)

    def test_all_error_paths_use_friendly_error_with_the_response_body(self):
        self.assertNotIn("friendlyError(data.error)", self.js)
        for m in re.finditer(r"friendlyError\(", self.js):
            line = self.js[m.start(): self.js.index("\n", m.start())]
            if line.lstrip().startswith("function friendlyError"):
                continue
            self.assertRegex(line + self.js[m.end(): m.end() + 200], r"friendlyError\([^;]*(data|retry_after_seconds)")

    def test_loading_indicators_removed_on_success_error_and_catch_paths(self):
        chat = self.js[self.js.index("function sendMessage()"): self.js.index("sendBtn.addEventListener")]
        self.assertGreaterEqual(chat.count("typingEl.remove()"), 2)
        tryon = self.js[self.js.index("function runTryon"): self.js.index("fileInput.addEventListener")]
        self.assertGreaterEqual(tryon.count("loadingEl.remove()"), 2)
        self.assertGreaterEqual(tryon.count("tryonBusy = false"), 2)

    def test_tryon_disable_enable_function_applies_to_existing_and_new_cards(self):
        js = self.js
        self.assertIn("function setTryonAvailability()", js)
        self.assertIn('root.querySelectorAll("button.tryon")', js)
        self.assertIn("function applyTryonButton(btn)", js)
        # new cards call it right after the button is built
        card = js[js.index("function addRecommendationCard"): js.index("function addTryonResultCard")]
        self.assertIn("applyTryonButton(tryBtn)", card)
        # the original label is restored when try-on is available again
        self.assertIn("btn.textContent = btn._origLabel", js)
        # unsupported products keep their existing hiding logic and are not touched by the disable code
        self.assertIn('if (rec.tryon_supported === false) tryBtn.style.display = "none";', js)
        apply_fn = js[js.index("function applyTryonButton"): js.index("function setTryonAvailability")]
        self.assertNotIn("style.display", apply_fn)

    def test_state_is_initialised_from_page_product_and_updated_from_tryon_responses(self):
        js = self.js
        for key in ("shopper_tryons_left", "tryon_available", "tryon_retry_after_seconds"):
            self.assertIn(key, js)
        self.assertIn("var tryonState = { shopperLeft: null, available: true, retryAfter: null };", js)
        self.assertIn("applyTryonResponse(data);", js)
        self.assertIn("data.shopper_remaining", js)

    def test_ui_strings(self):
        for text in (
            "خلصت تجاربك اليوم",
            "التجربة غير متاحة حالياً",
            "باقي لك تجربة وحدة اليوم",
            "No try-ons left today",
            "Try-on unavailable right now",
        ):
            self.assertIn(text, self.js)
        self.assertIn("lastTryonNoteShown", self.js)

    def test_no_leftover_references_to_removed_code(self):
        js = self.js
        # every helper that is called is defined
        for fn in (
            "readJson", "toCount", "formatWait", "updateTryonState", "applyTryonResponse",
            "applyTryonButton", "setTryonAvailability", "tryonBlockReason", "newShopperId", "cleanId",
        ):
            self.assertRegex(js, rf"function {fn}\(", fn)
        # old single-argument error helper usage / raw r.json chains are gone
        self.assertNotIn("return r.json();", js)

    def test_existing_behaviour_is_still_wired(self):
        js = self.js
        for needle in (
            "window.ControlCart.cartId()", "controlCart().add(", "trackEvent(\"add_to_cart\")", "trackEvent(\"buy_click\")",
            "function sizeBadge", "function markSize", "window.ControlWidget = {", "function uiLang()",
            "tryon_supported !== false", "var NUDGE_MESSAGES",
        ):
            self.assertIn(needle, js, needle)
        self.assertNotIn("tryonPrivacyNote", js)


@unittest.skipIf(NODE is None, "node is not installed")
class WidgetNodeTests(SimpleTestCase):
    def test_node_check_passes(self):
        result = subprocess.run([NODE, "--check", str(EMBED)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const code = fs.readFileSync(process.argv[2], "utf8");

class El {
  constructor(tag) {
    this.tagName = String(tag).toLowerCase(); this.children = []; this.parent = null; this.attrs = {};
    this.style = {}; this.listeners = {}; this.className = ""; this.textContent = ""; this.disabled = false;
    this.value = ""; this.files = []; this._html = null; this.scrollTop = 0; this.scrollHeight = 0;
    const self = this;
    this.classList = {
      add(c) { if (!self.hasClass(c)) self.className = (self.className + " " + c).trim(); },
      remove(c) { self.className = self.className.split(/\s+/).filter((x) => x && x !== c).join(" "); },
      contains(c) { return self.hasClass(c); },
    };
  }
  hasClass(c) { return this.className.split(/\s+/).indexOf(c) !== -1; }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html || ""; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  click() {}
  fire(t) { (this.listeners[t] || []).forEach((f) => f({ key: "Enter" })); }
  remove() { if (this.parent) { this.parent.children = this.parent.children.filter((x) => x !== this); this.parent = null; } }
  after(n) { const p = this.parent; if (!p) return; p.children.splice(p.children.indexOf(this) + 1, 0, n); n.parent = p; }
  replaceWith(n) { const p = this.parent; if (!p) return; p.children[p.children.indexOf(this)] = n; n.parent = p; this.parent = null; }
  attachShadow() { this.shadow = new El("shadow"); return this.shadow; }
  matches(sel) {
    const m = /^(\w+)?(?:\.([\w-]+))?(?:\[([\w-]+)="([^"]*)"\])?$/.exec(sel);
    if (!m) return false;
    if (m[1] && this.tagName !== m[1].toLowerCase()) return false;
    if (m[2] && !this.hasClass(m[2])) return false;
    if (m[3] && (this.attrs[m[3]] !== m[4] && this[m[3]] !== m[4])) return false;
    return true;
  }
  querySelectorAll(sel) {
    const out = [];
    (function walk(e) { e.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); }); })(this);
    return out;
  }
  querySelector(sel) {
    const found = this.querySelectorAll(sel)[0];
    if (found || this._html === null) return found || null;
    // innerHTML-built markup is not parsed by this stub: create the wanted child lazily.
    const m = /^(\w+)?(?:\.([\w-]+))?(?:\[([\w-]+)="([^"]*)"\])?$/.exec(sel);
    const el = new El(m[1] || "div");
    if (m[2]) el.className = m[2];
    if (m[3]) el.attrs[m[3]] = m[4];
    return this.appendChild(el);
  }
}

function textsOf(el) {
  const out = [];
  (function walk(e) { e.children.forEach((c) => { out.push({ cls: c.className, text: c.textContent }); walk(c); }); })(el);
  return out;
}

async function settle() { for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r)); }

async function scenario(opts) {
  const created = [];
  const body = new El("body");
  const timers = [];
  const fetchLog = [];
  const queues = JSON.parse(JSON.stringify(opts.responses || {}));
  const store = Object.assign({}, opts.storage || {});
  const sandbox = {
    console, URL, setImmediate, Promise, JSON, Math, Date, Array, Object, String, Number, isFinite, Error,
    FormData: class { constructor() { this.entries = []; } append(k, v) { this.entries.push([k, v]); } },
    CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },
    setTimeout(fn, ms) { timers.push({ fn, ms: ms || 0 }); return timers.length; },
    clearTimeout(id) { if (timers[id - 1]) timers[id - 1] = { fn() {}, ms: 0 }; },
    setInterval() { return 0; },
    crypto: { randomUUID: () => "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" },
    localStorage: opts.storageThrows
      ? { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } }
      : { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } },
    location: { href: "https://shop.example/p/1" },
    matchMedia: () => ({ matches: true }),
    dispatchEvent() {},
    fetch(url, init) {
      const path = url.replace("https://api.example.com", "");
      fetchLog.push({ path, init });
      const q = queues[path] || [{ status: 200, body: { ok: true } }];
      const item = q.length > 1 ? q.shift() : q[0];
      return Promise.resolve({
        status: item.status,
        json: () => (item.body === "BAD" ? Promise.reject(new Error("bad json")) : Promise.resolve(JSON.parse(JSON.stringify(item.body)))),
      });
    },
    document: {
      currentScript: { getAttribute: (n) => ({ "data-store-key": "sk_test_1234567890" }[n] || null), src: "https://api.example.com/static/apitokens/embed.js" },
      body,
      createElement(tag) { const e = new El(tag); created.push(e); return e; },
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  await settle();
  const find = (cls) => created.find((e) => e.hasClass(cls));
  const flush = (pred) => {
    for (let i = 0; i < timers.length; i++) {
      const t = timers[i];
      if (t && !t.done && pred(t.ms)) { t.done = true; t.fn(); }
    }
  };
  const api = {
    sandbox, fetchLog, store, created, find, flush,
    msgs: () => textsOf(find("panel")),
    tryBtns: () => find("panel").querySelectorAll("button.tryon"),
    async open() { sandbox.ControlWidget.open(); await settle(); },
    async chat(text) {
      sandbox.ControlWidget.open(text); flush((ms) => ms <= 2000); await settle();
    },
    async tryon() {
      const btn = api.tryBtns()[0];
      btn.fire("click");
      const input = created.find((e) => e.type === "file");
      if (input && !input.files.length && input.listeners.change) { input.files = [{ name: "me.jpg" }]; input.fire("change"); }
      await settle();
    },
  };
  return api;
}

const PRODUCT = { sku: "A1", title: "فستان", price: "100", currency: "SAR", image_url: "https://x/y.jpg", tryon_supported: true, product_url: "https://shop.example/p/1" };
const page = (extra) => ({ "/widget/page-product/": [{ status: 200, body: Object.assign({ ok: true, product: PRODUCT }, extra || {}) }] });

(async () => {
  const out = {};
  const btnState = (b) => ({ disabled: b.disabled, text: b.textContent, title: b.attrs.title || "" });

  // 1. limit already reached on page load -> existing page-product card is disabled from the start.
  let w = await scenario({ responses: page({ shopper_tryons_left: 0, tryon_available: false, tryon_retry_after_seconds: 7500 }) });
  await w.open();
  out.limitOnLoad = { btns: w.tryBtns().map(btnState), body: JSON.parse(w.fetchLog[0].init.body) };
  w.flush((ms) => ms > 1000000);
  out.limitOnLoadAfterReset = w.tryBtns().map(btnState);

  // 2. store exhausted on load
  w = await scenario({ responses: page({ shopper_tryons_left: null, tryon_available: false, tryon_retry_after_seconds: null }) });
  await w.open();
  out.storeOnLoad = w.tryBtns().map(btnState);

  // 3. missing keys mean available; try-on flow: success(1 left) -> rate limited (429) -> shopper limit
  w = await scenario({
    responses: Object.assign(page(), {
      "/widget/tryon/": [
        { status: 200, body: { ok: true, image_url: "https://x/r.jpg", remaining: 9, shopper_remaining: 1 } },
        { status: 429, body: { ok: false, error: "rate_limited", retry_after_seconds: 30 } },
        { status: 200, body: { ok: false, error: "shopper_limit_reached", shopper_remaining: 0, retry_after_seconds: 3600 } },
      ],
    }),
  });
  await w.open();
  out.availableByDefault = w.tryBtns().map(btnState);
  await w.tryon();
  out.afterSuccess = { btns: w.tryBtns().map(btnState), msgs: w.msgs().map((m) => m.text), form: w.fetchLog.filter((f) => f.path === "/widget/tryon/").map((f) => f.init.body.entries) };
  w.tryBtns()[0].fire("click"); await settle();
  out.afterRateLimited = { btns: w.tryBtns().map(btnState), last: w.msgs().filter((m) => m.cls === "msg error").map((m) => m.text) };
  w.tryBtns()[0].fire("click"); await settle();
  out.afterShopperLimit = { btns: w.tryBtns().map(btnState), last: w.msgs().filter((m) => m.cls === "msg error").map((m) => m.text), typing: w.msgs().filter((m) => m.cls === "typing").length };
  const notes = w.msgs().filter((m) => m.text === "باقي لك تجربة وحدة اليوم").length;
  out.noteCount = notes;
  // a card created later is disabled too
  w.sandbox.ControlWidget.open();
  out.cardCountBeforeNew = w.tryBtns().length;

  // 4. store_daily_limit and quota_exceeded disable; unparseable body does not wedge the widget
  for (const code of ["store_daily_limit", "quota_exceeded"]) {
    w = await scenario({ responses: Object.assign(page(), { "/widget/tryon/": [{ status: 200, body: { ok: false, error: code, retry_after_seconds: 5000, remaining: 0 } }] }) });
    await w.open(); await w.tryon();
    out[code] = { btns: w.tryBtns().map(btnState), errors: w.msgs().filter((m) => m.cls === "msg error").map((m) => m.text), typing: w.msgs().filter((m) => m.cls === "typing").length };
  }
  w = await scenario({ responses: Object.assign(page(), { "/widget/tryon/": [{ status: 500, body: "BAD" }] }) });
  await w.open(); await w.tryon();
  out.badBody = { btns: w.tryBtns().map(btnState), errors: w.msgs().filter((m) => m.cls === "msg error").map((m) => m.text), typing: w.msgs().filter((m) => m.cls === "typing").length };

  // 5. chat limit / rate limit errors by body; typing indicator removed; send button re-enabled; shopper_id sent
  for (const [status, err, secs] of [[429, "rate_limited", 45], [200, "chat_limit_reached", 5400]]) {
    w = await scenario({ responses: Object.assign(page(), { "/widget/chat/": [{ status, body: { ok: false, error: err, retry_after_seconds: secs } }] }) });
    await w.chat("هلا");
    const chatCall = w.fetchLog.find((f) => f.path === "/widget/chat/");
    out[err] = {
      errors: w.msgs().filter((m) => m.cls === "msg error").map((m) => m.text),
      typing: w.msgs().filter((m) => m.cls === "typing").length,
      sendDisabled: w.find("panel").querySelector(".send").disabled,
      body: chatCall && JSON.parse(chatCall.init.body),
    };
  }

  // 6. shopper id persistence / fallback; event body
  w = await scenario({ responses: page(), storage: { cw_shopper_sk_test_: "SavedShopper-01" } });
  await w.open();
  w.find("panel").querySelector(".msgs");
  const buy = w.find("panel").querySelector("a.buy");
  buy.fire("click"); await settle();
  out.persisted = {
    pageBody: JSON.parse(w.fetchLog[0].init.body),
    event: w.fetchLog.filter((f) => f.path === "/widget/event/").map((f) => JSON.parse(f.init.body)),
  };
  w = await scenario({ responses: page() });
  out.fresh = { store: w.store, body: JSON.parse(w.fetchLog[0].init.body) };
  w = await scenario({ responses: page(), storageThrows: true });
  await w.chat("هلا");
  out.blocked = { page: JSON.parse(w.fetchLog[0].init.body), chat: JSON.parse(w.fetchLog.find((f) => f.path === "/widget/chat/").init.body) };

  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


@unittest.skipIf(NODE is None, "node is not installed")
class WidgetNodeBehaviourTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "harness.js"
            script.write_text(HARNESS, encoding="utf-8")
            proc = subprocess.run([NODE, str(script), str(EMBED)], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f"harness failed: {proc.stderr}\n{proc.stdout}")
        cls.out = json.loads(proc.stdout.strip().splitlines()[-1])

    SHOPPER_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

    def test_limit_reached_on_load_disables_page_product_card(self):
        s = self.out["limitOnLoad"]
        self.assertEqual(len(s["btns"]), 1)
        self.assertTrue(s["btns"][0]["disabled"])
        self.assertEqual(s["btns"][0]["text"], "خلصت تجاربك اليوم")
        self.assertIn("ساعتين", s["btns"][0]["title"])
        self.assertIn("5 دقائق", s["btns"][0]["title"])
        self.assertRegex(s["body"]["shopper_id"], self.SHOPPER_RE)
        # when the retry time passes the button comes back with its original label
        back = self.out["limitOnLoadAfterReset"][0]
        self.assertFalse(back["disabled"])
        self.assertEqual(back["text"], "جرّب شكلك فيه 📸")

    def test_store_unavailable_on_load(self):
        b = self.out["storeOnLoad"][0]
        self.assertTrue(b["disabled"])
        self.assertEqual(b["text"], "التجربة غير متاحة حالياً")

    def test_missing_keys_mean_available(self):
        b = self.out["availableByDefault"][0]
        self.assertFalse(b["disabled"])
        self.assertEqual(b["text"], "جرّب شكلك فيه 📸")

    def test_tryon_success_sends_shopper_id_and_shows_one_left_note_once(self):
        form = dict(self.out["afterSuccess"]["form"][0])
        self.assertRegex(form["shopper_id"], self.SHOPPER_RE)
        self.assertFalse(self.out["afterSuccess"]["btns"][0]["disabled"])
        self.assertIn("باقي لك تجربة وحدة اليوم", self.out["afterSuccess"]["msgs"])
        self.assertEqual(self.out["noteCount"], 1)

    def test_rate_limited_429_body_is_parsed_and_does_not_disable(self):
        s = self.out["afterRateLimited"]
        self.assertFalse(s["btns"][0]["disabled"])
        self.assertTrue(any("30 ثانية" in m for m in s["last"]), s["last"])

    def test_shopper_limit_reached_disables_buttons_and_says_when_to_retry(self):
        s = self.out["afterShopperLimit"]
        self.assertTrue(s["btns"][0]["disabled"])
        self.assertEqual(s["btns"][0]["text"], "خلصت تجاربك اليوم")
        self.assertTrue(any("ساعة" in m for m in s["last"]), s["last"])
        self.assertEqual(s["typing"], 0)

    def test_store_daily_limit_and_quota_exceeded_disable(self):
        for code in ("store_daily_limit", "quota_exceeded"):
            s = self.out[code]
            self.assertTrue(s["btns"][0]["disabled"], code)
            self.assertEqual(s["btns"][0]["text"], "التجربة غير متاحة حالياً", code)
            self.assertEqual(s["typing"], 0, code)
            self.assertEqual(len(s["errors"]), 1, code)
        self.assertIn("خلص رصيد الصور المسموح لهذي الفترة.", self.out["quota_exceeded"]["errors"][0])
        self.assertIn("جرّب بكرة", self.out["store_daily_limit"]["errors"][0])

    def test_unparseable_error_body_is_a_friendly_error_and_does_not_wedge(self):
        s = self.out["badBody"]
        self.assertEqual(s["typing"], 0)
        self.assertEqual(len(s["errors"]), 1)
        self.assertFalse(s["btns"][0]["disabled"])

    def test_chat_limit_errors_by_body(self):
        rl = self.out["rate_limited"]
        self.assertTrue(any("45 ثانية" in m for m in rl["errors"]), rl["errors"])
        cl = self.out["chat_limit_reached"]
        self.assertTrue(any("ساعة و30 دقيقة" in m for m in cl["errors"]), cl["errors"])
        for s in (rl, cl):
            self.assertEqual(s["typing"], 0)
            self.assertFalse(s["sendDisabled"])
            self.assertRegex(s["body"]["shopper_id"], self.SHOPPER_RE)

    def test_shopper_id_persistence_and_storage_fallback(self):
        p = self.out["persisted"]
        self.assertEqual(p["pageBody"]["shopper_id"], "SavedShopper-01")
        self.assertEqual([e["shopper_id"] for e in p["event"]], ["SavedShopper-01"])
        fresh = self.out["fresh"]
        self.assertEqual(fresh["store"]["cw_shopper_sk_test_"], fresh["body"]["shopper_id"])
        self.assertRegex(fresh["body"]["shopper_id"], self.SHOPPER_RE)
        blocked = self.out["blocked"]
        self.assertRegex(blocked["page"]["shopper_id"], self.SHOPPER_RE)
        self.assertEqual(blocked["page"]["shopper_id"], blocked["chat"]["shopper_id"])
