# ADR 0024: Deploy-side defence — API-key gate + per-IP rate limit (Phase 3.2)

> Status: Accepted · Date: 2026-06 (Phase 3.2) · Supersedes: none

## Context

Before Phase 3.2 the API surface (``/ask``, ``/mcp``, ``/dashboards``,
``/conversations``) was wide open by design — local dev with no auth
is the right shape when the only consumer is the chat UI on
localhost. For the imminent Fly.io public deploy that posture is
unsafe.

The threat we care about is not "someone steals our data" (the demo
database is a copy of public Northwind; nothing sensitive). The
threat is **someone burning the operator's DeepSeek credit**:

* `/ask` and `/ask/stream` each cost ~5 LLM calls ≈ **$0.0005** per
  hit at DeepSeek pricing.
* A script that hits the endpoint 100k times/day ≈ **$1500/month**
  on the operator's bill.
* Both endpoints accept anonymous traffic; nothing currently gates
  them at the application layer.
* CORS doesn't help — it only blocks browser requests; a `curl`
  script ignores CORS entirely.

We need a deploy-side defence that:

1. **Doesn't break local dev.** Off by default. Set an env var to
   turn on.
2. **Caps real-world abuse to bounded dollars** even if the deploy
   URL leaks (which it will — that's the whole point of a public
   demo).
3. **Doesn't require auth UX in the chat panel** — anonymous
   visitors of the deployed web UI should still be able to ask
   questions. The Next.js front-end is a trusted server-side
   intermediary; the API key lives there, not in the browser.
4. **Stays under 100 lines of code.** No `slowapi` / no
   `starlette-limiter` / no Redis. Big-budget rate limiting belongs
   to a CDN; we're solving "stop the script kiddies" not "stop a
   DDoS".

## Decision

### 1. Two-layer defence in one middleware

`apps/api/copilot/security.py` ships a single FastAPI HTTP
middleware that combines:

* **API-key gate** — when `DEMO_API_KEY` env var is set, every
  request to a non-bypass path must carry `X-API-Key: <value>`.
  Wrong / missing → `401`.
* **Per-IP rate limit** — when `RATE_LIMIT_PER_MINUTE` > 0, requests
  to LLM-cost paths (`/ask`, `/ask/stream`, `/mcp/*`) are counted
  in a 60-second sliding window per IP. Over budget → `429` with
  `Retry-After: 60`.

Order matters: API-key check runs FIRST. An unauthenticated request
returns 401 immediately and never advances the rate-limit counter,
so an attacker scanning for our URL can't budget-starve a
legitimate user via the same IP space.

Bypass paths (always open):

| Path | Why |
|---|---|
| `/health` | Fly liveness probe runs unauthenticated and frequently |
| `/metrics` | Prometheus scrape (would be poisoned by a wrong API key) |

`/admin/stats` is NOT in the bypass list — it leaks cache hit rate +
settings (non-secret but operational telemetry); a public deploy
should gate it.

### 2. Front-end forwards the key server-side

The Next.js Route Handlers (under `apps/web/app/api/`) read
`process.env.DEMO_API_KEY` at request time and attach
`X-API-Key: <value>` to every outgoing fetch via the
`serverHeaders()` helper in `apps/web/lib/server-fetch.ts`.

Three properties that fall out:

1. **Browser users never see the key.** It only ever lives in the
   Next.js server process (`process.env` is server-only by
   default; the Route Handlers run in the Node runtime).
2. **CORS stays strict** because the browser only ever talks to its
   own origin (the Next.js Route Handler proxy).
3. **A curl directly to `<api>.fly.dev/ask`** without the header →
   401. To use the API directly, you need to be on the operator
   team that has the key.

### 3. Hand-rolled rate limiter (no `slowapi`)

```python
class RateLimit:
    def __init__(self, max_per_window: int, window_seconds: int) -> None: ...
    def allow(self, key: str) -> bool: ...
```

Sliding-window deque per IP, lazy GC of empty buckets when the dict
size crosses 4096. ~30 LOC including docstring.

Why not `slowapi`:

* Adds a transitive dep and a small global mutable state.
* Decorator-based per-route limits don't cover the mounted MCP
  sub-app (`/mcp/*` is a Starlette sub-app — slowapi's hook into
  FastAPI's route decorator doesn't reach inside).
* The whole module is ~80 lines including docstring. Cheaper to
  read than to vendor.

Why not `starlette-limiter` / `fastapi-limiter`:

* Both need Redis for multi-process synchronisation. We deploy a
  single Fly machine with auto-scale-to-zero; cardinality between
  replicas is a non-issue. If we ever scale up, the per-process
  limit means an attacker would have to spread across N IPs to
  multiply the budget — still bounded.

