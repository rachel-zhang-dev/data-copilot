"""LangGraph nodes for the semantic layer (Phase 3.1 / ADR 0023).

Two nodes + two routers wire the semantic layer into the existing data
branch:

  coverage_check (ok)
      ↓
  metric_router  ← LLM picks structured spec OR declines
      │
  ┌───┴───┐
  │       │
  │       └→  generate_sql  ← fallback path (existing text-to-SQL)
  │
  metric_resolver  ← compile spec deterministically → state.sql
      ↓
  validate_sql  ← shared with fallback path (LIMIT injection,
                  defense-in-depth even on our own SQL)

Fail-soft on every axis:

* feature flag off  → router emits ``path: fallback`` without an LLM call.
* LLM raises        → fallback.
* JSON parse fails  → fallback.
* Pydantic ResolverSpec invalid → fallback.
* ResolverError in the compiler (unreachable join graph, unknown metric/
  dimension) → ``metric_resolver`` flips ``path: fallback`` and the
  post-resolver router sends control to ``generate_sql``.

The semantic layer is *additive*: in the worst case it costs one extra
LLM call per turn and falls through to exactly the existing pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from copilot.agent.state import AgentState
from copilot.llm import get_llm
from copilot.semantic.models import get_semantic_model
from copilot.semantic.prompts import (
    METRIC_ROUTER_SYSTEM,
    METRIC_ROUTER_USER_TEMPLATE,
    format_menu,
)
from copilot.semantic.resolver import ResolverError, ResolverSpec, compile_sql

log = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)


def _strip_fence(text_: str) -> str:
    return _FENCE_RE.sub("", text_).strip()


def _message_text(msg: Any) -> str:
    content = msg.content
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "") if isinstance(part, dict) else str(part) for part in content
    )


def _fallback_envelope(reason: str) -> dict[str, Any]:
    """Compact envelope when we're declining the semantic path.

    The router writes this verbatim into ``state.semantic``; the
    post-router router consults ``path`` to decide where to go next.
    """
    return {"path": "fallback", "answerable": False, "reason": reason}


# ---------------------------------------------------------------------------
# Node 1 — metric_router
# ---------------------------------------------------------------------------


def metric_router_node(state: AgentState) -> dict[str, Any]:
    """Decide whether this question can be answered by the semantic layer.

    Always returns at least ``{"semantic": {...}}``. On the happy
    "answerable" path the envelope also contains a validated ``spec``
    dict that ``metric_resolver_node`` compiles. On any failure path
    the envelope's ``path`` is ``"fallback"`` so the post-router
    router sends control to ``generate_sql``.

    Cost is only charged when an LLM call actually happened.
    """
    # Lazy flag import — same posture as ``coverage_check``.
    from copilot.agent import feature_flags

    if not getattr(feature_flags, "SEMANTIC_LAYER_ENABLED", True):
        log.info("metric_router: disabled (feature flag) → fallback")
        return {"semantic": _fallback_envelope("semantic layer disabled")}

    try:
        model = get_semantic_model()
    except Exception as exc:
        # Broader than ``(FileNotFoundError, ValueError)`` on purpose:
        # the semantic layer is an optimisation, not the critical path,
        # so *any* loader hiccup (e.g. an unexpected layout that breaks
        # path resolution, or a transient FS error) should degrade to
        # the SQL writer rather than 500 the whole request. The 2026-06
        # outage was an ``IndexError`` from a too-deep ``parents[]``
        # walk that slipped past the old narrow filter.
        log.warning(
            "metric_router: semantic.yml unavailable (%s: %s); fallback",
            type(exc).__name__,
            exc,
        )
        return {"semantic": _fallback_envelope("semantic model unavailable")}

    menu = format_menu(model)
    user_msg = METRIC_ROUTER_USER_TEMPLATE.format(
        metrics=menu["metrics"],
        dimensions=menu["dimensions"],
        question=state["question"],
    )

    try:
        llm = get_llm(
            temperature=0.0,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        response = llm.invoke(
            [
                SystemMessage(content=METRIC_ROUTER_SYSTEM),
                HumanMessage(content=user_msg),
            ]
        )
    except Exception as exc:
        log.warning("metric_router: LLM call failed (%s); fallback", exc)
        return {"semantic": _fallback_envelope("router unavailable")}

    # Cost is charged once the LLM call resolves; circular-import-safe
    # by reusing the helper that lives in ``nodes.py``.
    from copilot.agent.nodes import _llm_cost

    cost = _llm_cost(response, user_msg)
    raw = _message_text(response).strip()

    try:
        payload = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        log.warning("metric_router: JSON parse failed (%s); raw=%r", exc, raw[:200])
        return {
            "semantic": _fallback_envelope("router output unparsable"),
            "cost": cost,
        }

    if not isinstance(payload, dict):
        log.warning("metric_router: top-level not a dict; fallback")
        return {
            "semantic": _fallback_envelope("router output not a JSON object"),
            "cost": cost,
        }

    if not payload.get("answerable", False):
        envelope = _fallback_envelope(
            str(payload.get("reason") or "router declined this question").strip()
        )
        log.info("metric_router: declined → %s", envelope["reason"][:80])
        return {"semantic": envelope, "cost": cost}

    try:
        spec = ResolverSpec.model_validate(
            {
                "metric": payload.get("metric"),
                "dimensions": payload.get("dimensions") or [],
                "time_range": payload.get("time_range"),
                "filters": payload.get("filters") or [],
            }
        )
    except (ValidationError, TypeError) as exc:
        log.warning("metric_router: spec validation failed (%s); fallback", exc)
        return {
            "semantic": _fallback_envelope(f"router spec invalid: {exc}"),
            "cost": cost,
        }

    log.info(
        "metric_router: answerable metric=%s dims=%s",
        spec.metric,
        spec.dimensions,
    )
    return {
        "semantic": {
            "path": "semantic_layer",
            "answerable": True,
            "reason": str(payload.get("reason") or "").strip(),
            "spec": spec.model_dump(),
        },
        "cost": cost,
    }


def route_after_metric_router(state: AgentState) -> str:
    """Pick the next node after ``metric_router_node`` ran.

    * ``path == "semantic_layer"``  → compile via ``metric_resolver``.
    * anything else (fallback / missing) → existing text-to-SQL path.
    """
    semantic = state.get("semantic") or {}
    if semantic.get("path") == "semantic_layer":
        return "metric_resolver"
    return "generate_sql"


# ---------------------------------------------------------------------------
# Node 2 — metric_resolver
# ---------------------------------------------------------------------------


def metric_resolver_node(state: AgentState) -> dict[str, Any]:
    """Compile the router's ``ResolverSpec`` into SQL (deterministic).

    Writes ``state.sql`` on success so the downstream pipeline
    (``validate_sql`` → ``check_risk`` → ``execute_sql`` → ``critique_sql``
    → …) treats the semantic-layer SQL identically to LLM-written SQL.

    On any compile failure (unknown metric, unreachable join graph,
    unsupported time range) we flip ``semantic.path`` to ``fallback``
    and let ``route_after_metric_resolver`` re-route to
    ``generate_sql`` so the LLM gets a clean second chance. The
    spec stays attached on the envelope for diagnostics.
    """
    semantic = state.get("semantic") or {}
    spec_dict = semantic.get("spec")
    if not isinstance(spec_dict, dict):
        log.warning(
            "metric_resolver: state.semantic.spec missing or non-dict; falling back"
        )
        return {
            "semantic": {
                **semantic,
                "path": "fallback",
                "compile_error": "resolver received no spec",
            }
        }

    try:
        spec = ResolverSpec.model_validate(spec_dict)
        model = get_semantic_model()
        sql = compile_sql(model, spec)
    except (ResolverError, ValidationError, FileNotFoundError, ValueError) as exc:
        log.warning(
            "metric_resolver: compile failed (%s); falling back to LLM text-to-SQL",
            exc,
        )
        return {
            "semantic": {
                **semantic,
                "path": "fallback",
                "compile_error": str(exc),
            }
        }

    log.info("metric_resolver: compiled SQL (%d chars)", len(sql))
    return {
        "sql": sql,
        "semantic": {
            **semantic,
            "sql": sql,
        },
    }


def route_after_metric_resolver(state: AgentState) -> str:
    """Pick the next node after ``metric_resolver_node`` ran.

    On a successful compile the SQL lives on ``state.sql`` and we
    continue to ``validate_sql`` (which still injects LIMIT + does
    sqlglot AST sanity-checking — defense in depth even on our own
    SQL). On a compile failure we route back to ``generate_sql`` for
    the LLM to take over.
    """
    semantic = state.get("semantic") or {}
    if semantic.get("path") == "semantic_layer" and state.get("sql"):
        return "validate_sql"
    return "generate_sql"
