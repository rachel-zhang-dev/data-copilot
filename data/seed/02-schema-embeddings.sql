-- Schema for the schema-RAG retriever (week 3).
--
-- One row per table in the public schema; the embedding column holds a
-- BAAI/bge-m3 vector of the textual description. The retriever uses a
-- cosine-distance ORDER BY to pick the top-K most relevant tables for a
-- user question.
--
-- This file runs at first container init only; the table starts empty.
-- The Python indexer (./scripts/dev.sh index) populates it.

CREATE TABLE IF NOT EXISTS schema_embeddings (
    id          SERIAL       PRIMARY KEY,
    table_name  TEXT         NOT NULL UNIQUE,
    description TEXT         NOT NULL,
    embedding   vector(1024) NOT NULL,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- HNSW is overkill for ~14 rows but matches what we would deploy in
-- production and avoids a future migration once the schema grows.
-- vector_cosine_ops because BGE-M3 vectors are normalised and we use
-- the <=> distance operator in the retriever query.
CREATE INDEX IF NOT EXISTS schema_embeddings_hnsw
    ON schema_embeddings
    USING hnsw (embedding vector_cosine_ops);
