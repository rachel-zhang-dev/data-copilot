"""Unit tests for the week-8 ``Insight`` parser.

``parse_insight`` is the single point that defends every downstream
consumer (``state.insight``, ``AskResponse.insight``) from a
misbehaving LLM. The contract is "anything that isn't a valid Insight
returns None"; the tests below pin both halves of that.
"""

from __future__ import annotations

import pytest
from copilot.agent.insight import Insight, parse_insight


def test_parses_minimal_valid_envelope() -> None:
    raw = '{"headline": "91 customers."}'
    out = parse_insight(raw)
    assert isinstance(out, Insight)
    assert out.headline == "91 customers."
    assert out.bullets == []
    assert out.metric_highlights == []


def test_parses_full_envelope() -> None:
    raw = (
        '{"headline": "Germany leads at 11.", '
        '"bullets": ["Followed by France (7)", "Spain trails (3)"], '
        '"metric_highlights": [{"label": "Germany", "value": 11, "format": "integer"}]}'
    )
    out = parse_insight(raw)
    assert out is not None
    assert out.bullets[0].startswith("Followed by France")
    assert out.metric_highlights[0].label == "Germany"
    assert out.metric_highlights[0].value == 11


def test_strips_markdown_fences() -> None:
    raw = '```json\n{"headline": "ok"}\n```'
    out = parse_insight(raw)
    assert out is not None
    assert out.headline == "ok"


def test_returns_none_on_invalid_json() -> None:
    assert parse_insight("not actually json") is None


def test_returns_none_on_missing_required_field() -> None:
    # ``headline`` is required
    assert parse_insight('{"bullets": ["a"]}') is None


def test_returns_none_on_blank_input() -> None:
    assert parse_insight("") is None
    assert parse_insight("   \n  ") is None


def test_rejects_oversized_bullets_list() -> None:
    raw = '{"headline": "x", "bullets": ' + str(["b"] * 50).replace("'", '"') + "}"
    assert parse_insight(raw) is None


@pytest.mark.parametrize("garbage", ["null", "true", "[]", '"just a string"', "42"])
def test_returns_none_on_non_object_root(garbage: str) -> None:
    """A bare value at the root (string, list, number, null) is valid
    JSON but cannot validate against the Insight schema."""
    assert parse_insight(garbage) is None
