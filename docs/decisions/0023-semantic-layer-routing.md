# ADR 0023: Mini semantic layer + routing mode (Phase 3.1)

> Status: Accepted · Date: 2026-06 (Phase 3.1) · Supersedes: none

## Context

ADR 0021 added a critic LLM as the seventh defensive layer against
"SQL ran fine but answered the wrong question". The critic catches
semantic errors **after** they happen — it can't prevent the LLM
from picking a wrong JOIN direction in the first place.

The dbt Semantic Layer 2026 benchmark
([blog](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026))
quantified the alternative — moving the "what does this metric
mean?" decision out of the LLM:

| Model | Path | Accuracy |
|---|---|---|
| Claude Sonnet 4.6 | Text-to-SQL | 90.0% |
| Claude Sonnet 4.6 | **Semantic Layer** | **98.2%** |
| GPT-5.3 Codex | Text-to-SQL | 84.1% |
| GPT-5.3 Codex | **Semantic Layer** | **100.0%** |

And on real unnormalised ERP tables: 10-31% raw text-to-SQL vs 72-100%
with a semantic layer.

ThoughtWorks Tech Radar 2026 moved raw text-to-SQL to **"Hold"**
specifically because of this gap. Snowflake's Cortex Analyst (Mar 2026)
shipped "Semantic Views" as a first-class Snowflake schema object;
Databricks Genie connects to metric views; Cube and dbt expose
metrics as MCP tools; Wren AI built a whole product around its
metadata-definition language.

The takeaway: **production text-to-SQL agents are no longer just
text-to-SQL**. The flexible LLM path is the fallback; the deterministic
semantic-layer path is the default for everything modelled.

This ADR lands the smallest viable version of that architecture in
this project — a "Phase 3.1 mini semantic layer" with six metrics,
five dimensions, six relationships, and a router that picks between
the two paths.

## Decision

### 1. Topology — semantic-first, LLM-fallback (Snowflake's "Routing Mode" pattern)

```
classify_intent → retrieve_schema → coverage_check
                                       ↓ ok
                                  metric_router    ← LLM picks {metric, dims, time, filters}
                                       │
                              ┌────────┴────────┐
                              │                 │
                       semantic_layer      fallback
                              │                 │
                              ↓                 ↓
                       metric_resolver    generate_sql  ← existing text-to-SQL
                              │                 │
                              └────────┬────────┘
                                       ↓
                                  validate_sql → check_risk → execute_sql → critique_sql → summarize
```

Two new nodes:

* `metric_router_node` (`agent/semantic_node.py`) — one LLM call.
  Sees a compact menu of metrics + dimensions and the user question.
  Returns `{answerable: bool, metric?, dimensions?, time_range?, filters?, reason}`.
* `metric_resolver_node` — pure-Python compiler. Takes a validated
  `ResolverSpec` and emits SQL via `semantic/resolver.py`.

The resolver's SQL flows into the EXISTING `validate_sql` →
`check_risk` → `execute_sql` → `critique_sql` → `summarize_result`
pipeline. Both paths share defenses; only the "who wrote the SQL?"
question differs.

### 2. YAML model, not code

`data/semantic.yml` declares metrics + dimensions + relationships +
table aliases + time columns. Loaded once at startup, validated via
Pydantic, cached.

Why YAML (vs code):

* Editable by data team, not engineers.
* Diffs are reviewable in PRs.
* Future SaaS extraction (lift this whole module into a microservice)
  doesn't require deploying code.
* Mirrors what Snowflake Cortex's `semantic_view` YAML / dbt
  MetricFlow / Cube `cube` JS look like. Anyone who's worked with
  any of those reads our YAML in 30 seconds.

Why not full dbt MetricFlow / Cube JS / Snowflake YAML:

* Those each have ~30 features (parameter views, derived metrics,
  custom granularities, conformed dimensions across cubes). The MVP
  needs maybe 5 of them. Inheriting the full surface area to use
  10% of it would be bad taste.
* If the project scales, swapping our loader for "load a real dbt
  MetricFlow spec" is a 100-line change — the resolver + router
  surface stays.

### 3. What the LLM is and isn't allowed to decide

