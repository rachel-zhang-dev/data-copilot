"""Shared pytest fixtures.

This file is auto-discovered by pytest. Anything defined here is
visible to every test file in the ``tests/`` package without needing
an explicit import.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

# Provide harmless defaults for any required setting **before** the
# config module is imported anywhere else. Without this, importing
# ``copilot.config`` during test collection blows up because
# ``DATABASE_URL`` is required (it became required in week 2).
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key-not-used")
os.environ.setdefault("SILICONFLOW_API_KEY", "test-key-not-used")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://copilot:copilot_dev_pwd@localhost:5432/northwind",
)
os.environ.setdefault("LANGSMITH_TRACING", "false")


class StubMessage:
    """Minimal stand-in for ``langchain_core.messages.AIMessage`` used in
    mocks. We only ever read ``.content``, so a tiny class is enough and
    avoids importing the real one in test scope."""

    def __init__(self, content: str) -> None:
        self.content = content


class StubLLM:
    """Records every invocation and returns a queued response.

    Tests construct one of these, hand it to ``monkeypatch`` so
    ``copilot.llm.get_llm`` returns it, and then assert on
    ``.calls`` afterwards.
    """

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses) or [""]
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> StubMessage:
        self.calls.append(messages)
        # Cycle through responses; the last one repeats indefinitely so
        # tests that share an LLM across many calls do not need to count
        # them precisely.
        if len(self.calls) <= len(self._responses):
            return StubMessage(self._responses[len(self.calls) - 1])
        return StubMessage(self._responses[-1])


@pytest.fixture()
def stub_llm_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[StubLLM]]:
    """Return a factory tests use to install a ``StubLLM``.

    Usage::

        def test_x(stub_llm_factory):
            llm = stub_llm_factory("SELECT 1")
            ...

    The factory patches ``copilot.llm.get_llm`` AND the symbol re-exported
    via ``copilot.agent.nodes.get_llm`` (Python imports it by name, so
    monkeypatching only the original module would not reach the node).
    """
    installed: list[StubLLM] = []

    def _factory(*responses: str) -> StubLLM:
        llm = StubLLM(*responses)
        monkeypatch.setattr("copilot.llm.get_llm", lambda *_a, **_k: llm)
        monkeypatch.setattr("copilot.agent.nodes.get_llm", lambda *_a, **_k: llm)
        installed.append(llm)
        return llm

    yield _factory  # type: ignore[misc]