### 4. NOT a replacement for billing caps

The middleware caps abuse at the **application layer**. The hard
backstop is at the **provider layer**:

* **DeepSeek dashboard**: set a per-key monthly spending limit
  (e.g. $5/mo). Cannot be bypassed by any application-level bug.
* **Fly billing**: set a hard spending limit (e.g. $10/mo).
  Machines get paused if the cap is hit.

Both are documented in [`../deployment-security.md`](../deployment-security.md).
The middleware is the polite layer; the billing caps are the
"I might have a bug" layer.

### 5. Defaults that favour local dev

| Env var | Default | Effect |
|---|---|---|
| `DEMO_API_KEY` | unset | API-key check disabled |
| `RATE_LIMIT_PER_MINUTE` | 30 | Rate limit ENABLED in production code… |
| `RATE_LIMIT_PER_MINUTE` (test env) | 0 | …but the test suite sets 0 in `conftest.py` so existing tests aren't disturbed |

Local dev with no `.env` changes: identical behaviour to pre-Phase-3.2,
no surprise 429s when you hit the chat panel repeatedly.

Production deploy: set both env vars via `fly secrets set` on each
app (api + web with the same `DEMO_API_KEY` value).

## Alternatives explicitly rejected

### Real OAuth2 / Auth0 / Clerk integration

Add full user accounts. Rejected because:

* The project is a single-tenant demo. We don't need user accounts;
  we need "stop random scripts from costing me money".
* Auth0 / Clerk free tiers are generous for users but add a third
  party to the chain — more moving parts.
* The day we have multiple actual users with different permissions
  is the day to revisit. That day is not today.

### Rate-limit at the Fly load balancer / Cloudflare

Push the rate limit out to the edge instead of the application.
Rejected because:

* Fly's built-in rate limiting is basic and not free for ANYTHING
  more than the defaults.
* Adding Cloudflare in front means another vendor, another DNS
  layer, more potential failure modes.
* In-process is good enough for a demo deploy. The bounded cost of
  abuse is $5-15/month with the billing caps; we don't need to
  drop more layers in front.

### IP-allowlist instead of API key

Restrict the API to specific IP ranges (operator's home + CI).
Rejected because:

* Recruiters need to access the demo from their laptops on whatever
  network. An IP allowlist defeats the "share a URL" use case.
* The shared-secret model handles this cleanly — share the URL +
  the key together when you want someone to have access; the URL
  alone is a dead end.

### Auth on every endpoint (no bypass list)

Require `X-API-Key` on `/health` and `/metrics` too. Rejected because:

* Fly's liveness probe doesn't have a way to inject custom headers.
  Gating `/health` would break the health check → Fly would kill
  the machine → operator gets a paged.
* `/metrics` is read by Prometheus; same problem.
* The bypass list is exactly two endpoints. Both expose nothing
  sensitive: `/health` returns `{"status": "ok", "version": "..."}`,
  `/metrics` returns Prometheus counters (cardinality of requests,
  status codes — operational, not secret).

## Risks and known limitations

* **In-process limiter only.** Two replicas = 2× the limit. We
  deploy a single Fly machine so this is moot today. If we ever
  scale horizontally we should reconsider (Redis-backed limiter).
* **Per-IP keying doesn't help against IP rotation.** A real
  attacker with a botnet bypasses the rate limit by cycling IPs.
  Acceptable — the billing caps are the hard backstop, and a
  botnet operator targeting a portfolio demo is not a realistic
  threat.
* **`/admin/stats` exposes some operational data.** Cache hit rate,
  embedding cache backend, retry budgets. Non-secret but tells an
  attacker about our stack. Gated behind `DEMO_API_KEY` in
  production, open in local dev. Acceptable trade-off.
* **No CSRF protection.** Not relevant — the Route Handler proxy
  only accepts requests from its own origin (CORS), and the API
  doesn't have cookie-auth state.
* **The Streamable HTTP MCP sub-app's response body bypasses the
  middleware's `JSONResponse` shape on 429 returns** — Fly's load
  balancer would never see that because we 429 BEFORE the request
  reaches the MCP handler. Verified by `test_security.py`.

## Compatibility / migration

* Pure additive code; ~150 LOC across `security.py` + middleware
  registration + 9 Route Handler updates.
* No new dependencies (zero new packages — keeps the deploy image
  size unchanged).
* Existing tests pass with no changes after one line in
  `conftest.py` sets the rate-limit env var to `0` for the test
  session.
* Local dev unchanged — both env vars default to "off". Run
  `./scripts/dev.sh demo` and you don't see any new auth UI.
* Production deploy adds two `fly secrets set` calls per app.
* See `docs/deployment-security.md` for the operator runbook.