| Decision | LLM does it | Code does it |
|---|---|---|
| Which metric is being asked about | ✅ (with semantic-name + description) | |
| Which dimension to group by | ✅ | |
| Which year to filter on | ✅ | |
| Whether to add an equality filter | ✅ | |
| Exact SQL aggregation expression | | ✅ (YAML literal) |
| Which tables to JOIN | | ✅ (resolver BFS) |
| JOIN order / direction | | ✅ (resolver BFS) |
| Whether to use `DISTINCT` | | ✅ (YAML literal) |
| `LIMIT` injection | | ✅ (resolver) |
| Static SQL safety (sqlglot) | | ✅ (existing validate_sql, runs even on our SQL) |

The flexible-vs-rigid line lands precisely where the LLM is good
(name + intent matching) vs where it's bad (exact SQL composition).

### 4. The router's "DEFAULT TO false" bias is intentional

The system prompt for `metric_router_node` instructs:

> **DEFAULT TO `answerable: false`. The fallback path is competent on
> its own; routing a question to the semantic layer that the semantic
> layer cannot actually answer produces a worse outcome than just
> letting the LLM write SQL.**

Why:

* A false-positive routes a question the semantic layer can't
  actually serve through the deterministic compiler, which either
  produces ResolverError (we re-route to LLM, costing two LLM
  calls + the user sees no badge) or — worse — silently produces
  wrong-but-confident SQL via a mismatched dimension.
* A false-negative just costs an extra LLM call per turn; the
  existing text-to-SQL pipeline competently handles the question.

The cost asymmetry shapes the default. We'd rather miss out on
covering 100% of theoretically-answerable questions than serve
even 5% of cases through a wrong-shape semantic compile.

### 5. The resolver's join planner walks the FULL graph

A subtle bug: "revenue by country" requires `{order_details,
customers}`, but Northwind has no direct `order_details ↔ customers`
relationship — the JOIN must traverse the bridge table `orders`.
The resolver:

1. Builds undirected adjacency over EVERY declared relationship
   (not just edges between required tables).
2. Picks the alphabetically-first required table as root.
3. BFS to record parent + on-clause for every reachable node.
4. For each required table != root, walks the parent chain to root,
   collecting unique edges (two required tables sharing a parent
   only emit one JOIN).
5. Emits edges sorted by BFS distance from root so each JOIN's
   "new" alias has its parent already in the FROM/JOINs above it.

Bridge tables get pulled in automatically; the YAML doesn't need
to declare a "transitive" relationship.

### 6. Fail-soft on every axis

Same posture as the coverage gate (ADR 0016) and critic (ADR 0021):

| Failure | Result |
|---|---|
| `SEMANTIC_LAYER_ENABLED=False` | router short-circuits, no LLM call, fallback |
| `data/semantic.yml` missing | router falls back; API still starts |
| Router LLM raises | fallback, no cost recorded |
| Router returns unparsable JSON | fallback, cost IS recorded (call happened) |
| Router emits `answerable: true` but spec validation fails | fallback (defense — LLM said yes but spec is bad) |
| Resolver compile raises `ResolverError` mid-stream | flip envelope to `fallback`, route to `generate_sql` |

Nothing in this list shows the user an error; the worst case is "the
LLM text-to-SQL path runs, same as before". The semantic layer is
purely additive.

### 7. State + AskResponse shape

`AgentState.semantic` carries:

```jsonc
{
  "path": "semantic_layer" | "fallback",
  "answerable": bool,
  "reason": "str",                       // one line, for debug
  "spec": { metric, dimensions, time_range, filters, limit } | null,
  "sql": "str" | null,                   // populated by resolver
  "compile_error": "str" | null          // populated only on a resolver failure
}
```

`AskResponse.semantic` is the same envelope. The FE renders a small
`SemanticPill` ("⚖ revenue · by country · 1997") on the semantic
path and nothing on the fallback path — most turns today are still
fallback so the chat surface looks unchanged.

### 8. 9th A/B experiment

`feature_flags.SEMANTIC_LAYER_ENABLED` (default True). New A/B preset
`WITHOUT_SEMANTIC_LAYER`, new driver `run_semantic_layer_ab`,
registered in the eval CLI as `semantic_layer`.

Hypothesis (testable on the next eval run):
* `success_rate` improves on `aggregation`, `count`, simple
  `single_table_filter`, and `join` categories.
* `success_rate` flat on `follow_up` (router declines), `investigate`
  (multi-hop is supervisor-owned), `schema_explore`, `chitchat`.
* `avg_total_tokens` rises by ~+200 per turn (router prompt is
  ~150 in + ~80 out tokens).
* `avg_attempts` should DROP slightly on covered categories — the
  resolver doesn't trip the self-heal retry loop.

## Alternatives explicitly rejected

