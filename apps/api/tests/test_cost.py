"""Unit tests for the week-9 cost-accounting helpers.

The reducer is the only piece graph code depends on — pin its
field-wise summation behaviour, the bootstrap-from-None contract,
and the per-source helpers' shapes.
"""

from __future__ import annotations

import pytest
from copilot.cost import (
    UNIT_PRICES_USD_PER_1K,
    add_cost,
    db_explain_cost,
    db_select_cost,
    embedding_call_cost,
    estimate_tokens_from_chars,
    estimate_usd,
    llm_call_cost,
    usage_from_response,
    zero_cost,
)

# ---------------------------------------------------------------------------
# add_cost reducer
# ---------------------------------------------------------------------------


def test_add_cost_sums_known_fields() -> None:
    a = {"llm_calls": 1, "est_tokens_in": 100, "est_usd": 0.001}
    b = {"llm_calls": 2, "est_tokens_in": 50, "est_tokens_out": 10, "est_usd": 0.0007}
    out = add_cost(a, b)  # type: ignore[arg-type]
    assert out["llm_calls"] == 3
    assert out["est_tokens_in"] == 150
    assert out["est_tokens_out"] == 10
    assert abs(out["est_usd"] - 0.0017) < 1e-9


def test_add_cost_handles_none_inputs() -> None:
    """When the state hasn't been touched yet, ``state.get('cost')`` is
    None; the reducer must treat that as zero, not error."""
    out = add_cost(None, {"llm_calls": 1})  # type: ignore[arg-type]
    assert out["llm_calls"] == 1
    out2 = add_cost(None, None)
    assert out2["llm_calls"] == 0
    assert out2["est_usd"] == 0.0


def test_add_cost_returns_fully_populated_dict() -> None:
    out = add_cost({}, {})
    for field in (
        "llm_calls",
        "embedding_calls",
        "db_explain_calls",
        "db_select_calls",
        "est_tokens_in",
        "est_tokens_out",
        "est_usd",
    ):
        assert field in out


# ---------------------------------------------------------------------------
# Per-source increments
# ---------------------------------------------------------------------------


def test_llm_call_cost_counts_one_call() -> None:
    c = llm_call_cost("deepseek-chat", tokens_in=1000, tokens_out=500)
    assert c["llm_calls"] == 1
    assert c["est_tokens_in"] == 1000
    assert c["est_tokens_out"] == 500
    # 1k in * $0.00014 + 0.5k out * $0.00028 = $0.00014 + $0.00014 = $0.00028
    assert abs(c["est_usd"] - 0.00028) < 1e-9


def test_embedding_call_cost_has_no_output_tokens() -> None:
    c = embedding_call_cost("BAAI/bge-m3", tokens_in=12)
    assert c["embedding_calls"] == 1
    assert c["est_tokens_in"] == 12
    # No tokens_out for embeddings
    assert "est_tokens_out" not in c


def test_db_costs_are_counter_only() -> None:
    assert db_explain_cost() == {"db_explain_calls": 1}
    assert db_select_cost() == {"db_select_calls": 1}


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_estimate_usd_known_model() -> None:
    # deepseek-chat: $0.00014 / 1k in, $0.00028 / 1k out
    usd = estimate_usd("deepseek-chat", tokens_in=2000, tokens_out=1000)
    expected = (2 * 0.00014) + (1 * 0.00028)
    assert abs(usd - expected) < 1e-9


def test_estimate_usd_unknown_model_uses_fallback() -> None:
    """An unrecognised model lands on the conservative fallback price,
    which is deliberately ~10x DeepSeek — better to overestimate."""
    usd = estimate_usd("not-a-real-model", tokens_in=1000, tokens_out=1000)
    assert usd >= 0.001  # at least 1k tokens * fallback rate


def test_known_models_present_in_table() -> None:
    """Sanity: regenerated price tables shouldn't accidentally drop
    the models we actually run against."""
    assert "deepseek-chat" in UNIT_PRICES_USD_PER_1K
    assert "BAAI/bge-m3" in UNIT_PRICES_USD_PER_1K


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, meta: dict | None) -> None:
        self.response_metadata = meta or {}


def test_usage_from_response_reads_openai_keys() -> None:
    r = _FakeResp({"token_usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    assert usage_from_response(r) == (10, 20)


def test_usage_from_response_reads_alt_keys() -> None:
    r = _FakeResp({"token_usage": {"input_tokens": 5, "output_tokens": 7}})
    assert usage_from_response(r) == (5, 7)


def test_usage_from_response_returns_none_when_missing() -> None:
    assert usage_from_response(_FakeResp({})) is None
    assert usage_from_response(_FakeResp(None)) is None


def test_usage_from_response_returns_none_on_partial_payload() -> None:
    """Only one of in / out present should bail to None so the caller
    falls through to the chars/4 estimator."""
    r = _FakeResp({"token_usage": {"prompt_tokens": 10}})
    assert usage_from_response(r) is None


@pytest.mark.parametrize("text,tokens", [("", 0), ("abc", 0), ("abcd", 1), ("abcdefgh", 2)])
def test_estimate_tokens_from_chars(text: str, tokens: int) -> None:
    assert estimate_tokens_from_chars(text) == tokens


def test_zero_cost_has_all_fields() -> None:
    z = zero_cost()
    assert all(z.get(k) == 0 or z.get(k) == 0.0 for k in z)
