# ADR 0021: SQL verification loop / critic (Phase 2.3)

> Status: Accepted · Date: 2026-06 (Phase 2.3) · Supersedes: none

## Context

The agent's first six defensive layers — coverage gate, schema-aware
retrieval (RAG), static safety (sqlglot), risk gate (planner cost +
HITL), self-healing retry, and Postgres itself — together do a good
job at catching **syntactic** SQL failures. None of them catch
**semantic** failures: SQL that runs cleanly and returns plausible
numbers but answers a *different question* than the user asked.

Examples that all six layers happily pass through:

* User asks "top 5 customers by revenue", SQL ranks by order count.
* User asks "orders in 1997", SQL filters 1998.
* User asks "total revenue", SQL returns average.
* User asks "customers and their orders", SQL inner-joins and silently
  drops customers with zero orders.

This is the highest-stakes failure mode of a text-to-SQL agent:
the user gets a confident-looking chart with the wrong number and
trusts it. ADR 0016 ("coverage gate") prevents the agent from
hallucinating SQL for impossible questions; this ADR adds a
post-execution gate that catches SQL that "did something, but the
wrong something".

## Decision

### 1. Add a critic node between `execute_sql` and `summarize_result`

```
... → execute_sql → critique_sql ─┬─ ok / suspicious → summarize_result → ...
                                  └─ wrong (with budget) → record_critic_rejection → generate_sql (loop)
```

The critic is a second LLM call that sees the **question + schema +
SQL + first 5 rows of the result** and returns a structured verdict:

```jsonc
{
  "verdict": "ok" | "suspicious" | "wrong",
  "reason":  "one-sentence explanation, citing SQL or rows",
  "concerns": ["specific issue 1", "specific issue 2"]  // 0-3
}
```

See [`apps/api/copilot/agent/critic.py`](../../apps/api/copilot/agent/critic.py)
for the node, the `CriticVerdict` pydantic model, the JSON parser,
and the `route_after_critic` router.

### 2. Three verdicts, three behaviours

| Verdict | Agent does | User sees |
|---|---|---|
| `ok` | Pass through to `summarize_result` silently | Normal answer |
| `suspicious` | Pass through, attach verdict to `AskResponse.critic` | Answer + amber ⚠ "low confidence" badge with reason + concerns |
| `wrong` | Append a synthetic `Attempt`, route back to `generate_sql` with the critic's feedback in a new prompt (`CRITIC_FIX_SYSTEM`) | If retry succeeds: clean answer. If retry still flagged: answer + red ⚠️ "reviewer flagged this as wrong" badge |

The user is **never** blocked from seeing the answer. Even on
`wrong` with no retry budget, we render the answer with a
prominent badge. Reason: hiding the answer removes the user's
ability to spot-check against their own knowledge — the worst
mode for a high-stakes BI tool. The badge says "we have doubts",
the user decides.

### 3. Budget: one critic-retry per turn

`RETRY_BUDGET["critic_rejected"] = 1`. Combined with the existing
`HARD_RETRY_CEILING = 5` from `nodes.py`, the worst-case path is:

```
initial + 2 execution-retries + 1 critic-retry = 4 generate_sql calls per turn
```

Why one and not more: in pilot runs the second critic retry almost
always converges on the same wrong pattern as the first (the LLM
has a sticky bias against the actual semantic fix). The marginal
LLM dollars and latency aren't worth a < 5% additional catch rate;
the UI badge handles the unrecoverable cases more honestly.

### 4. Distinct retry prompt (`CRITIC_FIX_SYSTEM`)

