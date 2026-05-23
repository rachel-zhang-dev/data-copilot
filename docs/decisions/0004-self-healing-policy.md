# ADR 0004: Self-healing retry policy

> Status: Accepted · Date: 2026-05 (Week 4) · Supersedes: none

## Context

Through Week 3, any failure in `validate_sql_node` or
`execute_sql_node` immediately routed the agent to `finalize_error`,
producing a polite refusal back to the user. This was correct but
left value on the table: most LLM-generated SQL failures are simple
mistakes (a singular table name, a column typo, a missing JOIN) that
the same model can fix on a second pass when shown the error message.

Week 4 closes the loop. We need to decide:

1. Which errors should be retried?
2. How many retries per error type?
3. What context goes into the retry prompt?

## Decision

We use a **per-class retry budget** keyed on the error category, with
a hard global ceiling as a safety net.

```python
RETRY_BUDGET: dict[ErrorClass, int] = {
    "execution_failed": 2,   # column / table typos: high LLM fix-rate
    "unsafe_sql":       1,   # semantic intent issue: one corrective shot
    "fatal":            0,   # network / programmer errors: do not retry
}

HARD_RETRY_CEILING = 5       # absolute upper bound across all classes
```

The retry prompt includes (a) the focused schema produced by the
Week 3 retriever, (b) the original question, (c) the most recent
failed SQL, (d) the error message, and (e) explicit "do not just
re-issue the same SQL" guidance. Earlier failures are NOT included —
keeping the prompt size predictable and avoiding the "model gets
confused by mounting failure history" anti-pattern.

The implementation lives in
[`apps/api/copilot/agent/nodes.py`](../../apps/api/copilot/agent/nodes.py)
(`classify_error`, `can_retry`, retry-aware `generate_sql_node`).
The retry prompt template is in
[`apps/api/copilot/agent/prompts.py`](../../apps/api/copilot/agent/prompts.py)
(`RETRY_SQL_SYSTEM`, `RETRY_SQL_USER_TEMPLATE`).

## Why per-class instead of one global N

The two error classes have very different fix profiles:

* **execution_failed** — almost always a syntactic or naming mistake
  (column doesn't exist, JOIN condition wrong). DeepSeek given the
  Postgres error message corrects these on the next try in our
  observation. Higher budget (2) means we tolerate two consecutive
  blunders before giving up; the data shows almost all cases recover
  by attempt #2.
* **unsafe_sql** — the safety layer rejected the SQL because it was
  not a read-only SELECT. This usually means the LLM misread the
  question (e.g. "remove duplicates" -> DELETE). One corrective
  shot ("rewrite as a SELECT") is enough; if the SQL is still unsafe
  after that, the user's intent is probably itself destructive and
  retrying is throwing tokens at a wall.

A flat budget would either be too generous on `unsafe_sql` (wasting
tokens defending against destructive intent) or too stingy on
`execution_failed` (giving up before the easy second-shot fix).

## Why not retry `fatal`

`fatal` covers errors we did not predict — provider 5xx, networking,
programmer mistakes inside our own code. None of these benefit from
re-prompting the LLM. Better to terminate with a useful error than
to obscure the bug behind retry latency.

## Why a hard ceiling on top of per-class budgets

Defence in depth. If someone (a future ADR, a misconfigured env var
override) raises `execution_failed` to 50, we still cap total LLM
calls at 5. LangGraph also has its own `recursion_limit` (default
25) but the safety net should not be at the framework boundary.

## Why not include full attempt history in the retry prompt

We considered three prompt designs:

* **Last failure only** (chosen). Compact, fast, matches the natural
  "fix this one thing" instruction.
* **All previous failures.** Adds repetition; once we've already
  retried twice the prompt grows linearly and starts to include
  failures the LLM has already addressed.
* **Diff-based.** Show "this is what you tried, this is the error,
  here is the schema" without showing the schema each time. Saves
  tokens but couples retry to the previous-attempt's schema, which
  may now be stale (Week 3 retriever can adjust).

For Week 4 the simple "last failure only" design is enough. Future
work (Week 6 evaluation) may revisit if data shows the model
benefits from seeing more history.

## Consequences

### Good

* Recovers from common LLM mistakes without user friction.
* Preserves the existing graceful-degradation path: when the budget
  runs out, the user sees the same polite refusal as before, just
  with "after N attempts" appended for honesty.
* Each retry is **observable**: `state.attempts` is a JSON-friendly
  list that LangSmith captures verbatim, and `AskResponse.attempts`
  exposes the count to API consumers.
* Easy to evaluate: Week 6's eval harness can A/B different retry
  policies by varying the budget map.

### Bad / accepted trade-offs

* Token cost can up to triple on a question that hits two
  consecutive `execution_failed` errors. We accept this because the
  alternative — failing the user — is worse.
* The retry prompt assumes the schema retrieved in Week 3 is still
  correct. If a schema-related error caused the failure, retrying
  with the same schema may not help. Mitigation: reset the
  retriever in Week 5 when we add multi-turn dialogue.
* Hard-coded budgets are not yet user-tunable. Adding env-var
  overrides was deliberately deferred (see plan §五.7).

## Future work

* Week 6: measure self-healing effectiveness on a fixed eval set;
  use the data to refine the budget map and decide whether to
  expose retry knobs in `.env`.
* Week 7: when human-in-the-loop arrives, the retry path could
  optionally pause for human confirmation between attempts on
  destructive-intent questions.
* Week 9: cache retry-prompt + response pairs to short-circuit
  repeated identical retries (reduces token spend at scale).
