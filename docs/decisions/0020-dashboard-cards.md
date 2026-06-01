# ADR 0020: Dashboard cards — snapshot model + storage (Phase 2.1, backend only)

> Status: Accepted (backend) / Pending (FE) · Date: 2026-06 (Phase 2.1) · Supersedes: none

## Context

Phase 1.4 shipped *saved conversations*: a user can pin an entire
chat and return to it later. Phase 2.1 takes the next step toward
BI tooling — letting users **extract a single turn** from any chat
into a stand-alone card, arrange those cards on a named grid, and
revisit the grid later as a dashboard.

The motivating use-case: an analyst asks five different questions
in a single investigation; three of the answers are "keep these in
front of the leadership team". Today there's no way to do that
without screenshots. After Phase 2.1 they pick the three turns,
add them to a "Q3 Sales Brief" dashboard, drag-arrange, and that
dashboard URL becomes the artifact.

This commit ships **backend only** (DDL + service + 7 endpoints +
ADR). The FE grid + drag-drop renderer comes in Phase 2.1.1 — split
because the UI work needs its own design pass (react-grid-layout
choice, drag UX, refresh semantics).

## Decision

### 1. Two tables, FK cascade, owned by us

```
dashboards
   ├── id (UUIDv4 string, PK)
   ├── title, description
   └── created_at / updated_at

dashboard_items                  (one row per card)
   ├── id (UUIDv4 string, PK)
   ├── dashboard_id (FK → dashboards.id, ON DELETE CASCADE)
   ├── source_thread_id            ← provenance only, NOT a runtime FK
   ├── source_turn_index           ← provenance only
   ├── title
   ├── sql, answer, chart_kind, chart_spec, rows, row_count, insight
   │       ← SNAPSHOT of one assistant turn, frozen at extract time
   └── position_x, position_y, width, height
           ← 12-column grid coords (react-grid-layout conventions)
```

See [data/seed/05-dashboards.sql](../../data/seed/05-dashboards.sql).

Both tables are owned by us — there is no LangGraph entanglement
on the dashboard read path. Phase 1.4 had to bridge the bookmark
to LangGraph state to derive a title; Phase 2.1 doesn't, because
the snapshot is fully self-contained (see §3).

### 2. Snapshot model — cards are static at extract time

When the user clicks "Add to dashboard" on a turn:

* The FE has the full **live** `AskResponse` in memory for the
  current turn (`sql`, `answer`, `chart_kind`, `chart_spec`,
  `rows`, `row_count`, `insight`). It POSTs that whole payload
  to `POST /dashboards/{id}/items`.
* The backend stores it verbatim into `dashboard_items` and returns
  the row.
* Dashboard rendering reads ONLY from these snapshot columns. The
  SQL is **not** re-run.

Why snapshot (not "stored query that re-runs at render time"):

| | Snapshot (chosen) | Dynamic re-query |
|---|---|---|
| Cost per dashboard load | 0 LLM, 0 SQL | N SQL queries (one per card) |
| Data freshness | Stale | Live |
| Render latency | Fast — single SELECT | Bounded by slowest re-query |
| Fragility | None — schema changes are invisible | Cards break on column rename |

We picked snapshot for the MVP because:

* The point of pinning a card is "remember this finding" — the
  finding's *value* is the headline + chart + the underlying
  rows, not "what is true right now".
* Re-running queries on every dashboard load multiplies cost
  proportionally with the dashboard size; a 12-card dashboard
  becomes ~$0.06 / load. Snapshots are free.
* A future "Refresh card" button is a non-breaking superset — the
  `sql` column is already stored, so Phase 2.1.2 (or later) can
  add a per-card refresh endpoint that overwrites the snapshot.

### 3. Why the FE posts the snapshot (and not "the endpoint reads it from `dialogue`")

A natural-sounding alternative: `POST /dashboards/{id}/items` takes
`{thread_id, turn_index}` and the backend reads LangGraph state to
populate the snapshot.

We rejected this because **`dialogue` doesn't carry the chart**.
Each entry in `state.dialogue` is `{role, content, sql, row_count}`
— the four things the SQL Specialist's `append_to_dialogue_node`
keeps. The `chart_spec` / `insight` / full `rows` payloads live
exclusively on the **live `AskResponse`** for the current turn;
they're transient state that never makes it into the persisted
dialogue.

This is intentional (it keeps PostgresSaver row size small) but it
means the only complete copy of a turn's render data lives in the
browser at extract time. So the FE-posts-the-snapshot path matches
that reality.

