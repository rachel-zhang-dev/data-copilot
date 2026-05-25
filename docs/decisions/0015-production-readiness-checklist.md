# ADR 0015: Production readiness checklist

> Status: Accepted (as a backlog snapshot) · Date: 2026-05 (Week 12.6) · Supersedes: none

## Context

After 13 weeks of feature work the agent ships every capability we
set out to build: schema-aware RAG, self-healing SQL, HITL, multi-
turn dialogue, structured insight, visualisation, cost reporting,
multi-agent supervisor + analyst, streaming front-end, containerised
deploy. ``./scripts/deploy.sh all`` and the project is on a public
Fly.io URL — **for a demo**.

That URL is **not production-ready** in the "real users with money
or PII on the other side" sense. Eleven distinct gaps stand between
the current state and that bar. They are recorded here so:

* future commits link back to this ADR when one of them is closed
  (the same pattern ADRs 0007 / 0010 use for follow-up work),
* interview reviewers see a structured "I know what production
  actually means" statement alongside the demo,
* a future maintainer doesn't have to re-derive the list from the
  audit comments scattered through ADRs 0006-0014.

We **deliberately stop here for the portfolio milestone**. Closing
P0+P1 takes another ~2 weeks of focused work; multi-tenancy (ADR 0006)
takes ~6. Neither is required to demonstrate the design competence
this repository is meant to demonstrate.

## The eleven gaps

### P0 — refuse-to-launch-on-real-users blockers

#### 1. No authentication on any HTTP endpoint
`/ask`, `/ask/stream`, `/admin/stats`, `/metrics`, and the front-end's
Route Handlers are all publicly reachable. The agent is willing to
spend the operator's DeepSeek / SiliconFlow credits for anyone who
finds the URL. `/admin/stats` additionally leaks the live cache
hit-rate and per-model price-table state.

**Close it by**: FastAPI `Depends(...)` accepting an `X-API-Key`
header for the user endpoints, plus a separate admin token (or IP
allowlist) for the admin/metrics routes. Tracked at the schema
level in [ADR 0006 §"authentication"](./0006-security-and-multi-tenancy.md);
the day-1 implementation is API-key, not full SSO.

**Effort**: API-key version ~0.5 day; full SSO (Clerk / Auth0 / WorkOS) ~3-5 days.

#### 2. No rate limiting
A single client can fan out one thousand `/ask` calls in parallel.
The `conversation_lock` pool (Week 5, capped at 8 connections) will
saturate, LangGraph's `PostgresSaver` writes will queue, and the
DeepSeek quota burns through in minutes.

**Close it by**: `slowapi` middleware against Redis (the cache backend
ADR 0010 already provisions). Suggested defaults:
60 req/min/IP, 10 req/min/conversation_id.

**Effort**: ~0.5 day.

#### 3. No request body / row-count caps
`AskRequest.question` accepts arbitrary length. `execute_sql_node`
caps SQL via the safety layer's `LIMIT` injection, but the result
rows are then *also* embedded in `AskResponse.chart_spec.data.values`
(see ADR 0009 §"data duplication") so a legitimate 1000-row answer
ships a multi-MB JSON payload.

**Close it by**: `Field(..., max_length=2000)` on `question`; hard
row-count ceiling in `execute_sql_node` with a "truncated" flag; move
Vega-Lite `data.values` out of the response into a named external
data source the client wires in.

**Effort**: ~0.5 day.

#### 4. HITL pending threads never clean up
A user who asks an expensive question, sees the confirmation prompt,
and closes the tab leaves a paused checkpoint sitting in
``checkpoints`` forever. Over a few months that table grows without
bound and the dialogue context becomes stale before the user returns
to resume.

**Close it by**: A scheduled Fly Machine that runs `DELETE FROM
checkpoints WHERE state->'__interrupt__' IS NOT NULL AND created_at
< now() - interval '24 hours'` (the LangGraph schema exposes the
shape; double-check current key path before relying on this). Add a
metric `paused_checkpoint_age_seconds_p99` so the dashboard catches
the failure mode early.

**Effort**: ~1 day.

#### 5. The eval has never run against a real LLM
Every "schema_rag adds +47pp" or "analyst costs ~$0.0005/turn"
number in ADRs 0007 and 0014 was produced from unit-test stubs and
`chars/4` token estimates. The real DeepSeek failure modes (rate
limits, JSON-mode disobedience, prompt-length truncation) have not
been exercised end-to-end.

**Close it by**: `./scripts/dev.sh eval` against real keys + real
Postgres + real OpenAI-compatible endpoints; write the resulting
markdown reports into `docs/eval/` and amend the ADRs with the
empirical numbers.

**Effort**: ~0.5 day (mostly waiting; needs DeepSeek + SiliconFlow
budget).

### P1 — should land within a week of the first user

#### 6. No CI
All "ruff/mypy/pytest green" claims are produced by the operator
running them locally. A future contributor (or a future Cursor
session) could regress any of them silently.

**Close it by**: `.github/workflows/ci.yml` running the backend
sweep (ruff + mypy + pytest "not integration"), the frontend sweep
(typecheck + test + build), and `docker build` smoke tests on every
PR. Add a CI badge to the README.

**Effort**: ~0.25 day. Landed alongside this ADR.

#### 7. No staging environment
`data-copilot-api.fly.dev` is the only environment. Prompt changes,
graph-topology changes, and config edits all hit live users with no
soak window.

