"""Per-turn cost accounting (week 9).

Every node that talks to a paid resource — the LLM, the embedding
provider, the database — emits a ``CostBreakdown`` increment into
``state.cost``. LangGraph's reducer adds them up so the final value
on ``AskResponse.cost`` is the true cost of a turn (across self-
heals, HITL pauses, the lot).

Two design choices worth flagging:

* **TypedDict, not Pydantic.** ``state.cost`` is serialised into the
  checkpoint and shipped over HTTP; ``TypedDict`` keeps the wire
  format trivially JSON-compatible without a model_dump step. The
  ``add_cost`` reducer enforces the schema by construction.

* **Heuristic USD per-token unit prices.** The exact bill ships in a
  monthly invoice; what the agent needs to surface is "roughly how
  much did this run cost" to within an order of magnitude, so the
  pitch in the README is concrete rather than vague. The
  ``UNIT_PRICES`` table is hand-maintained; missing entries fall
  through to a conservative default that overestimates rather than
  understates.
"""

from __future__ import annotations

import logging
from typing import TypedDict

log = logging.getLogger(__name__)


class CostBreakdown(TypedDict, total=False):
    """The cost components of one turn (or, after the reducer runs,
    of one cumulative thread).

    ``total=False`` so an increment from a single node only has to set
    the fields it actually moves; the reducer fills in zeros.
    """

    llm_calls: int
    embedding_calls: int
    db_explain_calls: int
    db_select_calls: int
    est_tokens_in: int
    est_tokens_out: int
    est_usd: float


_FIELDS: tuple[str, ...] = (
    "llm_calls",
    "embedding_calls",
    "db_explain_calls",
    "db_select_calls",
    "est_tokens_in",
    "est_tokens_out",
    "est_usd",
)


def zero_cost() -> CostBreakdown:
    """Return an all-zero ``CostBreakdown`` (every field present).

    The reducer uses this as the seed when a node emits its first
    increment; downstream consumers (``AskResponse``, CLI, eval
    grader) always see a fully-populated dict so they never have to
    ``.get(field, 0)``.
    """
    return CostBreakdown(
        llm_calls=0,
        embedding_calls=0,
        db_explain_calls=0,
        db_select_calls=0,
        est_tokens_in=0,
        est_tokens_out=0,
        est_usd=0.0,
    )


def add_cost(left: CostBreakdown | None, right: CostBreakdown | None) -> CostBreakdown:
    """Field-wise sum of two ``CostBreakdown`` increments.

    Bound to the ``cost`` field in ``AgentState`` so successive node
    contributions accumulate inside one turn (and across turns, since
    LangGraph persists the merged value through the checkpointer).

    Either argument may be missing entirely; we treat that as zero
    rather than raising, which lets new state objects bootstrap
    cleanly the first time a node writes to ``cost``.
    """
    out = zero_cost()
    for src in (left, right):
        if not src:
            continue
        for f in _FIELDS:
            if f in src:
                out[f] = out[f] + src[f]  # type: ignore[literal-required]
    return out


# ---------------------------------------------------------------------------
# Per-token USD pricing
# ---------------------------------------------------------------------------
#
# Values are best-effort, in USD per **1 K** tokens for chat models and
# per **1 K** tokens for embedding models. Prices change; this table
# overestimates by design so nobody is surprised by their invoice.
#
# Sources (as of 2026-Q1):
#   * DeepSeek-chat:        ~$0.14 / 1M input,  $0.28 / 1M output
#   * BAAI/bge-m3 (SF free): $0 (free tier; charged as $0.05 / 1M as a buffer)
#   * GPT-4o-mini:          $0.15 / 1M in,      $0.60 / 1M out

UNIT_PRICES_USD_PER_1K: dict[str, dict[str, float]] = {
    "deepseek-chat": {"in": 0.00014, "out": 0.00028},
    "deepseek-reasoner": {"in": 0.00055, "out": 0.0022},
    "gpt-4o-mini": {"in": 0.00015, "out": 0.0006},
    "gpt-4o": {"in": 0.0025, "out": 0.01},
    "BAAI/bge-m3": {"in": 0.00005, "out": 0.0},
}
_FALLBACK_PRICE: dict[str, float] = {"in": 0.001, "out": 0.001}
"""Used when the configured model name is absent from the table —
deliberately ~10x more expensive than DeepSeek so the resulting
estimate is a safe upper bound."""


def estimate_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Project a token count onto USD using ``UNIT_PRICES_USD_PER_1K``."""
    price = UNIT_PRICES_USD_PER_1K.get(model, _FALLBACK_PRICE)
    return (tokens_in / 1000.0) * price["in"] + (tokens_out / 1000.0) * price["out"]


# ---------------------------------------------------------------------------
# Increment helpers — used by graph nodes to publish their cost contribution
# ---------------------------------------------------------------------------


def llm_call_cost(
    model: str,
    *,
    tokens_in: int,
    tokens_out: int,
    n_calls: int = 1,
) -> CostBreakdown:
    """Build one LLM-call increment.

    Callers pass real token counts when ``response.response_metadata``
    populates them (DeepSeek does), otherwise a ``chars/4`` estimate.
    """
    return CostBreakdown(
        llm_calls=n_calls,
        est_tokens_in=tokens_in,
        est_tokens_out=tokens_out,
        est_usd=estimate_usd(model, tokens_in, tokens_out),
    )


def embedding_call_cost(model: str, *, tokens_in: int) -> CostBreakdown:
    """Build one embedding-call increment. ``tokens_out`` is always 0
    for embeddings."""
    return CostBreakdown(
        embedding_calls=1,
        est_tokens_in=tokens_in,
        est_usd=estimate_usd(model, tokens_in, 0),
    )


def db_explain_cost() -> CostBreakdown:
    """One Postgres ``EXPLAIN`` call. Free in dollar terms — we record
    the count for observability."""
    return CostBreakdown(db_explain_calls=1)


def db_select_cost() -> CostBreakdown:
    """One Postgres ``SELECT`` execution. Same observability story."""
    return CostBreakdown(db_select_calls=1)


# ---------------------------------------------------------------------------
# Token estimation helpers
# ---------------------------------------------------------------------------


def usage_from_response(response: object) -> tuple[int, int] | None:
    """Best-effort extraction of ``(tokens_in, tokens_out)`` from a
    LangChain ``AIMessage``.

    DeepSeek and OpenAI both populate ``response_metadata["token_usage"]``;
    some smaller providers don't. The caller falls back to ``chars/4``
    when this returns ``None`` so the cost reducer always advances.
    """
    meta = getattr(response, "response_metadata", None) or {}
    usage = meta.get("token_usage") if isinstance(meta, dict) else None
    if not isinstance(usage, dict):
        return None
    in_keys = ("prompt_tokens", "input_tokens", "promptTokens")
    out_keys = ("completion_tokens", "output_tokens", "completionTokens")
    tokens_in = next((int(usage[k]) for k in in_keys if k in usage), None)
    tokens_out = next((int(usage[k]) for k in out_keys if k in usage), None)
    if tokens_in is None or tokens_out is None:
        return None
    return tokens_in, tokens_out


def estimate_tokens_from_chars(text: str) -> int:
    """Rough ``chars / 4`` heuristic used when the provider doesn't
    report real usage. Off by ~2x for CJK-heavy strings but trends are
    preserved, which is what the eval / cost panel needs."""
    return max(0, len(text) // 4)
