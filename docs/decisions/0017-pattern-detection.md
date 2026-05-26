# ADR 0017: Statistical pattern detection (Phase 1.2)

> Status: Accepted · Date: 2026-05 (Phase 1.2) · Supersedes: none

## Context

Through Phase 1.1 the agent answers data questions, summarises the
rows, and refuses what the schema can't cover. The next gap is more
subtle: when the agent DOES return a successful result, it tells the
user the headline number ("USA leads with 13 customers") and a
couple of structural observations ("21 countries total") but it
doesn't notice the **statistical features** of the result — outliers,
trends, sudden jumps. A real analyst would never look at the
country-by-customer distribution and not flag that USA is sitting
~3σ above everyone else.

Two reproducible cases motivated this ADR:

1. **The "everyone got a count" trap.** Asking "Count customers
   grouped by country" returns 21 rows of `(country, count)`. Today's
   `summarize_result` writes "USA leads with 13" and stops there. A
   human would point out that USA is an outlier and that the top 3
   countries hold 38% of customers — both deterministic facts the
   LLM can verify from the rows but rarely surfaces on its own.

2. **The "trend you couldn't see" trap.** Asking "monthly orders in
   1997" returns 12 rows. The LLM occasionally points out the trend
   if it's blatant, but on noisier data it just narrates first / last
   values and misses the underlying ~+10% / month growth.

Phase 1.2 closes both with a small dedicated node that runs
**pure-statistics detectors** over the result set and prepends a
handful of pattern bullets to the existing insight envelope.

## Decision

Add a `detect_patterns_node` between `summarize_result` and
`visualize`. It runs two deterministic detectors (outliers, trends)
in numpy, ranks the findings by severity, and asks the LLM ONLY to
translate the structured findings into natural language. The
statistics themselves never go through a model.

### 1. Two detectors, both pure numpy

* **Outliers** — Tukey IQR fence for the gating decision (robust to
  skew), z-score for the user-facing severity ladder
  (`info`/`notable`/`high` at |z| ≥ 2 / 3). Minimum 4 rows.
* **Trends** — OLS fit against row index, R² used as the gate
  (`notable` ≥ 0.5, `high` ≥ 0.85), with a "relative slope" floor of
  5% of `mean(|y|)` to suppress trivial / constant-ish series.
  Minimum 5 rows.

The full implementation lives in
[apps/api/copilot/agent/patterns/detectors.py](../../apps/api/copilot/agent/patterns/detectors.py).
Every detector is a pure function `(rows, ...) -> list[Finding]`,
returns `[]` rather than raising on "not applicable" inputs.

### 2. LLM only renders, never decides

The detector produces structured `Finding` dicts with the actual
numbers (`value`, `z_score`, `slope`, `r_squared`, `delta_pct`, etc.)
already populated. `detect_patterns_node` then makes one LLM call
(`PATTERN_RENDER_SYSTEM` + JSON mode) whose only job is to write one
bullet sentence per finding, anchored to the numbers in the payload.

Why this split:

* **No hallucination risk on the facts.** The statistics are
  deterministic; the LLM can't invent a 3σ where none exists.
* **Reproducibility.** Re-runs produce identical findings even when
  the LLM phrases them slightly differently.
* **Language matching for free.** The same `_LANGUAGE_DIRECTIVE`
  the rest of the Phase 1.1 prompts use guarantees Chinese-question
  → Chinese-bullet rendering.

### 3. Merge into `insight.bullets`, not a new UI

Pattern bullets are **prepended** to the existing `insight.bullets`
list (capped at 6 total). The front-end's `InsightPanel` already
renders `bullets`, so Phase 1.2 ships with **zero front-end
changes** — the bullets appear in the same panel as the legacy
summary observations, just at the top.

Why prepend, not append: pattern findings are typically the most
informative thing about the result ("USA is an outlier at 13") and
deserve to be read first. The legacy bullets ("21 countries total")
are still visible below.

`AskResponse.patterns` is also emitted as a structured list so
future phases can add chart annotations / severity badges / chip
rows without re-running the statistics.

### 4. Fail-SOFT on every error path

Every failure mode degrades silently:

* Less than `MIN_ROWS_FOR_OUTLIER` (4) or `MIN_ROWS_FOR_TREND` (5)
  → detector returns `[]` → node returns `{}` (no LLM call).
* No numeric column → same.
* LLM call raises → fall back to a deterministic template per
  finding ("USA (50) is 3.0σ above the mean — notable outlier").
* LLM returns malformed JSON → same template fallback.
* `PATTERNS_DETECTION_ENABLED=False` → skip the entire node.

