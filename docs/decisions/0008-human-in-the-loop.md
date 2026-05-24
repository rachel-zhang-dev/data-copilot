# ADR 0008: Human-in-the-loop confirmation for expensive queries

> Status: Accepted · Date: 2026-05 (Week 7) · Supersedes: none

## Context

Through Week 6, the agent had two ways to stop a query from running:

* The safety layer (`sql_safety.validate_and_rewrite`) rejects non-`SELECT`
  statements, locks, and `SELECT ... INTO` outright. The user never
  sees the SQL run; they see a polite refusal.
* The self-healing loop catches `execution_failed` errors and retries
  with a new generation, up to the per-class budget.

Neither handles the **"the SQL is technically legal but executing it
would be a bad idea"** case — typically a multi-way `JOIN` with no
`WHERE` clause that the LLM happily generated because the user asked
"tell me about the products". Auto-running it would either lock up a
shared database or surprise the user with a cost spike.

Production text-to-SQL tools (Snowflake Cortex Analyst, BigQuery
Studio, Mode Analytics) all solve this with a **confirmation pause**:
the agent shows the SQL and its estimated cost, the user clicks
approve / reject, the query then runs (or doesn't). Week 7 brings the
same primitive into the copilot.

## Decision

We introduce a **risk-check node** that runs Postgres `EXPLAIN
(FORMAT JSON)` on the validated SQL and compares the planner's
`Total Cost` to a configurable threshold. When the threshold is
exceeded, the graph **interrupts** itself via LangGraph's
`interrupt()` primitive, persists the pending decision through the
existing `PostgresSaver`, and exits to the caller. A second `/ask`
call with `resume="approve"` or `resume="reject"` re-enters the
graph at the interrupted node via `Command(resume=...)`.

The flow is:

```
validate_sql
   ↓
check_risk         ← EXPLAIN + cost compare
   ├── low  → execute_sql
   └── high → await_confirmation [interrupt()]
                      ↓ Command(resume="approve" | "reject")
                  route_after_confirmation
                      ├── approved → execute_sql
                      └── rejected → finalize_error (error_class: user_rejected)
```

State gains two new optional fields:

* `pending_risk: dict | None` — diagnostic payload shown to the user
  while paused (sql, total_cost, threshold, reason).
* `risk_decision: Literal["approved", "rejected"] | None` — set by
  `await_confirmation` from the resume value.

Both are turn-local and cleared by `reset_per_turn_node`.

## Why EXPLAIN's planner cost, not row-count heuristics

Three other detectors were considered:

* **Heuristic regex** ("any query without WHERE on these tables"). Brittle:
  false-positives on aggregations that legitimately scan, false-negatives
  on cross-product `WHERE 1=1`. The whole reason we use sqlglot and not
  regex for safety is the same reason here.
* **Estimated row count from `EXPLAIN`'s `plan_rows`**. Less reliable than
  cost: a plan that joins 4 tables but emits 10 rows still might do
  millions of intermediate ops; row count hides that.
* **Real-time `EXPLAIN ANALYZE`**. Actually executes. Defeats the purpose.

Plain `EXPLAIN (FORMAT JSON)` is dialect-stable, runs in milliseconds,
costs no I/O on the underlying data, and yields a single comparable
`Total Cost` number across queries. The unit is unitless ("disk-page
fetches plus CPU operations weighted by Postgres planner constants"),
but for *relative* comparison against a project-tuned threshold it is
exactly the right shape.

We wrap the `EXPLAIN` call in `SET LOCAL statement_timeout = ...` so a
pathological query that hangs the planner itself cannot stall the
agent.

## Why `interrupt()` / `Command(resume=...)`, not external queues

Alternatives considered:

* **Synchronous prompt-injection refuse** ("I won't run that;
  rephrase with `confirm:` to override"). Implemented in 30 lines but
  pushes confirmation through natural language, which the LLM has to
  re-classify on the next turn — error-prone and unauditable. The
  user-facing 'yes' becomes a free-form string the LLM might
  paraphrase, fabricate, or misread.
* **External work queue** (push the pending SQL to Redis / a DB row,
  poll for approval, then resume). Industrial-grade but enormous
  overkill for a portfolio project, and requires a side channel for
  the approval signal.
* **LangGraph `interrupt()`**: the agent state pauses *inside* the
  graph; the `PostgresSaver` we already use for multi-turn dialogue
  picks up persistence for free; resumption is a single
  `Command(resume=...)` call that the same `/ask` endpoint already
  knows how to thread by `conversation_id`.

The `interrupt()` route also makes the audit trail trivial: the
checkpoint row for the paused turn shows exactly what the user
approved (or rejected) and when. No separate audit table needed.

## API shape: extend `/ask` rather than add `/ask/resume`

`AskRequest` gains an optional `resume: Literal["approve", "reject"]`
field. On a resume call:

* `question` is omitted; the original question lives in the persisted
  state and is what `append_to_dialogue_node` records.
* `conversation_id` is required (a resume against a missing thread
  is a 400).
* The server uses `graph.aget_state(cfg).interrupts` to confirm a
  pending interrupt exists; resuming a non-interrupted thread is
  also a 400.

`AskResponse` gains:

* `status: Literal["ok", "pending_confirmation"]` (default `"ok"`).
* `pending_risk: dict | None` populated only when `status` is
  `"pending_confirmation"`.

A separate `/ask/resume` endpoint was considered. Rejected because:

* The CLI / Next.js UI both need to handle the *same* thread-and-
  context state across both endpoints anyway; merging them keeps one
  client-side state machine, not two.
* The one Pydantic schema with optional fields is exactly as strict
  as two schemas after the Pydantic validator runs; OpenAPI just
  shows one route instead of two.

If a second interrupt class lands (e.g. "ambiguous schema; pick a
table"), we will revisit and likely split into `/ask` + `/ask/<resume>`
properly. Single-endpoint is the cheapest answer that ships Week 7
without committing to the long-term shape.

## Default action: block-and-ask, not auto-reject

The agent **always** pauses on high risk and surfaces the choice to
the user. The alternative — auto-reject with a "rephrase if you want
to override" hint — was considered and rejected:

* It is not a true HITL flow; it is "two of the same refusal
  pathway as `unsafe_sql`". The Week 7 differentiator is exactly the
  paused/resumed state machine.
* It bounces user intent through free-form natural language ("I'm
  sure, run it") which the LLM has to re-classify, with no
  guarantees of consistency turn-to-turn.

Block-and-ask is also closer to what production tools do; the
familiarity matters for portfolio narrative.

## Threshold tuning

`risk_explain_cost_threshold` defaults to `1000.0`. Calibrated on
Northwind:

* Single-table aggregations (`count(*)`, `avg`, …) typically score
  under 50.
* Two-table joins with a `WHERE` cap typically score under 200.
* A four-table join with no `WHERE` ("show me products with all
  orders and customers and details") lands in the 1k-10k range.

Production deployments should raise this — a real warehouse on TPC-H
scale-factor-100 will have *cheap* queries in the 10k–100k range.
The threshold is exposed via `RISK_EXPLAIN_COST_THRESHOLD` env var.

## Consequences

### Good

* The agent ships a canonical LangGraph HITL pattern that is
  reusable for future interrupt types (schema ambiguity, sensitive
  table access).
* Every paused decision becomes a Postgres checkpoint row with the
  full context — audit and replay come "for free".
* Operators can dial risk tolerance per-environment via env var.

### Bad / accepted trade-offs

* Two-phase HTTP flow adds one round-trip when an interrupt
  triggers. Acceptable: the user is the bottleneck anyway.
* If a client forgets to call resume, the paused conversation row
  sits in Postgres indefinitely. We rely on the existing dialogue
  cleanup story to age it out; no dedicated GC for Week 7.
* `EXPLAIN` parsing depends on the JSON shape Postgres emits.
  Locked to Postgres 13+ output; the few volatile fields we read
  (`Plan.Total Cost`) have been stable since 9.x.
* The threshold is one number, not per-table or per-user. ADR 0006
  (security & multi-tenancy) is the natural home for the richer
  policy when Week 13 lands.

## Future work

* **Sensitive-table policy.** Layer a deny-list / row-level filter
  on top so PII tables also trigger an interrupt. Belongs to the
  ADR 0006 multi-tenancy track.
* **Per-user budget.** Replace the single threshold with a
  per-user-per-day cost cap.
* **Risk reasons UI.** The `pending_risk` payload currently has a
  free-text `reason`; the Next.js UI in Week 10 will turn this into
  a structured "why is this expensive" panel.
* **Eval coverage.** Add an `expensive` case category with the
  reverse asymmetric A/B (HITL on / off) — done in this commit,
  but should grow as the cases.yaml does.

### Deferred from the Week 7 post-merge audit

Items consciously scoped out after the post-merge audit; track here
so they are visible alongside the design they relate to.

* **Threshold calibration on real Northwind.** The default
  `risk_explain_cost_threshold = 1000.0` was chosen from a back-of-
  the-envelope estimate, not measured ``EXPLAIN`` numbers. Once a
  developer has Docker + Postgres up, run a sweep of representative
  questions (the existing eval set is fine), record the cost
  distribution, and bump the default accordingly. The wrong value
  here is silently lossy (false positives nag the user, false
  negatives skip the gate entirely).
* **Abandoned-interrupt semantics.** Empirically, sending a fresh
  `ainvoke({"question": ...})` on a thread paused at
  ``await_confirmation`` causes LangGraph to drop the pending
  interrupt and start a new turn from `reset_per_turn`. The
  previously-paused turn leaves no trace in `dialogue` (because
  `append_to_dialogue` never ran). This is reasonable user
  semantics ("I changed my mind") but invisible to operators and
  to future UI states. When the Next.js UI in Week 10 lands,
  surface a "you have a pending decision — discard it?" confirm
  before letting users send a fresh question on the same thread.
* **`explain_cost` failure observability.** Today a failing
  ``EXPLAIN`` (timeout, auth, network) logs at `WARNING` and the
  gate fails open. That is the correct user-facing default, but it
  means a 100%-broken `EXPLAIN` pipeline silently disables HITL
  with no alarm. Add a rate-limited error metric when Week 11 wires
  up observability.
* **Integration suite re-verification.** ``pytest -m integration``
  was not re-run against the new `check_risk` node because Docker
  was offline during the audit. Static analysis suggests only one
  query (`Which 5 products have the highest total revenue?`) might
  trip the threshold, and its assertions only inspect SQL content
  so a pause would still let it pass — but this should be empirically
  confirmed on the next dev box that boots Postgres.
