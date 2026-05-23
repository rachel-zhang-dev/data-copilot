# ADR 0007: Evaluation methodology and A/B experiment design

> Status: Accepted · Date: 2026-05 (Week 6) · Supersedes: none

## Context

Through Week 5 the agent gained three substantial features —
schema-aware retrieval (Week 3), self-healing retries (Week 4), and
multi-turn dialogue (Week 5). Each was justified intuitively in its
own ADR. None had been measured.

Week 6 closes that loop with an evaluation harness that:

1. Runs the agent over a fixed question set with deterministic
   per-case assertions.
2. Compares "feature on" vs "feature off" via three independent A/B
   experiments, one per major Week 3-5 feature.
3. Produces a committable markdown report so the empirical state of
   the agent at any commit hash is visible alongside the code.

## Decision

We use **comparative experiments** as the primary methodology. The
runner takes a list of `CaseSpec` and an `ExperimentConfig`, flips
the relevant feature flags via `feature_flags.override(...)`, and
returns a typed `ExperimentResult` with four metrics
(`success_rate`, `avg_attempts`, `avg_latency_ms`, `avg_total_tokens`)
plus per-category breakdown.

Three A/Bs ship in Week 6:

| Experiment | Baseline | Treatment | What it proves |
|---|---|---|---|
| **A1: schema_rag**       | full DDL dump (week-2 behaviour) | week-3 retriever active        | RAG helps JOIN cases without hurting simple ones; reduces token cost |
| **A2: self_healing**     | retry budget = 0 across the board | week-4 default budget           | retries rescue typo / wrong-name cases at marginal token cost |
| **A3: dialogue_context** | history block stripped from prompt | week-5 dialogue injection       | follow-ups need history; chitchat / single-shot don't |

Implementation lives in [`apps/api/copilot/eval/`](../../apps/api/copilot/eval).
Cases live in [`data/eval/cases.yaml`](../../data/eval/cases.yaml).
Reports land in `docs/eval/<timestamp>-<experiment>.md`.

## Why comparative, not standalone

A "we run 80 cases and 67% pass" number is interesting; a "schema RAG
adds +47 pp on JOIN cases at -61% token cost" number is decisive.
The Week 3-5 ADRs each made a hypothesis; the eval is where those
hypotheses get tested.

The portfolio framing matters too. "I built X" is a junior signal;
"I built X and used data to demonstrate it improves Y" is a senior
one. Comparative experiments make every prior week's work
falsifiable.

## Why deterministic graders, not LLM-judge

The grader inspects each run's `RunResult` with simple assertions —
substring match on the SQL, regex on the answer, row-count bounds.
No LLM is called.

Rationale:

* **Reproducibility.** A deterministic grader gives the same score on
  the same `(case, run)` pair forever. LLM-judge scores drift with
  provider model versions, prompt changes, and temperature.
* **Cost.** Running the eval costs maybe ¥1-2 in DeepSeek tokens.
  Adding LLM-judge would multiply that by 2-3x for marginal value
  given our metrics are mostly structural ("does the SQL JOIN
  `order_details`?").
* **Scope.** This is a portfolio project. Deterministic answers are
  enough to demonstrate the methodology. A future commit can layer
  LangSmith's `criteria` evaluator on top if a particular A/B
  benefits from quality scoring.

LLM-judge is intentionally cancelled in the Week 6 todo list, with
the file structure left in place (an empty `graders/llm_judge.py`
slot) so a future PR can add it without restructuring.

## Why not RAGAS

We considered using `ragas` (already a transitive dep). Rejected
because:

* RAGAS was designed for RAG-chatbot eval — its core metrics
  (faithfulness, answer relevancy, context precision/recall) target
  document retrieval, not SQL execution.
* Mapping our `dialogue` / `attempts` / `sql` shape to RAGAS's
  `(question, contexts, answer, ground_truth)` quadruple is a
  square-peg job that obscures what we're actually measuring.
* Half the metrics RAGAS provides are LLM-judge based and fail the
  reproducibility argument above.

We may revisit RAGAS in Week 12 (polish) if the eval set has grown
enough to warrant a third metric layer.

## Why feature flags via a context manager, not env vars

`copilot.agent.feature_flags.override(...)` is a `with`-block:

```python
with feature_flags.override(schema_rag_enabled=False, retry_budget=...):
    result = await run_eval(cases, cfg)
```

Three reasons over env vars:

1. **Atomic flip + restore.** All three flags swap together at block
   entry and restore at exit, including via exception. Cannot leak
   into the next experiment.
2. **No process restart.** A single Python process can run all three
   A/Bs sequentially. Env-var driven flags would require re-launching.
3. **Existing code unchanged.** The override mutates the already-existing
   `RETRY_BUDGET` dict in place, so `monkeypatch.setattr(nodes,
   "RETRY_BUDGET", ...)` style tests keep working.

The trade-off is global mutable state, but the eval runner
explicitly serialises cases (no `asyncio.gather`) so there is no
window for a parallel reader to see a half-flipped state.

## Why per-case state injection for follow-up

Follow-up cases need prior turns visible to `generate_sql`. The
naive way is to call `graph.ainvoke` once per setup turn. Cost is
linear in setup-history depth and adds nondeterminism (the LLM is
in the loop for setup turns we don't actually want to evaluate).

Instead, the runner constructs an initial state with the
`dialogue` field pre-populated from `case.setup_history` and runs a
single graph invocation. The agent sees the same context in
`generate_sql` either way; we save the per-setup-turn LLM cost.

Risk: if the agent ever starts depending on state fields beyond
`dialogue` (e.g. cumulative `attempts` records from prior turns),
this shortcut would diverge from real behaviour. We accept the risk
because (a) such cross-turn state coupling is exactly what
`reset_per_turn_node` was built to prevent in Week 5, (b) the eval
has no way to fabricate a believable prior `attempts` list anyway.

## Consequences

### Good

* Every commit can show "what was the agent's measured quality at
  this point in time" by inspecting `docs/eval/`.
* Future feature work has a regression detector: run the harness,
  read the diff, decide.
* Each Week 3-5 ADR's hypothesis becomes empirically supported (or
  refuted) by the corresponding A/B's per-category breakdown.

### Bad / accepted trade-offs

* 32 hand-written cases is small. A real eval set would have
  hundreds, ideally with held-out test/eval splits. We have neither;
  the methodology generalises but the absolute numbers should be
  read as "rough signal", not "5-decimal benchmark".
* Token estimates are heuristic (chars/4). Off-by-2x in some
  Chinese-heavy cases. Trends remain meaningful; absolute numbers
  do not.
* No statistical significance testing. With one run per case, a 5-
  case category yields five datapoints — too few for meaningful
  CIs. Plan §"future work" calls this out.

## Future work

* **Week 11 (deploy)**: wire the harness into CI so each PR generates
  a delta report against `main`. Failure of the regression check
  blocks merge.
* **Week 12 (polish)**: revisit RAGAS / LangSmith Datasets + Experiments
  push for a richer dashboards story; consider LLM-judge for the
  follow-up category where deterministic checks are weaker.
* **As needed**: grow `cases.yaml` to ≥100 cases. Each new feature
  ADR should include the cases it expects to move.
