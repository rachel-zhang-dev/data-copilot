"""Conversation history compaction (week 5).

When a long conversation makes the dialogue list bigger than
``compaction_threshold_tokens``, this node summarises the older turns
into a single synthetic ``assistant`` ``Turn`` and replaces the list
with ``[summary, *last_N_verbatim]``.

The replacement is delivered through the ``replace_or_append`` reducer
on the ``dialogue`` field; we return ``{"dialogue": {"replace": ...}}``
which signals "overwrite the field" rather than the default append.

Token counting
--------------
We use a cheap heuristic (chars / 4) instead of a real tokenizer.
Reasons:

* Avoids dragging in ``tiktoken`` for one feature.
* Compaction trigger doesn't need to be exact — it is purely a budget.
* Errors lean conservatively (chars/4 slightly *overestimates* tokens
  for English, slightly *underestimates* for Chinese), and the threshold
  has a 16x safety margin to DeepSeek's 64k context window.

If profiling later shows the heuristic is too coarse, swap in
``tiktoken.encoding_for_model("cl100k_base")`` here without any
caller changes.

Failure handling
----------------
The summariser calls the LLM. If that call fails (network blip,
provider 5xx) we fall back to a hard truncation: drop everything
except the last N turns. The user-facing experience degrades a bit
(early context is lost without explanation) but the agent does not
break, which is the goal.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from copilot.agent.prompts import COMPACTION_SYSTEM, COMPACTION_USER_TEMPLATE
from copilot.agent.state import AgentState, Turn
from copilot.config import get_settings
from copilot.cost import (
    CostBreakdown,
    estimate_tokens_from_chars,
    llm_call_cost,
    usage_from_response,
)
from copilot.llm import get_llm

log = logging.getLogger(__name__)


_AVG_CHARS_PER_TOKEN = 4
"""Heuristic. English averages 4 chars/token in BPE; CJK is closer to
1-1.5 chars/token. The threshold is set generously enough that the
discrepancy does not matter."""


def count_tokens(dialogue: list[Turn]) -> int:
    """Return a rough estimate of how many tokens ``dialogue`` will
    consume when fed to a chat model. See module docstring for why
    we don't run a proper tokenizer here.
    """
    total_chars = 0
    for turn in dialogue:
        total_chars += len(turn.get("content", ""))
        total_chars += len(turn.get("sql", "") or "")
        # role label, separators
        total_chars += 12
    return total_chars // _AVG_CHARS_PER_TOKEN


def _format_old_turns(turns: list[Turn]) -> str:
    """Render the to-be-summarised turns into a plain-text block."""
    lines = []
    for t in turns:
        prefix = "User:" if t["role"] == "user" else "Assistant:"
        body = t.get("content", "").strip()
        sql = t.get("sql")
        if sql:
            body = f"{body}\n  (SQL: {sql})"
        lines.append(f"{prefix} {body}")
    return "\n\n".join(lines)


def compact_history_node(state: AgentState) -> dict[str, Any]:
    """Summarise old turns when the dialogue exceeds the token budget.

    Runs at the very end of every turn (after ``append_to_dialogue``).
    Idempotent: a second call on already-compacted state is cheap and
    a no-op.
    """
    dialogue = state.get("dialogue") or []
    settings = get_settings()
    threshold = settings.compaction_threshold_tokens
    keep_n = settings.compaction_keep_last_n

    tokens = count_tokens(dialogue)
    if tokens <= threshold:
        return {}  # no-op; LangGraph treats empty dict as "no changes"

    # We keep the last ``keep_n`` turns verbatim. Anything older is
    # eligible for summarisation. If there's nothing to summarise (the
    # threshold was crossed by very long turns alone) we have to fall
    # back to truncation.
    if len(dialogue) <= keep_n:
        log.warning(
            "compaction skipped: dialogue has %d turns (<= keep_n=%d) but "
            "exceeds %d tokens. Recent turns are too verbose to compact.",
            len(dialogue),
            keep_n,
            threshold,
        )
        return {}

    older = dialogue[:-keep_n]
    recent = dialogue[-keep_n:]

    log.info(
        "compaction trigger: %d tokens > %d threshold; summarising %d "
        "older turns, keeping last %d verbatim",
        tokens,
        threshold,
        len(older),
        len(recent),
    )

    try:
        summary, compaction_cost = _summarise_with_llm(older)
    except Exception as exc:
        log.warning("compaction LLM failed (%s); hard-truncating instead", exc)
        return {"dialogue": {"replace": list(recent)}}

    summary_turn: Turn = {
        "role": "assistant",
        "content": f"[Earlier in this conversation] {summary}",
    }
    return {
        "dialogue": {"replace": [summary_turn, *recent]},
        "cost": compaction_cost,
    }


def _summarise_with_llm(older: list[Turn]) -> tuple[str, CostBreakdown]:
    """Ask the LLM to summarise the older turns into a brief paragraph.

    Returns ``(summary_text, cost_increment)`` so the caller can fold
    the LLM call into the cumulative ``state.cost`` even on the
    compaction path (which would otherwise be invisible to operators).
    """
    user_msg = COMPACTION_USER_TEMPLATE.format(turns=_format_old_turns(older))
    llm = get_llm(temperature=0.2, max_tokens=400)
    response = llm.invoke(
        [
            SystemMessage(content=COMPACTION_SYSTEM),
            HumanMessage(content=user_msg),
        ]
    )
    content = response.content
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    summary = str(content).strip()

    model = get_settings().deepseek_model
    usage = usage_from_response(response)
    if usage is not None:
        tokens_in, tokens_out = usage
    else:
        tokens_in = estimate_tokens_from_chars(user_msg)
        tokens_out = estimate_tokens_from_chars(summary)
    cost = llm_call_cost(model, tokens_in=tokens_in, tokens_out=tokens_out)
    return summary, cost
