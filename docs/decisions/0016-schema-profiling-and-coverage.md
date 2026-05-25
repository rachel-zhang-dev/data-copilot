# ADR 0016: Schema profiling + coverage gate (Phase 1.1)

> Status: Accepted · Date: 2026-05 (Phase 1.1) · Supersedes: none

## Context

Through Week 12.5 the agent answers "data" questions by writing SQL
against the retrieved tables and chatters back canned text on
"chitchat" questions. There's no third option — if the user asks for
a concept the schema doesn't have (e.g. "conversion rate" on a
sales-only DB), the LLM hallucinates plausible-looking SQL against
columns that don't exist, the safety layer or DB rejects it, the
self-healing loop chews through its retry budget, and the user
finally sees a generic "I tried 3 times and still got it wrong"
message. Cost, latency, and trust all take the hit. There's also no
discovery affordance — a new user who lands on the chat panel has no
way to know what they CAN ask without typing things and watching
them fail.

Two reproducible cases motivated this ADR:

1. **The "conversion rate" trap.** Asking "Can you analyse why
   conversion is dropping?" produced a confident but meaningless
   SQL joining `customers` to `customer_demographics` (a near-empty
   Northwind table) with the LLM rationalising it post-hoc as
   "limited data on customer segmentation."
2. **The "what's in here?" gap.** First-time users on Northwind
   spend their first 3-4 questions probing what tables exist by
   guessing entity names.

Phase 1.1 closes both with a single new dependency: a cached,
column-level profile of every business table.

## Decision

Adopt **both** an explorer mode AND a guardrail mode, sharing one
persistent profile table and one set of prompts.

### 1. Persistent `schema_profiles` table

Built at `./scripts/dev.sh index` time, in the SAME transaction as
`schema_embeddings`, so the two derived tables can never drift apart.
One row per `(table_name, column_name)` capturing what `pg_stats` /
`pg_class` / `pg_description` already know — null ratio, distinct
count, top-k sample values, FK targets, comments. A sentinel row
with `column_name = '*'` carries the table-level summary
(row count, table-level COMMENT).

Source code:

* DDL — [data/seed/03-schema-profiles.sql](../../data/seed/03-schema-profiles.sql)
* Builder — [apps/api/copilot/profiler.py](../../apps/api/copilot/profiler.py)
* Indexer hook — [apps/api/copilot/indexer.py](../../apps/api/copilot/indexer.py)

The profile is fast to read (`PRIMARY KEY (table_name, column_name)`
+ a `table_name` covering index → < 1 ms per request) and small
(~150 rows per 15-table Northwind; rough projection ~5 MB for a
1000-table production schema). Indexer cost is dominated by
`ANALYZE` (1-2 s on Northwind, seconds-to-minutes on production
schemas — the same `ANALYZE` cost we'd pay for accurate planner
estimates anyway).

### 2. Three-way intent classifier (`data` / `chitchat` / `schema_explore`)

`classify_intent_node` now emits one of three labels. The new
`schema_explore` label fires on questions like "what data do you
have?", "show me the tables", "你有哪些数据" — questions that are
meta about the database rather than queries against it.

### 3. `explore_schema_node` (new branch)

When `intent="schema_explore"`, the graph routes to
`explore_schema_node`, which feeds the FULL profile through the LLM
in JSON mode and gets back a topic-grouped tour plus 3-5 sample
questions. The response is rendered by the front-end's `SchemaTour`
component as a clickable card.

Source code:

* Node — [apps/api/copilot/agent/explore.py](../../apps/api/copilot/agent/explore.py)
* Component — [apps/web/components/SchemaTour.tsx](../../apps/web/components/SchemaTour.tsx)

### 4. `coverage_check_node` (new gate on the data branch)

Inserted between `retrieve_schema` and `generate_sql`. Feeds the
retrieved-table slice of the profile + the question to the LLM in
JSON mode; gets back `{verdict, reason, missing_concepts,
suggested_questions}`.

* `verdict="ok"` → continue to `generate_sql` (the common case).
* `verdict="refuse"` → divert to `explain_uncovered_node`, which
  produces a friendly structured refusal. The graph NEVER writes SQL
  on this branch.

Source code:

* Node — [apps/api/copilot/agent/coverage.py](../../apps/api/copilot/agent/coverage.py)
* Component — [apps/web/components/CoverageRefusal.tsx](../../apps/web/components/CoverageRefusal.tsx)

### 5. Fail-OPEN everywhere

Every failure mode on the gate path defaults to "let the SQL writer
try":

* `schema_profiles` empty (indexer never ran) → skip the gate, log a
  one-shot warning.
* LLM returned non-JSON → treat as `verdict="ok"`.
* LLM call raised → treat as `verdict="ok"`.
* `COVERAGE_CHECK_ENABLED=False` env flag → skip the gate entirely.

