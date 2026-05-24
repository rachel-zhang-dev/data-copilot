# ADR 0012: Deployment platform, observability, and Redis migration

> Status: Accepted · Date: 2026-05 (Week 11) · Supersedes: none

## Context

Through Week 10 the project runs locally and ships a working
full-stack story. Three gaps remain before it can plausibly serve a
demo from a public URL:

* **Container images** — no Dockerfile means no "click here to run"
  pitch for reviewers. The agent has to be cloned, set up, and
  babysat through `uv sync` + `pnpm install`.
* **Observability** — `cost.py` produces a USD figure per turn, but
  nobody reads it. There is no operator dashboard, no metrics, no
  way to tell whether the cache is doing anything in production.
* **Multi-replica readiness** — ADR 0010 promised an in-memory →
  Redis swap when scale demanded it. With deploys imminent, the
  swap moves from "future work" to "ship a switch and exercise it
  once."

Week 11 ships all three behind a single ADR because each individual
decision is small and they share the same audience (the operator).

## Decision

### Platform: Fly.io for both apps

Two apps, both with their own `fly.toml`:

* `data-copilot-api`  → FastAPI image, internal port 8000, scales to
  zero between demos.
* `data-copilot-web`  → Next.js standalone image, internal port 3000,
  same scale-to-zero.

Connections wire up via:

* `apps/web` reads `API_BASE_URL=https://data-copilot-api.fly.dev` and
  proxies SSE through its Route Handler.
* `apps/api` reads `CORS_ORIGINS=https://data-copilot-web.fly.dev`
  (week-11 audit fix — was hard-coded to `localhost:3000` previously).

Why Fly.io over Render / Railway / Vercel / Hetzner:

| | Fly | Render | Vercel | Hetzner |
|---|---|---|---|---|
| SSE friendly | ✅ | ✅ | ⚠️ (Edge buffers) | ✅ |
| Postgres with pgvector | ✅ | ✅ | external | manual |
| Two-app monorepo | ✅ | ✅ | ⚠️ (API as serverless only) | ✅ |
| Scale-to-zero | ✅ | ✅ | n/a | ❌ |
| Free tier sufficient | ✅ | ✅ | ✅ (frontend) | ❌ |
| Single config file per app | ✅ | ⚠️ | ✅ | ❌ |

Fly.io wins on the SSE + pgvector combination and on operator
ergonomics (a single `fly deploy -c <toml>` per app). Vercel is the
tempting "frontend default" but its Edge-runtime SSE story has been
flaky on long-lived connections; the Node-runtime workaround
defeats the point.

### Container images: multi-stage, non-root, lockfile-pinned

Both Dockerfiles split build dependencies from the runtime layer:

* **Backend**: `python:3.12-slim` base, `uv sync --frozen --no-dev`
  resolves wheels into `/opt/venv`, runtime image copies the venv +
  `copilot/` only. Final size ≈ 250 MB.
* **Frontend**: `node:22-alpine` base, `pnpm install --frozen-lockfile`,
  `next build` with `output: "standalone"` so the runtime layer only
  carries `server.js` + minimal `node_modules`. Final size ≈ 150 MB.

Both images run as a non-root UID (10001), pinned so the Fly checker
accepts them and volume mounts behave predictably across hosts.

Image `HEALTHCHECK` matches the Fly `[[http_service.checks]]` block so
`docker run` and `fly status` report the same liveness.

### Observability: Prometheus + LangSmith + structured logs

Three layers, no Sentry:

1. **`GET /metrics`** via `prometheus-fastapi-instrumentator`. Default
   counters (request totals, latency histograms, in-flight gauges) are
   enough for the dashboard story we need at this scale; the package
   handles route templating so cardinality stays bounded.

2. **`GET /admin/stats`** — JSON dashboard that humans (and Grafana's
   JSON datasource) can read. Surfaces:
     * Embedding cache `hits / misses / size / hit_rate / backend`.
     * Process uptime.
     * Operator-relevant settings (model name, cache size, retry
       budget, risk threshold) — no secrets.

3. **LangSmith traces** (already on since Week 5) handle per-turn
   distributed tracing. We don't double-pay for OTel collectors.

Logs stay JSON via `structlog`; Fly's log shipper picks them up
automatically. No third-party log SaaS.

### Cache backend: dual implementation, one switch

`copilot/cache.py` ships two classes behind a single `Protocol`
(`EmbeddingCacheBackend`):

* `TTLCache` — process-local, FIFO + TTL (week 9 default).
* `RedisCache` — Redis-backed, atomic counters via `INCR`, namespaced
  keys (`data_copilot:embed:vec:<model>::<text>`), TTL via `SETEX`.

`get_embedding_cache()` picks one based on `REDIS_URL`. Empty →
in-memory; set → Redis. Every node imports `get_embedding_cache()`
and never touches the backend directly — the protocol surface is the
only public API.

Provider choice for Redis: **Upstash** when deployed. Reasoning:

