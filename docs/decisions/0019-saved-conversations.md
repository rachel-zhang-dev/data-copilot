# ADR 0019: Saved conversations + sidebar (Phase 1.4)

> Status: Accepted · Date: 2026-06 (Phase 1.4) · Supersedes: none

## Context

Through Phase 1.3 the agent answers single-shot questions well and
handles multi-step investigations gracefully. But every chat
disappears the moment the page closes — there's no way to come back
to "that conversation where I figured out the Beverages drop" a day
later. Two missing affordances:

1. **No persistence handle for the user.** The `conversation_id` /
   `thread_id` IS persisted (PostgresSaver keeps the LangGraph state
   indefinitely), but the FE has no UI to enumerate or pin past
   threads.
2. **No discovery affordance for repeat work.** The closest thing a
   user can do today is paste back the question text. That loses
   intermediate drill-down structure, lost cost / latency, and the
   chart context.

Phase 1.4 closes both with the smallest UI that earns the name:
**pin/unpin + sidebar drawer + click-to-replay**. No tags search, no
"cards" extraction, no dashboards — those promote to Phase 2 if the
basic shape proves useful.

## Decision

Adopt a **bookmarks-over-LangGraph** split:

```
                            saved_conversations
                           (one row per user pin)
                          ┌────────────────────────┐
                          │ thread_id  PK          │
                          │ title                  │
                          │ tags    TEXT[]         │
                          │ notes                  │
                          │ pinned_at, updated_at  │
                          └─────────┬──────────────┘
                                    │ 1:1
                                    ▼ via thread_id
              checkpoints / checkpoint_blobs / checkpoint_writes
              (LangGraph PostgresSaver — DO NOT MODIFY)
```

### 1. New `saved_conversations` table, owned by us

The bookmark metadata sits in its own table that we control. We
**never** write into LangGraph's `checkpoints` / `checkpoint_*` —
those are the PostgresSaver's private surface and may change schema
across LangGraph versions. The only relationship between our row and
LangGraph's state is the foreign-key-ish `thread_id` column (not an
actual FK because LangGraph offers no integrity guarantee we can
trust across upgrades). See [data/seed/04-saved-conversations.sql](../../data/seed/04-saved-conversations.sql).

### 2. Zero-friction "Pin" — no dialog

When the user clicks the Pin button (top-right of the chat header):

* If the thread isn't bookmarked → `POST /conversations/{id}/save`
  with an empty body. The backend auto-derives a title from the
  first user question (`derive_title()` in
  [apps/api/copilot/saved.py](../../apps/api/copilot/saved.py)),
  capped at 80 chars.
* If the thread IS bookmarked → `DELETE /conversations/{id}/save`.
  No confirmation prompt; users routinely "undo" by re-clicking,
  and the backend treats unsave as cheap (one row delete; the
  underlying LangGraph state is untouched so a re-pin is a one-row
  insert).

Inline title editing happens later, in the sidebar row, via
double-click. This keeps the click path on the header button
single-step — the very thing the "C" option in the Phase 1.4 plan
was chosen for.

### 3. Sidebar = persistent rail, optionally collapsed

Same mental model as ChatGPT / Claude: left-aligned drawer with a
"+ New chat" affordance at the top and one row per pinned
conversation. The expanded width is `w-64` (16rem); collapsed it
becomes `w-12` with two icon buttons (toggle + new chat). State is
persisted to `localStorage` under `data-copilot:sidebar-collapsed`.

Why client-side and not a user-pref column:

* Phase 1.4 doesn't introduce user auth — there's no "user" to
  attach a server pref to.
* The cost of guessing wrong on first load is a single re-toggle.

### 4. Four endpoints, all proxied through Next Route Handlers

| Method | Path | Used for |
|---|---|---|
| `POST`   | `/conversations/{id}/save`     | Pin or update (idempotent) |
| `DELETE` | `/conversations/{id}/save`     | Unpin |
| `GET`    | `/conversations/saved`         | Sidebar list + tiny preview |
| `GET`    | `/conversations/{id}/messages` | Replay dialogue on row click |

Each goes through a `app/api/conversations/...` Route Handler so the
browser only ever talks to its own origin — same pattern as
`/api/ask` (week 10). No CORS preflight, `API_BASE_URL` stays out of
the client bundle.

