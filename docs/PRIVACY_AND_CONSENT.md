# Privacy and consent (v2 memory engine)

Audience: the product owner and merchants. This is a practical engineering inventory plus a
to-do list for the legal side. It is **not legal advice**: have a Saudi-qualified lawyer review
the notice text, the processor contracts and the cross-border transfer position before launch.

Scope: merchants switched to the **v2** engine (server-side customer memory). v1 merchants keep
the conversation only in the shopper's browser tab and send the history with every request; they
are not covered by the memory sections below (they still use the LLM and kie.ai processors).

---

## 1. What v2 stores, and where

All rows are scoped to one merchant (`ApiToken`). The same person at two merchants has two
unrelated profiles; nothing is shared across merchants.

| Table | What it holds | Personal data? |
| --- | --- | --- |
| `CustomerProfile` | Pseudonymous shopper: random `public_id`, detected `language`, `gender_signal` (`""`/female/male, inferred from wording), `visit_count`, `first_seen_at`, `last_seen_at`, `expires_at`. No name, phone, email or IP. | Yes (pseudonymous identifier plus an inferred attribute) |
| `Conversation` | One chat session: counters (try-ons, buy clicks, add-to-carts), recommended SKUs, discount code offered, platform cart id, order id / total / currency when an order is attributed, a small `state` JSON. The legacy v1 `messages` JSON is empty for v2. | Yes (links to the profile, order attribution) |
| `Message` | Every message: the **verbatim customer free text** and the assistant reply, plus `meta` (cards / offer shown) and `client_msg_id`. `event` rows record try-on and page-view events. | Yes. Customers may type anything (names, phone numbers, health or body details, addresses). We do not detect or redact it. |
| `PreferenceFact` | Extracted preferences (size, colour, style, budget, "gift for X"...), polarity like/avoid, source `explicit` or `inferred`, a short `evidence` quote from the customer's own words, status. | Yes |
| `ProductReaction` | Per product: shown / viewed / asked / liked / rejected / tried_on / buy_click / add_to_cart, with `reason_type`, a short `reason_text` and the price at the time. | Yes |
| `Objection` | Purchase objections (price, trust, quality, fit, shipping, timing) with a short text. | Yes |
| `TurnTrace` | Per-turn diagnostics: analyzer output, memory changes, constraints, output-guard results, LLM call count, latency, models used, error. Analyzer output can echo customer wording. | Yes (derived) |

Browser side (the widget): `session_token`, `shopper_token` and `session_id` in `localStorage`
(or in `sessionStorage` when `data-consent="required"` and consent has not been given, see
section 5), and a `..._open` flag in `sessionStorage` (panel open or closed). The tokens are
HMAC-signed with `SECRET_KEY` and carry the merchant id; a token is useless at another merchant.
A weak `SECRET_KEY` means forgeable customer identities, which is why production refuses to
start with one (`apitokens.E003`).

### Photos

We do **not** store try-on photos. The shopper's photo goes from the browser through our
endpoint to **kie.ai** (a third-party image-generation processor) and the generated result
**URL** is returned to the widget. How long kie.ai keeps the photo and the result is governed by
kie.ai's own terms, which we do not control. Open item: get kie.ai's retention, deletion and
no-training terms in writing.

### Third-party processors

| Processor | Receives | Purpose |
| --- | --- | --- |
| LLM provider (`LLM_PROVIDER`: Anthropic, OpenAI, Gemini or Groq) | Conversation text, memory facts injected into the prompt, catalogue excerpts | Generate replies |
| Analyzer LLM (`ANALYZER_PROVIDER`, defaults to the chat provider) | The latest customer message and recent context | Extract preferences and objections |
| kie.ai | Shopper photo, product image URL | Virtual try-on |

All three are outside Saudi Arabia (see section 4, point 4).

---

## 2. Retention

- **Profile retention** is per merchant: `ApiToken.profile_retention_days`, default **90** days.
  `expires_at` is refreshed on every visit (`last_seen_at` plus the retention), so an active
  customer is kept and an inactive one expires.
- **`purge_expired_data`** (run daily by cron, see `docs/DEPLOYMENT_AND_ROLLOUT.md`) forgets every
  profile whose `expires_at` has passed: it deletes their messages, traces, facts, reactions,
  objections and the profile row, and keeps the conversation counters (see section 3).
- **Turn traces** older than **60 days** are deleted by the same command (`--trace-days N`
  changes it), whether or not the profile expired.
- **Identity tokens**: the session token lasts 24 h and is renewed on every turn
  (`V2_SESSION_TTL_SECONDS`), the shopper token 180 days (`V2_SHOPPER_TTL_SECONDS`). They only
  bound how long a browser can *claim* a profile; actual data retention is the profile
  retention above.
- **Rate-limit counters** (`RateLimitBucket`): the counter key contains the client IP for the
  rate-limit window; `purge_rate_limits` (weekly) removes expired ones.
- Web-server access logs and application logs are outside this table. The application logs
  merchant ids, rule names and, for some security events, client IPs, not message text. Set log
  rotation (suggest 30 days) and decide who can read them.

