"""Application configuration loaded from environment variables.

We use pydantic-settings so config is type-checked, documented, and
fails fast at startup if required values are missing.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- LLM ----------
    deepseek_api_key: str = Field(..., description="DeepSeek API key")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # ---------- Embeddings ----------
    dashscope_api_key: str = Field(..., description="Alibaba DashScope API key")
    embedding_model: str = "text-embedding-v3"

    # ---------- Observability ----------
    langsmith_api_key: str | None = None
    langsmith_tracing: bool = True
    langsmith_project: str = "data-copilot-dev"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # ---------- Database ----------
    database_url: str = Field(..., description="PostgreSQL connection string")

    # ---------- API ----------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    app_env: str = "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton settings instance."""
    return Settings()  # type: ignore[call-arg]