The existing `RETRY_SQL_SYSTEM` is framed around "the SQL just
failed — fix the error". A critic rejection is the opposite —
the SQL ran cleanly but is semantically off. Reusing the
execution-failure prompt produces awkward rewrites ("Postgres
didn't actually reject this, so… what am I fixing?").

`CRITIC_FIX_SYSTEM` + `CRITIC_FIX_USER_TEMPLATE` (in
`prompts.py`) frame it correctly: "a reviewer found a semantic
problem; address every concern". The user message carries the
reviewer's `verdict`, `reason`, and bullet list of `concerns`
verbatim.

`generate_sql_node` branches on `last["error_class"]` to pick
the right prompt; existing self-healing paths (`execution_failed`,
`unsafe_sql`) are unaffected.

### 5. Fail-soft, like every other LLM-dependent node

Critic LLM raises → verdict ok, no cost recorded.
Critic returns unparsable JSON → verdict ok, cost recorded.
Critic disabled by feature flag → verdict ok, no LLM call.
No SQL on state (defensive) → verdict ok, no LLM call.

A broken critic must never block a legitimate answer. Same
posture as the coverage gate (ADR 0016) and pattern detector
(ADR 0017).

### 6. A/B feature flag for eval measurement

`feature_flags.CRITIC_ENABLED` defaults to `True`. The eval
harness gets a parallel field on `ExperimentConfig` and a new
`WITHOUT_CRITIC` preset; `eval/experiments/critic.py` is the
eighth A/B driver, registered in `__main__.py` as `critic`.

The hypothesis under test:

* `success_rate` on the new `semantic_trap` category should jump
  from near-0% (baseline: critic off, SQL passes silently) to
  high-percent under treatment.
* `success_rate` on the other 11 categories should stay flat
  (modulo one extra LLM call's worth of cost / latency).
* `avg_total_tokens` should rise by ~600 per turn (the critic
  prompt is ~500 input + ~100 output tokens at typical sizes).
* `avg_latency_ms` should rise by ~800-1500 ms per turn (one
  more DeepSeek round-trip).

## Frontend surface

* `lib/types.ts` adds the `Critic` interface and a `critic` field
  to `AskResponse`.
* `components/CriticBadge.tsx` renders nothing on `ok` and a
  warn-coloured / error-coloured card with reason + bullet
  concerns on `suspicious` / `wrong`. Embedded in `ChatTurn`
  above the `InsightPanel` so users see the warning before the
  numbers it warns about.
* No FE surface on dashboard cards yet — the snapshot model in
  ADR 0020 doesn't carry `critic`. Adding it is a one-column
  schema migration + UI conditional; deferred to keep this ADR
  focused. Until then a card extracted from a suspicious turn
  loses its badge; the chat-side answer still shows it.

## Alternatives explicitly rejected

### Self-consistency (run N SQLs, vote)

Run `generate_sql` 3-5 times with different temperatures, execute
all, return the majority answer. Rejected because:

* Cost is 3-5× per turn (every successful answer pays for N runs,
  not just the suspicious ones). The critic is 1× extra LLM call,
  fixed.
* Self-consistency doesn't catch *systematic* errors — if the
  model has a blind spot, all N samples share it.
* Adds significant graph complexity (parallel branches + result
  hashing); the critic is one new node.

The eval harness can still measure self-consistency later as a
parallel ninth A/B if we want to compare; the two are orthogonal.

### Two different models (DeepSeek writes SQL, GPT-4o critiques)

Cleaner separation of biases — the critic doesn't share the
writer's blind spots. Rejected for now because:

* Adds a second LLM provider (cost + auth + retries + cache).
* The DeepSeek-critiquing-DeepSeek bias is real but smaller than
  expected in pilot runs (catches ~60-70% of plausible-but-wrong
  SQL on Northwind, vs ~80-85% with a different-model critic).
* Easy to upgrade later: `get_llm` already takes a `model` kwarg;
  the critic could specify a different one without changing any
  call sites.

Tracked as a future iteration.

### Critic blocks the answer on `wrong`

Refuse to show any answer when the critic says wrong, force the
user to rephrase. Rejected because:

* Removes the user's ability to spot-check. If the critic is the
  one being wrong (false positive), the user has no recourse.
* The user's natural-language question is often ambiguous;
  forcing them to rephrase doesn't necessarily improve the
  signal.
* Showing the answer + the critic's objection lets the user use
  their own domain knowledge as the final arbiter — the most
  honest UX.

### Run critic on empty results

We considered short-circuiting "0 rows = ok" because most of the
critic's signal comes from row content. Rejected because:

* Empty results CAN indicate a wrong filter (e.g. user asked
  "orders in 1997", SQL filtered 1798 — returns 0 rows
  legitimately or wrongly?). The critic's prompt explicitly
  tells it not to escalate on row count alone but to consider
  the SQL.
* Skipping critic on 0 rows would save ~10% of LLM calls but
  miss the JOIN-direction-wrong-AND-empty cases.

## Risks and known limitations

* **Cost.** Every successful data turn now does one extra LLM
  call (~$0.0005 at DeepSeek pricing for typical Northwind
  prompts; up to ~$0.002 for wider schemas). The eval harness's
  cost panel makes this visible; the A/B quantifies the trade-off.
* **Latency.** ~+800-1500 ms per turn. Streaming hides most of
  it — the phase event for `critique_sql` flashes briefly between
  `execute_sql` and `summarize_result`.
* **False positives.** A nitpicky critic can flag valid SQL as
  suspicious. We tuned the system prompt with "DEFAULT TO ok"
  and "do not nitpick formatting" but no prompt is perfect. The
  eval set's `correct` and `kpi` categories tracks regression
  here — if the critic flags > 5% of those, the prompt needs
  retuning.
* **No dashboard-side surface yet.** A suspicious turn pinned to
  a dashboard loses its badge. Documented in §"Frontend surface".
* **Snapshot not extended.** `dashboard_items` doesn't have a
  `critic` column. Adding it is the obvious follow-up Phase 2.3.1.

## Compatibility / migration

* No schema migration on Postgres — the critic is an in-memory
  state field.
* `AskResponse.critic` is additive — every existing FE caller
  keeps working (the field is nullable). Old clients that don't
  know about `critic` ignore it.
* No change to LangGraph state persistence — `critic` is a
  turn-local field cleared by `reset_per_turn_node` like other
  per-turn data.
* Eval harness picks up the new A/B automatically on the next
  run; existing reports under `docs/eval/` keep their meaning.