---

## 3. Forget me and memory review

### Forget me: `ControlWidget.forget()` / `POST /widget/v2/forget/`

The body is `{"shopper_token": "..."}` with the store key header. In one transaction it deletes
all `Message` rows, all `TurnTrace` rows, the `PreferenceFact`, `ProductReaction` and
`Objection` rows (cascade) and the `CustomerProfile` itself, for every conversation of that
profile. The widget then clears its own storage (localStorage and sessionStorage).

**What stays:** the `Conversation` row, with `messages`, `recommended_skus` and `state` emptied
and the link to the profile removed. It keeps only the counters (try-ons, buy clicks,
add-to-carts, discount code, platform cart id) and the order id / total / currency if an order
was attributed. The reason is the merchant's sales attribution and billing quotas. It contains
no customer text and no link to a person. If the lawyer decides an attributed order id is still
personal data, add an option to blank `order_id` and `platform_cart_id` on forget.

Behaviour notes: the call is rate limited (30 per minute per IP and merchant). If the token is
invalid or expired the endpoint returns `{"ok": true, "forgotten": false}` (nothing to delete).
Data already sent to the LLM and kie.ai processors is not recalled by this call.

### Review and correct: `POST /widget/v2/memory/`

`action: "list"` returns the active facts (`id`, `key`, `value`, `polarity`, `source`,
`subject`) and the rejected SKUs. `remove_fact` (with `fact_id`) retracts a fact: it is kept with
status `retracted` and no longer used. `unreject` (with `sku`) lets a rejected product be
recommended again. The widget UI does **not** expose this yet; the store (or a later widget
version) must add a "what you remember about me" screen if it wants to offer access and
correction. The endpoint does not return the messages themselves, so a full access request
(export of everything held about one person) is currently a manual task for staff in the admin,
and needs the shopper's `public_id`.

---

## 4. Saudi PDPL: what the owner must decide and implement

(Personal Data Protection Law, Royal Decree M/19 of 1443 AH, with its executive regulations;
SDAIA is the regulator. Confirm the current text with counsel.)

1. **Consent before persistent memory.** Remembering a shopper across visits (shopper token in
   `localStorage`, profile kept up to 90 days) is a purpose beyond answering the current
   question. Treat it as needing opt-in consent. Technical support exists:
   `data-consent="required"` (section 5). **Decision needed:** make it mandatory for all KSA
   merchants, or leave it per merchant.
2. **Notice (transparency).** Before collection the shopper must be told who the controller is
   (the merchant; we act as processor for the merchant, which needs a data-processing
   agreement), what is stored, why, for how long, who receives it (LLM providers, kie.ai), that
   it goes abroad, and how to withdraw. Template below.
3. **Purpose limitation and minimisation.** Memory is used only to personalise recommendations
   and the conversation for that merchant. Do not use it for advertising, cross-merchant
   profiling or model training. Customers can type sensitive data into free text; consider a
   redaction filter for phone and ID numbers (open decision).
