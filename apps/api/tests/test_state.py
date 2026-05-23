"""Unit tests for the state module — focus on the custom reducer."""

from __future__ import annotations

from copilot.agent.state import Turn, replace_or_append


def _u(content: str) -> Turn:
    return {"role": "user", "content": content}


def _a(content: str) -> Turn:
    return {"role": "assistant", "content": content}


def test_replace_or_append_appends_by_default() -> None:
    left: list[Turn] = [_u("first")]
    right: list[Turn] = [_a("second")]
    out = replace_or_append(left, right)
    assert out == [_u("first"), _a("second")]


def test_replace_or_append_handles_empty_left() -> None:
    out = replace_or_append([], [_u("hello")])
    assert out == [_u("hello")]


def test_replace_or_append_handles_empty_right() -> None:
    left: list[Turn] = [_u("a"), _a("b")]
    out = replace_or_append(left, [])
    assert out == left


def test_replace_or_append_replace_sentinel_overrides_existing() -> None:
    left: list[Turn] = [_u("old1"), _a("old2"), _u("old3")]
    sentinel: dict[str, list[Turn]] = {"replace": [_a("summary")]}
    out = replace_or_append(left, sentinel)
    assert out == [_a("summary")]


def test_replace_or_append_replace_with_empty_list_clears_field() -> None:
    left: list[Turn] = [_u("old")]
    out = replace_or_append(left, {"replace": []})
    assert out == []


def test_replace_or_append_returns_new_list_not_mutate_input() -> None:
    """Reducers must be pure — LangGraph relies on this for time-travel."""
    left: list[Turn] = [_u("a")]
    right: list[Turn] = [_a("b")]
    out = replace_or_append(left, right)
    assert left == [_u("a")]  # input unchanged
    assert out is not left  # different list object
