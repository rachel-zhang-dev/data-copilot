# Architecture

> Last updated: week 2 — multi-node text-to-SQL pipeline.

## High-level diagram

```mermaid
flowchart TB
    subgraph Client["Client (Next.js)"]
        UI[Chat UI<br/>+ result viewer]
    end

    subgraph API["API (FastAPI)"]
        Endpoint[/POST /ask/]
        Graph["LangGraph Agent"]
    end

    subgraph Agent["LangGraph Agent"]
        direction TB
        N1["classify_intent"]
        N1b["small_talk"]
        N2["retrieve_schema<br/>(week 3 — RAG)"]
        N3["generate_sql"]
        N4{"validate_sql"}
        N5["execute_sql"]
        N6["summarize_result"]
        N7["finalize_error<br/>(week 4: → rewrite_sql)"]

        N1 -- chitchat --> N1b
        N1 -- data --> N3
        N3 --> N4
        N4 -- valid --> N5 --> N6
        N4 -- invalid --> N7
        N5 -- db error --> N7
    end

    subgraph Data["Data layer"]
        PG[(PostgreSQL<br/>+ pgvector)]
        Schema[(Schema docs<br/>embeddings)]
    end

    subgraph LLM["External LLMs"]
        DS[DeepSeek<br/>chat]
        DSE[DashScope<br/>embeddings]
    end

    subgraph Obs["Observability"]
        LS[LangSmith]
    end

    UI -->|HTTP| Endpoint
    Endpoint --> Graph
    Graph -. uses .-> Agent
    Agent -->|SQL| PG
    Agent -->|retrieve| Schema
    Agent -->|prompt| DS
    Agent -->|embed| DSE
    Agent -. trace .-> LS
```

## Components

| Component | Tech | Purpose |
|-----------|------|---------|
| Frontend | Next.js 15 + Tailwind + shadcn/ui | Chat UI with streaming responses and result visualisation. (Wired up week 10.) |
| API | FastAPI + Pydantic v2 | Thin HTTP layer over the agent. |
| Agent runtime | LangGraph | State machine that orchestrates retrieval, generation, validation, and self-healing. |
| LLM | DeepSeek (chat) | Primary text-to-SQL and summarisation model. OpenAI-compatible API for portability. |
| Embeddings | DashScope `text-embedding-v3` | Cheap, high-quality embeddings for schema retrieval. |
| Vector store | pgvector inside Postgres | Co-locating data and embeddings simplifies ops; same connection pool. |
| Observability | LangSmith | Traces, evaluations, and regression dashboards. |

## What is implemented today (end of week 2)

| Concern | Status | Notes |
|---|---|---|
| Intent classification (data vs chitchat) | ✅ | `classify_intent_node`; tiny prompt, `temperature=0` |
| Schema introspection | ✅ | Full DDL pulled from `information_schema` at startup, cached |
| SQL generation | ✅ | `generate_sql_node`; DeepSeek with full schema in prompt |
| SQL safety / row cap | ✅ | sqlglot AST validation + auto `LIMIT` (see [ADR 0002](decisions/0002-sql-safety.md)) |
| SQL execution | ✅ | SQLAlchemy + psycopg3, sync pool managed in lifespan |
| Result summarisation | ✅ | `summarize_result_node`; rows JSON-previewed to LLM |
| Error finalisation | ✅ | Deterministic templates per error class |
| Schema retrieval (RAG) | ⏳ week 3 | replaces "dump full DDL" with top-k table embeddings |
| Self-healing loop | ⏳ week 4 | currently `finalize_error` terminates; will loop back to `generate_sql` |
| Multi-turn dialogue | ⏳ week 5 | state already has `messages` with reducer in place |

## Why LangGraph rather than plain LangChain?

Text-to-SQL is **not a single forward pass**. Real systems need to:

- branch ("is this a metadata question or a data question?"),
- loop ("the SQL failed; rewrite and try again"),
- pause for human approval (week 7),
- accumulate state (chat history, retry count, intermediate results).

LangChain's LCEL chains model a single linear data flow.
LangGraph models a **stateful directed graph**, which fits this problem
naturally and gives us first-class support for cycles, checkpointing,
and human-in-the-loop.

## Roadmap

See [`../README.md`](../README.md) for the 12-week plan.
Architectural decisions are recorded under [`./decisions/`](./decisions/).
