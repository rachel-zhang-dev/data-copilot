# Blog series outline — *Building a production-grade text-to-SQL agent: 11 weeks of LangGraph*

> Working draft. Each post is a 1500–2500 word read with one screenshot and
> one code excerpt. Cross-linked back to the repo's [ADRs](decisions/) so
> readers who want to go deeper land in the right place.

The throughline of the series is **honesty about trade-offs**. Every post
shows the alternatives I rejected and why; nothing is "I built X". The
reader leaves knowing the reasoning, not just the recipe.

---

## Post 1 — *"Why text-to-SQL is harder than text-to-anything"* (intro)

**Hook**: a screenshot of the agent answering "Top 5 products by total
revenue" with a chart, then the equivalent five-table JOIN it had to
generate. "This took eleven weeks. Here is why each week mattered."

**Sections**:

* The naive demo: GPT-4 + a one-shot prompt. Why it falls over within
  a day in any non-trivial schema (token limits, JOIN reasoning, no
  recourse on errors).
* Three failure modes a production text-to-SQL tool must handle:
  syntactic SQL errors, expensive-but-legal SQL, and "the user meant
  something else". The remaining posts each address one or two of
  those.
* What I built — one paragraph + a hero image of the architecture.
* The shape of the series: a post per major feature, each as an A/B-
  comparable improvement over the previous baseline.

**Cross-links**: [ADR 0001](decisions/0001-tech-stack.md), full repo.

---

## Post 2 — *"AST-based SQL safety in 50 lines (and why regex doesn't work)"*

The Week 2 sql-safety story. A surprisingly approachable post for
readers who haven't built parsers before.

* Three failure modes a regex denylist can't catch (`'DELETE'` in a
  string literal, `WITH … SELECT`, brand-new keywords like `LISTEN`).
* `sqlglot` AST traversal in 50 lines: parse, check root node type,
  reject `FOR UPDATE` / `INTO`, inject `LIMIT`. One screenshot of the
  validator's unit tests passing.
* The 12 unit tests that codify the policy.
* What we left out (and why): postgres-level read-only role belongs
  to the deploy story; this is the application layer.

**Cross-links**: [ADR 0002](decisions/0002-sql-safety.md).

---

## Post 3 — *"Schema RAG that actually scales: pgvector + foreign keys"*

The Week 3 retrieval story. The post most likely to rank for SEO.

* The setup: 14-table Northwind, JOIN questions failing because the
  LLM didn't know the bridge table.
* Why dumping the full schema in every prompt wastes 60% of the
  budget (back-of-envelope tokens math, real numbers from this repo).
* Top-K vector retrieval is half the answer; the **foreign-key 1-hop
  expansion** is the other half. Walk through "Top 5 products by
  sales" → top-K returns `products`, FK expansion drags in
  `order_details`, the LLM JOINs correctly.
* The named-table fast path: when the user's question literally
  contains a table name, skip the embedding round-trip entirely.
* Choosing SiliconFlow + BGE-M3 over DashScope and OpenAI; the
  one-env-var swap pattern.

**Cross-links**: [ADR 0003](decisions/0003-embedding-provider.md).

---

## Post 4 — *"Self-healing SQL with a per-class retry budget"*

The Week 4 story. Most engaging because every reader has watched an
LLM produce nearly-right SQL that fails on a typo.

* The retry surface area: `unsafe_sql` vs `execution_failed` vs
  everything-else. Two of those benefit from re-prompting; one
  doesn't.
* Per-class budget = `{execution_failed: 2, unsafe_sql: 1, fatal: 0}`.
  Why a single global N is the wrong shape (table at the top of the
  ADR).
* The retry prompt: only the LAST failure, not the full history.
  Citations to the few RLHF / agent papers that argue for compact
  feedback over long history.
* The state-machine view: in LangGraph, a "loop" is just two
  conditional edges back to `generate_sql`. The graph reads like the
  flowchart in the post.

**Cross-links**: [ADR 0004](decisions/0004-self-healing-policy.md).

---

## Post 5 — *"Multi-turn conversations without inventing your own session store"*

The Week 5 story. The post that makes a reader say "oh that's the
correct way".

* What "follow-up" means in text-to-SQL: "And France?" alone is
  useless; the agent has to remember the last turn's filters.
