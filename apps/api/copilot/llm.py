"""LLM client factory.

DeepSeek exposes an OpenAI-compatible HTTP API: same URL shape, same
JSON payloads, same auth header style. So instead of writing a custom
DeepSeek client, we reuse ``langchain-openai`` and just point it at the
DeepSeek base URL.

The big win is portability — switching to OpenAI, Together, OpenRouter,
or any other OpenAI-compatible provider becomes a one-line change in
``.env``::

    DEEPSEEK_BASE_URL=https://api.openai.com/v1
    DEEPSEEK_MODEL=gpt-4o-mini

This also means our LangGraph nodes never know which provider they are
talking to, which keeps the agent code clean.
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from copilot.config import get_settings


def get_llm(temperature: float = 0.0, **kwargs: object) -> ChatOpenAI:
    """Return a configured chat model.

    Args:
        temperature: 0.0 for deterministic SQL generation; raise it for
            tasks that benefit from variety (summarisation, insight
            generation, brainstorming).
        **kwargs: any extra arg accepted by ``ChatOpenAI``
            (``max_tokens``, ``timeout``, ``streaming``, …).
    """
    settings = get_settings()
    return ChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=temperature,
        **kwargs,  # type: ignore[arg-type]
    )