### Full dbt MetricFlow / Cube semantic spec

Adopt one of the existing spec languages instead of inventing a tiny
schema. Rejected because:

* dbt MetricFlow's surface (semantic models, measures, metrics,
  saved queries, conformed dimensions, joinable entities) is ~30
  concepts. We need 5.
* Pulling in `dbt-core` to parse a MetricFlow spec adds 150+ MB of
  transitive deps for one tiny module.
* If a future user wants to bring their existing dbt YAML, write
  a separate `loader_dbt.py` that converts dbt's shape to our
  internal `SemanticModel`. Smaller surface, narrower blast radius.

### Router as a tool call instead of JSON

Use OpenAI/Anthropic-style function calling: declare each metric as a
tool, let the LLM "call" `compute_revenue(group_by, year)`. Rejected
because:

* DeepSeek's function-calling support is uneven; we'd be locked to
  more expensive providers.
* JSON output is the project's existing pattern (insight envelope,
  coverage envelope, critic envelope). One more JSON envelope keeps
  the prompt + parsing stack consistent.
* Forcing JSON via `response_format={"type": "json_object"}` already
  achieves the same "must be parseable" guarantee.

### Skip the router; ALWAYS try semantic first, fall through on compile failure

Why have a router at all? Just attempt to compile every question.
Rejected because:

* Most user questions hit unmodelled concepts (analytical exploration,
  schema questions, "why is X declining"). Forcing them through the
  compiler first means a wasted compile attempt + a fallback every
  time. Net cost: ~1 round-trip per turn for no value.
* The router's "answerable" verdict is also useful telemetry —
  tracks the coverage gap over time and tells us which metrics to
  add to the YAML next.

### Run BOTH paths and pick the best answer

Compile via semantic layer in parallel with text-to-SQL, compare
results, return the more confident one. Rejected because:

* 2× LLM cost per turn for marginal gain.
* "More confident" has no clean definition for SQL — same row counts
  doesn't mean same semantics.
* Adds graph complexity (parallel branches + result reconciliation).
  The critic (ADR 0021) already provides a post-hoc sanity check on
  whichever path ran.

### Hand-write the resolver's SQL in Python, no YAML

Each metric is a function returning SQL. Rejected because:

* Loses the "data team edits this" property — a metric change becomes
  a code review.
* No clean way to surface the metric menu to the router LLM — we'd
  end up generating a YAML from the functions anyway.
* Diffs in a YAML PR are readable to non-engineers; diffs in Python
  with embedded SQL aren't.

## Risks and known limitations

* **Small initial coverage (6 metrics).** The semantic layer covers
  customer/order/revenue questions cleanly; product details,
  shipping, employee performance are still fallback. Each new metric
  is one YAML stanza + tests; the next time we look at the eval
  reports we'll know which categories are bleeding accuracy through
  fallback and prioritise those.
* **Bridge tables can produce duplicate-fan-out JOINs in edge cases.**
  Joining `order_details → orders → customers` with a metric that
  sums over `order_details` is correct; joining the same path with
  a metric that counts orders should use `COUNT(DISTINCT o.order_id)`
  (which our YAML does). Authors of new metrics need to remember
  this — the resolver doesn't auto-add DISTINCT.
* **No metric inheritance / derived metrics.** "revenue per customer"
  isn't a metric; it's the user computing one. If we want it the
  YAML would need either an `avg_order_value`-style flattened
  expression or a future derived-metric feature.
* **No time-range vocabulary beyond `year`.** Quarter, month, relative
  windows ("last 30 days") all route through fallback. Adding them
  is one Pydantic union + one resolver branch.
* **The router can be wrong about "answerable".** False-positive
  produces a ResolverError which then falls through to text-to-SQL.
  False-negative just routes to fallback. The first case wastes one
  LLM call per occurrence; the second is invisible. Acceptable.

## Compatibility / migration

* `data/semantic.yml` is new — no migration on existing databases.
  Repo init / fresh checkouts pick it up automatically.
* `AskResponse.semantic` is additive; existing FE callers that don't
  know about it ignore the field.
* `AgentState.semantic` is a TypedDict optional field; existing nodes
  that don't read it are unaffected.
* Eval reports under `docs/eval/` keep their meaning; the 9th A/B
  (`semantic_layer`) is a separate driver that doesn't perturb the
  other 8.
* Feature flag default is `True`; setting `SEMANTIC_LAYER_ENABLED=False`
  in env reverts every turn to pre-Phase-3.1 behaviour without code
  changes.