* The temptation: build a session store in Redis. The reality:
  `langgraph-checkpoint-postgres` already does this, on the Postgres
  you already have, with one constructor call.
* Three subtle issues that showed up only after running for real:
  - Sync `PostgresSaver` doesn't implement `aput`, so `await
    graph.ainvoke(...)` raised `NotImplementedError` on first turn.
    Fix: switch to `AsyncPostgresSaver`.
  - Two concurrent `/ask` calls on the same conversation_id silently
    drop history. Fix: `pg_advisory_lock` keyed on `blake2b(thread_id)`.
  - State-merge reducer mismatch on the `dialogue` field — append
    most of the time, replace once compaction runs. The custom
    `replace_or_append` reducer is six lines.

**Cross-links**: [ADR 0005](decisions/0005-conversation-persistence.md).

---

## Post 6 — *"Comparative eval, the only kind that matters"*

The Week 6 story. The post recruiters will actually read because it's
the most "production-engineering"-flavoured.

* Why running 80 cases through the agent and saying "67% pass" is
  meaningless without a comparison group.
* The three A/Bs (RAG on/off, self-heal on/off, dialogue context
  on/off) and the actual measured numbers from this repo. Charts
  pulled from `docs/eval/`.
* Why deterministic graders > LLM-judge for this scale: cost,
  reproducibility, and "the SQL contains `order_details`" is a
  good enough signal.
* Why I rejected RAGAS (chatbot eval, wrong shape for SQL).
* The eval scaffold as a regression detector: any future PR can run
  the harness and surface a per-category delta.

**Cross-links**: [ADR 0007](decisions/0007-eval-methodology.md),
[`docs/eval/`](eval/).

---

## Post 7 — *"Human-in-the-loop with LangGraph's `interrupt()`"*

The Week 7 story. The clever-but-pragmatic post.

* The third failure class: SQL that's legal, safe, but expensive.
* Postgres `EXPLAIN (FORMAT JSON)` returns a `Total Cost` number that's
  the right shape for "is this expensive?" without `EXPLAIN ANALYZE`
  actually executing it. Wrap it in `SET LOCAL statement_timeout` so
  the planner itself can't hang the agent.
* `interrupt()` + `Command(resume=...)` over an external work queue:
  the agent state pauses *inside* the graph, the existing
  PostgresSaver picks up the persistence for free, no sidecar
  service.
* "Block-and-ask" vs "auto-reject and let the user override in
  natural language": why the former is the correct default and what
  Snowflake / Mode / BigQuery Studio chose.
* The audit trail comes free: the checkpoint row for the paused
  turn shows what was approved and when.

**Cross-links**: [ADR 0008](decisions/0008-human-in-the-loop.md).

---

## Post 8 — *"Charts the agent decides, charts the agent renders"*

The Week 8 story. Visually the prettiest post.

* Why "let the LLM decide the chart" is the wrong default: cost,
  latency, and the heuristic ("one nominal + one quantitative
  column = bar") catches >95% of cases for free.
* The five-bucket decision table (kpi / bar / line / grouped_bar /
  table). One screenshot of each rendered.
* Why Vega-Lite over Chart.js / a custom mini-schema: the spec is
  the wire format, the LLM has training on it, and adding heatmap
  later doesn't break the schema.
* The structured `Insight` envelope that replaces the legacy single-
  sentence answer. Pydantic-validated, with caps so a misbehaving
  LLM can't return 50KB bullets. Fail-soft: parse error → fall back
  to raw text as `answer`, never block the user.

**Cross-links**: [ADR 0009](decisions/0009-visualization-and-insight.md).

---

## Post 9 — *"Cost, caches, and other things you only think about in production"*

The Week 9 story. The post developers reading on their lunch break
forward to their tech leads.

* Three small wins that feel boring until they save a $200 bill:
  - In-process TTL embedding cache (week 9) → Redis-backed
    (week 11) by changing one env var, zero code changes — because
    the protocol surface is the only public API.
  - Per-turn cost reducer in LangGraph state. `state.cost` field-
    wise sums across self-heals and HITL resumes. The CLI's
    `--show-cost` prints it.
  - `tenacity` retries on 429 / 5xx (with `Retry-After` honoured).
    Three lines that turn "the agent crashed" into "the agent waited
    600ms and continued".
* Why I cache embeddings but NOT SQL results: pure-function vs
  data-mutation argument.
* The hand-maintained price table — and why it's safer to
  overestimate by 10x than to call a billing API.

**Cross-links**: [ADR 0010](decisions/0010-caching-and-resilience.md).

---

## Post 10 — *"SSE streaming with zero new dependencies"*

The Week 10 story. The post the frontend folks read.

* Why SSE, not WebSocket: the agent's data flow is strictly
  server-to-client during a turn. The few times the client needs to
  send something (resume after HITL pause), it's a separate HTTP
  request.
* Phase events — one per LangGraph node — turn "wait 8s" into
  "watch the agent think for 8s". Same total latency, different
  story to the user.
* Auto-generating TS types from the FastAPI OpenAPI document so the
  frontend can never ship a schema mismatch.
* Three React `useState`s + a URL parameter > Zustand / Redux for
  the chat state machine.

**Cross-links**: [ADR 0011](decisions/0011-frontend-and-streaming.md).

---

## Post 11 — *"Two Fly.io apps, three secrets, one demo URL"*

The Week 11 story. The closer.

* The deploy criteria: SSE-friendly, pgvector-friendly, scale-to-
  zero so demos cost $0 between sessions, single config file per
  app. Why Fly.io won, why Render / Vercel / Hetzner didn't.
* Multi-stage non-root Dockerfiles. The numbers (250 MB API,
  150 MB web) and why those are the targets.
* Observability without Sentry / Datadog: `/metrics` for Prometheus,
  `/admin/stats` JSON for humans, LangSmith for distributed traces,
  structlog → Fly's log shipper. The fewest moving parts that
  answers "is the agent up", "is the cache helping", "how much
  did the last hour cost".
* The Redis migration: the in-memory → Upstash Redis swap is one
  env var because the protocol surface was right from the start.

**Cross-links**: [ADR 0012](decisions/0012-deployment-and-observability.md).

---

## Post 12 — *"What I'd do differently if I started over"* (closer)

The retrospective post. Honesty bonus: this is what makes the series
trustworthy.

Three things I'd change if I started fresh tomorrow, with reasoning:

1. **Build the eval harness in Week 2, not Week 6.** Every Week 3-5
   ADR is intuition until Week 6 measures it. Doing eval first
   would have caught the dialogue-context bug a week earlier and
   made every subsequent design decision data-driven.
2. **Adopt LangGraph's stream API earlier.** Week 5 multi-turn
   without streaming made the UX feel slow even when latency was
   fine. Token streaming inside `generate_sql` is on the deferred
   list for ADR 0011 §future.
3. **Lock the LLM provider price table to a CI check.** DeepSeek
   adjusted prices once during development; my hand-maintained table
   silently overestimated for two weeks. A nightly job that scrapes
   the provider's pricing page and fails CI on drift would have
   caught it.

What stays the same:
* LangGraph as the runtime.
* Postgres for *everything* — business data, vectors, checkpoints.
* Comparative methodology in the eval.
* Refusing to add Sentry / Datadog at this scale.

The series ends with: "the repo and the ADRs are the long-form
version of these posts. Steal whatever's useful."

---

## Publishing notes (for me, not the post)

* Cross-post the lot to a personal blog and to dev.to / Medium for
  reach. Don't bother with Hashnode — too small an audience.
* Lead the series with Post 1 + Post 4 (self-healing) + Post 11
  (deploy). Those three carry the most weight individually.
* Keep one screenshot + one code excerpt per post. Less is more.
* Linkedin post for each major release: link to the post, not the
  repo, so I get a click-through metric.
* If a post hits, repurpose into a 60-90s video — same script, faster.

## Image kit (TODO before publishing)

* Hero image: agent answering "Top 5 products by sales" with chart
  visible. Used in Post 1 + LinkedIn carousel.
* Architecture diagram: same `mermaid` block from `docs/architecture.md`,
  rendered to PNG. Used in Post 1 + Post 11.
* Eval delta chart: bar chart of "schema_rag on vs off, by category"
  from a real `docs/eval/` run. Used in Post 6.
* Five chart kinds: kpi / bar / line / grouped_bar / table side-by-
  side, captured from the live UI. Used in Post 8.
