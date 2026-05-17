"""LLM client factory.

DeepSeek exposes an OpenAI-compatible API, so we reuse `langchain-openai`
and just point it at the DeepSeek endpoint. This makes the project
trivially portable to OpenAI / Together / OpenRouter / etc.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from copilot.config import get_settings


def get_llm(temperature: float = 0.0, **kwargs: object) -> ChatOpenAI:
    """Return a configured chat model.

    Args:
        temperature: 0 for deterministic SQL generation, higher for
            creative tasks like summarization or insight generation.
    """
    settings = get_settings()
    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=temperature,
        **kwargs,  # type: ignore[arg-type]
    )
