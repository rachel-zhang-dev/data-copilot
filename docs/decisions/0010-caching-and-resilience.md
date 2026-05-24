# ADR 0010: Embedding cache, cost reporting, and retry resilience

> Status: Accepted · Date: 2026-05 (Week 9) · Supersedes: none

## Context

Through Week 8 the agent is feature-complete enough to demo end-to-end:
the graph reasons, retries, pauses, charts, and summarises. What it
does not yet do is **survive the rough edges of running for real**:

* Every turn embeds the question afresh, paying ~150 ms and one
  SiliconFlow API call even for the *exact same* question asked
  twice in a row.
* Operators have no idea what a turn costs. Cost is silently
  proportional to LLM calls + retries + embedding calls; without a
  number the "cost-aware design" claim in the README is empty.
* A single transient 429 / 5xx / timeout from the LLM provider
  surfaces to the user as a stack trace. Self-healing (Week 4)
  handles *logical* SQL failures, not *network* failures.

Week 9 lands three small, independent improvements that close those
three gaps. Each is light enough that a single ADR can cover all
three rather than splitting hairs.

## Decision

### 1. Embedding cache (in-memory, TTL)

We cache the output of `embeddings.embed_query(question)` keyed by
`(model_name, query_text)`. The cache lives in-process as a
`cachetools.TTLCache`:

* `EMBEDDING_CACHE_MAX_SIZE` (default 1024 entries)
* `EMBEDDING_CACHE_TTL_SECONDS` (default 3600)
* Stats counters (`hits`, `misses`, `size`) exposed via
  `cache.cache_stats()` for future observability.

We **do not** cache schema retrieval, SQL results, or full LLM turns.
Each is harder to invalidate correctly (data changes underneath SQL
results; dialogue context makes LLM-turn caching nearly impossible to
key) for a marginal latency win on top of the embedding cache.

The cache is **process-local**. When Week 11 introduces multiple
replicas, the migration path is a `RedisCache` subclass with the same
interface — the `embedding_cache_enabled` feature flag and the
`cache.cache_stats()` shape stay constant.

### 2. Cost report

Each node that incurs cost emits a small `CostBreakdown` increment
that LangGraph merges into `state.cost` via a field-wise additive
reducer. The accumulator is **cumulative across the conversation** —
self-heal retries, HITL resumes, and follow-up turns all add to the
same `cost` field. Callers that want a per-turn delta diff against
the previous turn's checkpointed value. The CLI and eval grader
deliberately show the cumulative figure because that is the number
operators ask about ("how much has this conversation cost me?").
The shape:

```python
class CostBreakdown(TypedDict):
    llm_calls: int                # classify + generate + summarize + retries
    embedding_calls: int          # net of cache hits (hits don't count)
    db_explain_calls: int
    db_select_calls: int
    est_tokens_in: int            # provider tokens-in, where known
    est_tokens_out: int           # provider tokens-out, where known
    est_usd: float                # tokens × per-model unit price
```

Tokens come from `AIMessage.response_metadata["token_usage"]` when
LangChain populates it (DeepSeek does), otherwise from a fall-back
`chars / 4` heuristic. Unit prices live in `copilot.cost.UNIT_PRICES`
keyed by model name, defaulting to DeepSeek's published rates.

`AskResponse.cost` surfaces the per-turn breakdown. The CLI's
`./scripts/dev.sh ask --show-cost "..."` prints it.

A `/admin/cost?conversation_id=X` endpoint was considered and
deferred — surfacing per-turn cost on `/ask` covers the actual demo
need, and a cumulative aggregator over the checkpoint is properly a
Week 11 dashboard concern.

### 3. Retries with exponential backoff

LangChain's `ChatOpenAI` accepts `max_retries` and uses an internal
exponential-backoff between attempts. We bump it from the default
(2) to a configurable `LLM_MAX_RETRIES = 3` and wire it through
`get_llm()`. SiliconFlow embeddings get the same treatment via a
small `tenacity`-decorated wrapper around `embed_query` — the
underlying `OpenAIEmbeddings` class does not surface a retry knob
directly.

Retried error classes:

* `httpx.TimeoutException` / `httpx.ConnectError`
* HTTP 429 (rate limit) — with respect to `Retry-After` if present
* HTTP 5xx (server side)

`unsafe_sql` and other domain errors are **not** retried at the
network layer — that is the self-healing loop's job (Week 4).

## Why only embedding gets cached

Three other layers were on the table:

