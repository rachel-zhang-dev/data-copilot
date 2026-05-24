# ADR 0009: Result visualisation and structured insight

> Status: Accepted · Date: 2026-05 (Week 8) · Supersedes: none

## Context

Through Week 7 the agent answered every data question with the same
two-part response: the SQL it ran, and a single natural-language
sentence (`answer`). Even when the result was clearly chart-shaped — a
country breakdown, a top-N list, a monthly trend — the user got a wall
of rows + one sentence, no visual.

Week 8 closes that gap with **two complementary outputs** on every
successful data turn:

1. A chart specification the UI can render directly.
2. A structured "insight" envelope — headline + bullet observations
   + metric highlights — that replaces the single-sentence `answer`.

These are surfaced over the same HTTP / CLI / future-Slack channel,
so any consumer can read either or both without a side channel.

## Decision

### Visualisation: Vega-Lite, heuristic classifier first

`visualize_node` runs after `summarize_result_node` on every
successful data turn. It does three things:

1. **Classify** the result shape into one of five buckets:
   `kpi` · `bar` · `line` · `grouped_bar` · `table`.
2. **Emit** a Vega-Lite v5 spec for the three "real chart" kinds.
3. **Skip** spec generation for `kpi` / `table` / `empty` — the
   UI renders those directly from `rows`.

The classifier inspects column count, row count, and per-column
inferred type (`quantitative` · `temporal` · `nominal`). The mapping
is a small decision table:

| Shape                                                  | Chart kind |
|--------------------------------------------------------|------------|
| `n_rows == 0`                                          | `table` (no spec) |
| `n_rows == 1` and at least one quantitative column     | `kpi` |
| `n_rows > 50`                                          | `table` |
| Exactly one temporal column and ≥1 quantitative        | `line` |
| Exactly one nominal column and exactly one quantitative| `bar` |
| Exactly one nominal column and ≥2 quantitative         | `grouped_bar` |
| Anything else                                          | `table` |

State gains two new fields:

* `chart_kind: Literal["kpi", "bar", "line", "grouped_bar", "table"] | None`
* `chart_spec: dict | None` — Vega-Lite JSON when applicable.

### Insight: replace `answer` with a structured envelope

`summarize_result_node` already calls the LLM to write a single
sentence. Week 8 changes the prompt so the model returns JSON of the
shape:

```python
class Insight(BaseModel):
    headline: str          # the one-liner that used to be ``answer``
    bullets: list[str]     # 0-4 short observations
    metric_highlights: list[MetricHighlight]  # 0-N { label, value, format? }
```

The node parses the JSON, sets `state.answer = insight.headline`
(for backward compatibility with every existing caller), and writes
the full envelope to `state.insight`. On any parse failure we fall
back to the legacy behaviour: `answer` gets the raw LLM text and
`insight` stays `None`. **No user is ever blocked by a JSON parse
error** — the response degrades gracefully.

## Why Vega-Lite, not Chart.js or a custom mini-schema

Three options were on the table.

* **Vega-Lite v5** is an industry-standard declarative grammar; the
  spec doubles as both a wire format (JSON) and a renderable
  artifact (`react-vega` is a one-line component). LLM-of-the-day
  has seen many Vega-Lite samples in its training corpus, so even
  if we ever push it down into LLM fallback territory the success
  rate is high.

* **Chart.js JSON** is simpler, but the schema mixes data + visuals +
  options in one bag and doesn't compose into dashboards as cleanly.
  We would have to re-invent the layering / facetting bits that
  Vega-Lite ships with.

* **Custom mini-schema** (`{type, x, y, series}`) is what most quick
  prototypes do. It's the cheapest *now* and the most expensive
  *later*: every new chart type means another field on every
  consumer, and the LLM has zero prior on the format so we cannot
  hand it off in a later iteration.

Vega-Lite wins because it makes the *next* feature cheaper, not the
*current* one.

## Why heuristic-first, with LLM fallback deferred

