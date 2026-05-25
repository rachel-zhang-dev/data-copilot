-- Schema profiles table (Phase 1.1 / ADR 0016).
--
-- One row per (table, column) capturing cheap-to-compute metadata that
-- the LangGraph agent uses at runtime:
--
--   * "Coverage check" node — decides whether the retrieved schema can
--     actually answer the user's question, or whether we should refuse
--     with an explanation (e.g. "no conversion rate data in this DB").
--   * "Explore schema" node — answers "what data do you have?" with a
--     grouped overview + suggested questions.
--
-- The table is populated by ``./scripts/dev.sh index`` (same lifecycle
-- as ``schema_embeddings`` — see ADR 0016 §"Why a persistent table"),
-- inside the same transaction so the embedding index and the profile
-- table never drift out of sync.
--
-- A table-level summary is written as a sentinel row with
-- ``column_name = '*'``; per-column rows use the real column name.

CREATE TABLE IF NOT EXISTS schema_profiles (
    table_name      TEXT        NOT NULL,
    column_name     TEXT        NOT NULL,         -- '*' = table-level summary row
    data_type       TEXT,
    row_count       BIGINT,                       -- pg_class.reltuples (approx)
    null_ratio      REAL,                         -- pg_stats.null_frac, NULL when unknown
    distinct_count  BIGINT,                       -- pg_stats.n_distinct normalised, NULL when unknown
    sample_values   JSONB,                        -- top-K from pg_stats.most_common_vals
    min_value       TEXT,                         -- pg_stats.histogram_bounds[0]
    max_value       TEXT,                         -- pg_stats.histogram_bounds[-1]
    fk_target       TEXT,                         -- "table.column" if this column is a FK
    column_comment  TEXT,                         -- pg_description / COMMENT ON
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (table_name, column_name)
);

-- Cheap covering index for the most common access pattern: "give me
-- everything about table X". The PK already covers (table, column)
-- lookups, but a column-store-style scan-by-table is so common we add
-- a dedicated index on table_name to make it index-only.
CREATE INDEX IF NOT EXISTS schema_profiles_by_table
    ON schema_profiles (table_name);
