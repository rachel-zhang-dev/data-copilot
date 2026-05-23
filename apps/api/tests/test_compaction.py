"""Unit tests for compaction.py: the dialogue summarisation node."""

from __future__ import annotations

import pytest
from copilot.agent import compaction
from copilot.agent.compaction import compact_history_node, count_tokens


def _u(content: str) -> dict:
    return {"role": "user", "content": content}


def _a(content: str, sql: str | None = None) -> dict:
    out: dict = {"role": "assistant", "content": content}
    if sql:
        out["sql"] = sql
    return out


# ---------------------------------------------------------------------------
# count_tokens
# ---------------------------------------------------------------------------


def test_count_tokens_empty() -> None:
    assert count_tokens([]) == 0


def test_count_tokens_grows_with_content() -> None:
    short = count_tokens([_u("hi")])
    long = count_tokens([_u("hi" * 200)])
    assert long > short


def test_count_tokens_includes_sql_field() -> None:
    no_sql = count_tokens([_a("answer")])
    with_sql = count_tokens([_a("answer", sql="SELECT 1 FROM very_long_table_name_xyz")])
    assert with_sql > no_sql


# ---------------------------------------------------------------------------
# compact_history_node
# ---------------------------------------------------------------------------


def test_compact_no_op_when_under_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPACTION_THRESHOLD_TOKENS", "10000")
    monkeypatch.setenv("COMPACTION_KEEP_LAST_N", "6")
    from copilot.config import get_settings

    get_settings.cache_clear()

    state = {"dialogue": [_u("Q1"), _a("A1")]}
    out = compact_history_node(state)
    assert out == {}  # no-op


def test_compact_no_op_when_dialogue_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """If we have fewer turns than keep_last_n, nothing to compact —
    even if the threshold is crossed by a single very-long turn."""
    monkeypatch.setenv("COMPACTION_THRESHOLD_TOKENS", "10")
    monkeypatch.setenv("COMPACTION_KEEP_LAST_N", "6")
    from copilot.config import get_settings

    get_settings.cache_clear()

    huge = "x" * 10_000
    state = {"dialogue": [_u(huge), _a(huge)]}
    out = compact_history_node(state)
    assert out == {}


def test_compact_summarises_older_turns_when_over_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPACTION_THRESHOLD_TOKENS", "20")
    monkeypatch.setenv("COMPACTION_KEEP_LAST_N", "2")
    from copilot.config import get_settings

    get_settings.cache_clear()

    # Simulate a fake LLM that returns a deterministic summary
    class FakeLLM:
        def invoke(self, _msgs):
            class Resp:
                content = "User asked about products and customers."

            return Resp()

    monkeypatch.setattr(compaction, "get_llm", lambda *a, **k: FakeLLM())

    dialogue = []
    for i in range(5):
        dialogue.append(_u(f"Question {i} with some padding"))
        dialogue.append(_a(f"Answer {i} with some padding"))

    state = {"dialogue": dialogue}
    out = compact_history_node(state)

    assert "dialogue" in out
    assert "replace" in out["dialogue"]
    new_dialogue = out["dialogue"]["replace"]

    # 1 summary + last 2 (keep_last_n) = 3 total
    assert len(new_dialogue) == 3
    assert new_dialogue[0]["role"] == "assistant"
    assert "Earlier in this conversation" in new_dialogue[0]["content"]
    assert "products" in new_dialogue[0]["content"]
    # Last two are the most recent verbatim turns
    assert new_dialogue[-1] == dialogue[-1]
    assert new_dialogue[-2] == dialogue[-2]


def test_compact_falls_back_to_truncation_when_llm_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPACTION_THRESHOLD_TOKENS", "20")
    monkeypatch.setenv("COMPACTION_KEEP_LAST_N", "2")
    from copilot.config import get_settings

    get_settings.cache_clear()

    class FlakyLLM:
        def invoke(self, _msgs):
            raise ConnectionError("boom")

    monkeypatch.setattr(compaction, "get_llm", lambda *a, **k: FlakyLLM())

    dialogue = []
    for i in range(5):
        dialogue.append(_u(f"Question {i}" * 10))
        dialogue.append(_a(f"Answer {i}" * 10))

    state = {"dialogue": dialogue}
    out = compact_history_node(state)

    new_dialogue = out["dialogue"]["replace"]
    # No summary turn (LLM failed); just the last 2 verbatim
    assert len(new_dialogue) == 2
    assert all("Earlier in this conversation" not in t["content"] for t in new_dialogue)
