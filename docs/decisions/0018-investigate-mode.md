# ADR 0018: Investigate mode — intent-aware drill-down budget (Phase 1.3)

> Status: Accepted · Date: 2026-06 (Phase 1.3) · Supersedes: none

## Context

Through Phase 1.2 the agent answers single questions well: classifier
routes to the right node, coverage gate refuses what the schema
can't cover, patterns detector flags outliers and trends. But the
ceiling is still **one** SQL query per turn (or two, if the analyst
asks for a drill-down). Real research questions — *"why did
Beverages sales drop in Q3?"*, *"deep dive into our top customer"*,
*"investigate the Germany-France gap"* — don't fit that shape. A
human analyst would chain several queries:

1. Look at the headline metric.
2. Slice it by the most obvious dimension.
3. Notice an anomaly in the slice.
4. Drill in on that anomaly.

The week-12.5 supervisor already supports analyst-driven drill-downs
(ADR 0014) but caps the chain at **2 hops total** — initial answer
+ one drill-down. That worked for "make the dataset feel
exploratory" but blocks the multi-step pattern above.

## Decision

Lift the hop ceiling from a hard `MAX_HOP_COUNT = 2` constant to an
**intent-aware budget** stored in
`copilot.agents.supervisor.HOP_BUDGETS`:

```python
HOP_BUDGETS = {
    "data": 2,          # legacy default; covers 90% of single questions
    "investigate": 6,   # multi-step research budget (Phase 1.3)
}
_DEFAULT_HOP_BUDGET = 2  # for unknown / missing intent
```

A new `investigate` intent (Phase 1.3 / fourth label after `data`,
`chitchat`, `schema_explore`) reserved for research questions. The
classifier prompt grew a section + examples — "why is X declining" /
"investigate Y" / "deep dive into Z" map to `investigate`; plain
"how many" / "list" / "top N" stay on `data`. See
[apps/api/copilot/agent/prompts.py](../../apps/api/copilot/agent/prompts.py).

Code paths:

```
classify_intent ───► investigate ────► retrieve_schema  (same as data)
                                         │
                                         ▼
                                       coverage_check
                                         │ ok
                                         ▼
                                       generate_sql ─► validate_sql ─►
                                       check_risk ─► execute_sql ─►
                                       summarize_result ─► detect_patterns ─►
                                       visualize
                                         │
                                         ▼
                                       analyst (sees intent + hop_budget=6)
                                         │ drill_down emitted up to 5 times
                                         ▼
                                       sql_specialist (loop, hop_count++)
```

The data branch is **unchanged** — same prompts, same retriever,
same self-healing, same visualisation. The only difference is the
supervisor reads `state.sql_result.intent` to choose the hop budget
and the analyst's prompt makes use of that budget when emitting
drill-downs.

### Why "intent as the budget key" not "a separate `mode` flag"

We considered three alternative triggers:

1. **Explicit UI toggle**: user clicks "Deep Research" → API gets
   `mode=investigate`. Pro: zero classifier uncertainty. Con: users
   have to know what to click; doesn't degrade naturally if they
   forget; the FE / API contract grows.
2. **SQL-level escalation**: supervisor auto-escalates if the first
   answer looks ambiguous (e.g. row count near 0, big variance). Pro:
   automatic. Con: behavior becomes opaque; same question becomes
   2-hop or 6-hop depending on data.
3. **Intent classifier verdict (chosen)**: pre-existing
   `classify_intent_node` learns a fourth label. Pro: consistent
   with Phase 1.1 (`schema_explore`) — one mechanism for all
   non-default routing; eval can A/B the classifier and the budget
   separately. Con: classifier accuracy gates everything; an
   over-eager `investigate` verdict triples cost on a question that
   wanted 1 hop.

We picked #3 because (a) it slots into existing infrastructure, (b)
the eval harness can already grade `expected_intent`, and (c) the
prompt boundaries are tightly defined ("why" + "investigate" +
"deep dive" + "what's driving") so false-positive rate stays low on
the eval set we tested.

### Why 6 hops, not 3 or 10

`6 = 1 initial + up to 5 drill-downs`. Reasoning:

* `3` is too tight — a real investigation often needs *one* slice
  + *one* anomaly drill + *one* root-cause query. That's 4 hops; 3
  cuts off the root-cause step.
* `10` lets a malformed analyst loop chew through ~$0.005 per turn
  (each hop adds 1 LLM call for the analyst + 1-2 for the
  specialist). We don't yet have eval data on whether deeper chains
  actually help.
* `6` is the smallest budget that lets every motivating
  example finish, and at ~$0.003 / question is in line with the
  per-question budget elsewhere in the project.

The budget is a constant rather than a `Settings` field — Phase 1.3
treats it as topology, not a per-deploy knob. If a future deployment
needs deeper investigations the constant moves; until then,
fewer-degrees-of-freedom keeps reasoning about the agent simpler.

### Analyst prompt changes

The analyst now sees three new fields in its user prompt:

