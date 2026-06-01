# ADR 0020: Dashboard cards — snapshot model + storage (Phase 2.1 + 2.1.1 + 2.2)

> Status: Accepted · Date: 2026-06 (Phase 2.1 backend → 2.1.1 FE → 2.2 back-link) · Supersedes: none

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

This decision shipped in three commits:

* **Phase 2.1** (backend) — DDL + service + 7 endpoints + ADR.
* **Phase 2.1.1** (FE) — Route Handler proxies, an "📌 Add to dashboard"
  disclosure on every chat turn, a `/dashboards` index page, and a
  `/dashboards/[id]` detail page that hosts a `react-grid-layout`
  12-column grid (drag, resize, inline rename, delete). FE notes are
  recorded in §"Phase 2.1.1 — Frontend additions" below.
* **Phase 2.2** (back-link) — every card grows a "View source chat →"
  footer link that deep-links into `/?conversation=<id>&turn=<n>`.
  The chat panel reads those params at the server boundary and
  auto-loads the conversation + scrolls the matching turn into
  view. Closes the analyst's loop: "this card looks off → jump back
  to the original chat and ask a follow-up". Notes in
  §"Phase 2.2 — Back-link to source chat" below.

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

## Phase 2.1.1 — Frontend additions

### Grid library: `react-grid-layout/legacy`

`react-grid-layout` 2.x is a TypeScript-first rewrite that groups
the old flat props (`cols`, `rowHeight`, `compactType`, …) into
nested config objects (`gridConfig`, `dragConfig`, `compactor`).
The new surface is more powerful but the FE doesn't need any of
the extra knobs (custom compactors, position strategies); on the
other hand the v1 API is small enough to read in one sitting.

We import from the bundled `react-grid-layout/legacy` entry, which
ships specifically as a "100% runtime-compatible" migration path
for v1 users. Trade-off accepted:

* (+) Component stays under 200 lines; the prop set
  (`cols=12`, `rowHeight=60`, `draggableHandle=".drag-handle"`,
  `compactType="vertical"`) reads exactly the same as every v1
  RGL tutorial on the web.
* (-) We're on a soft-deprecation path. The upstream README
  positions `/legacy` as a migration step, not the long-term
  home. If we ever need the v2-only features (custom compactor,
  framework-agnostic `core` builds), we'll port.

### Width detection: ResizeObserver, not `WidthProvider`

The v1 `WidthProvider` HOC measures via window listeners and a
remount-on-resize pattern that fights React 19's strict mode. We
own a `ResizeObserver` on the grid container instead (one effect
in `DashboardGrid.tsx`). Cleaner, ~10 lines, no hidden side
effects.

### Layout persistence: `onDragStop` + `onResizeStop`, NOT `onLayoutChange`

`onLayoutChange` fires on every mount (and on every frame during
a drag). Naïve persistence would either fire spurious PATCHes on
first render or hammer the backend at ~60 Hz. The `Stop`
callbacks fire exactly once when the user releases the mouse —
that's the "save point" the FE and backend agree on.

The grid hands us the full layout each time, so the FE diffs
position / size against the in-memory items and PATCHes only
what changed. With dozens of cards per dashboard the linear
scan is cheap; we don't memoise.

### Snapshot composition lives in the FE