The reasoning: a buggy gate that wrongly refuses a valid question is
a worse user experience than the pre-Phase-1.1 hallucination
behaviour. The gate is a quality-of-life feature, not a safety
boundary; the SQL-safety policy (ADR 0002) remains the only thing
that protects against bad writes.

### 6. New eval bucket + A/B

Two new categories in [data/eval/cases.yaml](../../data/eval/cases.yaml):

* `unanswerable` (5 cases) — "conversion rate", "signup funnel",
  "A/B test", "30-day retention", "ad spend". Each asserts
  `expected_verdict: refuse`.
* `schema_explore` (3 cases) — "what data do you have?", "what
  tables are available?", "what kinds of questions can I ask?".
  Each asserts `expected_intent: schema_explore` and
  `expected_verdict: explore`.

The fifth A/B experiment (`coverage_check`) pairs `BASELINE_FULL`
against `WITHOUT_COVERAGE_CHECK`; the report's `by_category` table
shows the two new buckets going from near-0% with the gate off to
near-100% with it on, while the original 32 cases stay flat.

Hand off via `./scripts/dev.sh eval --experiment coverage_check`.

## Alternatives considered

### Persistence: in-memory vs Redis vs live query

| Option | Pros | Cons |
|---|---|---|
| **Persistent table (chosen)** | Same lifecycle as `schema_embeddings`; multi-replica consistent; 1 ms reads | Schema changes need re-index (same constraint already exists) |
| In-memory `TTLCache` | No DDL | Multi-replica drift on Fly.io; first request on each replica pays the build cost |
| Redis | Cross-replica share | Adds a Redis dep to a feature that needs no cross-machine writes; doesn't survive cluster restart |
| Live `pg_stats` every request | Always fresh | +50-100 ms per data turn; pg_stats can still be stale relative to data |

We picked the persistent table because it parallels the existing
embedding-index pattern exactly, keeps the hot path < 1 ms, and
gives identical behaviour across replicas.

### Trigger: guardrail vs explorer vs both vs neither

The plan offered four options:

* **Guardrail only** — solves the hallucination case but no
  discovery affordance.
* **Explorer only** — solves discovery but the "conversion rate"
  trap remains.
* **Both (chosen)** — addresses both motivating cases with one
  prompt-and-profile investment.
* **Neither** — defer to multi-step research mode (Phase 1.4).

Doing both is incremental on top of one another (the explorer just
adds an intent and a node, the guardrail just adds one node and one
prompt) and shares the profile. Splitting them across phases would
have wasted prompt-engineering work.

### Why not LLM-judge the verdict twice?

We considered a confirmation step: when the gate says `refuse`,
re-query the LLM with the original question + full DDL (not just
the profile) to confirm. We didn't ship this because:

* Costs double on every refused case.
* In the eval set, false-positive (wrong refusal) under prompt-only
  gating is < 5%. The marginal cost isn't justified.
* If we later see false-positives climbing, the right move is to
  tighten the gate prompt, not bolt on a second judge.

### Why not refuse cheaply with a regex over schema?

A regex-based "does the schema mention conversion / funnel /
campaign?" check would be 0 cost. We didn't ship it because it
generalises poorly — Northwind has no "campaign" column but DOES
have `freight` and `discount`, which could justify some marketing-
adjacent questions. The LLM-as-gate reads context and concept,
which is what we want; the cost (~$0.00002 / call) is acceptable.

## Risks and known limitations

* **PII through profiles.** `pg_stats.most_common_vals` exposes
  high-frequency values; for tables with sensitive data
  (`customers.email`, `users.ssn`) this would leak. Northwind has no
  such columns, but real deployments will. Mitigation, future:
  honour `ALTER TABLE … SET (security_barrier=on)` or an explicit
  per-table allowlist. Tracked for Phase 1.5 (multi-tenancy).
* **Stale profiles.** If the schema changes between indexer runs,
  the gate may refuse questions that the new schema can actually
  answer. The pain is the same as for `schema_embeddings`; the
  remedy (`./scripts/dev.sh index --force`) is the same too.
* **Three-way classifier confusion.** Edge questions like "are there
  any customers?" can sound like exploration; the prompt's example
  set deliberately handles common cases, but adversarial wording
  may flip routes. The gate's fail-open behaviour caps the damage
  (worst case: a refused turn that should have produced a count).

## Compatibility / migration

* DDL ships as `data/seed/03-schema-profiles.sql`, applied
  automatically on a fresh Postgres init. Existing local DBs need
  one manual `psql` import (or `docker compose down -v` + restart).
* `./scripts/dev.sh index --force` populates both `schema_embeddings`
  and `schema_profiles`. Old indexer runs will get a one-shot
  warning on next agent start until re-run.
* `AskResponse` gains two new fields (`intent`, `coverage`). Both
  are nullable; legacy clients can ignore them. `openapi-typescript`
  picks them up automatically on the next `pnpm gen:types`.
* `feature_flags.COVERAGE_CHECK_ENABLED=False` reverts the gate
  behaviour for callers who want the pre-Phase-1.1 path.