### 5. Replay reads from LangGraph state, not from a snapshot

When the user clicks a saved row, the FE calls
`GET /conversations/{id}/messages`. The backend issues
`sql_graph.aget_state({"configurable": {"thread_id": id}})` and
projects the resulting `dialogue` field into the same `Turn` shape
the FE already renders for live conversations.

Why not snapshot the dialogue at pin time:

* The pinned thread can be **continued** — if we snapshot, the
  replay diverges from the live state the moment the user types a
  follow-up.
* PostgresSaver already keeps the dialogue durable, so the snapshot
  would be a stale copy of something we already have.
* The aget_state hop is cheap (single PK lookup).

### 6. Saved-list preview computed on the fly

The `GET /conversations/saved` response includes `last_question`,
`last_answer`, and `turn_count` per row. These come from the same
`aget_state`-style read used by replay, on demand at list time.

For ≤100 saved conversations this is one fast PK lookup per row
(~1 ms each). If the list ever balloons past a few hundred, the
right fix is to snapshot the preview into `saved_conversations`
columns at pin time — kept out of v1 because the simpler approach
is fast enough.

## Alternatives explicitly rejected

### Extending PostgresSaver's metadata

Considered storing pin / title / tags as fields inside the
`checkpoint_metadata` column LangGraph already writes per
checkpoint. Rejected because:

* `checkpoint_metadata` is "the metadata FOR THIS checkpoint" (e.g.
  parent IDs, source step) — overloading it with user bookmarks
  conflates two distinct lifecycles.
* LangGraph's schema may evolve; if `checkpoint_metadata` becomes
  versioned / pruned, our bookmarks vanish silently.
* Querying "all pinned conversations" would require scanning every
  checkpoint row. With Phase 1.3 multi-hop turns producing 6-10
  checkpoints each, the scan cost grows fast.

### A "cards" feature instead of bookmarks-of-conversations

The original Phase 1.3 plan suggested cards: extract a single turn
(question + SQL + chart + insight) into a standalone tile. We
deferred to Phase 2.1 (Dashboard) because:

* Cards without a layout to put them in is just a list of disjoint
  tiles — no clear UI affordance.
* Bookmarks of full conversations give the same "save the good
  ones" affordance with much less new UI surface.
* Phase 2.1 Dashboard will likely want cards as its primitive, so
  the design discussion belongs there.

### Authenticated user / multi-tenant scoping

Phase 1.4 ships single-tenant. The `saved_conversations` table has
no `user_id` column. Adding it now would be premature — Phase 3
(see roadmap) covers auth, RLS, and multi-tenancy as one
coordinated migration.

## Risks and known limitations

* **Single-tenant model.** All bookmarks are visible to anyone with
  access to the API. The local-dev / single-operator deploys this
  project targets don't have auth, and the production demo at
  fly.dev is intentionally public (it has a single-shared-state).
  Multi-tenant gets its own ADR in Phase 3.
* **No bulk operations.** No "delete all", no "import / export
  pinned list", no "select multiple and tag". MVP scope.
* **Preview cost grows linearly.** ~1 ms × N rows on each
  `GET /conversations/saved`. Fine for N ≤ 100; we'll cache to
  bookmark columns if it gets noticeable.
* **Title is a 1-shot auto-derive.** If the user pins on the first
  turn, the title is the (likely vague) first question. They can
  rename it from the sidebar, but the default isn't always great.
  Hard to improve without an explicit dialog (which the "C" UX
  choice rejected).
* **Drawer width is fixed (`w-64`).** Long titles ellipsise. Phase
  1.5 may add a resize handle if the truncation gets annoying.

## Compatibility / migration

* `data/seed/04-saved-conversations.sql` runs at first DB init.
  Existing DBs need a one-shot `psql` import:

  ```bash
  docker exec -i data-copilot-postgres psql -U copilot -d northwind \
      < data/seed/04-saved-conversations.sql
  ```

* `AskResponse` does NOT change in Phase 1.4. The bookmark fields
  live on their own response shape (`SavedConversation`). Legacy
  clients see no contract change.

* The FE adds two new client components (`PinButton`, `SavedDrawer`)
  and re-laid out `ChatPanel`. The middle column keeps its
  `max-w-3xl` chat width — only the framing changed.
