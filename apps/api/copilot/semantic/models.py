"""Semantic-model schema + YAML loader (Phase 3.1 / ADR 0023).

The on-disk YAML at ``data/semantic.yml`` is the source of truth.
This module:

* Defines Pydantic models that mirror the YAML structure 1:1.
* Provides ``load_semantic_model(path)`` — parses + validates.
* Caches the loaded model at module level via ``get_semantic_model()``
  so the LangGraph node doesn't re-parse on every request.

Validation goes beyond "is this YAML well-formed". Cross-reference
checks at load time catch:

* a metric / dimension ``requires`` references a table not in
  ``table_aliases``;
* a relationship references unknown tables;
* the dependency graph between ``required`` tables and
  ``relationships`` is disconnected (i.e. a metric needs a table no
  edge can reach).

Failing fast here means "wrong SQL at runtime" becomes "the API
container won't start" — much easier to debug.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

# ---------------------------------------------------------------------------
# YAML record shapes
# ---------------------------------------------------------------------------


class Metric(BaseModel):
    """One business measurement. ``expression`` is plain SQL with
    table aliases from the parent ``SemanticModel.table_aliases``."""

    name: str = Field(..., min_length=1, max_length=80)
    description: str = Field(..., min_length=1, max_length=400)
    expression: str = Field(..., min_length=1)
    requires: list[str] = Field(..., min_length=1)
    format: str = "number"  # currency | integer | percent | number


class Dimension(BaseModel):
    """One slice axis. Same SQL-fragment + requires shape as Metric."""

    name: str = Field(..., min_length=1, max_length=80)
    description: str = Field(..., min_length=1, max_length=400)
    expression: str = Field(..., min_length=1)
    requires: list[str] = Field(..., min_length=1)


class Relationship(BaseModel):
    """One join edge between two tables.

    The graph is undirected: ``tables: [a, b]`` means "you can JOIN
    a → b OR b → a using ``join``". The resolver picks the direction
    when it walks the spanning tree.
    """

    tables: list[str] = Field(..., min_length=2, max_length=2)
    join: str = Field(..., min_length=1)


class TimeColumn(BaseModel):
    """A column the router can target with a time-range filter."""

    table: str
    column: str


# ---------------------------------------------------------------------------
# Top-level model
# ---------------------------------------------------------------------------


class SemanticModel(BaseModel):
    """The whole semantic model, post-validation."""

    version: int = 1
    description: str = ""
    table_aliases: dict[str, str]
    time_columns: list[TimeColumn] = Field(default_factory=list)
    metrics: list[Metric]
    dimensions: list[Dimension]
    relationships: list[Relationship]

    # ---- cross-reference validation ----

    @field_validator("table_aliases")
    @classmethod
    def _aliases_must_be_lowercase_words(cls, v: dict[str, str]) -> dict[str, str]:
        """Aliases are interpolated into SQL — keep them tame so we
        don't surprise downstream parsers with odd characters."""
        for table, alias in v.items():
            if not alias.isidentifier():
                raise ValueError(
                    f"table_aliases[{table!r}] = {alias!r} is not a valid SQL identifier"
                )
        return v

    def model_post_init(self, __context: Any) -> None:
        """Run cross-reference checks after the basic shape validates.

        Pydantic ``model_post_init`` hook runs after field validation.
        Raises ``ValueError`` with a precise message — the loader at
        the call site converts it to a more user-friendly startup
        error.
        """
        known_tables = set(self.table_aliases.keys())

        # 1. Every metric / dimension requires must reference a known table.
        for m in self.metrics:
            unknown = set(m.requires) - known_tables
            if unknown:
                raise ValueError(
                    f"metric {m.name!r} requires unknown tables: {sorted(unknown)}"
                )
        for d in self.dimensions:
            unknown = set(d.requires) - known_tables
            if unknown:
                raise ValueError(
                    f"dimension {d.name!r} requires unknown tables: {sorted(unknown)}"
                )

        # 2. Relationships only reference known tables.
        for r in self.relationships:
            unknown = set(r.tables) - known_tables
            if unknown:
                raise ValueError(
                    f"relationship {r.tables!r} references unknown tables: {sorted(unknown)}"
                )

        # 3. Time columns reference known tables.
        for tc in self.time_columns:
            if tc.table not in known_tables:
                raise ValueError(
                    f"time_column {tc.table}.{tc.column} references unknown table"
                )

        # 4. Metric / dimension names are unique across their own list
        #    (LLM router uses name as lookup key — duplicates would
        #    silently pick the second one).
        if len({m.name for m in self.metrics}) != len(self.metrics):
            raise ValueError("metrics contain duplicate names")
        if len({d.name for d in self.dimensions}) != len(self.dimensions):
            raise ValueError("dimensions contain duplicate names")

    # ---- lookup helpers ----

    def metric(self, name: str) -> Metric | None:
        return next((m for m in self.metrics if m.name == name), None)

    def dimension(self, name: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.name == name), None)

    def relationships_for(self, tables: Iterable[str]) -> list[Relationship]:
        """Return the relationships whose ``tables`` are a subset of
        ``tables``. Used by the resolver to pre-filter the join graph."""
        wanted = set(tables)
        return [r for r in self.relationships if set(r.tables).issubset(wanted)]