4. **Cross-border transfer.** Conversation text goes to an LLM provider and photos to kie.ai,
   all outside the Kingdom. The PDPL transfer rules (and SDAIA's regulations on transfers)
   require a legal basis and safeguards: record which provider and region each merchant uses,
   sign data-processing agreements, and confirm each provider's no-training and retention
   settings (for example API zero-retention options). **Decision needed:** whether the
   customer's consent covers the transfer, or another basis or an SDAIA assessment is needed.
5. **Data-subject rights.** Information, access, correction, deletion, withdrawal of consent and
   a copy of the data. Status: deletion is `forget()` (done); correction is `/widget/v2/memory/`
   (API only); access and portability are a manual export (open); withdrawal is
   `revokeConsent()`. Define the response time (confirm the legal period with counsel) and who
   handles requests at the merchant and at us.
6. **Breach handling.** Write the procedure: who decides, and notification to SDAIA and to the
   affected people without undue delay when harm is likely (confirm the exact deadline with
   counsel). Technical starting points: rotating `DJANGO_SECRET_KEY` invalidates every customer
   token, rotating the consume key and store keys limits API abuse, and `TurnTrace` plus the logs
   show what was accessed.
7. **DPO / contact.** Name a data-protection contact, publish an email (`SUPPORT_EMAIL` shows on
   `/privacy/`) and keep a record of processing activities.
8. **Children.** Fashion stores are used by minors; decide the stance (age gate or not).
9. **Merchant contract.** Merchants are the controllers. The terms must say so, forbid putting
   the widget on pages that collect special categories of data, and describe our list of
   processors and how changes to it are notified.

### Notice template (adapt, then have it reviewed)

Arabic (Saudi dialect, for the cookie / preferences banner):

> نستخدم مساعد التسوّق الذكي عشان نساعدك تختار. تبغى نتذكّر ذوقك ومقاسك وتفضيلاتك للمرات
> الجاية؟ لو وافقت بنحفظ محادثتك مع المساعد وتفضيلاتك (مثل المقاس واللون والميزانية) لمدة
> **٩٠ يوم** من آخر زيارة، وبنستخدمها بس عشان نحسّن ترشيحاتنا لك في متجر **[اسم المتجر]**.
> كلامك مع المساعد يُعالج عند مزوّدي ذكاء اصطناعي خارج المملكة، وصورة التجربة (لو رفعتها)
> تروح لمزوّد توليد الصور وما نخزّنها عندنا. تقدر تسحب موافقتك وتمسح كل شي في أي وقت من
> **[رابط الخصوصية]**. للاستفسار: **[البريد]**.
> [أوافق] [لا، شكراً]

English:

> We use an AI shopping assistant to help you choose. May we remember your taste, size and
> preferences for next time? If you agree, we keep your chat with the assistant and your
> preferences (such as size, colour and budget) for **90 days** after your last visit, and use
> them only to improve recommendations at **[Store name]**. Your messages are processed by AI
> service providers outside Saudi Arabia, and a try-on photo (if you upload one) is sent to an
> image-generation provider and is not stored by us. You can withdraw consent and delete
> everything at any time at **[privacy link]**. Contact: **[email]**.
> [I agree] [No thanks]

Keep the retention figure in sync with the merchant's `profile_retention_days`.

---

## 5. Wiring consent into a cookie banner

Add the attribute on the script tag:

```html
<script src="https://YOUR-DASHBOARD/static/apitokens/embed.js"
        data-store-key="STORE_KEY" data-consent="required"></script>
```

Behaviour with `data-consent="required"`:

| State | Where the v2 tokens live | Cross-visit memory |
| --- | --- | --- |
| Before consent (default) | `sessionStorage` only (dies with the tab); nothing in `localStorage` | No (the next visit gets a fresh identity) |
| After `grantConsent()` | Moved to `localStorage`; the flag `ctl_w2_<key>_consent=1` is also stored in `localStorage` | Yes |
| After `revokeConsent()` | Server delete (same as `forget()`), `localStorage` cleared, consent flag removed, back to `sessionStorage` | No |

Without the attribute nothing changes: tokens live in `localStorage` as before (no consent gate).

JavaScript API on `window.ControlWidget`: `hasConsent()` (boolean; always true without the
attribute), `grantConsent()`, `revokeConsent()` (returns a promise of true or false), plus the
unchanged `forget()` and `open(message)`. Consent given earlier is remembered in `localStorage`,
so a returning visitor who consented is not asked again. If the server cannot be reached,
`revokeConsent()` still stops persistence immediately and resolves `false` (the tokens stay in
the tab so the store can retry).

```js
// example with any banner
function whenWidgetReady(fn) {
  if (window.ControlWidget) return fn();
  var t = setInterval(function () { if (window.ControlWidget) { clearInterval(t); fn(); } }, 200);
}
banner.onAccept = function () { whenWidgetReady(function () { ControlWidget.grantConsent(); }); };
banner.onDecline = function () { /* nothing: stays in session-only mode */ };
privacyPage.withdrawButton.onclick = function () { ControlWidget.revokeConsent(); };
```

On the first page load the widget reads `localStorage` only for the consent flag; stale tokens
left in `localStorage` by an earlier non-consent install are ignored until consent is granted.

**Known gap (be explicit with the lawyer):** before consent the *server* still creates a
`CustomerProfile` and stores the conversation for that visit, because the assistant needs it to
work within the session. With the token only in `sessionStorage` nobody can reconnect to it after
the tab closes, but the rows stay until `expires_at` (90 days by default). Options, none
implemented yet: (a) accept it as necessary to provide the requested service and disclose it;
(b) add a short retention (for example 1 day) for profiles that never received consent, which
needs a consent flag sent from the widget to the server; (c) a "no memory at all" mode for
unconsented sessions. Decide before launching in KSA.

---

## 6. Open product and legal decisions

1. Is consent opt-in by default for all merchants (`data-consent="required"` mandatory), or per merchant?
2. How to handle the server rows of unconsented sessions (section 5, options a, b, c).
3. Controller and processor roles, the merchant data-processing agreement, and who is the contact for requests.
4. Cross-border transfer basis for the LLM providers and kie.ai; per-provider region and zero-retention settings; written kie.ai retention terms.
5. Should `forget` also blank the order attribution (`order_id`, `platform_cart_id`)?
6. Access and portability: build an export endpoint, or keep it a manual staff task (and its response time)?
7. A memory review screen in the widget ("what you remember about me") using `/widget/v2/memory/`.
8. Redaction of phone numbers, ID numbers and other sensitive content in free text, before storage and before sending to the LLM.
9. Retention defaults (90 days profile, 60 days traces): confirm or shorten; whether traces should avoid storing customer wording.
10. Breach notification procedure, owner and deadlines.
11. Age policy for minors.
12. Log retention and where logs are stored (they hold merchant ids and, in access logs, customer IPs).
13. Update the public privacy notice at `/privacy/` to describe v2 memory, processors and rights (check the current page text against this document).