A side effect: extracting a card from a **saved-and-reloaded** chat
yields a partial snapshot (no chart, no insight). The FE handles
this by graying out the "Add to dashboard" button on replayed turns
in the Phase 2.1.1 work; backend-side this is fine because the
snapshot columns are all nullable.

### 4. Patch surface stops at title + position

`PATCH /dashboards/{did}/items/{iid}` accepts ONLY:

* `title`
* `position_x` / `position_y` / `width` / `height`

Snapshot columns (`sql`, `answer`, `chart_*`, `rows`, `insight`)
are deliberately absent from the patch shape. Reasoning:

* A user "editing the SQL on a card" is a footgun — the card's
  answer / chart would no longer match the new SQL, but the FE
  has no way to know. The user would think the card was updated;
  in reality the card lies.
* The supported way to change a card's underlying data is
  "re-extract": delete the card, ask the same question again,
  POST a fresh snapshot. The defaults make this a 2-click flow.

The pydantic model in `main.py` (`DashboardItemPatch`) enforces
this at the wire level — any `sql` field on the PATCH body is
silently dropped (test: `test_patch_item_does_not_accept_snapshot_columns`).

### 5. `updated_at` bumps on every item touch

Every `add_item` / `update_item` / `delete_item` also
`UPDATE dashboards SET updated_at = now()` on the parent.
Reason: the dashboard index page sorts newest-touched first, and
users expect "I just dragged a card" to count as touching the
dashboard. Doing this in the service layer (not via a trigger)
keeps the SQL grep-able when debugging "why did my dashboard jump
to the top".

## Alternatives explicitly rejected

### Storing cards in `saved_conversations`

We considered an `extracted` boolean column on `saved_conversations`
to mark "this saved conversation has been extracted into a card".
Rejected because:

* Saved conversations are **conversation-level**; cards are
  **turn-level**. Cramming a turn into a conversation row would
  require either denormalising (sql / answer per row, killing the
  "I saved a whole chat" affordance) or adding a `card_turn_index`
  column (which is just a worse `dashboard_items` table).
* The lifecycle is independent: you can extract cards without
  pinning the chat, and you can pin a chat without extracting
  cards. Separate tables match the user's mental model.

### Single `cards` table with no `dashboards` parent

A "loose card pool" where every card lives at top level and the
user filters by tag. Rejected because:

* Dashboard = "this set of cards together tells a story" is the
  core BI affordance. Without a dashboard primitive you can't
  share a curated set, can't reorder, can't title.
* Tags are a useful secondary index but not a replacement for a
  named container.

### Re-query at render time (with a per-card "stale" badge)

We considered always re-running the SQL with a "card last refreshed
2h ago" badge. Rejected on cost grounds (see §2 table) and because
the eval data shows >70% of turn answers don't change day-over-day
on the Northwind demo — re-running is mostly wasted work.

## Risks and known limitations

* **No FE in this commit.** The endpoint surface is complete; users
  can `curl` the API but the chat UI has no "Add to dashboard"
  button yet. The FE work (grid layout, drag-drop, dashboard list
  page, card render component) is tracked as Phase 2.1.1.
* **Snapshot bloat.** A card with 100 rows × wide columns can take
  ~50-200 KB of JSONB. For typical Northwind queries this is fine,
  but a 50-card dashboard could approach single-row Postgres TOAST
  thresholds (~2 MB / row inline). We'll watch the `pg_total_relation_size`
  on `dashboard_items` after a few weeks of real use.
* **Replayed turns produce partial snapshots.** Re-extracting a
  card from a saved-conversation replay loses `chart_spec` /
  `insight` / `rows`. The FE will gate the button accordingly in
  Phase 2.1.1; backend is fine with nullable columns. A long-term
  fix is extending the `Turn` shape in `state.py` to carry the
  chart and rows — but that doubles checkpoint row size, so we
  defer until the cost is justified.
* **Single-tenant.** No `user_id` column. Same posture as Phase 1.4
  bookmarks; multi-tenancy is one coordinated migration in Phase 3.
* **No card-level RBAC.** Anyone with API access sees every card.
  Acceptable for the local-dev / single-operator deploys this
  project targets.

## Compatibility / migration

* Existing DBs need a one-shot `psql` import of
  `data/seed/05-dashboards.sql`; fresh containers pick it up at
  init.
* `AskResponse` is unchanged. Dashboards have their own response
  shape (`Dashboard` / `DashboardItem`) on their own URL surface.
* No change to LangGraph state, the agent graph, or any existing
  endpoint. Phase 2.1 backend is purely additive.