The five-class decision table covers almost every Northwind question:
counts, lists, aggregations by category, simple time series, top-N.
Running an LLM on each `visualize` call would add ~1 second per turn
and ~$0.001 cost for a job a regex-class rule can do deterministically.

LLM fallback (for genuinely ambiguous shapes — multi-level nominals,
mixed temporal+nominal, heatmap candidates) is left as a follow-up;
the scaffold (state fields, node wiring, Vega-Lite emission) is in
place so adding the fallback later is purely additive.

This mirrors the Week 4 / Week 6 pattern: deterministic first, LLM as
a last resort only when the deterministic path can't decide.

## Why upgrade `summarize_result_node` rather than add `insight_node`

A separate `insight_node` was considered. Reasons against:

* Two LLM calls per data turn doubles cost and adds ~1 s latency for
  marginal benefit — the LLM is already looking at the rows once;
  asking it for a single JSON object instead of a single sentence
  is the same prompt depth.
* The `answer` field needs to stay populated for every existing
  caller (CLI, current Next.js stub, eval graders). Replacing
  `summarize_result_node` rather than running alongside it keeps
  the contract: "after this node runs, `answer` is set."
* Backward-compat fallback is easy to express in one node ("if JSON
  parse fails, just take the raw text as `answer`"). Splitting
  insight into its own node would require coordinating two fallback
  paths.

The downside is that turning insight off (e.g., for an A/B) means
flipping a flag in `summarize_result_node` rather than removing a
graph edge. That's a feature-flag problem (we already have the
infra) and not a graph topology problem.

## Failure handling

Every new path has a fail-soft default that lands the user back on
pre-Week-8 behaviour:

| Failure                         | Behaviour |
|---------------------------------|-----------|
| LLM returns non-JSON            | `answer` = raw text, `insight` = `None` |
| LLM JSON misses required fields | `answer` = raw text, `insight` = `None` |
| `visualize_node` raises         | `chart_spec` / `chart_kind` left `None`; SQL + rows + answer still returned |
| Result has zero rows            | `chart_kind = "table"`, no spec; `answer` says "no rows" |

The node-level try/except in `visualize_node` is intentional: a
visualisation bug must never block a user from seeing their data.

## Consequences

### Good

* Every analytical answer now arrives with a renderable artifact.
  The Next.js UI lands in Week 10 with one `<VegaLite>` line of
  code, no extra glue.
* The structured `insight` field unlocks future features (export
  to slide deck, alert-on-threshold, etc.) with no further prompt
  engineering.
* Heuristic classification is fully deterministic and unit-tested.
* No new infrastructure or dependencies — Vega-Lite is the consumer's
  problem; the agent just emits JSON.

### Bad / accepted trade-offs

* The decision table is calibrated for Northwind shapes. Wider
  schemas may want a sixth or seventh kind (heatmap, sankey,
  scatter). Add them as needed; the wire format does not have to
  change.
* The LLM JSON-mode prompt adds ~50 tokens vs the legacy
  one-sentence prompt. Not a meaningful latency / cost regression.
* `Insight.headline` will sometimes differ in tone from what the
  legacy NL `answer` produced; the eval set's `answer_must_match`
  assertions stay liberal enough that this is not a regression.

## Future work

* **LLM fallback for ambiguous shapes.** A node that, when the
  classifier returns `table`, asks the LLM "would something
  prettier than a table actually fit here?" with a Vega-Lite
  example schema. Strictly opt-in via feature flag because of cost.
* **Chart-kind assertions in the eval set.** Extend `Expect` with
  a `chart_kind_any_of: list[str]` field; add the assertion to the
  deterministic grader. Already discussed in ADR 0007's deferred
  list — picks up Week 8's new output naturally.
* **Heatmap / scatter / sankey.** Three more shape buckets that
  Northwind doesn't naturally surface. Land when the case set grows
  beyond TPC-H scale.
* **Insight diff for time series.** When two turns return the same
  shape, surface "vs previous turn" deltas inside `metric_highlights`.
  Useful but needs a stable identity for the comparison.
