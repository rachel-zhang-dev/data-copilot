"""Unit tests for the schema retriever.

We mock the DB layer (``list_tables``, ``get_foreign_keys``,
``get_schema_ddl``, ``get_table_ddl``) and the embedding layer so the
tests run in milliseconds without Postgres or SiliconFlow.

The retriever's responsibilities under test:

1. ``directly_named_tables`` — exact table-name shortcut works
2. ``expand_with_foreign_keys`` — 1-hop expansion is correct
3. ``retrieve_schema_node`` — happy path, named-only, full-fallback
"""

from __future__ import annotations

from typing import Any

import pytest
from copilot.agent import retriever
from copilot.embeddings import EmbeddingError

# ---------------------------------------------------------------------------
# directly_named_tables
# ---------------------------------------------------------------------------


def test_directly_named_tables_picks_explicit_mentions() -> None:
    out = retriever.directly_named_tables(
        "How many rows are in customers?",
        ["customers", "orders", "products"],
    )
    assert out == {"customers"}


def test_directly_named_tables_is_case_insensitive() -> None:
    out = retriever.directly_named_tables(
        "Show me Customers and PRODUCTS",
        ["customers", "products", "orders"],
    )
    assert out == {"customers", "products"}


def test_directly_named_tables_uses_word_boundary_not_substring() -> None:
    out = retriever.directly_named_tables(
        "Discontinued items report",
        ["items", "discontinued_items"],
    )
    assert out == {"items"}


# ---------------------------------------------------------------------------
# expand_with_foreign_keys
# ---------------------------------------------------------------------------


def test_expand_returns_seed_when_graph_empty() -> None:
    seed = {"customers"}
    assert retriever.expand_with_foreign_keys(seed, {}) == seed


def test_expand_one_hop_pulls_in_neighbours() -> None:
    fk_graph = {
        "products": {"order_details", "categories"},
        "order_details": {"products", "orders"},
        "orders": {"order_details", "customers"},
    }
    out = retriever.expand_with_foreign_keys({"products"}, fk_graph, max_hops=1)
    assert out == {"products", "order_details", "categories"}


def test_expand_two_hops_pulls_further_but_bounded() -> None:
    fk_graph = {
        "a": {"b"},
        "b": {"a", "c"},
        "c": {"b", "d"},
        "d": {"c"},
    }
    out = retriever.expand_with_foreign_keys({"a"}, fk_graph, max_hops=2)
    assert out == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# retrieve_schema_node
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch out every DB call the retriever makes; return a dict so
    tests can assert which paths were taken."""
    calls: dict[str, Any] = {"get_table_ddl": [], "get_schema_ddl": 0}
    fk_graph = {
        "products": {"order_details", "categories"},
        "order_details": {"products", "orders"},
        "orders": {"order_details", "customers"},
        "customers": {"orders"},
        "categories": {"products"},
    }
    tables = ["categories", "customers", "order_details", "orders", "products"]

    monkeypatch.setattr(retriever, "list_tables", lambda: tables)
    monkeypatch.setattr(retriever, "get_foreign_keys", lambda: fk_graph)

    def fake_table_ddl(names: list[str]) -> str:
        calls["get_table_ddl"].append(names)
        return f"DDL({','.join(names)})"

    def fake_full_ddl() -> str:
        calls["get_schema_ddl"] += 1
        return "FULL_DDL"

    monkeypatch.setattr(retriever, "get_table_ddl", fake_table_ddl)
    monkeypatch.setattr(retriever, "get_schema_ddl", fake_full_ddl)
    return calls


def test_retrieve_schema_uses_vector_results_then_expands(
    mock_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        retriever,
        "vector_search_tables",
        lambda _q, _k: ["products"],
    )

    out = retriever.retrieve_schema_node({"question": "Top 5 best sellers"})

    assert "DDL(" in out["relevant_schema"]
    # Expanded set should include products + order_details + categories (1-hop)
    assert mock_db["get_table_ddl"]
    last_call = mock_db["get_table_ddl"][-1]
    assert "products" in last_call
    assert "order_details" in last_call
    assert "categories" in last_call


def test_retrieve_schema_skips_vector_when_question_names_table(
    mock_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When vector search fails but the question literally names a
    table, we should still succeed using the named-tables shortcut."""

    def boom(_q: str, _k: int) -> list[str]:
        raise EmbeddingError("boom")

    monkeypatch.setattr(retriever, "vector_search_tables", boom)

    retriever.retrieve_schema_node({"question": "list all customers"})

    assert mock_db["get_schema_ddl"] == 0  # no fallback
    last_call = mock_db["get_table_ddl"][-1]
    assert "customers" in last_call
    assert "orders" in last_call  # 1-hop expansion


def test_retrieve_schema_falls_back_when_vector_fails_and_no_named(
    mock_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_q: str, _k: int) -> list[str]:
        raise EmbeddingError("provider down")

    monkeypatch.setattr(retriever, "vector_search_tables", boom)

    out = retriever.retrieve_schema_node({"question": "what is going on"})

    assert out["relevant_schema"] == "FULL_DDL"
    assert mock_db["get_schema_ddl"] == 1


def test_retrieve_schema_falls_back_when_index_empty(
    mock_db: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def empty_index(_q: str, _k: int) -> list[str]:
        raise RuntimeError("schema_embeddings is empty")

    monkeypatch.setattr(retriever, "vector_search_tables", empty_index)

    out = retriever.retrieve_schema_node({"question": "what is going on"})

    assert out["relevant_schema"] == "FULL_DDL"
    assert mock_db["get_schema_ddl"] == 1
