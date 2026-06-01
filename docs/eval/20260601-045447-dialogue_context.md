# Eval A/B — dialogue_context
> Generated 2026-06-01T05:08:03+00:00 · 3 cases per side

- **baseline**: `dialogue_context_off` — generate_sql does not see previous turns; follow-ups are blind.
- **treatment**: `full_features` — All week-3/4/5 features enabled — the production default.

## Summary

| Metric | baseline | treatment | Δ |
|---|---|---|---|
| success_rate | 0.0% | 66.7% | **+66.7 pp** |
| avg_attempts | 0.67 | 0.67 | +0.00 |
| avg_latency_ms | 3031 | 3985 | +954 |
| avg_total_tokens | 692 | 696 | +4 |

## By category

| Category | n | baseline | treatment | Δ |
|---|---|---|---|---|
| follow_up | 3 | 0.0% | 66.7% | **+66.7 pp** |

## Fixed by treatment

- **followup-france-after-germany** (follow_up)
  question: And France?
  baseline sql: `SELECT * FROM employees WHERE country = 'France' LIMIT 100`
  treatment sql: `SELECT COUNT(*) FROM customers WHERE country = 'France' LIMIT 100`

- **followup-cheaper-than-that** (follow_up)
  question: Which products are cheaper than that?
  baseline sql: `SELECT product_id, product_name, unit_price FROM products WHERE unit_price < (SELECT unit_price FROM products WHERE product_name = 'That') LIMIT 100`
  treatment sql: `SELECT product_name, unit_price FROM products WHERE unit_price < 263.50 ORDER BY unit_price DESC LIMIT 100`

## Regressions (treatment broke what baseline got right)

_(none)_