* `intent` — `"data"` / `"investigate"` / etc.
* `hop_count` of `hop_budget` — what the budget is and what's left.
* `drill_history` — the chain of questions already asked this turn,
  numbered. The analyst is told never to repeat one.

For `intent="investigate"`, the eligibility line is *"You SHOULD
emit a drill_down if the question isn't fully answered yet"* (vs
*"You MAY emit a single drill_down"* on `data`). Wording matters —
GPT / DeepSeek-class models follow soft prescriptive cues more
reliably than reasoning about budget arithmetic.

### Belt-and-suspenders cap

Three independent places refuse drill-downs past budget:

1. `analyst_node` strips `drill_down` from its own response when
   `hop_count >= hop_budget` (defence against a misbehaving LLM).
2. `route_after_analyst` returns `END` when the budget is hit.
3. `analyst.drill_down` cap inside the `AnalystResponse` schema is
   `Optional[DrillDownRequest]` — non-emitting analysts can't
   override.

Even if all three fail, the global LangGraph recursion limit catches
the rest. The user-facing failure mode is "answer carries the last
finished hop's result", never "infinite loop".

## Alternatives explicitly rejected

* **`research_plan_node` that emits an N-step plan upfront**.
  Considered for Phase 1.3; rejected because:
  - the existing greedy analyst loop already produces sensible
    chains in eval-bench,
  - a global plan blows up if the data contradicts an early step,
  - it adds one more LLM call per turn whose value is unclear until
    we see real failure cases for the greedy approach.

  Promoted to Phase 1.3.1 as a follow-up if the eval shows greedy
  chains are systematically bad.

* **Per-deploy `MAX_HOP_INVESTIGATE` env var**. We can add it later;
  not adding it now keeps the surface tight.

* **Different category in `data/eval/cases.yaml`**. Initially the
  plan was to keep investigate cases in the `aggregation` bucket
  with `expected_drill_count_min: 2`. We split into a dedicated
  `investigate` category so the by-category aggregation table in
  the markdown report shows the budget's impact cleanly.

## Risks and known limitations

* **Classifier false positives**. If `classify_intent` over-routes a
  simple data question to `investigate`, we burn 3-6× the cost on a
  one-shot question. Mitigation: prompt examples are tight; eval
  asserts `expected_intent` per case; future work can swap to a
  cheaper rule-based classifier for known phrases.
* **Greedy drill-down can drift off topic**. The analyst sees the
  immediate previous answer + the question history, but not a
  "global goal" reminder. If hops 3-4 drift, the user gets a
  bizarre final answer. The shipped `_LANGUAGE_DIRECTIVE` partly
  helps (consistent voice) but won't fix conceptual drift. Phase
  1.3.1 may add a "stay-on-topic" prompt line.
* **Observed terminator bias** (Jun 2026). With Phase 1.3 shipped,
  DeepSeek-class analyst LLMs tend to *under-use* the budget on
  multi-part research questions like *"who are X's customers, what
  do they buy, and how do they compare to peers?"* — they emit one
  drill-down, see a satisfying answer to the first sub-question,
  and stop. The architecture allows 6 hops; LLM behaviour tops out
  at 2-3. Mitigation candidates for Phase 1.3.1: a stronger
  "investigate-mode drill_down is the primary tool" line, or an
  explicit "you have N hops remaining and N+1 sub-questions in the
  user's prompt" counter the prompt can reason against.
* **Cost ceiling**. On the upper bound, a 6-hop investigate turn
  with each hop hitting the full SQL pipeline can run ~$0.005 and
  60-90 seconds. We log every hop's cost; the UI's `CostPanel`
  shows the cumulative figure so users see what they're paying for.
* **Eval gap**: today the eval runner uses the SQL Specialist
  graph directly (not the supervisor wrapper). That means
  `state.drill_downs` is never populated in the run result and the
  per-case `expected_drill_count_min` assertions can't fire. The
  classifier-side assertion (`expected_intent: investigate`) still
  works. Migrating the eval runner to use the supervisor graph is
  tracked as Phase 1.3.1 — a 30-line change that has cost / latency
  implications for the other six A/Bs and so deserves its own
  commit.

## Compatibility / migration

* `Intent` literal gains `"investigate"`. The TS mirror in
  `apps/web/lib/types.ts` is updated to match; legacy clients that
  switch on intent will hit a never branch for the new label, which
  is fine (Phase 1.3 doesn't change `AskResponse` rendering for the
  data path).
* `MAX_HOP_COUNT` constant (Week 12.5) stays exported as an alias
  for `_DEFAULT_HOP_BUDGET = 2` so any external test that imported
  it keeps working.
* `feature_flags.PATTERNS_DETECTION_ENABLED` semantics unchanged.
  Phase 1.3 doesn't introduce a feature flag — the budget map is a
  module-level constant patched by the eval runner during A/B runs.
* `ExperimentConfig.investigate_mode_enabled` is the seventh A/B
  toggle. When False, the runner downgrades
  `HOP_BUDGETS["investigate"]` to 2 for the duration of the run.
