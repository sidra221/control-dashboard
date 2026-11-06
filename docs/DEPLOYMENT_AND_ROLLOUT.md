# Deployment and rollout (gunicorn + nginx + Postgres)

This is the exact checklist for putting the v1/v2 build on a production host and moving
merchants to v2 one at a time. Both engines run in the same code base; the engine is chosen
**per merchant** from the staff dashboard ("Engine" toggle), so deploying the code changes
nothing for any merchant until you switch it.

Numbers marked `<!-- INTEGRATOR: ... -->` are placeholders to fill in after the live runs.

---

## 1. Environment variables

Put them in `.env` next to `manage.py` (read by `config/settings.py`) or in the systemd unit.
`.env.example` documents all of them. Required for production:

| Variable | Value / rule |
| --- | --- |
| `DJANGO_SECRET_KEY` | **Required, real.** `python -c "import secrets;print(secrets.token_urlsafe(64))"`. With `DJANGO_DEBUG=0`, a key that is a placeholder, shorter than 50 chars or with under 12 distinct characters fails the system check `apitokens.E003`, so `manage.py migrate`, `check` and `runserver` refuse to run. Gunicorn itself does not run system checks, so the `migrate` and `check --deploy` steps below are the gate: do not skip them. Why: the v2 session and shopper identity tokens are HMAC-signed with it, so a weak or leaked key lets anyone forge customer identities and read or delete their memory. Store it in a secrets manager; never commit it. Rotating it invalidates all customer tokens (customers start fresh, no data loss on the server) and logs staff out. |
| `DJANGO_DEBUG` | `0`. With `1` on a public host the check `apitokens.W002` warns (tracebacks exposed, SECRET_KEY check skipped). |
| `DJANGO_ALLOWED_HOSTS` | Your domain(s), comma separated. `localhost,127.0.0.1` alone only if n8n calls gunicorn directly on loopback. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://your-domain.com` |
| `SITE_BASE_URL` | `https://your-domain.com` (used for Shopify webhook URLs). |
| `POSTGRES_DB` `POSTGRES_USER` `POSTGRES_PASSWORD` `POSTGRES_HOST` `POSTGRES_PORT` | Setting `POSTGRES_DB` switches from SQLite to Postgres. Optional `POSTGRES_CONN_MAX_AGE` (default 60). |
| `LLM_PROVIDER` | `anthropic`, `openai`, `gemini` or `groq`, plus that provider's `*_API_KEY` and `*_MODEL`. |
| `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT` | e.g. `gpt-6-luna` and `low`. Empty effort = not sent (needed for older models such as gpt-4o). Higher effort = slower turns. |
| `V2_SELLER_MODE` | `loop` (default): tool-result loop, hard product constraints, sales stages, discount policy. `legacy`: the Phase 1 single-call seller. **Instant engine-level rollback without a deploy** (set it and restart). Merchant-level rollback is the dashboard Engine toggle. |
| `V2_SPECULATIVE` | Default `0`. `1` runs the analyzer and the reply in parallel and redoes the reply when the analysis changed the plan. Measured to cost more than it saves; leave off. |
| `ANALYZER_REASONING_EFFORT` | Unset (default) = the original behaviour (`low` when the chat model uses reasoning). An empty value sends no reasoning parameter (needed for a non-reasoning analyzer model such as `gpt-4o-mini`). Not changed for latency reasons. |
| `ANALYZER_PROVIDER`, `ANALYZER_MODEL` | v2 memory analyzer. Empty = same provider and model as the chat. A smaller, faster model here cuts per-turn latency. |
| `KIEAI_API_KEY` | Virtual try-on (kie.ai). |
| `RATE_LIMIT_BACKEND` | `database` (default, uses the Postgres you already have) or `cache` (only with a shared Redis/Memcached; the check `apitokens.E002` rejects LocMem and file caches). |
| `TRUSTED_PROXY_COUNT` | `1` behind nginx. Wrong value breaks per-IP limits: too low means every shopper looks like one IP (limits trip), too high lets a client spoof its IP. |
| `CONSUME_LEGACY_MODE` | `internal` (default) while n8n still uses the old public token, then `off` (section 6). |
| `CONSUME_LEGACY_ALLOWED_NETWORKS` | Default `127.0.0.0/8,::1/128` (n8n on the same host). If n8n is on another host list its address (CIDR), as a stop-gap until it uses the consume key. |
| `V2_SESSION_TTL_SECONDS` | Default 86400 (24 h, renewed every turn). |
| `V2_SHOPPER_TTL_SECONDS` | Default 15552000 (180 days). Real data retention is per merchant `profile_retention_days` (default 90). |
| `DJANGO_HSTS_SECONDS` | Default 2592000 (30 days), applied when `DJANGO_DEBUG=0`. |
| `SALLA_*`, `ZID_*`, `SUPPORT_EMAIL`, `DEMO_STORE_KEY` | Platform integrations, privacy-page contact, demo store (leave `DEMO_STORE_KEY` empty in production if you do not want `/demo/`). |

