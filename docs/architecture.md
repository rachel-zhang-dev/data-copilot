# Architecture

> Last updated: project bootstrap (week 1).

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

    subgraph Agent["LangGraph Agent (week 2+)"]
        direction TB
        N1["1. understand_question"]
        N2["2. retrieve_schema<br/>(RAG)"]
        N3["3. generate_sql"]
        N4{"4. validate_sql"}
        N5["5. execute_sql"]
        N6["6. summarize_result"]
        N7["rewrite_sql"]

        N1 --> N2 --> N3 --> N4
        N4 -- valid --> N5 --> N6
        N4 -- error --> N7 --> N3
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