* **Schema retrieval (top-K tables)**: already fast (~5 ms in
  pgvector for 14 rows). The latency win on top of caching
  `embed_query` is < 5 ms — not worth the staleness risk if the
  index gets rebuilt.

* **SQL → rows**: data changes underneath. A 60-second TTL would
  catch back-to-back duplicate questions and miss the obvious
  staleness pitfalls, but it changes the agent's contract: now
  "what's the customer count?" can return a stale number without a
  re-execution. Punting this until we have an explicit "this table
  is mutable" annotation.

* **Full LLM turn (question → answer)**: would have to key on
  `(question, dialogue_context, schema, feature_flags)` because all
  of those change the prompt. Hash collisions and key drift make
  this a maintenance burden well beyond what a portfolio project
  should carry.

Embeddings sit in the sweet spot: pure function of `(model, text)`,
no underlying-data dependency, hot path for every data turn that
goes through schema RAG.

## Why in-memory, not Redis (yet)

* Single replica on Fly.io (the Week 11 deployment target) means a
  process-local cache hits 100% of the time it could — there is no
  cross-replica miss to worry about.
* Redis adds an operational dependency (TLS, auth, monitoring) and
  one more failure mode (Redis down → agent slow but not broken,
  if implemented carefully).
* The migration path is well-trodden: swap `_BACKEND = TTLCache(...)`
  for `_BACKEND = RedisCache(...)` exposing the same `get`/`set`
  interface, leave the `embed_query` wrapper untouched.

The README's Week 11 roadmap line records "swap embedding cache to
Redis if scaling out" so this commitment is on the timeline even
before the first cache miss bites.

## Why exponential backoff isn't a stack overhaul

`tenacity` is the de-facto Python retry library and already a
transitive dep via `langchain-core`. Reusing it costs zero new
wheels. LangChain's built-in `max_retries` on `ChatOpenAI` covers the
chat path with no extra code. The two-line wrapper for embeddings
keeps the symmetry without forking the LangChain client itself.

We do **not** wrap Postgres calls in retries. Connection-pool
errors are surfaced to LangGraph's error handler, which already has
self-healing semantics; layering tenacity on top adds latency to
genuinely fatal misconfigurations.

## Failure modes

| Scenario                              | Behaviour |
|---------------------------------------|-----------|
| Cache miss                            | Embed, store, return — unchanged from pre-week-9 |
| Cache hit                             | Return cached vector; `embedding_calls` stays at 0 |
| Cache disabled (flag off)             | Bypass entirely; legacy path |
| LLM provider 5xx ×3                   | Last exception propagates; existing fail-soft catches in nodes apply |
| LLM provider 429 with `Retry-After`   | Wait the header value, then retry, up to budget |
| Embedding provider 429                | Tenacity wrapper retries; on exhaustion, raises `EmbeddingError` which `retrieve_schema_node` already catches |

The cache itself never *raises*: a miss is just a miss. A
`RuntimeError` from `TTLCache` would be caught by the wrapper and
logged, treating it as a forced miss.

## Consequences

### Good

* Repeat questions are visibly faster (no embedding round-trip).
* `eval` reports gain a meaningful `avg_total_tokens` column once
  the cost reducer is in place — token figures stop being pure
  `chars/4` estimates for the LLM portion.
* Operators see a concrete USD figure per turn — the "cost-aware"
  pitch becomes demonstrable.
* Transient provider hiccups stop becoming user-visible 500s.

### Bad / accepted trade-offs

* Per-process cache means a server restart cold-starts the cache.
  Acceptable for one replica; the Redis swap removes this when
  multi-replica.
* Token-from-`response_metadata` is provider-dependent. DeepSeek
  populates it; some smaller providers do not. The `chars/4`
  fallback keeps the field always populated but makes mixed-provider
  A/Bs noisier.
* `cost.est_usd` depends on a hand-maintained price table. The
  alternative (calling an external billing API) is brittle.

## Future work

* **Redis backend for the cache** when Fly.io scales out (Week 11).
  Same interface, swap-in change; the embedding cache is the only
  in-memory state the agent carries.
* **Per-user cost limits.** Reuse the `risk_explain_cost_threshold`
  pattern: a daily USD budget per `conversation_id` rejects new
  questions when exceeded.
* **Caching for SQL results.** Requires per-table mutability
  annotations (and an invalidation path on data changes). Belongs
  to a future "warehouse mode" feature.
* **Embedding-cache stats endpoint.** `GET /admin/cache/stats`
  returning the `(hits, misses, size)` triple — useful for
  dashboards. Deferred to Week 11.
