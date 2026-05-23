# ADR 0003: Embedding provider — SiliconFlow BGE-M3

> Status: Accepted · Date: 2026-05 (Week 3) · Supersedes: provisional choice in [ADR 0001](0001-tech-stack.md)

## Context

Week 3 introduces schema-aware retrieval: every business table gets a
text description that is embedded and stored in pgvector, so each
question retrieves only the slice of schema it actually needs. We
needed to pick an embedding provider with the following properties:

1. Reachable from China without a VPN.
2. Cheap or free at this project's volume (~14 docs at index time,
   one query embedding per `/ask` call).
3. Quality good enough that "products" beats "shippers" on a query
   like "top 5 best sellers".
4. Swappable — one of the project's design pillars is "any
   OpenAI-compatible provider works after a one-line `.env` change".

## Decision

We use **SiliconFlow** as the default embedding provider, calling
**`BAAI/bge-m3`** via its OpenAI-compatible `/v1/embeddings` endpoint.

The implementation lives in
[`apps/api/copilot/embeddings.py`](../../apps/api/copilot/embeddings.py).
Switching to any other OpenAI-compatible provider is three env-var
changes:

```env
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536
```

## Alternatives considered

### Alibaba DashScope (the original ADR-0001 default)

DashScope's `text-embedding-v3` is a fine model, similarly priced, and
domestic. The blocker was operational: opening a DashScope account
requires Aliyun real-name verification and explicit activation of the
"百炼" product, both buried inside the Aliyun console. For a
single-developer portfolio project where the win over BGE-M3 is small,
the friction is not justified. DashScope remains a one-env-var swap if
the eventual deployment target already lives in Aliyun.

### OpenAI `text-embedding-3-small`

Model quality is excellent (and dim 1536 is generous), but accessing
OpenAI from inside China requires a stable proxy and a USD-billed
credit card. Both are non-trivial for a project that should "just
work" for any developer who clones the repo.

### Local sentence-transformers (e.g. `bge-small-zh-v1.5`)

Attractive because it removes the API dependency, but adds ~500 MB
of wheels (PyTorch, sentence-transformers, tokenizers) and a model
download on first run. We intentionally keep `pyproject.toml` slim;
production deployments don't ship inference models inside the API
process. Mentioned as a future evaluation in the README roadmap.

## Why BGE-M3 specifically

* **Multilingual SOTA.** The 2024 BGE-M3 paper is the current top open
  multilingual embedding model, comparable to OpenAI 3-small on
  retrieval benchmarks and stronger on Chinese.
* **Free on SiliconFlow.** SiliconFlow offers BGE family models at no
  cost as part of its model marketplace strategy. Even at higher
  volumes the project would not start paying.
* **1024 dimensions.** A reasonable middle ground; matches the
  `vector(1024)` column we declared in
  [`data/seed/02-schema-embeddings.sql`](../../data/seed/02-schema-embeddings.sql).

## Consequences

### Good

* **Zero new SDK.** SiliconFlow's OpenAI-compatible endpoint means we
  reuse `langchain-openai` (already a dependency for DeepSeek chat).
  The `dashscope` Python SDK has been dropped from `pyproject.toml`.
* **Observability for free.** The same LangSmith tracing that already
  captures DeepSeek calls captures embedding calls — they appear as
  child runs of the `retrieve_schema` node.
* **Easy to evaluate alternatives.** A future ADR could add a
  side-by-side comparison (DashScope vs BGE-M3 vs local
  sentence-transformers) by swapping env vars and re-running the eval
  harness.

### Bad / accepted trade-offs

* **One more API key to manage.** SiliconFlow joins DeepSeek and the
  optional LangSmith key in `.env`. Mitigated by treating these as a
  single unit in the README quick-start.
* **Network dependency at request time.** Every `/ask` call now makes
  an embedding HTTP request before generating SQL. Latency overhead
  is ~150 ms in normal conditions. The retriever falls back to the
  full schema on timeout/error, so this is a graceful degradation,
  not a hard failure mode.

## Future work

* Eval harness (Week 6) will A/B BGE-M3 vs DashScope on a fixed
  question set and let the data settle the choice.
* Local sentence-transformers may be revisited for offline /
  air-gapped deployments.
