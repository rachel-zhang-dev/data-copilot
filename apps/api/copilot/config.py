"""Application configuration loaded from environment variables.

We use pydantic-settings so config is type-checked, documented, and
fails fast at startup if required values are missing.

The `.env` file is located by walking up from this module until we
find a directory containing `pyproject.toml`. This way the config
works no matter where the process is launched from (project root,
`apps/api/`, a notebook, a test runner, etc.).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_project_root() -> Path:
    """Walk upwards until we find pyproject.toml; fall back to cwd."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


_ENV_PATH = _find_project_root() / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_PATH,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- LLM (required) ----------
    deepseek_api_key: str = Field(..., description="DeepSeek API key")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # ---------- Embeddings (required from week 3 onwards) ----------
    siliconflow_api_key: str = Field(
        ...,
        description=(
            "SiliconFlow API key. Used for BGE-M3 embeddings. Register at "
            "https://cloud.siliconflow.cn/ — BGE models are free to call. "
            "Swap to any OpenAI-compatible embedding provider by changing "
            "EMBEDDING_BASE_URL and EMBEDDING_MODEL."
        ),
    )
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = Field(
        default=1024,
        ge=64,
        le=8192,
        description=(
            "Vector dimension produced by the embedding model. Must match "
            "the dimension of the schema_embeddings.embedding column."
        ),
    )
    schema_top_k: int = Field(
        default=4,
        ge=1,
        le=50,
        description=(
            "How many tables the schema retriever returns before foreign-key "
            "expansion. 4 is enough for Northwind; raise it for wider schemas."
        ),
    )

    # ---------- Observability (always optional) ----------
    langsmith_api_key: str | None = None
    langsmith_tracing: bool = True
    langsmith_project: str = "data-copilot-dev"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # ---------- Database (required from week 2 onwards) ----------
    database_url: str = Field(
        ...,
        description=(
            "PostgreSQL connection string, e.g. "
            "postgresql://copilot:copilot_dev_pwd@localhost:5432/northwind. "
            "Required since week 2 — start the DB with ./scripts/dev.sh up."
        ),
    )
    sql_max_rows: int = Field(
        default=100,
        ge=1,
        le=10_000,
        description=(
            "Cap injected into every LLM-generated SELECT when the model "
            "forgets a LIMIT. Keeps runaway queries from flooding the agent."
        ),
    )

    # ---------- Conversation memory (week 5) ----------
    compaction_threshold_tokens: int = Field(
        default=4_000,
        ge=10,
        le=60_000,
        description=(
            "When the cumulative ``dialogue`` token count exceeds this, "
            "compact_history_node summarises older turns into one synthetic "
            "turn. 4k is a comfortable buffer in DeepSeek's 64k context. "
            "The minimum is set low so unit tests can exercise the trigger "
            "with tiny dialogues; production deployments should keep the "
            "default or higher."
        ),
    )
    compaction_keep_last_n: int = Field(
        default=6,
        ge=1,
        le=100,
        description=(
            "How many of the most recent turns to keep verbatim when "
            "compaction triggers. Earlier turns are summarised into one."
        ),
    )

    # ---------- Human-in-the-loop (week 7) ----------
    risk_explain_cost_threshold: float = Field(
        default=1_000.0,
        ge=0.0,
        description=(
            "Postgres planner ``Total Cost`` above which the agent pauses "
            "for a human approve / reject decision before executing the "
            "SQL. Calibrated for Northwind; raise to ~10_000 on a real "
            "warehouse. Set to 0 to disable the risk check entirely (every "
            "validated query runs immediately)."
        ),
    )
    risk_explain_timeout_ms: int = Field(
        default=500,
        ge=10,
        le=60_000,
        description=(
            "Statement timeout applied to the EXPLAIN call itself. Defends "
            "against pathological queries that hang the planner. EXPLAIN "
            "does not execute the SQL, so this can stay tight."
        ),
    )

    # ---------- Phase 1.1: schema coverage gate (ADR 0016) ----------
    coverage_check_enabled: bool = Field(
        default=True,
        description=(
            "When True, every data-intent turn runs through "
            "``coverage_check_node`` before SQL generation. The node "
            "asks the LLM whether the retrieved schema can plausibly "
            "answer the question; on ``refuse`` the graph diverts to "
            "``explain_uncovered_node`` and never writes SQL. Disable "
            "to fall back to the pre-Phase-1.1 behaviour (always "
            "generate SQL) — used by the eval baseline."
        ),
    )

    # ---------- Visualisation (week 8) ----------
    chart_max_rows: int = Field(
        default=50,
        ge=1,
        le=10_000,
        description=(
            "Result-row ceiling for emitting a real chart spec. Above "
            "this, ``visualize_node`` returns ``chart_kind='table'`` "
            "and no Vega-Lite spec (a 200-bar chart is unreadable)."
        ),
    )

    # ---------- Caching & resilience (week 9) ----------
    embedding_cache_enabled: bool = Field(
        default=True,
        description=(
            "When True, ``embed_query`` results are stored in a process-"
            "local TTL cache keyed by ``(model, text)``. Disable to "
            "force a fresh call on every question (used by the "
            "embedding_cache A/B in the eval harness)."
        ),
    )
    embedding_cache_max_size: int = Field(
        default=1024,
        ge=1,
        le=1_000_000,
        description="Maximum number of cached embedding vectors.",
    )
    embedding_cache_ttl_seconds: int = Field(
        default=3_600,
        ge=1,
        le=30 * 24 * 3_600,
        description="How long a cached embedding stays valid (default 1 h).",
    )
    llm_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description=(
            "How many times the LangChain LLM client retries on 429 / "
            "5xx / timeout. 0 disables retries. Backoff is exponential "
            "and managed by LangChain internally."
        ),
    )
    embedding_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description=(
            "How many times the embedding wrapper retries on transient "
            "failures (429 / 5xx / timeout). Mirrors ``llm_max_retries``."
        ),
    )

    # ---------- API ----------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    app_env: str = "development"
    cors_origins: str = Field(
        default="http://localhost:3000",
        description=(
            "Comma-separated origins allowed by the FastAPI CORS middleware. "
            "Defaults to the local Next.js dev server; set this to the "
            "production Fly.io / Vercel front-end origin at deploy time."
        ),
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Parsed, whitespace-trimmed origin list. ``["*"]`` permits any
        origin and is intended only for local development."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ---------- Deployment / observability (week 11) ----------
    redis_url: str | None = Field(
        default=None,
        description=(
            "When set, the embedding cache routes through Redis instead of "
            "the in-memory ``TTLCache``. Format: "
            "``redis://[:password@]host:port/db`` (or ``rediss://`` for TLS). "
            "Unset (the default) keeps the process-local cache that ships "
            "since week 9."
        ),
    )
    metrics_enabled: bool = Field(
        default=True,
        description=(
            "Whether to expose Prometheus metrics at ``GET /metrics``. "
            "Off in CI / unit tests to keep the registry clean; on in "
            "production so Fly.io and Grafana Cloud can scrape."
        ),
    )

    # ---------- Phase 3.2: deploy-side defence (ADR 0024) ----------
    demo_api_key: str | None = Field(
        default=None,
        description=(
            "Optional shared-secret gate on the LLM-expensive endpoints "
            "(``/ask``, ``/ask/stream``, ``/mcp``, ``/dashboards``, "
            "``/conversations``). When set, requests MUST carry "
            "``X-API-Key: <value>``; missing or wrong key returns 401. "
            "Unset (the default) leaves every endpoint open — fine for "
            "local dev where the chat UI talks to the API on localhost. "
            "REQUIRED for public deploys: without it, a leaked URL means "
            "a leaked DeepSeek bill. The Next.js front-end's Route "
            "Handlers forward the key server-side so browser users never "
            "see it. Generate one with: ``openssl rand -hex 32``."
        ),
    )
    rate_limit_per_minute: int = Field(
        default=30,
        ge=0,
        le=10_000,
        description=(
            "Per-IP request budget for the expensive endpoints in a 60s "
            "sliding window. 30/min is generous for a single human user "
            "and tight enough to cap a runaway script. ``0`` disables "
            "the limit entirely. The limiter runs in-process; if you "
            "scale to multiple replicas you'll get N * the limit, which "
            "is fine — DeepSeek + Fly spending caps are the real backstop."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton settings instance."""
    return Settings()  # type: ignore[call-arg]
