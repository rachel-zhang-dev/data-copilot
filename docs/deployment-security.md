# Deploy-side security checklist

> Phase 3.2 / ADR 0024 — operator runbook for a public Fly.io deploy.
> Five steps, ~10 minutes total. Read once, do once per deploy.

The goal is **bounded dollars regardless of abuse**. Three
independent caps fence the worst case at ~$15/mo even if the URL
leaks and someone scripts against it:

| Cap | Where | Hard ceiling |
|---|---|---|
| 🔑 **App-layer `X-API-Key`** (this repo's middleware) | `DEMO_API_KEY` env var | Anonymous traffic → 401, no LLM call ever |
| ⏱ **App-layer per-IP rate limit** (this repo) | `RATE_LIMIT_PER_MINUTE` env var (default 30) | Burst from a single IP → 429, no LLM call past the cap |
| 💳 **DeepSeek per-key spending limit** | DeepSeek dashboard | Monthly USD cap; key auto-disabled past it |
| 💳 **Fly per-org spending limit** | Fly dashboard | Monthly USD cap; machines paused past it |

The first two are the polite layer — they don't waste DeepSeek
calls. The last two are the "I might have a bug" backstop — they
stop bleeding money even if everything above fails.

---

## 1. Generate a deploy API key

Pick a long random string. Anything ≥ 32 hex chars is overkill
strong for this use case:

```bash
openssl rand -hex 32
# → 5e4f3c2b1a0... (copy this, you'll set it on Fly in step 4)
```

Treat this like a password: don't commit it, don't paste it into
public chat. Anyone with this key can call your API at full speed.

---

## 2. Set the DeepSeek spending cap (recommended: $5/mo)

The single most important cap — it sits closest to the actual cost.

1. Log in to [platform.deepseek.com](https://platform.deepseek.com).
2. **Billing** → **Spending limit** → set monthly cap to **$5 USD**
   (you can raise this later if you actually need it).
3. Optionally: create a SEPARATE API key just for the deploy (so
   if you ever need to rotate, you don't disturb local dev).
   Generate it under **API Keys** → **Create new key** → copy
   the new key.

What this gets you: once the monthly spend on this key hits $5,
DeepSeek auto-disables further calls and emails you. Your deploy
returns errors but doesn't keep billing.

---

## 3. Set the Fly spending cap (recommended: $10/mo)

Lower priority than the DeepSeek cap (Fly compute is already cheap
with scale-to-zero), but a useful catch-all.

1. Log in to [fly.io/dashboard](https://fly.io/dashboard).
2. **Organization** → **Billing** → **Spending limits** → set
   monthly hard cap to **$10**.
3. Fly will email you when you cross 50% / 75% / 90% / 100%.

What this gets you: bandwidth + compute + Postgres combined
can't exceed your cap. Past the cap, Fly pauses machines.

---

## 4. Set the secrets on both Fly apps

Replace `<YOUR-32-CHAR-HEX-KEY>` with what you generated in step 1,
and the URL with your actual web app's URL:

```bash
# Required on the API app: DEMO_API_KEY enforces the X-API-Key gate.
fly secrets set -a data-copilot-rz-api \
  DEMO_API_KEY=<YOUR-32-CHAR-HEX-KEY> \
  DEEPSEEK_API_KEY=<your-deepseek-key-from-step-2> \
  SILICONFLOW_API_KEY=<your-siliconflow-key> \
  DATABASE_URL='postgresql://...?sslmode=require' \
  CORS_ORIGINS=https://data-copilot-rz-web.fly.dev

# Required on the WEB app: DEMO_API_KEY here is what the Next.js
# Route Handlers forward to the API on behalf of browser users.
fly secrets set -a data-copilot-rz-web \
  DEMO_API_KEY=<SAME-32-CHAR-HEX-KEY-AS-API-APP> \
  API_BASE_URL=https://data-copilot-rz-api.fly.dev
```

Notes:

* **Same `DEMO_API_KEY` value on BOTH apps.** The API checks it;
  the Web forwards it server-side. Different values → web users
  get 401 because the Next.js proxy sends the wrong key.
* **`CORS_ORIGINS` on the API** MUST be the literal web URL, not
  `*`. The middleware doesn't accept wildcards.
* You can OMIT `LANGSMITH_API_KEY` and `REDIS_URL` — the app
  starts fine without them (LangSmith tracing just disables;
  cache falls back to in-process TTL).
* Setting secrets restarts the affected app once (~10 seconds).

---

## 5. Verify

After the deploy completes, three curl checks:

```bash
# (a) Liveness probe works (no key needed)
curl https://data-copilot-rz-api.fly.dev/health
# → {"status":"ok","version":"0.12.0"}

# (b) Anonymous /ask is now rejected
curl -X POST https://data-copilot-rz-api.fly.dev/ask \
  -H 'content-type: application/json' \
  -d '{"question":"hi"}'
# → 401 {"detail":"X-API-Key header missing or invalid"}

# (c) Authenticated /ask works
curl -X POST https://data-copilot-rz-api.fly.dev/ask \
  -H 'content-type: application/json' \
  -H "X-API-Key: <YOUR-32-CHAR-HEX-KEY>" \
  -d '{"question":"How many customers are there?"}'
# → 200 {answer, sql, ...}
```

The web URL should also work end-to-end (anonymous browser users
see the chat UI; the Next.js server proxies requests with the
key attached):

```bash
open https://data-copilot-rz-web.fly.dev
# Ask "How many customers are there?" → should answer.
```

If anonymous /ask returns anything other than 401, you forgot
to set `DEMO_API_KEY` on the API app. Re-run step 4 and the
secret-set restart will fix it.

---

## What this does NOT defend against

Honest disclaimers so you don't oversell what this buys you:

* **Targeted DDoS** with rotating IPs from a botnet. The per-IP
  limiter doesn't help; only the billing caps do. Realistic
  threat for a Fortune 500 SaaS, not for a portfolio project.
* **Stolen `DEMO_API_KEY`.** If you commit it, post it on Twitter,
  or share it with someone untrustworthy — anyone with the key can
  burn your DeepSeek credit up to the $5 cap. Mitigation: don't
  leak it; rotate via `fly secrets set DEMO_API_KEY=$(openssl rand -hex 32)`
  on both apps whenever you suspect a leak (~30 seconds).
* **Bugs in the agent that loop forever on a single request.** The
  retry budgets in `nodes.py` (RETRY_BUDGET) + the critic's
  per-turn limit cap the worst case at ~5 LLM calls per request,
  so even a stuck loop would only burn ~$0.0025 before completing.
* **Postgres data exfiltration via SQL injection.** Mitigated by
  `sql_safety.validate_and_rewrite` which gates every query
  through sqlglot AST validation — no `INSERT` / `UPDATE` / `DELETE`
  can reach the database regardless of what the LLM proposes.
  (Unrelated to deploy-side defence; it's the agent's own protection.)

---

## Rotating the key later

```bash
# Generate fresh
NEW_KEY=$(openssl rand -hex 32)

# Update BOTH apps in lockstep (web first prevents stale-key 401s
# during the few seconds between restarts)
fly secrets set DEMO_API_KEY=$NEW_KEY -a data-copilot-rz-web
fly secrets set DEMO_API_KEY=$NEW_KEY -a data-copilot-rz-api

# Verify; should return 200 (the new key is in effect)
curl -X POST https://data-copilot-rz-api.fly.dev/ask \
  -H "X-API-Key: $NEW_KEY" \
  -H 'content-type: application/json' \
  -d '{"question":"ping"}'
```