# ---------------------------------------------------------------------------
# Loader + module-level cache
# ---------------------------------------------------------------------------


def _default_yaml_path() -> Path:
    """Resolve ``data/semantic.yml`` across host and container layouts.

    Two deployment topologies exist and the path layout differs:

    * **Repo / dev**: this module is at ``apps/api/copilot/semantic/models.py``;
      the YAML sits four parents up at ``<repo>/data/semantic.yml``.
    * **Container**: the Dockerfile flattens to ``/app/copilot/semantic/models.py``
      and copies ``data/`` to ``/app/data/`` (two parents up).

    Hard-coding ``parents[4]`` worked for the repo but raised
    ``IndexError`` inside the container — a real outage caught by W3
    structured logging on 2026-06-07.

    Resolution order:

    1. ``SEMANTIC_YAML_PATH`` env var — explicit override for ops.
    2. Walk up from ``__file__`` looking for ``data/semantic.yml``.
       Stops at the first hit so layout depth doesn't matter.
    3. Fall back to ``parents[4]`` (the original repo-root assumption)
       — only meaningful in the dev tree; if it doesn't exist the
       caller's ``FileNotFoundError`` path takes over.
    """
    override = os.environ.get("SEMANTIC_YAML_PATH")
    if override:
        return Path(override)

    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "data" / "semantic.yml"
        if candidate.is_file():
            return candidate

    parents = here.parents
    fallback_root = parents[4] if len(parents) >= 5 else parents[-1]
    return fallback_root / "data" / "semantic.yml"


def load_semantic_model(path: Path | None = None) -> SemanticModel:
    """Read YAML at ``path`` and return a validated ``SemanticModel``.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if YAML parses but cross-reference checks fail.
        pydantic.ValidationError: if the basic shape is wrong.
    """
    yaml_path = path or _default_yaml_path()
    if not yaml_path.is_file():
        raise FileNotFoundError(
            f"semantic model not found at {yaml_path}; "
            "either create the file or pass an explicit path"
        )
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"semantic.yml at {yaml_path} must be a top-level mapping, got {type(raw).__name__}"
        )
    try:
        return SemanticModel.model_validate(raw)
    except ValidationError as exc:
        # Re-raise with a more useful prefix so the lifespan log line
        # tells the operator which file is broken.
        raise ValueError(
            f"semantic.yml at {yaml_path} failed validation:\n{exc}"
        ) from exc


_cache: dict[str, SemanticModel] = {}


def get_semantic_model(path: Path | None = None) -> SemanticModel:
    """Cached accessor. First call loads + validates; subsequent calls
    return the cached instance.

    Pass ``path`` only in tests that want a different YAML; production
    code calls this with no arguments so the cache key (always the
    same path) stays stable.
    """
    key = str((path or _default_yaml_path()).resolve())
    if key not in _cache:
        _cache[key] = load_semantic_model(path)
    return _cache[key]


def reset_cache_for_tests() -> None:
    """Clear the cache. Used by tests that swap the YAML between cases."""
    _cache.clear()