Generate the key once:

```bash
python -c "import secrets;print(secrets.token_urlsafe(64))"
```

---

## 2. nginx

nginx terminates TLS, must pass the client address and scheme, and must **overwrite** (not
append to) any `X-Forwarded-For` the client sent, otherwise rate limits can be spoofed.
`TRUSTED_PROXY_COUNT=1` assumes exactly this single proxy.

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;
    # ssl_certificate / ssl_certificate_key ...

    client_max_body_size 12m;           # try-on photo uploads

    location /static/ {
        alias /srv/control-dashboard/staticfiles/;   # after collectstatic
        expires 1h;                                  # embed.js changes: keep this short
    }

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $remote_addr;   # overwrite, do not use $proxy_add_x_forwarded_for
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 130s;            # must exceed the gunicorn --timeout below
        proxy_send_timeout 130s;
    }
}

server { listen 80; server_name your-domain.com; return 301 https://$host$request_uri; }
```

If a CDN or another load balancer sits in front of nginx, set `TRUSTED_PROXY_COUNT=2` and make
sure the outer proxy is the one that writes the real client IP.

---

## 3. gunicorn

A v2 turn makes **2 to 4 sequential LLM calls** (analyzer, reply, plus a regeneration when the
output guard rejects a draft, plus retries on provider errors), each several seconds. Workers
are therefore blocked on network I/O most of the time, so use a threaded worker so one process
serves several slow turns. The code is thread-safe (database counters, per-conversation DB lock).

```bash
gunicorn config.wsgi:application \
  --bind 127.0.0.1:8000 \
  --workers 3 --threads 8 --worker-class gthread \
  --timeout 120 --graceful-timeout 30 --keep-alive 5 \
  --max-requests 1000 --max-requests-jitter 100 \
  --access-logfile - --error-logfile -
```

- **`--timeout 120`**: must exceed the worst-case turn (analyzer + reply + regeneration + a
  provider retry, tens of seconds). Gunicorn's default 30 s kills turns mid-flight and the
  customer sees an error. Keep nginx `proxy_read_timeout` above it.
- **Concurrency** = workers x threads (here 24 simultaneous turns). Rule of thumb:
  `workers = CPU cores + 1`, threads 4 to 8. Size it from the expected number of simultaneous
  shoppers, not total shoppers. Measured turn latency (8-turn Saudi conversation x3, `gpt-6-luna` effort `low`, small catalog, one dev machine): p50 ≈ 4.3 s, p95 ≈ 6.8 s, two sequential LLM calls minimum (analyzer ≈ 1.8 s + reply ≈ 2.4 s). The concurrency a production host sustains was NOT measured.
- **Database connections**: each thread can hold one, so Postgres `max_connections` must be above
  workers x threads plus cron and admin (with `POSTGRES_CONN_MAX_AGE=60`). Use pgbouncer if you scale out.
- Run it under systemd with `Restart=always` and `EnvironmentFile=/srv/control-dashboard/.env`.

---

## 4. Migrations

```bash
python manage.py migrate
python manage.py collectstatic --noinput
```

Migrations added on the v2 branch, in order:

| # | What it does | Additive? |
| --- | --- | --- |
| `0013_remove_shopper_limits_and_demo_accounts` | **Drops** `ApiToken.tryon_limit_days`, `ApiToken.tryons_per_shopper` and the `TryOnRecord` and `DemoCustomer` tables | **No, destructive.** Back up first (`pg_dump`). Not reversible by switching engines. Skip nothing; if production already ran it (it is on master) it is a no-op. |
| `0014_consume_key_and_rate_limit_bucket` | New `RateLimitBucket` table; `ApiToken.consume_key_hash/prefix/rotated_at`, `legacy_consume_enabled`, `legacy_consume_last_used_at`. A data step sets `legacy_consume_enabled=True` for **all existing merchants** so n8n keeps working after the deploy | Yes (new columns / table) |
| `0015_v2_memory` | New v2 tables (`CustomerProfile`, `Message`, `PreferenceFact`, `ProductReaction`, `Objection`, `TurnTrace`) and the `engine`, `profile_retention_days` and v2 columns on `ApiToken` / `Conversation`. Existing rows default to v1 | Yes |
| `0016_discountcode_validity_dates` | Two optional columns on `DiscountCode`: `starts_at`, `expires_at` (empty = no limit). Existing codes keep working unchanged | Yes (verified: AddField only) |

Procedure: `pg_dump` -> `migrate` on a copy first if the data is valuable -> `migrate` on
production -> restart gunicorn. New columns have defaults, so old and new code can run against
the new schema during the restart.

---

## 5. Post-deploy commands

Run on the production host with the production environment:

```bash
python manage.py check --deploy
python manage.py ratelimit_selftest            # must print PASS on the production Postgres
python manage.py ratelimit_selftest --threads 64 --limit 20   # optional heavier run
```

- `check --deploy` must show **no errors**. `apitokens.E003` means a weak `DJANGO_SECRET_KEY`.
  Warnings `security.W005` and `W021` (HSTS subdomains / preload) are known and deliberate.
- `ratelimit_selftest` fires N concurrent increments at one counter and requires exactly `limit`
  to be admitted. It must pass **on Postgres**, not only SQLite; exit code 1 on failure. If it
  fails, per-IP limits are not reliable: do not roll out v2.

Cron (as the app user):

```cron
# customer-memory retention + 60-day trace cleanup (daily, off-peak)
15 3 * * *  cd /srv/control-dashboard && .venv/bin/python manage.py purge_expired_data >> /var/log/control-dashboard/purge.log 2>&1
# expired rate-limit counters (weekly, optional but keeps the table small)
30 3 * * 0  cd /srv/control-dashboard && .venv/bin/python manage.py purge_rate_limits >> /var/log/control-dashboard/purge.log 2>&1
```

`purge_expired_data` enforces the retention promised in `docs/PRIVACY_AND_CONSENT.md`; verify it
runs (its log line is `forgot N expired customers, deleted M old traces`).

---

## 6. n8n consume-key migration

The try-on credit endpoint (`POST /api/tokens/consume/`) used to accept the **public** widget
token, which any visitor can read from the page, so anyone could burn a merchant's credits. It
now wants a secret per-merchant consume key. Migration `0014` keeps old setups working (loopback
only) until you move n8n.

Per merchant, in this order:

1. Dashboard: **rotate the consume key** for the merchant (the form posts `rotate_consume_key`).
   The plaintext key (`ck_...`) is shown **once**; copy it. Only a hash is stored.
2. n8n: in the HTTP Request node that calls consume, add the header `X-Consume-Key: ck_...`
   (an `Authorization: Bearer` header is also accepted). Keep sending the body `{"count": 1}`.
3. Verify from n8n: the response is `{"ok": true, ...}` and the dashboard shows the credit used.
   The log line `consume_legacy_credential merchant=<id>` must stop appearing for that merchant.
4. Dashboard: turn **off** legacy consume for the merchant (`toggle_legacy_consume`).
5. When **every** merchant is migrated (check `legacy_consume_last_used_at` is old / empty in the
   admin), set `CONSUME_LEGACY_MODE=off` and restart. The old path is then closed globally.

Check from the **internet** (not from the host) that the public token no longer works:

```bash
curl -s -X POST https://your-domain.com/api/tokens/consume/ \
  -H "X-Api-Token: <PUBLIC_STORE_KEY>" -H "Content-Type: application/json" -d '{"count":1}'
# expected: {"ok": false, "error": "legacy_credential_not_allowed"}
```

(The endpoint always answers HTTP 200 and puts the result in `ok` / `error`, so n8n does not stop.)
Every rejected attempt logs `consume_public_key_rejected`. Also test that a wrong consume key
returns `invalid_consume_key` and a correct one returns `ok: true`.

---

## 7. Controlled per-merchant rollout of v2

Deploying the code does not enable v2. The dashboard has an **Engine** toggle per merchant
(v1 / v2). Go slowly:

1. **Pre-flight (once per release)**, section 9: run `eval_v2` and `bench_v2` with the target
   provider and keys; `check --deploy`, `ratelimit_selftest` and the crons in place.
2. **Internal test store first.** Create a merchant for a store you control with a real
   catalogue, switch it to v2, install the widget (with `data-consent="required"` if that is the
   chosen consent model, see `docs/PRIVACY_AND_CONSENT.md`). Play 30 to 50 realistic Saudi-Arabic
   conversations: size questions, price objections, "show me something cheaper", gifts, English,
   try-on, leaving and coming back the next day, "forget me".
3. **What to look at**: the dashboard **conversations** page for that merchant (v2 transcripts) and
   the Django admin `TurnTrace` rows (`analyzer`, `memory_changes`, `constraints`, `guard`,
   `llm_calls`, `latency_ms`, `models_used`, `error`).
4. **Success criteria** (all of them, over at least 50 conversations):
   - p95 turn latency within the agreed target `<!-- INTEGRATOR: fill target and measured p50/p95 -->`
   - `error` empty on at least 99% of traces; no 5xx from `/widget/v2/*`
   - the output guard (`guard` in the trace) fires rarely and never leaves non-Saudi dialect,
     invented prices, discount codes or stock claims in a shown reply
   - remembered facts match what the customer said (no wrong "explicit" facts); returning
     customers are recognised and greeted without repeating questions
   - rejected products are not re-recommended; forget-me removes the rows
   - no rate-limit or backend errors in the logs (section 8)
   - `<!-- INTEGRATOR: fill eval_v2 pass rate and judge scores vs v1 baseline -->`
5. **Then** switch 1 to 3 friendly real merchants, one at a time, a few days apart, watching the
   same signals daily. Widen only when the criteria hold.
6. **Instant rollback**: set the merchant's Engine back to **v1** in the dashboard. The next
   widget load goes to the v1 path. **No data is lost**: v2 rows (profiles, messages, memory)
   stay in the database and are used again if you switch back; the widget's browser tokens just go
   unused. v1 and v2 conversations both show in the conversations page. Retention still applies
   to the v2 data while the merchant is on v1.
7. A rollback of the **code** is separate: migrations `0014` and later are additive, so the old
   code runs on the new schema; do not reverse `0013` (it already dropped tables).

---

## 8. Monitoring: log keys to alert on

Application logs go to stdout (journald / file). Alert on these keys:

| Key | Meaning | Action |
| --- | --- | --- |
| `llm_all_attempts_failed` | Every attempt with the LLM provider failed for a turn | Page. Provider outage, bad key or quota. The customer got an error. |
| `analyzer_failed` | The memory analyzer call or parse failed (the turn continues without new memory) | Investigate if more than a few per hour; check `ANALYZER_MODEL`. |
| `ratelimit_backend_failed` | The rate-limit store raised (rule fails closed or open per rule) | Page. Check Postgres / cache; re-run `ratelimit_selftest`. |
| `rate_limit_unavailable` | A fail-closed rule returned 503 because the store was down | Page. Customers are being refused. |
| `consume_public_key_rejected` | Someone tried the public key on the consume endpoint | Expected during migration; a spike after it is abuse (IP is logged). |
| `output_guard` | A draft reply violated promo / grounding / dialect rules (`stage=first` regenerated, `stage=strip` hard-stripped) | Track the rate; a sustained rise means a prompt or model regression. |
| `saudi_dialect_substituted` | The model kept using non-Saudi words after a retry and they were substituted | Track per merchant; a rise means the model drifted. |
| `rate_limited` | Normal throttling | Alert only on a sudden surge from many IPs. |
| `consume_legacy_credential` | n8n still on the old public token | Should reach zero before `CONSUME_LEGACY_MODE=off`. |
| `v2_token_invalid` / `v2_token_wrong_merchant` | Bad or cross-merchant identity token | A burst can mean tampering or a rotated `SECRET_KEY`. |
| `trace_save_failed`, `conversation_log_failed` | Diagnostics could not be saved | Investigate; reply still served. |

Also watch: gunicorn worker timeouts (`WORKER TIMEOUT`), Postgres connections, disk growth of
`Message` / `TurnTrace` (purge cron), and kie.ai balance.

---

## 9. Live evaluation before each rollout

These commands exist in the repo (`--help` on each). They call real LLM APIs and need the production-like environment keys.

```bash
# quality: scripted Saudi-Arabic scenarios against the real provider
python manage.py eval_v2 [--only NAME[,NAME]] [--list] [--provider P --model M --effort E] [--judge] [--out eval_v2.json] [--transcripts]

# latency / cost: repeated turns, comparable runs
python manage.py bench_v2 [--runs N] [--out bench_<label>.json] [--label before]
python manage.py bench_v2 --compare bench_before.json bench_after.json

# try-on integration check (kie.ai) -- COSTS MONEY, run only with the owner's approval
python manage.py kieai_live_check --person /path/to/person.jpg \
    --product-image https://your-cdn/product.jpg --confirm-cost [--end-to-end]
```

- `eval_v2 --judge` adds an LLM judge score (extra cost). `--only` runs a subset.
- Run `bench_v2` before and after any model / `OPENAI_REASONING_EFFORT` / `ANALYZER_MODEL`
  change and compare.
- `kieai_live_check` generates a real image; without `--confirm-cost` it refuses to run (tested). **It has NOT been run against the real kie.ai yet: it needs the owner's approval of the spend (1 paid image, 2 with `--end-to-end`).**
  `--end-to-end` also exercises the consume-credit and widget path.
- Record results here: `<!-- INTEGRATOR: fill eval_v2 pass rate, judge averages, bench p50/p95 latency, cost per turn, date and model -->`

---

## 10. Known limits

- **No streaming.** The widget shows "typing" until the full reply arrives, then types it out
  (about 1.2 s cap). Perceived latency is the whole 2 to 4 sequential LLM calls.
- **Gemini free tier = 15 requests per minute.** One v2 turn uses 2 to 4 calls, so the free tier
  supports only a handful of concurrent shoppers; use a paid key for any real traffic.
- **One in-flight turn per conversation**: a second message sent while the first is processing is
  refused or queued by the per-conversation lock; fast double-sends are de-duplicated by `client_msg_id`.
- **Rate limits** (per IP unless noted, defaults): chat 20/min and 200/hour per IP, 1000/min per
  store key; try-on 6/min per IP; events 60/min; memory/forget 30/min; consume 600/min per IP, 300/min
  per merchant. Many shoppers behind one corporate or mobile-carrier NAT share an IP.
- **Discount policy (demo phase): no cooldown between conversations.** A code is offered at most once per conversation, only for merchant-configured, active, in-date codes, never in the first two customer turns, only to an engaged and hesitant customer, after a cheaper alternative was suggested, and never to a customer who decided to buy without an objection. The same returning customer can receive an eligible code again in a NEW conversation. A cross-conversation cooldown was removed on purpose for the demo and can be reconsidered after launch.
- **Memory is only as good as the analyzer.** Inferred facts are never used as hard filters;
  customers can correct them only through the API today (no widget UI yet).
- **Try-on** depends on kie.ai latency and credits; photos are not stored by us.
- **Consent gap:** before consent the server still stores the session's conversation (see
  `docs/PRIVACY_AND_CONSENT.md`, section 5).
- **Arabic dialect enforcement** targets known non-Saudi words; it is a safety net, not a
  guarantee, and applies to Arabic replies only.
- Latency: see the gunicorn section for the one measurement we have (OpenAI `gpt-6-luna`); other providers/models were not measured.
