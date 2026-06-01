-- Saved conversations metadata (Phase 1.4 / ADR 0019).
--
-- Rows in this table are USER bookmarks on top of LangGraph's own
-- checkpoint tables. We never write into ``checkpoints`` / ``checkpoint_*``
-- (they belong to LangGraph's PostgresSaver) — instead this table sits
-- alongside and references conversations by ``thread_id`` (the same UUID
-- that ``/ask`` returns to the caller as ``conversation_id``).
--
-- Lifecycle:
--   1. User asks a question        → conversation_id assigned, LangGraph
--                                     writes its checkpoint rows.
--   2. User clicks "Save"          → one INSERT here.
--   3. User unpins                  → one DELETE here. The underlying
--                                     LangGraph checkpoints stay
--                                     (intentional — quick re-save is
--                                     a one-row INSERT, not a replay).
--
-- Title defaults to NULL and the API auto-derives one from the first
-- question (Phase 1.4 zero-friction "save"). The user can edit the
-- title later via ``PATCH /conversations/{id}/save``.

CREATE TABLE IF NOT EXISTS saved_conversations (
    thread_id   TEXT        PRIMARY KEY,
    title       TEXT        NOT NULL,
    tags        TEXT[]      NOT NULL DEFAULT '{}',
    notes       TEXT,
    pinned_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The list view is ORDER BY pinned_at DESC; a btree on the timestamp
-- keeps pagination O(log n) even when the table grows. Skipped a tag
-- index for v1 — fine until we have hundreds of pinned conversations.
CREATE INDEX IF NOT EXISTS saved_conversations_pinned_at
    ON saved_conversations (pinned_at DESC);
