"""Schema explorer node (Phase 1.1 / ADR 0016).

Triggered when ``classify_intent_node`` returns ``schema_explore``.
The user is asking "what data do you have?" or "show me the tables";
we render the cached profile as a topic-grouped tour with a handful
of starter questions.

Why a dedicated node instead of reusing ``small_talk``?

* ``small_talk`` is a canned "I'm a SQL assistant"-style reply with no
  schema awareness — it can't tell the user "you have orders going
  back to 1996" because it never reads the DB.
* The explorer's output is structured (topics + sample questions),
  so the front-end can render clickable starter chips. Reusing
  ``small_talk`` would force the FE to parse free-form text.

Failure modes are fail-soft on every axis:
  * Profile table empty   → deterministic listing of table names.
  * LLM error / non-JSON  → same fallback.
  * Profile renders empty → empty headline, empty topic list.

The node always populates ``answer`` because the downstream
``append_to_dialogue`` reads it; ``coverage`` carries the structured
envelope the FE consumes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from copilot.agent.coverage import _coerce_string_list, _llm_cost, _message_text
from copilot.agent.prompts import EXPLORE_SCHEMA_SYSTEM, EXPLORE_SCHEMA_USER_TEMPLATE
from copilot.agent.state import AgentState
from copilot.db import list_tables
from copilot.llm import get_llm
from copilot.profiler import format_profile_for_llm, load_profile

log = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_fence(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def parse_explore_response(raw: str) -> dict[str, Any] | None:
    """Parse one LLM reply into a structured tour. Returns ``None`` on
    any parse / validation failure so the node can fall back.

    Exported for tests.
    """
    if not raw or not raw.strip():
        return None
    cleaned = _strip_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("explore: JSON parse failed (%s)", exc)
        return None
    if not isinstance(payload, dict):
        return None

    headline = str(payload.get("headline", "")).strip()
    if not headline:
        return None

    topics_raw = payload.get("topics")
    if not isinstance(topics_raw, list):
        return None

    topics: list[dict[str, Any]] = []
    for entry in topics_raw[:8]:  # cap at 8 topics for prompt-size sanity
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        summary = str(entry.get("summary", "")).strip()
        tables = _coerce_string_list(entry.get("tables"), max_items=8)
        if not name or not tables:
            continue
        topics.append({"name": name, "summary": summary, "tables": tables})

    samples = _coerce_string_list(payload.get("sample_questions"), max_items=6)

    return {
        "headline": headline,
        "topics": topics,
        "sample_questions": samples,
    }


def _fallback_tour(question: str) -> dict[str, Any]:
    """Deterministic tour used when the LLM is unavailable or unparsable.

    Lists every business table grouped under a single "All tables"
    topic. Not as polished as the LLM-generated tour, but always
    correct and zero-cost — good enough as a fallback.
    """
    try:
        tables = list_tables()
    except Exception as exc:
        log.warning("explore: list_tables fallback failed (%s)", exc)
        tables = []
    headline = (
        f"This database has {len(tables)} tables. Pick one to ask a "
        "question about." if tables else
        "This database has no tables yet. Try seeding it first."
    )
    return {
        "headline": headline,
        "topics": (
            [
                {
                    "name": "All tables",
                    "summary": "Every table in the public schema.",
                    "tables": tables,
                }
            ]
            if tables
            else []
        ),
        "sample_questions": [],
    }


def explore_schema_node(state: AgentState) -> dict[str, Any]:
    """Produce a topic-grouped overview of the database.

    Always returns ``{answer, coverage}`` so the downstream
    ``append_to_dialogue`` and the front-end have a stable contract,
    regardless of LLM availability.

    ``coverage`` carries ``verdict="explore"`` so the FE can pick the
    SchemaTour component on this branch without needing a separate
    state field.
    """
    question = state["question"]

    try:
        all_tables = list_tables()
    except Exception as exc:
        log.warning("explore: list_tables failed (%s)", exc)
        all_tables = []

    profile_text = ""
    if all_tables:
        try:
            by_table = load_profile(all_tables)
            profile_text = format_profile_for_llm(by_table)
        except Exception as exc:
            log.warning("explore: load_profile failed (%s)", exc)

    parsed: dict[str, Any] | None = None
    cost = None

    if profile_text:
        try:
            llm = get_llm(
                temperature=0.2,
                model_kwargs={"response_format": {"type": "json_object"}},
            )
            user_msg = EXPLORE_SCHEMA_USER_TEMPLATE.format(
                profile=profile_text, question=question
            )
            response = llm.invoke(
                [
                    SystemMessage(content=EXPLORE_SCHEMA_SYSTEM),
                    HumanMessage(content=user_msg),
                ]
            )
            raw = _message_text(response).strip()
            cost = _llm_cost(response, user_msg)
            parsed = parse_explore_response(raw)
            if parsed is None:
                log.warning("explore: unparsable reply → fallback. raw=%r", raw[:300])
        except Exception as exc:
            log.warning("explore: LLM call failed (%s); using fallback", exc)

    if parsed is None:
        parsed = _fallback_tour(question)

    log.info(
        "explore: headline=%r topics=%d samples=%d",
        parsed["headline"][:80],
        len(parsed["topics"]),
        len(parsed["sample_questions"]),
    )

    out: dict[str, Any] = {
        "answer": parsed["headline"],
        "coverage": {
            "verdict": "explore",
            "headline": parsed["headline"],
            "topics": parsed["topics"],
            "suggested_questions": parsed["sample_questions"],
            # Keep these to match the coverage envelope shape — empty
            # because there's nothing missing or refused.
            "reason": "",
            "missing_concepts": [],
        },
    }
    if cost is not None:
        out["cost"] = cost
    return out