* Serverless billing model (no idle cost on scale-to-zero deploys).
* Free tier covers the demo (10k commands/day, 256 MB).
* `rediss://` TLS URL works as-is with the `redis` Python client.
* Surviving `fly secrets unset REDIS_URL` falls back to the
  in-memory path with zero code change — Upstash going down does
  not take the agent down.

### Week-10 audit fixes folded in

Week 11 also clears the three items the post-merge audit flagged:

* **Resume via SSE.** `ChatPanel.resumeTurn` now routes the
  approve/reject decision through `/api/ask/stream` (same shape as
  the initial question) so the post-approve `execute_sql →
  summarize → visualize` chain surfaces phase events. Previous
  non-streaming `postAsk` path produced a long pause + abrupt
  result; UX-regressed vs. the initial turn.
* **CORS origins env-driven.** `Settings.cors_origins` (comma-
  separated) parsed at boot; deploys override with the public
  front-end origin.
* **SSE heartbeat.** `_stream_ask` wraps the LangGraph async iterator
  in `asyncio.wait_for(..., timeout=15s)`; idle periods emit a
  `: heartbeat\n\n` comment line so Cloudflare / Fly's reverse
  proxy don't drop the connection.

Plus two Week-9 P3 follow-ups:

* `_last_was_cache_hit` is now a `ContextVar` rather than a plain
  module global, so future `asyncio.gather` over `embed_query`
  doesn't race the cost-attribution counter.
* `cost.estimate_usd` logs one `WARNING` per unknown model when it
  falls back to the conservative price — operators see a clear
  signal instead of silently inflated USD figures.

## Why no Sentry, no Datadog, no OTel collector

Each adds:

* A paid subscription (or self-hosted operations cost) at a scale we
  don't have.
* A second SDK in the dependency tree.
* Confusing overlap with LangSmith (which already traces every LLM
  call) and Fly's built-in log shipper.

The metrics we surface (`/metrics`, `/admin/stats`, LangSmith traces,
JSON logs) cover the operator questions a portfolio project actually
needs to answer: "is the agent up?", "is the cache helping?", "how
much did the last hour cost?". Anything more is observability
theatre.

If a future use case demands traces correlated with infra metrics,
the natural addition is an OTel exporter that ships LangSmith + Prom
data to a shared backend — not a wholesale Sentry rollout.

## Failure modes

| Scenario | Behaviour |
|---|---|
| Redis unreachable | `RedisCache.get` logs `WARNING`, returns `None` (cache miss); the network embedding call runs and succeeds. The agent stays up. |
| Redis writes failing | `RedisCache.set` logs `WARNING`, the next call is also a miss. Cost goes up; correctness untouched. |
| `/metrics` registration races with another worker | `Instrumentator.expose` runs once per process. ``metrics_enabled=false`` in tests so the default registry stays clean. |
| Fly auto-stop kills a paused HITL turn | The PostgresSaver state survives in `northwind` so the user's `?conversation_id=...` URL still resumes correctly when the machine wakes. |
| Heartbeat fires mid-LLM call | Comment line ignored by `EventSource` and our hand-rolled SSE parser (which only emits events for `event:` lines). |

## Consequences

### Good

* Reviewers click two Fly URLs and see the agent running end-to-end.
* `est_usd` becomes a visible operator metric, not just a wire-format
  field.
* The same code runs against in-memory cache locally and Redis in
  production with one env var.
* All three Week-10 UX / deploy gaps are closed before the demo
  story lands.

### Bad / accepted trade-offs

* Two Fly apps + an Upstash account is three control planes to
  log into. The deploy script papers over this with `./scripts/
  deploy.sh all`, but reviewers cloning the repo still need to set
  up secrets manually.
* `RedisCache.stats.size` does an O(N) `SCAN` per call. Fine for
  caches with thousands of entries; pathological at 100k+. The
  in-memory `TTLCache` is O(1).
* The price table in `cost.py` is still hand-maintained.
  `_WARNED_MODELS` rate-limits the noise, but the operator still
  has to manually keep it current.
* The Fly `auto_stop_machines = "stop"` setting means the *first*
  request after idle pays a ~3-5 s cold start. Acceptable for demo
  traffic; a paying customer would set `min_machines_running = 1`.

## Future work

* **CI**: GitHub Actions workflow that runs the backend test suite,
  the frontend Vitest suite, builds both images, and triggers
  `fly deploy` on green main. Roadmap item for Week 12 polish.
* **Per-environment secrets**: a `.env.staging` story for when a
  proper staging slot is needed. Deferred until the project actually
  has staging traffic.
* **Cost-per-conversation aggregator**: `/admin/stats` currently
  reports cache + uptime. A future endpoint that reads recent
  checkpoints and computes "USD spent in last 24 h per conversation"
  would unlock real billing-style dashboards.
* **OpenTelemetry exporter** if LangSmith ever drops free-tier limits
  on this project.
* **Redis backend stats**: today `RedisCache.stats.evictions` always
  returns 0 because Redis TTL evictions are silent. The keyspace
  notifications API would close this gap but adds operator setup.
