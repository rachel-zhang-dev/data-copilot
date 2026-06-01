"""Unit tests for the semantic-layer SQL compiler (Phase 3.1 / ADR 0023).

The compiler is pure (no I/O, no LLM); these tests assert byte-stable
output for several spec shapes against the real ``data/semantic.yml``.
Stability matters because the eval harness hashes SQL to detect drift
between runs.
"""

from __future__ import annotations

import pytest
from copilot.semantic.models import load_semantic_model
from copilot.semantic.resolver import (
    FilterClause,
    ResolverError,
    ResolverSpec,
    TimeRange,
    compile_sql,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def model():
    return load_semantic_model()


# ---------------------------------------------------------------------------
# Simple aggregation
# ---------------------------------------------------------------------------


def test_metric_only_compiles_to_scalar_select(model) -> None:
    spec = ResolverSpec(metric="customer_count")
    sql = compile_sql(model, spec)
    assert "COUNT(DISTINCT c.customer_id)" in sql
    assert "AS customer_count" in sql
    assert "FROM customers AS c" in sql
    assert "GROUP BY" not in sql  # no dimensions → no group
    assert sql.endswith("LIMIT 100")


def test_metric_plus_one_dimension_groups(model) -> None:
    spec = ResolverSpec(metric="customer_count", dimensions=["country"])
    sql = compile_sql(model, spec)
    assert "c.country AS country" in sql
    assert "GROUP BY c.country" in sql
    # ORDER BY is by the metric DESC NULLS LAST so "top N" reads right.
    assert "ORDER BY customer_count DESC NULLS LAST" in sql


# ---------------------------------------------------------------------------
# Join planning
# ---------------------------------------------------------------------------


def test_revenue_by_country_plans_orders_customers_order_details(model) -> None:
    """revenue requires order_details; country requires customers.
    The compiler must traverse order_details → orders → customers,
    emitting BOTH JOINs."""
    spec = ResolverSpec(metric="revenue", dimensions=["country"])
    sql = compile_sql(model, spec)
    assert "FROM customers AS c" in sql or "FROM order_details AS od" in sql
    assert "JOIN orders AS o ON o.customer_id = c.customer_id" in sql or "JOIN customers AS c ON o.customer_id = c.customer_id" in sql
    assert "JOIN order_details AS od ON o.order_id = od.order_id" in sql or "JOIN orders AS o ON o.order_id = od.order_id" in sql
    assert "GROUP BY c.country" in sql


def test_revenue_by_category_walks_three_joins(model) -> None:
    """revenue (order_details) + category (products, categories) →
    needs order_details, products, categories joined. Walk through
    od → p → cat."""
    spec = ResolverSpec(metric="revenue", dimensions=["category"])
    sql = compile_sql(model, spec)
    # Don't lock the JOIN order (BFS from sorted root); just assert the
    # required pieces are present.
    assert "od.unit_price * od.quantity" in sql
    assert "cat.category_name AS category" in sql
    assert "GROUP BY cat.category_name" in sql
    # All three tables must appear.
    for table_alias in ("AS od", "AS p", "AS cat"):
        assert table_alias in sql


# ---------------------------------------------------------------------------
# Time range
# ---------------------------------------------------------------------------


def test_year_time_range_adds_where_clause(model) -> None:
    spec = ResolverSpec(
        metric="order_count",
        dimensions=["month"],
        time_range=TimeRange(year=1997),
    )
    sql = compile_sql(model, spec)
    assert "EXTRACT(YEAR FROM o.order_date) = 1997" in sql
    assert "DATE_TRUNC('month', o.order_date) AS month" in sql


def test_metric_only_no_time_clause(model) -> None:
    spec = ResolverSpec(metric="customer_count")
    sql = compile_sql(model, spec)
    assert "WHERE" not in sql


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_equality_filter_on_dimension(model) -> None:
    spec = ResolverSpec(
        metric="order_count",
        filters=[FilterClause(dimension="country", value="Germany")],
    )
    sql = compile_sql(model, spec)
    assert "c.country = 'Germany'" in sql


def test_in_filter_renders_parenthesised_list(model) -> None:
    spec = ResolverSpec(
        metric="order_count",
        filters=[
            FilterClause(
                dimension="country", op="in", value=["Germany", "France"]
            )
        ],
    )
    sql = compile_sql(model, spec)
    assert "c.country IN ('Germany', 'France')" in sql


def test_filter_string_escaping_doubles_single_quote(model) -> None:
    spec = ResolverSpec(
        metric="order_count",
        filters=[FilterClause(dimension="country", value="Côte d'Ivoire")],
    )
    sql = compile_sql(model, spec)
    # Single-quote inside the value is doubled per SQL convention.
    assert "c.country = 'Côte d''Ivoire'" in sql


def test_filter_with_in_op_requires_non_empty_list(model) -> None:
    spec = ResolverSpec(
        metric="order_count",
        filters=[FilterClause(dimension="country", op="in", value=[])],
    )
    with pytest.raises(ResolverError, match="non-empty list"):
        compile_sql(model, spec)


def test_filter_on_unknown_dimension_rejected(model) -> None:
    spec = ResolverSpec(
        metric="order_count",
        filters=[FilterClause(dimension="not_a_dim", value="x")],
    )
    with pytest.raises(ResolverError, match="unknown dimension"):
        compile_sql(model, spec)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_metric_raises_with_available_list(model) -> None:
    spec = ResolverSpec(metric="net_promoter_score")
    with pytest.raises(ResolverError, match="available"):
        compile_sql(model, spec)


def test_unknown_dimension_raises(model) -> None:
    spec = ResolverSpec(metric="revenue", dimensions=["mystery"])
    with pytest.raises(ResolverError, match="unknown dimension"):
        compile_sql(model, spec)


def test_time_range_without_time_columns_errors(model) -> None:
    # Build a model with no time_columns to force the error.
    from copilot.semantic.models import SemanticModel

    no_time = SemanticModel.model_validate(
        {
            "version": 1,
            "table_aliases": {"customers": "c"},
            "metrics": [
                {
                    "name": "customer_count",
                    "description": "count",
                    "expression": "COUNT(*)",
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
    )
    spec = ResolverSpec(metric="customer_count", time_range=TimeRange(year=1997))
    with pytest.raises(ResolverError, match="time_columns"):
        compile_sql(no_time, spec)


# ---------------------------------------------------------------------------
# Spec validation (ResolverSpec)
# ---------------------------------------------------------------------------


def test_resolver_spec_caps_limit_at_1000() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResolverSpec(metric="x", limit=5000)


def test_resolver_spec_rejects_year_out_of_range() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TimeRange(year=1800)
    with pytest.raises(ValidationError):
        TimeRange(year=2200)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_spec_produces_byte_identical_sql(model) -> None:
    """The compiler must be a pure function — the eval harness hashes
    SQL to detect drift between runs."""
    spec = ResolverSpec(
        metric="revenue",
        dimensions=["category", "year"],
        time_range=TimeRange(year=1997),
    )
    a = compile_sql(model, spec)
    b = compile_sql(model, spec)
    assert a == b
