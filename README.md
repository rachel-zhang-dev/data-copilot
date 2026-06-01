# Data Copilot

> Text-to-SQL agent that **knows what it knows** — refuses gracefully when the schema can't answer, spots outliers + trends in every result, chains multi-step queries on "why" questions, saves the good conversations to a sidebar drawer, and lets you grid them into dashboards. Built on LangGraph + FastAPI + Next.js, deployed on Fly.io.

[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-480%20passing-brightgreen.svg)](#testing)
[![ADRs](https://img.shields.io/badge/ADRs-20-blue.svg)](docs/decisions/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

🔗 **[Live demo](https://data-copilot-web.fly.dev)** &nbsp;·&nbsp;
🎬 **[2-min walkthrough](docs/demo.gif)** &nbsp;·&nbsp;
📐 **[Architecture](docs/architecture.md)** &nbsp;·&nbsp;
📋 **[ADRs](docs/decisions/)** &nbsp;·&nbsp;
🧪 **[Eval reports](docs/eval/)**

<!-- Hero asset: replace docs/demo.gif with a recorded session.
     Suggested flow: ask a JOIN question → watch SSE phases stream
     → see the bar chart render → ask a follow-up. -->
![Data Copilot demo](docs/demo.gif)

---

## What it does

You ask a business question. A four-way classifier decides whether you want **data**, **chitchat**, a **schema tour**, or a deeper **investigation**. For data and investigate intents, a coverage gate first checks the schema can plausibly answer (so "why is conversion dropping?" on a sales-only DB gets an honest refusal, not hallucinated SQL). The SQL gets written, self-heals on failure, pauses for your approval if it looks expensive, executes, and streams back a chart + insight + statistical pattern findings — all the SQL it ran, all the cost it spent.

Then you pin the good ones to a sidebar drawer, and grid them into dashboards.

```
┌─────────────────────────────────────────────────────────────────┐
│  "Why are Beverages sales declining in Q3 1997?"                │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼  classify_intent (Phase 1.3) → investigate
        ▼  retrieve_schema (week 3)    + 1-hop FK expansion
        ▼  coverage_check (Phase 1.1)  → ok, gate passes
        ▼  generate_sql + self-heal    + risk gate + execute
        ▼  summarize_result + detect_patterns (Phase 1.2)
        ▼  ⤵ analyst decides this needs another hop
        ▼  ↻ drill_down: "Top products driving the Q3 drop"
        ▼  ↻ generate_sql → ... → patterns → analyst
        ▼  ↻ (up to 6 hops on investigate; 2 on plain data)
        ▼
   answer + chart + outlier callouts + cost — streamed via SSE
        │
        ▼  ★ "Save this chat" → sidebar (Phase 1.4)
        ▼  📌 "Add to dashboard" → grid (Phase 2.1, FE pending)
```

## Highlights

| | |
|---|---|
| 🧠 **LangGraph state machine, 17 nodes** | Self-healing retries, multi-turn dialogue with PostgresSaver, HITL via `interrupt()` / `Command(resume=…)` |
| 🤝 **Multi-agent supervisor + analyst** | Rule-based supervisor orchestrates a SQL Specialist and an Analyst worker; intent-aware drill-down budget (2 hops for data, **6 for investigate**) |
| 📚 **Schema-aware RAG** | BGE-M3 embeddings over pgvector + FK 1-hop expansion + named-table fast path + full-schema fallback |
| 🛡️ **Honest about its limits** | A cached `schema_profiles` table powers a coverage gate that refuses "no conversion-rate data here" instead of hallucinating SQL, plus a schema-tour intent that answers "what data do you have?" with clickable starter questions ([ADR 0016](docs/decisions/0016-schema-profiling-and-coverage.md)) |
| 📊 **Statistical pattern detection** | Outliers (Tukey IQR + z-score) and trends (OLS + R²) computed deterministically in numpy on every successful data turn; LLM only translates structured findings into natural language so the facts can't be hallucinated ([ADR 0017](docs/decisions/0017-pattern-detection.md)) |
| 🔬 **Investigate mode** | Open-ended research questions ("why is X declining", "deep dive into Y") get a fourth classifier intent and a 6-hop drill-down budget so the analyst can chain multiple sub-queries before answering — plain data questions stay on the cheap 2-hop ceiling ([ADR 0018](docs/decisions/0018-investigate-mode.md)) |
| 📌 **Saved conversations** | One-click pin on any chat → it lands in a left-rail drawer; click to replay full history and keep talking. Zero-friction (no dialog), with inline title editing in the sidebar ([ADR 0019](docs/decisions/0019-saved-conversations.md)) |
| 📐 **Dashboard cards** *(backend)* | Extract any assistant turn into a snapshot card and grid it onto a named dashboard. Static snapshots = $0 / dashboard-load; deleting the source chat never breaks a card. FE coming in Phase 2.1.1 ([ADR 0020](docs/decisions/0020-dashboard-cards.md)) |
| 🔍 **Comparative eval harness** | 50 hand-written cases across 11 categories × **7 A/B experiments** (RAG on/off, self-heal, dialogue context, analyst, coverage_check, patterns_detection, investigate_mode); markdown reports archived per run under [`docs/eval/`](docs/eval/) |
| 💰 **Cost & resilience** | TTL embedding cache (in-memory or Redis via env var), per-turn USD breakdown, exponential-backoff retries on 429/5xx |
| 📡 **Streaming Next.js UI** | SSE phase events, Vega-Lite charts, structured insight panel, HITL confirmation card, cost panel, saved-conversations drawer |
| 🚀 **Production-deploy ready** | Multi-stage non-root Dockerfiles, two Fly.io apps, Prometheus `/metrics`, structured JSON logs, LangSmith traces |
| 📋 **20 ADRs** | Every major decision (and the alternatives explicitly rejected) is recorded under [`docs/decisions/`](docs/decisions/) — including week-by-week build history (ADR 0001-0015) and Phase 1+2 capability ADRs (0016-0020) |

## Run in 60 seconds

```bash
git clone https://github.com/rachel-zhang-dev/data-copilot.git
cd data-copilot
./scripts/make-env.sh        # interactively prompts for the 2 required API keys
./scripts/dev.sh demo        # docker compose up + open browser
```

The full stack (FastAPI + Next.js + Postgres + pgvector) comes up in one command. Ask a question in the chat panel; SQL streams in, the chart renders, and follow-ups remember the prior turn.

You'll need:
- **Docker Desktop** / **OrbStack** / **Colima** — any container runtime that speaks `docker compose` v2.
- **DeepSeek API key** — register at [platform.deepseek.com](https://platform.deepseek.com), ~$1 minimum top-up. Used for the chat LLM.
- **SiliconFlow API key** — register at [cloud.siliconflow.cn](https://cloud.siliconflow.cn). Used for BGE-M3 embeddings; free tier covers this project comfortably.
- (Optional) **LangSmith** — for request tracing.

If you'd rather skip the chat UI and run individual commands, see the [CLI reference](#cli-reference) below.

---

<details>
<summary><strong>Project layout</strong></summary>

```
data-copilot/
├── apps/
│   ├── api/              FastAPI + LangGraph backend (Python 3.12)
│   │   ├── copilot/      importable Python package
│   │   │   ├── agent/    13 LangGraph nodes
│   │   │   ├── eval/     A/B harness, graders, reports
│   │   │   ├── cache.py  TTL / Redis dual backend
│   │   │   └── cost.py   per-turn USD + token reducer
│   │   ├── tests/        291 unit tests + 15 integration tests
│   │   └── Dockerfile    multi-stage, non-root, ~250 MB
│   └── web/              Next.js 15 + TypeScript frontend
│       ├── app/          App Router pages + Route Handlers (SSE proxy)
│       ├── components/   ChatPanel, ChartRenderer, CostPanel, …
│       ├── __tests__/    Vitest suites
│       └── Dockerfile    multi-stage, standalone output, ~150 MB
├── data/
│   ├── seed/             Northwind + pgvector init SQL
│   └── eval/             cases.yaml — 32 hand-written eval cases
├── docs/
│   ├── architecture.md   high-level + per-week status table
│   ├── code-walkthrough.md   line-by-line tour for newcomers
│   ├── decisions/        12 ADRs documenting every major decision
│   └── eval/             markdown reports archived per eval run
├── notebooks/
│   └── eval-walkthrough.ipynb   3-cell version of `dev.sh eval`
├── scripts/
│   ├── make-env.sh       interactive .env bootstrap
│   ├── dev.sh            local dev helper (up | api | ask | eval | demo …)
│   └── deploy.sh         Fly.io deploy wrapper
├── docker-compose.yml    full stack: postgres + api + web
├── pyproject.toml        uv-managed Python deps
└── .env.example
```

</details>

## CLI reference

The dev.sh script is the operator interface for the backend; the Next.js UI is the user interface.

```bash
# Bring everything up (one command)
./scripts/dev.sh demo

# Or à la carte
./scripts/dev.sh up                  # postgres + auto-build schema index
./scripts/dev.sh api                 # FastAPI on :8000
./scripts/dev.sh web                 # Next.js dev server on :3000
./scripts/dev.sh down                # tear it all down

# Talk to the agent without the UI
./scripts/dev.sh ask "How many customers are there?"
./scripts/dev.sh ask --cid abc-123 "And in Germany?"   # follow-up
./scripts/dev.sh ask --cid abc-123 --resume approve    # HITL approve

# Run the eval harness
./scripts/dev.sh eval                          # all 3 A/B experiments
./scripts/dev.sh eval --experiment schema_rag  # one A/B
./scripts/dev.sh eval --dry-run                # stdout only

# Test
./scripts/dev.sh test                          # 291 unit tests
./scripts/dev.sh test-integration              # real APIs + DB
```

### Example: a single-shot data question

```bash
./scripts/dev.sh ask "How many customers are based in Germany?"
```

```
--- SQL ---
SELECT COUNT(*) FROM customers WHERE country = 'Germany' LIMIT 100
--- ROWS (1) ---
[{"count": 11}]
--- ANSWER ---
There are 11 customers based in Germany.
```

Unsafe inputs like `"Drop the orders table"` are caught by the safety layer ([ADR 0002](docs/decisions/0002-sql-safety.md)).

## Feature deep dives

<details>
<summary><strong>Each major capability, with code refs and ADR pointers</strong></summary>

### Schema retrieval

Since week 3, the agent does not dump the entire schema into every
prompt. A retrieval node embeds your question, looks up the top-K
most relevant tables in pgvector, and expands the result one hop
along foreign keys — so `"top products by sales"` automatically
pulls in the bridge table `order_details` even though the question
never names it. See [ADR 0003](docs/decisions/0003-embedding-provider.md)
for why we picked SiliconFlow / BGE-M3.

The retrieval index is built automatically the first time you run
`./scripts/dev.sh up`. Rebuild manually with:

```bash
./scripts/dev.sh index --force   # full rebuild
./scripts/dev.sh index --check   # inspect current state, no writes
```

### Self-healing SQL

Since week 4, when generated SQL fails — either because the safety
layer rejected it (`unsafe_sql`) or because Postgres errored out
during execution (`execution_failed`) — the agent loops back to
`generate_sql` with the failed SQL and the error message in the
prompt, and the model takes another shot. Each error class has its
own retry budget (2 for execution failures, 1 for safety violations,
0 for everything else); see [ADR 0004](docs/decisions/0004-self-healing-policy.md).

The number of attempts is exposed in `AskResponse.attempts`. Pass
`?debug=true` to also receive the per-attempt failure history.

### Multi-turn dialogue

Since week 5, the agent supports follow-up questions. Pass an
existing `conversation_id` to continue a thread; omit it to start a
fresh one (the server allocates a UUID and returns it). State is
persisted to Postgres via LangGraph's `PostgresSaver`, so
conversations survive process restarts and span multiple replicas
behind a load balancer. See [ADR 0005](docs/decisions/0005-conversation-persistence.md).

```bash
# First turn: server allocates the conversation_id and returns it
curl -X POST http://localhost:8000/ask -H 'Content-Type: application/json' \
    -d '{"question": "How many customers are based in Germany?"}'
# => {"conversation_id": "abc-123", "turn_index": 1, "answer": "11", ...}

# Follow-up: the agent can now resolve "And France?"
curl -X POST http://localhost:8000/ask -H 'Content-Type: application/json' \
    -d '{"question": "And France?", "conversation_id": "abc-123"}'
# => {"conversation_id": "abc-123", "turn_index": 2,
#     "sql": "SELECT count(*) FROM customers WHERE country = 'France' LIMIT 100", ...}
```

When a conversation gets long enough to risk overflowing the LLM's
context window, a `compact_history_node` summarises the older turns
into a single synthetic entry. The threshold is configurable via
`COMPACTION_THRESHOLD_TOKENS` (default 4000). Per-turn retry
budgets reset between turns so a follow-up always starts fresh.

### Eval harness (week 6)

A reproducible A/B harness measures whether each Week 3-5 feature
actually moves the metrics. 32 hand-written cases × 4 metrics ×
3 A/B experiments yield committable markdown reports under
[`docs/eval/`](docs/eval/).

```bash
./scripts/dev.sh eval                          # all 3 experiments
./scripts/dev.sh eval --experiment schema_rag  # one A/B
./scripts/dev.sh eval --dry-run                # stdout only
```

The three experiments are `schema_rag`, `self_healing`, and
`dialogue_context` — each pairs the production default against a
"feature off" baseline so the per-feature contribution is visible.
See [ADR 0007](docs/decisions/0007-eval-methodology.md) for the
methodology and trade-offs (e.g. why deterministic graders, why not
RAGAS).

> **Note** &nbsp;The first `uv sync` downloads ~1 GB of wheels. Subsequent runs are instant.

### Human-in-the-loop confirmation (week 7)

Since week 7, when the agent generates SQL whose Postgres planner
cost exceeds a threshold (default `1000.0`, tunable via
`RISK_EXPLAIN_COST_THRESHOLD`), the graph pauses **before** executing
and surfaces a `pending_confirmation` response. The caller answers
with `resume="approve"` or `resume="reject"` on the same
`conversation_id`, and the graph picks up at the interrupt point via
LangGraph's `Command(resume=...)` primitive — persisted through the
same `PostgresSaver` that powers multi-turn dialogue, so the pause
survives process restarts.

```bash
# 1) Ask an expensive question — the agent pauses
./scripts/dev.sh ask "Show me every order with every product and customer detail"
# => --- PENDING CONFIRMATION ---
#    reason:     Postgres planner estimated total cost 1234.5 ...
#    total_cost: 1234.5
#    threshold:  1000.0
#    --- SQL ---
#    SELECT ... FROM orders JOIN products ... LIMIT 100
#    conversation_id: abc-123

# 2a) Approve — the agent runs the SQL and answers
./scripts/dev.sh ask --cid abc-123 --resume approve

# 2b) Or reject — the turn finalises with "I did not run that query"
./scripts/dev.sh ask --cid abc-123 --resume reject
```

The same pause/resume mechanic is wired into the HTTP API via the
optional `resume` field on `POST /ask`. See
[ADR 0008](docs/decisions/0008-human-in-the-loop.md) for why the
planner cost (vs row-count heuristics or `EXPLAIN ANALYZE`), why
`interrupt()` over external queues, and the per-class threshold
tuning notes.

### Visualisation + structured insight (week 8)

Every successful data turn now returns three new fields alongside
the rows:

- `chart_kind` — one of `kpi` / `bar` / `line` / `grouped_bar` /
  `table`, picked deterministically from the result shape.
- `chart_spec` — a [Vega-Lite v5](https://vega.github.io/vega-lite/)
  specification, populated for `bar` / `line` / `grouped_bar`; the
  Next.js UI in Week 10 renders it with one `<VegaLite>` call.
- `insight` — a structured `{headline, bullets, metric_highlights}`
  envelope produced by the LLM in JSON mode. The legacy single-
  sentence `answer` is the same string as `insight.headline`, so
  every existing caller keeps working.

```bash
./scripts/dev.sh ask "Count customers grouped by country"
# --- SQL ---
# SELECT country, count(*) FROM customers GROUP BY country LIMIT 100
# --- INSIGHT ---
#   - USA leads with 13 customers
#   - 21 countries total
#   metrics:
#     Top country (USA): 13
# --- CHART (bar) ---
# {"$schema":"https://vega.github.io/schema/vega-lite/v5.json", ...}
# --- ANSWER ---
# USA has the most customers, with 13.
```

Failure is fail-soft on both axes: a misbehaving LLM that returns
non-JSON degrades to the legacy NL-only `answer`; a malformed result
set falls back to `chart_kind="table"`. Neither path ever blocks a
user from seeing their rows. See
[ADR 0009](docs/decisions/0009-visualization-and-insight.md) for the
heuristic decision table, why Vega-Lite over Chart.js / custom
schemas, and why a structured `insight` envelope vs a separate
`insight_node`.

### Caching, cost reporting, and retry resilience (week 9)

Three operational improvements ship in Week 9:

- **Embedding cache.** A process-local `TTLCache` keyed by
  `(model, text)` short-circuits repeat questions before the
  SiliconFlow API call. Enabled by default; tunable via
  `EMBEDDING_CACHE_*` env vars. Multi-replica deploys (Week 11)
  swap the backend to Redis behind the same interface.
- **Cost report.** Every node publishes a small `CostBreakdown`
  increment into a cumulative `state.cost` (LLM / embedding / DB
  call counts plus token + USD estimates from a hand-maintained
  unit-price table). Surfaced on `AskResponse.cost` and via
  `./scripts/dev.sh ask --show-cost "..."`. The headline pitch
  ("cost-aware text-to-SQL") becomes a number rather than a
  promise.
- **Retries with exponential backoff.** LangChain's
  `ChatOpenAI.max_retries` (default 3) handles transient LLM
  failures; a `tenacity` wrapper around `embed_query` does the
  same for the embedding provider. Both retry only on 429 / 5xx /
  timeout — domain errors still flow to the self-healing loop.

```bash
./scripts/dev.sh ask --show-cost "How many customers in Germany?"
# --- COST (cumulative) ---
#   llm_calls=3 embedding_calls=1 db_explain=1 db_select=1
#   tokens: in=154 out=75
#   est_usd=$0.000042
# --- ANSWER ---
# There are 11 customers based in Germany.
```

Week 8's `summarize_result_node` also flips on DeepSeek's JSON
mode (`response_format={"type": "json_object"}`) so the structured
`Insight` envelope becomes the common case rather than the lucky
case. The `parse_insight` fallback stays in place as defence in
depth. See [ADR 0010](docs/decisions/0010-caching-and-resilience.md)
for why only embeddings get cached, why in-memory before Redis,
and the per-token USD pricing table.

### Streaming Next.js front-end (week 10)

The full agent surface — phases, charts, structured insight, HITL
pause, cost — now ships as a Next.js 15 app at `apps/web/`. The
backend exposes `POST /ask/stream` (Server-Sent Events); the
front-end consumes it through a Route-Handler proxy so the browser
talks to a single origin.

```bash
# First time only
cd apps/web && pnpm install

# Run both processes (two terminals)
./scripts/dev.sh api          # FastAPI on :8000
./scripts/dev.sh web          # Next.js on :3000

# Optional: regenerate TS types from the live OpenAPI document
cd apps/web && pnpm gen:types
```

SSE event taxonomy:

| event                    | when                                    | data                                  |
|--------------------------|-----------------------------------------|---------------------------------------|
| `phase`                  | once per node activation                | `{node, diff, internal}`              |
| `pending_confirmation`   | HITL gate paused the graph              | `{conversation_id, pending_risk}`     |
| `done`                   | turn finished                           | full `AskResponse`                    |
| `error`                  | server-side exception inside the stream | `{detail, type}`                      |

The single Client Component (`ChatPanel`) owns the chat state — no
Zustand, no Redux. Vega-Lite charts are lazy-imported per turn so
the initial bundle stays small. See
[ADR 0011](docs/decisions/0011-frontend-and-streaming.md) for the
SSE-vs-WebSocket trade-off, why the Route-Handler proxy, and the
`openapi-typescript`-driven type contract between front-end and
back-end.

### Deployment, observability, Redis cache (week 11)

Both apps ship as multi-stage Docker images and deploy to Fly.io
with one command each:

```bash
./scripts/deploy.sh api          # backend image → data-copilot-api.fly.dev
./scripts/deploy.sh web          # frontend image → data-copilot-web.fly.dev
./scripts/deploy.sh all          # both, in order
./scripts/deploy.sh smoke        # post-deploy health probes
```

Secrets (`DEEPSEEK_API_KEY`, `SILICONFLOW_API_KEY`, `DATABASE_URL`,
`CORS_ORIGINS`, optional `REDIS_URL`) are managed via
`fly secrets set -a <app> ...`; never committed.

Three observability surfaces ship in this week:

| URL                                | Purpose                                                         |
|------------------------------------|-----------------------------------------------------------------|
| `GET /health`                      | Cheap liveness probe                                            |
| `GET /metrics`                     | Prometheus counters + histograms (request rate, latency, …)     |
| `GET /admin/stats`                 | Human dashboard: cache hit-rate, uptime, non-secret settings    |
| `GET /api/health` (front-end)      | Wraps the upstream probe so the FE stays "alive" if API is asleep |

The embedding cache (week 9) gains a Redis backend: set `REDIS_URL`
and `get_embedding_cache()` swaps from `TTLCache` to `RedisCache`
behind the same `EmbeddingCacheBackend` protocol — every consumer
of the cache (the retriever, the cost reducer, the admin endpoint)
stays unchanged. The fallback is graceful: a Redis outage degrades
to cache-miss behaviour, never to user-facing errors.

Week 11 also folds in the three audit items from Week 10
(`resume`-via-SSE so the post-approve UX streams, env-driven CORS,
SSE heartbeats so reverse proxies don't drop idle connections) plus
two from Week 9 (`last_was_cache_hit` migrates to `ContextVar`,
unknown-model cost estimates log a one-shot warning). See
[ADR 0012](docs/decisions/0012-deployment-and-observability.md)
for the full rationale on Fly.io vs alternatives, Prometheus vs
Sentry, and the Redis-migration design.

> **Note** &nbsp;The first `uv sync` downloads ~1 GB of wheels. Subsequent runs are instant.

</details>

## Roadmap

| Week | Milestone |
|------|-----------|
| 1 ✅ | Project scaffold, environment, hello-world LangGraph node |
| 2 ✅ | Single-table text-to-SQL baseline (no RAG yet) |
| 3 ✅ | Schema retrieval with pgvector (multi-table) |
| 4 ✅ | Refactor to full LangGraph state machine with self-healing |
| 5 ✅ | Multi-turn dialogue + chat history compaction |
| 6 ✅ | Evaluation set + 3 A/B experiments |
| 7 ✅ | Human-in-the-loop confirmation for expensive queries |
| 8 ✅ | Visualisation generation + structured insight |
| 9 ✅ | Caching layer · cost report · retries with exponential backoff |
| 10 ✅ | Next.js front-end with streaming responses |
| 11 ✅ | Docker production image · Fly.io deploy · monitoring · Redis cache |
| 12 ✅ | Polish, demo video, blog outline, simplified onboarding |
| 12.5 ✅ | Multi-agent: supervisor + analyst pattern (bounded drill-down loop) |
| 1.1 ✅ | Phase 1 / step 1 — schema profiling + coverage gate + schema explorer ([ADR 0016](docs/decisions/0016-schema-profiling-and-coverage.md)) |
| 1.2 ✅ | Phase 1 / step 2 — statistical pattern detection (outliers + trends) merged into insight bullets ([ADR 0017](docs/decisions/0017-pattern-detection.md)) |
| 1.3 ✅ | Phase 1 / step 3 — investigate intent + intent-aware drill-down budget (2 hops for data, 6 for investigate) ([ADR 0018](docs/decisions/0018-investigate-mode.md)) |
| 1.4 ✅ | Phase 1 / step 4 — saved conversations + left-rail drawer + click-to-replay ([ADR 0019](docs/decisions/0019-saved-conversations.md)) |
| 2.1 🟡 | Phase 2 / step 1 — dashboard cards backend (DDL + 7 endpoints + ADR); FE grid renderer pending ([ADR 0020](docs/decisions/0020-dashboard-cards.md)) |
| 2.1.1 ⏳ | Phase 2 / step 1, FE half — "Add to dashboard" button on chat turns, dashboard list page, react-grid-layout renderer for cards, drag-resize, inline rename. Backend endpoints already shipped in 2.1; this is purely a Next.js + Tailwind frontend commit. Est. 2-3 days. |

## Project layout

```
data-copilot/
├── apps/
│   ├── api/                # FastAPI + LangGraph backend
│   │   ├── copilot/        # importable Python package
│   │   │   ├── agent/      # LangGraph nodes, state, graph builder
│   │   │   ├── config.py
│   │   │   ├── llm.py
│   │   │   └── main.py     # FastAPI app
│   │   └── tests/
│   └── web/                # Next.js UI (added in week 10)
├── data/
│   └── seed/               # SQL fixtures (Northwind / TPC-H subset)
├── docs/
│   ├── architecture.md
│   └── decisions/          # ADRs — one .md per major decision
├── scripts/
│   └── dev.sh              # one-stop local commands
├── docker-compose.yml      # Postgres + pgvector
├── pyproject.toml          # managed by uv
├── .env.example
├── .gitignore
└── README.md
```

## Contributing

This repository is primarily a personal learning project. Issues and PRs are welcome — feel free to file an issue if anything is unclear or broken.

## License

[MIT](LICENSE) © 2026 Rachel Zhang