The fallback bullets are **always grounded in the payload numbers**,
so even when the LLM is unavailable the user still sees a correct
statistical observation. The fallback bullets are English-only
(we don't know the user's language from inside the detector layer),
which is the only graceful downgrade users might notice.

### 5. Sixth A/B experiment: `patterns_detection`

[apps/api/copilot/eval/experiments/patterns_detection.py](../../apps/api/copilot/eval/experiments/patterns_detection.py)
pairs `BASELINE_FULL` against `WITHOUT_PATTERNS_DETECTION`. The
new `has_pattern` category in [data/eval/cases.yaml](../../data/eval/cases.yaml)
(3 cases) goes from 0/3 with the detector off to 3/3 with it on,
while every other category stays flat. The grader gained two new
assertion fields:

* `expected_pattern_kinds: [outlier, trend]` — every named kind
  must appear in `run.patterns`.
* `expected_pattern_min_count: 1` — at least N findings emitted.

## Alternatives considered

### Why not scipy?

`scipy.stats.linregress` and `scipy.stats.zscore` would be more
expressive than numpy primitives. We rejected scipy because:

* It adds ~90 MB to the production image — the entire backend
  image only just dipped under 250 MB in Week 11.
* Mann-Kendall, OLS slope, R², and z-score are all expressible in
  ~20 lines of numpy with no loss of accuracy at our sample sizes
  (n < 1000).
* p-values are not necessary for the user-facing decision — the
  severity ladder (`notable` / `high`) is based on R² thresholds
  and z-score thresholds that are themselves derived from
  hand-tuned heuristics, not a hypothesis test.

We re-evaluate if a future detector (e.g. seasonality / period
detection) genuinely needs scipy. For Phase 1.2's two detectors,
numpy is enough.

### Why not run the detector before `summarize_result`?

It was tempting to compute pattern bullets before the LLM writes
its first summary, so the summary itself could cite them. We didn't
because:

* It would require restructuring `summarize_result`'s prompt to
  consume an arbitrary number of patterns; the prompt is already
  well-calibrated for the no-pattern case.
* `summarize_result` and the pattern detector address different
  things: the former narrates what the rows show, the latter
  flags statistical features the LLM might miss. Keeping them
  parallel-but-separate makes both easier to debug.
* The "merge bullets" step at the end already gives the user a
  unified panel; readability isn't worse for the separation.

### Why merge into `insight.bullets` instead of a new component?

We considered a dedicated `PatternsPanel` UI component (badge per
finding, severity colour, click-to-explain). We didn't ship it in
Phase 1.2 because:

* The front-end's `InsightPanel` already renders `bullets` — zero
  FE code change for Phase 1.2 = faster ship, smaller risk surface.
* If Phase 1.3 / 1.4 grows the pattern surface (chart annotations,
  drill-into-this-outlier interactions), promoting to a dedicated
  component then is easy because `AskResponse.patterns` is already
  structured.

## Risks and known limitations

* **Tiny result sets dilute the signal.** Northwind's groupings
  typically produce 5–20 rows, and a single dominant outlier can
  inflate the sample std to the point where its own z falls below
  3σ. We mitigate by using IQR for gating (robust) and surfacing
  the most extreme finding regardless of severity, but on very
  small samples the detector may miss visually obvious outliers.
* **Trend detector is order-blind.** It fits against row index, not
  against an inferred temporal column. If the SQL writer doesn't
  `ORDER BY date`, a monthly series will look random. The
  `ORDER BY` is the user's / SQL writer's responsibility; we don't
  try to detect "this looks like a time column" — that's a Phase
  1.4 problem.
* **Detector inflates per-turn cost by one LLM call.** ~150 tokens
  in, ~100 out (~$0.00003 on DeepSeek). Worth it for the bullet
  quality on data turns that actually have patterns; the node
  short-circuits before any LLM call on KPI / chitchat / no-pattern
  turns, so chitchat-heavy conversations see no cost change.
* **Adversarial wording can't trick the detector**, but unhelpful
  visualisation choices can (e.g. asking for products ordered by
  category id will produce nominal "trend" findings that mean
  nothing). The `TREND_MIN_RELATIVE_SLOPE` floor is the main
  defence; further heuristic tightening lives in future ADRs.

## Compatibility / migration

* `pyproject.toml` makes `numpy>=1.26` an explicit main dependency.
  It was already in the runtime image transitively via LangChain;
  pinning means a future LangChain upgrade that drops it won't
  silently break this node.
* `AskResponse` gains one new nullable field (`patterns`). Legacy
  clients ignore it.
* `feature_flags.PATTERNS_DETECTION_ENABLED=False` reverts to the
  pre-Phase-1.2 behaviour (no detector, no extra bullets).
