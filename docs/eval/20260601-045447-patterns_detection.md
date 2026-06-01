# Eval A/B — patterns_detection
> Generated 2026-06-01T05:26:36+00:00 · 50 cases per side

- **baseline**: `patterns_detection_off` — Phase 1.2 detector disabled — no pattern bullets are merged into ``insight.bullets`` and ``patterns`` stays empty. ``has_pattern`` cases will fail their assertion; everything else should stay flat.
- **treatment**: `full_features` — All week-3/4/5 features enabled — the production default.

## Summary

| Metric | baseline | treatment | Δ |
|---|---|---|---|
| success_rate | 76.0% | 82.0% | **+6.0 pp** |
| avg_attempts | 0.70 | 0.72 | +0.02 |
| avg_latency_ms | 3554 | 3760 | +207 |
| avg_total_tokens | 787 | 790 | +4 |

## By category

| Category | n | baseline | treatment | Δ |
|---|---|---|---|---|
| aggregation | 4 | 100.0% | 100.0% | +0.0 pp |
| ambiguous | 3 | 33.3% | 33.3% | +0.0 pp |
| chitchat | 2 | 100.0% | 100.0% | +0.0 pp |
| count | 8 | 100.0% | 100.0% | +0.0 pp |
| destructive | 2 | 0.0% | 0.0% | +0.0 pp |
| expensive | 3 | 100.0% | 100.0% | +0.0 pp |
| follow_up | 3 | 66.7% | 66.7% | +0.0 pp |
| has_pattern | 3 | 0.0% | 100.0% | **+100.0 pp** |
| investigate | 4 | 0.0% | 0.0% | +0.0 pp |
| join | 5 | 100.0% | 100.0% | +0.0 pp |
| schema_explore | 3 | 100.0% | 100.0% | +0.0 pp |
| single_table_filter | 5 | 100.0% | 100.0% | +0.0 pp |
| unanswerable | 5 | 100.0% | 100.0% | +0.0 pp |

## Fixed by treatment

- **pattern-customers-by-country-outlier** (has_pattern)
  question: Count customers grouped by country.
  baseline sql: `SELECT country, COUNT(*) AS customer_count FROM customers GROUP BY country ORDER BY country LIMIT 100`
  treatment sql: `SELECT c.country, COUNT(*) AS customer_count FROM customers AS c GROUP BY c.country ORDER BY c.country LIMIT 100`

- **pattern-orders-by-employee-outlier** (has_pattern)
  question: How many orders did each employee handle in total?
  baseline sql: `SELECT e.employee_id, e.first_name, e.last_name, COUNT(o.order_id) AS total_orders FROM employees AS e LEFT JOIN orders AS o ON e.employee_id = o.employee_id GROUP BY e.employee_id, e.first_name, e.last_name ORDER BY total_orders DESC LIMIT 100`
  treatment sql: `SELECT e.employee_id, e.first_name, e.last_name, COUNT(o.order_id) AS total_orders FROM employees AS e LEFT JOIN orders AS o ON e.employee_id = o.employee_id GROUP BY e.employee_id, e.first_name, e.last_name ORDER BY total_orders DESC LIMIT 100`

- **pattern-monthly-orders-1997-trend** (has_pattern)
  question: How many orders were placed each month in 1997?
  baseline sql: `SELECT DATE_TRUNC('MONTH', order_date) AS month, COUNT(*) AS order_count FROM orders WHERE EXTRACT(YEAR FROM order_date) = 1997 GROUP BY DATE_TRUNC('MONTH', order_date) ORDER BY month LIMIT 100`
  treatment sql: `SELECT DATE_TRUNC('MONTH', order_date) AS month, COUNT(*) AS order_count FROM orders WHERE EXTRACT(YEAR FROM order_date) = 1997 GROUP BY DATE_TRUNC('MONTH', order_date) ORDER BY month LIMIT 100`

## Regressions (treatment broke what baseline got right)

_(none)_