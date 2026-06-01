"""Unit tests for the semantic-layer YAML loader (Phase 3.1 / ADR 0023).

We verify:

* The shipped ``data/semantic.yml`` loads cleanly. If this regresses,
  the API container won't even start — fail loud in CI before deploy.
* Cross-reference checks reject every category of bad YAML:
  unknown table in ``requires``, unknown table in a relationship,
  unknown table on a time column, duplicate metric / dimension names.
* Caching works (``get_semantic_model`` returns the same instance).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from copilot.semantic.models import (
    SemanticModel,
    get_semantic_model,
    load_semantic_model,
    reset_cache_for_tests,
)

# ---------------------------------------------------------------------------
# Real YAML
# ---------------------------------------------------------------------------


def test_shipped_semantic_yml_loads() -> None:
    """The YAML committed at ``data/semantic.yml`` MUST parse + cross-
    reference validate. Any change that breaks this also breaks API
    startup, so the test is the canonical smoke check."""
    model = load_semantic_model()
    assert model.version == 1
    assert {m.name for m in model.metrics} >= {
        "revenue",
        "order_count",
        "customer_count",
    }
    assert {d.name for d in model.dimensions} >= {"country", "category", "month"}
    # Aliases must cover every table referenced anywhere.
    referenced: set[str] = set()
    for m in model.metrics:
        referenced |= set(m.requires)
    for d in model.dimensions:
        referenced |= set(d.requires)
    for r in model.relationships:
        referenced |= set(r.tables)
    for tc in model.time_columns:
        referenced.add(tc.table)
    assert referenced.issubset(model.table_aliases.keys())


def test_get_semantic_model_caches() -> None:
    reset_cache_for_tests()
    a = get_semantic_model()
    b = get_semantic_model()
    assert a is b  # exact same object — no re-parse


# ---------------------------------------------------------------------------
# Cross-reference validation
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    p = tmp_path / "semantic.yml"
    p.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return p


_MIN: dict[str, Any] = {
    "version": 1,
    "table_aliases": {"customers": "c"},
    "metrics": [
        {
            "name": "customer_count",
            "description": "count customers",
            "expression": "count(*)",
            "requires": ["customers"],
        }
    ],
    "dimensions": [
        {
            "name": "country",
            "description": "country",
            "expression": "c.country",
            "requires": ["customers"],
        }
    ],
    "relationships": [],
}


def test_metric_references_unknown_table_rejected(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        **_MIN,
        "metrics": [
            {
                **_MIN["metrics"][0],
                "requires": ["customers", "orders_typo"],
            }
        ],
    }
    p = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="unknown tables"):
        load_semantic_model(p)


def test_dimension_references_unknown_table_rejected(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        **_MIN,
        "dimensions": [
            {
                **_MIN["dimensions"][0],
                "requires": ["mystery_table"],
            }
        ],
    }
    p = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="unknown tables"):
        load_semantic_model(p)


def test_relationship_references_unknown_table_rejected(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        **_MIN,
        "relationships": [
            {"tables": ["customers", "unknown_t"], "join": "c.x = u.y"}
        ],
    }
    p = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="references unknown tables"):
        load_semantic_model(p)


def test_time_column_references_unknown_table_rejected(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        **_MIN,
        "time_columns": [{"table": "no_such_table", "column": "ts"}],
    }
    p = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="time_column"):
        load_semantic_model(p)


def test_duplicate_metric_names_rejected(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        **_MIN,
        "metrics": [
            _MIN["metrics"][0],
            {**_MIN["metrics"][0]},  # duplicate
        ],
    }
    p = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="duplicate names"):
        load_semantic_model(p)


def test_alias_with_invalid_identifier_rejected(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        **_MIN,
        "table_aliases": {"customers": "c-1"},
    }
    p = _write(tmp_path, payload)
    with pytest.raises(ValueError, match="not a valid SQL identifier"):
        load_semantic_model(p)


def test_missing_file_raises_clear_error() -> None:
    with pytest.raises(FileNotFoundError, match="semantic model not found"):
        load_semantic_model(Path("/nonexistent/semantic.yml"))


def test_yaml_not_a_mapping_rejected(tmp_path: Path) -> None:
    p = tmp_path / "semantic.yml"
    p.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level mapping"):
        load_semantic_model(p)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def test_metric_dimension_lookup_returns_none_on_miss() -> None:
    model = load_semantic_model()
    assert model.metric("does_not_exist") is None
    assert model.dimension("does_not_exist") is None
    assert model.metric("revenue") is not None
    assert model.dimension("country") is not None


def test_pydantic_construction_via_model_validate_works(tmp_path: Path) -> None:
    """The lifespan logs ``SemanticModel.model_validate(...)`` paths
    rather than ``load_semantic_model`` in some future callers; sanity
    check that the direct API matches what the loader uses."""
    model = SemanticModel.model_validate(_MIN)
    assert model.metric("customer_count") is not None
