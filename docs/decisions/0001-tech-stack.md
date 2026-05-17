# ADR 0001 — Initial Technology Stack

- **Status:** Accepted
- **Date:** 2026-05-17

## Context

We are building an enterprise Text-to-SQL agent as a portfolio project
targeting AI Application Engineer roles. The stack must:

1. Reflect what mature foreign-company AI teams actually use in 2026.
2. Be cheap to develop on (we are paying out-of-pocket).
3. Be easy to demonstrate end-to-end on a single laptop.

## Decision

| Concern | Choice | Why |
|---------|--------|-----|
| Agent runtime | **LangGraph** | Industry consensus for stateful, multi-step agents in 2026. Cleaner than vanilla LangChain for loops and human-in-the-loop. |
| LLM SDK | **`langchain-openai`** pointed at DeepSeek's OpenAI-compatible endpoint | Zero vendor lock-in; same code runs against OpenAI / OpenRouter / Together. |
| Primary LLM | **DeepSeek-Chat** | ~10–50× cheaper than Claude / GPT-4o, strong on SQL generation in current benchmarks. We will benchmark Claude Sonnet and GPT-4o-mini later for comparison. |
| Embeddings | **DashScope `text-embedding-v3`** | Generous free tier, low latency from China, cheap. Hot-swappable for OpenAI / BGE later. |
| Vector store | **pgvector inside Postgres** | One database for schema metadata, embeddings, and (later) eval data. Production-friendly; Chroma is a toy. |
| Web framework | **FastAPI + Pydantic v2** | De-facto Python AI-app standard. |
| Frontend | **Next.js 15 + TypeScript** | Streaming responses, professional polish, leverages existing full-stack background. |
| Observability | **LangSmith** | Standard for LangChain stack. Free tier covers personal use. |
| Package manager | **uv** | 10× faster than pip / poetry; what new foreign-company projects are adopting. |
| Container | **Docker Compose** | Single command to bring up Postgres locally. Trivial port to fly.io / ECS later. |

## Consequences

- We accept being coupled to LangChain/LangGraph semantics. If the
  ecosystem shifts (e.g. towards `pydantic-ai` or DSPy) we may need to
  re-platform parts of the agent. We mitigate this by keeping prompts
  and tool implementations framework-agnostic where possible.
- Using DeepSeek as primary LLM means the project must be tested against
  at least one US-hosted LLM before landing job interviews, to prove
  portability.
- pgvector keeps our infra simple but caps us at a few million rows of
  embeddings. That is far more than we need for this project.
