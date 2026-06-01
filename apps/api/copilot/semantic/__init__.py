"""Semantic layer package (Phase 3.1 / ADR 0023).

Three modules:

* ``models``    — Pydantic shapes for the YAML + the runtime
                   ``SemanticModel`` object the rest of the code reads.
* ``resolver``  — deterministic compiler: turns a ``ResolverSpec``
                   (a structured ``{metric, dimensions, time, filters}``
                   query the router produced) into PostgreSQL.
* ``prompts``   — LLM prompts the router uses to map natural-language
                   questions to a ``ResolverSpec`` (or to "fallback").

The accompanying LangGraph nodes (``metric_router_node`` +
``metric_resolver_node``) live in ``copilot.agent.semantic_node`` so
the agent module owns all graph wiring.
"""

from copilot.semantic.models import (
    Dimension,
    Metric,
    Relationship,
    SemanticModel,
    get_semantic_model,
    load_semantic_model,
)
from copilot.semantic.resolver import (
    ResolverError,
    ResolverSpec,
    TimeRange,
    compile_sql,
)

__all__ = [
    "Dimension",
    "Metric",
    "Relationship",
    "ResolverError",
    "ResolverSpec",
    "SemanticModel",
    "TimeRange",
    "compile_sql",
    "get_semantic_model",
    "load_semantic_model",
]