**Close it by**: parallel `data-copilot-api-staging.fly.dev` and
`-web-staging` apps; `./scripts/deploy.sh staging` subcommand;
distinct LangSmith projects so traces stay clean.

**Effort**: ~0.5 day.

#### 8. `chart_spec.data.values` duplicates `rows`
Today the row data is embedded inside the Vega-Lite spec AND in
`AskResponse.rows`. Payload doubles on every successful data turn.
Northwind never trips it; a real warehouse query returning 100 rows
of 20 columns ships ~200 KB instead of ~100 KB.

**Close it by**: switch the spec to a named external data source
(`data: {name: "rows_handle"}`) and have the front-end inject the
row data at render time.

**Effort**: ~0.5 day (backend `visualize.py` + front-end
`ChartRenderer.tsx`).

#### 9. No monitoring alerts
Week 11 added `/metrics` (Prometheus exposition) and `/admin/stats`,
but no Grafana / Alertmanager consumes them. An outage at 02:00
shows up in `fly logs` only the next morning.

**Close it by**: Grafana Cloud free tier scrapes `/metrics`; three
alert rules to start (5xx > 5% over 5min, latency p99 > 30s, cache
hit_rate < 30%); Slack webhook for delivery.

**Effort**: ~1-2 days (mostly Grafana wiring; alert rules are 10
lines each).

#### 10. No schema-migration framework
`data/seed/*.sql` runs once at container init. There is no Alembic
revision graph, no `up`/`down` migrations, no way to evolve the
production database without a re-import.

**Close it by**: Alembic init, wrap the existing seed files as the
baseline migration, document the workflow in the README "schema
changes" section.

**Effort**: ~1 day.

#### 11. No backup / restore story
Postgres has a Fly volume; LangGraph checkpoints, schema_embeddings,
business data all live there. There is no automated dump, no
restore test, no off-site copy.

**Close it by**: Fly Postgres `fly postgres backup` schedule, write
a runbook for restore, exercise it once. Embedding cache (in-memory
or Redis) doesn't need backup; checkpoints arguably don't either
(a fresh Postgres just loses dialogue history) but business data
does.

**Effort**: ~1 day including the actual restore drill.

## Severity table

| #  | Gap                                        | Severity | Effort | Linked ADR        |
|----|--------------------------------------------|----------|--------|-------------------|
| 1  | No authentication                          | **P0**   | 0.5–5d | 0006              |
| 2  | No rate limiting                           | **P0**   | 0.5d   | —                 |
| 3  | No request body / row-count caps           | **P0**   | 0.5d   | 0009 (data dup)   |
| 4  | HITL pending cleanup                       | **P0**   | 1d     | 0008 future work  |
| 5  | Eval never run against real LLM            | **P0**   | 0.5d   | 0007 deferred     |
| 6  | No CI                                      | P1       | 0.25d  | (this commit)     |
| 7  | No staging environment                     | P1       | 0.5d   | —                 |
| 8  | chart_spec data duplication                | P1       | 0.5d   | 0009 known        |
| 9  | No monitoring alerts (Grafana wiring)      | P1       | 1–2d   | 0012 future work  |
| 10 | No schema-migration framework              | P1       | 1d     | —                 |
| 11 | No backup / restore story                  | P1       | 1d     | —                 |

**Closing P0**: ~3-7 days focused work.
**Closing P0 + P1**: ~7-13 days.
**Plus ADR 0006 multi-tenancy** (separate scope, ~6 weeks): full enterprise.

## Out of scope for this checklist (deliberately)

Items below are real production concerns but belong to **other ADRs**
or **other product surfaces**; listing them here would dilute the
"what would block launch on real users tomorrow" framing.

* **Multi-tenancy / row-level security / GDPR / SOC2**: ADR 0006
  covers this whole layer; ~6 weeks of work, separate roadmap track.
* **Cancel mid-stream**: ADR 0011 future work. Quality-of-life, not
  launch-blocking.
* **Token estimate renaming**: cosmetic; ADR 0007 / 0010 deferred lists.
* **Lighthouse / a11y**: real concern for B2C front-end, ADR 0011
  future work, not a blocker for a single-tenant B2B demo.
* **Audit log independent of LangSmith**: every HITL decision goes
  through a checkpoint write anyway, so the data exists; just needs a
  read API. Add when an operator actually asks for it.
* **Streaming Analyst over SSE**: ADR 0014 future work. The
  non-streaming path is fine for the demo.

## Consequences

### Good

* A future maintainer (or a future me) has a single page that
  answers "what's the gap between this and prod?"
* Interview signal: a structured "I know what I haven't built and
  why I haven't built it yet" is stronger than a longer feature list.
* When the project does need to ship to real users, the eleven
  items become commit titles ("fix(prod): close P0-1 — API-key
  auth"). No analysis to redo.

### Bad / accepted trade-offs

* The list ages: every Postgres version bump, every DeepSeek price
  change, every LangGraph breaking change shifts the picture. We
  accept that the *severity ordering* is more durable than the
  effort estimates.
* Calling this an "ADR" stretches the format — it's a backlog
  snapshot, not a decision. Documenting it as ADR 0015 keeps it
  cross-linkable from the other ADRs, which matters more than
  taxonomy purity.

## Future work

* Close items as commits land; cross-reference back to this ADR
  in the commit body (`closes ADR 0015 §3`).
* Re-audit quarterly (or before any "first real customer" event).
* When ADR 0006 (multi-tenancy) lands, fold items 1 + 5 from this
  list into ADR 0006's implementation scope rather than tracking
  them twice.