`ChatTurn.tsx` builds the `DashboardItemSnapshot` from the live
`AskResponse` (title from the user's question, capped at 80 chars
to match the backend's `snapshot_from_replay_turn`). The picker
POSTs that envelope verbatim — every field on the wire matches
the `DashboardItemRequest` pydantic model 1:1, so the static
contract is enforced by `openapi-typescript` regen and not just
trust.

### Replayed turns: button hidden, not disabled

ADR 0020 §"Risks" flagged that re-extracting from a saved-and-
reloaded chat yields a partial snapshot (`chart_spec` / `insight` /
`rows` are not in persisted `dialogue`). The FE gate is in
`ChatTurn.isExtractable`: we hide the button entirely when
`result.chart_kind === null`. Reasoning:

* Replayed turns set `chart_kind=null` deliberately in
  `ChatPanel.loadSavedConversation`, so this single check
  covers the case without any new metadata.
* A disabled button with a tooltip would invite the question
  "why can't I save this?". Hiding it sidesteps that — the user
  re-asks the question to get a fresh, fully-rendered turn, and
  the button reappears.

### Rename UX: double-click → input, matching `SavedDrawer`

Card title, dashboard title (both list tile and detail header),
and saved-conversation title all use the same `double-click →
input → Enter saves / Escape cancels` pattern. The fourth
appearance of this pattern is a soft signal that we should
extract it into a `<InlineRenameField />` primitive in a Phase
2.2 cleanup; tracked as future work, deferred because the
current three call sites still fit on one screen each.

### Why no react-grid-layout responsiveness?

The v1 `Responsive` + `WidthProvider` combination supports
breakpoint-specific layouts. We deliberately use plain
`GridLayout` because:

* A 12-column desktop layout collapsing to 1-column on mobile is
  easy to express with `cols=12` + content that naturally
  overflows. We don't need separate breakpoint layouts.
* Storing per-breakpoint positions would multiply the
  `dashboard_items` row size (4 numbers × N breakpoints) for a
  feature most users won't notice.

If demand surfaces we can switch to `ResponsiveGridLayout` and
add `position_lg / position_md / position_sm` columns without a
breaking schema change.

### Imports + bundle

`react-grid-layout/css/styles.css` is imported once in
`app/dashboards/[id]/page.tsx` (the only route that mounts a
grid) so the ~5 KB stylesheet doesn't ship to the chat page.
Bundle delta on `/dashboards/[id]`: ~21 KB first-load JS
(verified via `next build` summary).

## Phase 2.2 — Back-link to source chat

### Why this matters

ADR 0020 §1 deliberately kept `source_thread_id` + `source_turn_index`
on every `dashboard_items` row even though the snapshot is fully
self-contained at render time. Phase 2.2 cashes that decision in:
the columns become the back-link target for an "Open this card's
source conversation, scrolled to the turn that produced it" affordance.

This closes the analyst's loop. Without the back-link, a card on a
dashboard is a write-only artefact — the analyst can stare at it
but can't easily ask "wait, what was the question that produced
this 13?". With it, two clicks (the link, then any follow-up
question) get them back to live investigation in the original
conversation.

### Wire format: `/?conversation=<id>&turn=<n>`

A plain query-param deep-link, parsed at the server boundary in
`app/page.tsx`. Rejected alternatives:

* **A bespoke `/chat/<thread_id>?turn=<n>` route** — would require
  splitting the chat panel into a route group and threading the
  thread_id through layout. Not worth a second route when the chat
  page IS the only page.
* **POST with conversation_id in the body** — links must be GET
  (so users can share a dashboard URL with a teammate and the
  back-link still works in their browser). A query param is the
  canonical web pattern for "deep-link state on a single page".

### Server vs client parsing

Next.js 15 gives two routes for reading query params in App Router:

| | server (`searchParams` prop) | client (`useSearchParams` hook) |
|---|---|---|
| Suspense wrapping | none needed | required, or static rendering bails |
| Where the value lands | a server prop, passed down as plain JS | a hook reading from the URL on the client |
| Cost | the page becomes ƒ (dynamic) instead of ○ (static) | the page stays static, hook reads on hydrate |

We picked **server**. The chat page was already going to be
dynamic the moment any per-request thinking landed (it's never
been worth statically pre-rendering an empty chat shell). Reading
on the server keeps the client component prop interface clean
(`initialConversationId: string | null`) and avoids the awkward
`<Suspense>` wrapper that `useSearchParams` would force.

Build-output verification: `/` moves from `○` (static) to `ƒ`
(dynamic) with a +0.18 KB size delta. Acceptable.

### Deterministic turn IDs

Pre-Phase 2.2, `loadSavedConversation` minted random replay IDs
(`replay-${Math.random()...}`). That made it impossible to locate
a specific turn's DOM node from outside the panel. Phase 2.2
switches to `replay-<1-based-index>`, matching the `turn_index`
we already write into each replayed `AskResponse.turn_index` AND
the `source_turn_index` we store on dashboard cards. One scheme,
three places.

The deep-link scroll then becomes a one-liner:

```ts
document.querySelector(`[data-turn-id="replay-${pendingScrollToTurn}"]`)
  ?.scrollIntoView({ behavior: "smooth", block: "start" });
```

### React 19 strict-mode double-mount guard

The auto-load effect uses a `useRef(false)` latch (`deepLinkLoadedRef`)
to ensure the load fires exactly once. Without the latch, strict-mode
double-render in dev would issue two `GET /conversations/{id}/messages`
requests on every navigation — harmless but noisy.

### Edge cases handled

* **Source conversation deleted from LangGraph.** `loadConversation`
  returns 404 → the existing `console.error` path catches; the chat
  panel stays empty. No crash.
* **Target turn doesn't exist** (e.g. the conversation was truncated
  by compaction). The scroll effect's `querySelector` returns null;
  we just clear the pending target and the page lands at the top.
  No error UI; the user sees they're in the right conversation but
  not at the right turn.
* **`turn` param missing or non-numeric.** `Number.parseInt(..., 10)`
  returns NaN; we coerce to null and skip the scroll. No error.

## Risks and known limitations

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
