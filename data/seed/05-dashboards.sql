-- Dashboard tables (Phase 2.1 / ADR 0020).
--
-- A dashboard is a named grid of "cards". Each card is a SNAPSHOT of
-- one assistant turn (sql + answer + chart + rows) frozen at extract
-- time, plus a grid position (x, y, width, height). The Phase 2.1
-- MVP renders cards as static — no re-execution. A future Phase
-- 2.1.1 may add a "refresh card" button that re-runs the stored
-- ``sql``; the on-disk schema already supports that path.
--
-- Lifecycle:
--   1. User pins a conversation              → saved_conversations
--      (already shipped in Phase 1.4).
--   2. User extracts a specific turn         → dashboards.items
--      (this commit).
--   3. User arranges cards on a grid         → position_x/y/w/h.
--   4. User opens the dashboard later        → cards render from the
--      snapshot. SQL is NOT re-run on load.
--
-- Cards reference their source via ``source_thread_id`` +
-- ``source_turn_index`` purely for debug / "go back to the chat"
-- affordances; the FE only uses the snapshot columns to render.
-- That decoupling means deleting a saved conversation does NOT
-- invalidate the cards it produced (cards already hold all the data
-- they need).

CREATE TABLE IF NOT EXISTS dashboards (
    id          TEXT        PRIMARY KEY,        -- UUIDv4 string
    title       TEXT        NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dashboards_updated_at
    ON dashboards (updated_at DESC);


CREATE TABLE IF NOT EXISTS dashboard_items (
    id                 TEXT        PRIMARY KEY,     -- UUIDv4 string
    dashboard_id       TEXT        NOT NULL
        REFERENCES dashboards(id) ON DELETE CASCADE,

    -- Provenance — kept for debugging / "go to source chat" links.
    -- NOT used at card-render time (the FE reads the snapshot
    -- columns below instead). Both are nullable so a future
    -- "ad-hoc card" path can write a card with no chat origin.
    source_thread_id   TEXT,
    source_turn_index  INTEGER,

    -- Snapshot of the assistant turn at extract time. The FE reads
    -- ONLY these columns to render the card — we never re-run sql
    -- in Phase 2.1.
    title              TEXT        NOT NULL,
    sql                TEXT,
    answer             TEXT,
    chart_kind         TEXT,                          -- kpi|bar|line|grouped_bar|table
    chart_spec         JSONB,                         -- Vega-Lite v5 spec
    rows               JSONB,                         -- list[dict]
    row_count          INTEGER,
    insight            JSONB,                         -- {headline, bullets, metric_highlights}
    -- Phase 2.3.1 — preserve the critic verdict (ADR 0021) into the
    -- snapshot so a "suspicious" turn pinned to a dashboard keeps
    -- showing its ⚠ low-confidence badge instead of losing the
    -- warning. Shape: {verdict, reason, concerns}. NULL on cards
    -- extracted before the critic shipped or on turns where the
    -- critic was disabled by the feature flag.
    critic             JSONB,

    -- Grid layout. Coords are 12-column-grid units (react-grid-layout
    -- conventions). Defaults give a small KPI-shaped card the user
    -- can drag into place.
    position_x         INTEGER     NOT NULL DEFAULT 0,
    position_y         INTEGER     NOT NULL DEFAULT 0,
    width              INTEGER     NOT NULL DEFAULT 4,
    height             INTEGER     NOT NULL DEFAULT 3,

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Phase 2.3.1 — additive column for existing dev / prod databases
-- that already created ``dashboard_items`` before Phase 2.3.1. The
-- CREATE TABLE above is idempotent (``IF NOT EXISTS``) and so it
-- does NOT add the column to a pre-existing table; this ALTER
-- closes that gap. ``IF NOT EXISTS`` on both halves keeps the
-- whole seed file safely re-runnable.
ALTER TABLE IF EXISTS dashboard_items
    ADD COLUMN IF NOT EXISTS critic JSONB;

CREATE INDEX IF NOT EXISTS dashboard_items_dashboard
    ON dashboard_items (dashboard_id, created_at);
