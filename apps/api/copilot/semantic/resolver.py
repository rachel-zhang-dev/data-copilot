"""Deterministic SQL compiler for the semantic layer (Phase 3.1 / ADR 0023).

Input: a ``ResolverSpec`` that the LLM router produced — exactly one
metric, zero or more dimensions, optional time range, optional
equality filters.

Output: a PostgreSQL SELECT statement with a guaranteed LIMIT.

The whole point of this module: **the LLM never picks JOINs, never
picks aggregation grain, never picks filter logic.** It only chooses
which pre-defined metric / dimension to use. SQL correctness becomes
a function of the YAML, not of the model's reasoning skill.

Snowflake Cortex Analyst, dbt Semantic Layer, and Cube all do this
under different names. The dbt 2026 benchmark
(``docs/getdbt.com/blog/semantic-layer-vs-text-to-sql-2026``)
measured "approaches or hits 100% accuracy on queries covered by a
well-modeled semantic layer" — versus 84-90% raw text-to-SQL.

Failure modes are surfaced as ``ResolverError`` so the caller (the
LangGraph router) can fall through cleanly to the LLM text-to-SQL
path. We NEVER ship malformed SQL to the database.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from copilot.semantic.models import Relationship, SemanticModel

# ---------------------------------------------------------------------------
# Spec — what the LLM router produces
# ---------------------------------------------------------------------------


class TimeRange(BaseModel):
    """Time-window filter on the model's primary time column.

    Only ``year`` is supported in the MVP — Northwind doesn't have
    enough date variety to justify month / quarter / relative-window
    parsing yet. Other shapes raise ``ResolverError`` at compile time
    rather than silently no-op, so a buggy router prompt surfaces in
    the logs.
    """

    year: int | None = Field(None, ge=1900, le=2100)


class FilterClause(BaseModel):
    """One equality filter on a dimension's expression.

    Operators are deliberately limited to ``=`` and ``in``. A full
    expression language is out of scope for the MVP; questions that
    need ``BETWEEN``, ``LIKE``, ``IS NULL``, etc. fall through to the
    LLM text-to-SQL path.
    """

    dimension: str
    op: Literal["=", "in"] = "="
    value: str | int | float | list[str | int | float]


class ResolverSpec(BaseModel):
    """Structured query that resolves deterministically to SQL.

    Built by the LLM router from a natural-language question + the
    available menu of metrics and dimensions.
    """

    metric: str
    dimensions: list[str] = Field(default_factory=list)
    time_range: TimeRange | None = None
    filters: list[FilterClause] = Field(default_factory=list)
    limit: int = Field(100, ge=1, le=1000)

    @model_validator(mode="after")
    def _no_self_referential_filters(self) -> ResolverSpec:
        # Filter on a dimension that's also being grouped on is
        # redundant but legal; we don't reject. The router emits
        # whatever it wants, the compiler folds duplicates anyway.
        return self


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ResolverError(ValueError):
    """Raised when a spec cannot be compiled (unknown metric, no path
    between required tables, unsupported time range, etc.).

    Inherits ``ValueError`` so callers that catch broadly still work;
    the dedicated class lets the LangGraph router distinguish "this
    spec is bad — fall back to LLM" from other exceptions.
    """


# ---------------------------------------------------------------------------
# Join planner
# ---------------------------------------------------------------------------


def _plan_joins(
    required_tables: set[str], all_relationships: list[Relationship]
) -> tuple[str, list[tuple[str, str]]]:
    """Walk the relationship graph and return (root_table, [(table, on_clause), …]).

    Key subtlety: required tables may NOT be directly connected.
    "revenue by country" requires ``{order_details, customers}``, but
    Northwind has no direct order_details↔customers relationship — the
    JOIN must traverse the bridge table ``orders``. So this planner
    walks the FULL relationship graph (not just edges between required
    tables) and emits a spanning tree that covers everything required,
    pulling in bridge tables on demand.

    Algorithm:
      1. Build full undirected adjacency over every relationship.
      2. Pick a deterministic root (alphabetically first required
         table) for stable SQL output across runs.
      3. BFS from the root over the full graph, recording each node's
         parent + the join clause that reached it.
      4. For each required-but-not-root table, walk back along the
         parent chain to the root, collecting the (parent → child)
         edges. Deduplicate via an edge set so two required tables
         sharing a parent only emit one JOIN.
      5. Sort the collected edges by BFS distance from root so each
         JOIN's "new" alias is one whose parent has already been
         introduced in the FROM / preceding JOINs.

    Raises ``ResolverError`` when no path exists between the root and
    one of the required tables.
    """
    if not required_tables:
        raise ResolverError("at least one table must be required (metric.requires)")

    # Step 1: full adjacency over every declared relationship.
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for r in all_relationships:
        a, b = r.tables[0], r.tables[1]
        adjacency.setdefault(a, []).append((b, r.join))
        adjacency.setdefault(b, []).append((a, r.join))

    # Step 2: deterministic root.
    root = sorted(required_tables)[0]

    # If the root is isolated AND more than one required table exists,
    # we can't reach anyone — bail early.
    if root not in adjacency and len(required_tables) > 1:
        raise ResolverError(
            f"table {root!r} has no relationships defined; "
            "cannot reach other required tables"
        )

    # Step 3: BFS from root over the full graph. ``parent[node]`` is
    # the (parent_table, on_clause) edge that reached ``node``.
    parent: dict[str, tuple[str, str]] = {}
    distance: dict[str, int] = {root: 0}
    queue: list[str] = [root]
    while queue:
        node = queue.pop(0)
        for neighbour, on_clause in adjacency.get(node, []):
            if neighbour == root or neighbour in parent:
                continue
            parent[neighbour] = (node, on_clause)
            distance[neighbour] = distance[node] + 1
            queue.append(neighbour)

    # Step 4: confirm every required table is reachable.
    reachable = set(parent.keys()) | {root}
    missing = required_tables - reachable
    if missing:
        raise ResolverError(
            f"no relationship path connects {sorted(missing)} to {root!r}; "
            "either add a relationship to semantic.yml or fall back to text-to-SQL"
        )

    # Step 5: walk parent chains from each non-root required table,
    # collecting unique edges (some chains share segments).
    edges: dict[tuple[str, str], tuple[str, str, str]] = {}
    for target in required_tables - {root}:
        node = target
        while node != root:
            par, on = parent[node]
            # Edge key normalised to (alpha-smaller, alpha-larger) so
            # walks from different targets via the same edge collapse.
            edge_key = (par, node) if par < node else (node, par)
            if edge_key not in edges:
                edges[edge_key] = (par, node, on)
            node = par

    # Step 6: order edges so each new alias is introduced AFTER its
    # parent. BFS-distance from root gives a valid topological order.
    ordered = sorted(edges.values(), key=lambda e: distance[e[1]])
    return root, [(child, on) for (_par, child, on) in ordered]


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------


def compile_sql(model: SemanticModel, spec: ResolverSpec) -> str:
    """Project ``spec`` to a single PostgreSQL SELECT statement.

    The output is guaranteed to:

    * be a single statement,
    * be a SELECT (the sql_safety layer would reject otherwise),
    * have an explicit ``LIMIT`` (default 100, capped at 1000),
    * use the table aliases declared in the model.

    Re-runs of the same spec against the same model produce
    byte-identical SQL — useful for the eval harness (it hashes
    SQL to detect drift).
    """
    # ---- 1. Look up metric + dimensions ----
    metric = model.metric(spec.metric)
    if metric is None:
        raise ResolverError(
            f"unknown metric {spec.metric!r}; "
            f"available: {sorted(m.name for m in model.metrics)}"
        )
    dims = []
    for dname in spec.dimensions:
        dim = model.dimension(dname)
        if dim is None:
            raise ResolverError(
                f"unknown dimension {dname!r}; "
                f"available: {sorted(d.name for d in model.dimensions)}"
            )
        dims.append(dim)

    # ---- 2. Collect required tables (union over metric + dims + filters + time) ----
    required: set[str] = set(metric.requires)
    for d in dims:
        required |= set(d.requires)
    for f in spec.filters:
        dim = model.dimension(f.dimension)
        if dim is None:
            raise ResolverError(
                f"filter references unknown dimension {f.dimension!r}"
            )
        required |= set(dim.requires)
    if spec.time_range is not None:
        if not model.time_columns:
            raise ResolverError(
                "time_range provided but the semantic model declares no time_columns"
            )
        required.add(model.time_columns[0].table)

    # ---- 3. Plan joins (alias-aware) ----
    root, join_steps = _plan_joins(required, model.relationships)
    aliases = model.table_aliases
    from_clause = f"{root} AS {aliases[root]}"
    join_clauses = [
        f"JOIN {table} AS {aliases[table]} ON {on}" for table, on in join_steps
    ]

    # ---- 4. SELECT list ----
    select_parts = [f"{d.expression} AS {d.name}" for d in dims]
    select_parts.append(f"{metric.expression} AS {metric.name}")

    # ---- 5. WHERE clauses (time range first, then filters) ----
    where_parts: list[str] = []
    if spec.time_range is not None and spec.time_range.year is not None:
        tc = model.time_columns[0]
        alias = aliases[tc.table]
        where_parts.append(
            f"EXTRACT(YEAR FROM {alias}.{tc.column}) = {int(spec.time_range.year)}"
        )
    for f in spec.filters:
        dim = model.dimension(f.dimension)
        assert dim is not None  # checked in step 2
        rendered = _render_filter(dim.expression, f)
        where_parts.append(rendered)

    # ---- 6. GROUP BY + ORDER BY ----
    group_by_parts = [d.expression for d in dims]
    order_by = ""
    if dims:
        # ORDER BY the metric descending so "top N" reads correctly.
        order_by = f"\nORDER BY {metric.name} DESC NULLS LAST"

    # ---- 7. Assemble ----
    lines = [
        "SELECT " + ",\n       ".join(select_parts),
        f"FROM {from_clause}",
    ]
    lines.extend(join_clauses)
    if where_parts:
        lines.append("WHERE " + "\n  AND ".join(where_parts))
    if group_by_parts:
        lines.append("GROUP BY " + ", ".join(group_by_parts))
    if order_by:
        lines.append(order_by.lstrip("\n"))
    lines.append(f"LIMIT {spec.limit}")

    return "\n".join(lines)


def _render_filter(dimension_expr: str, f: FilterClause) -> str:
    """Render one filter clause as a SQL fragment. We deliberately use
    parameter-style literal interpolation rather than positional binds
    because the resolver returns SQL strings — binds would have to be
    smuggled out of band. Equality + IN on small literal sets only;
    the limited operator set keeps escaping straightforward.
    """
    if f.op == "=":
        return f"{dimension_expr} = {_sql_literal(f.value)}"
    if f.op == "in":
        if not isinstance(f.value, list) or not f.value:
            raise ResolverError(
                f"filter on {f.dimension!r} with op='in' requires a non-empty list"
            )
        rendered = ", ".join(_sql_literal(v) for v in f.value)
        return f"{dimension_expr} IN ({rendered})"
    # Pydantic Literal validator should catch this earlier; defensive.
    raise ResolverError(f"unsupported filter op {f.op!r}")  # pragma: no cover


def _sql_literal(v: str | int | float | list[str | int | float]) -> str:
    """Tiny escaper for the equality / IN filter values.

    Numbers go through as-is; strings get single-quoted with embedded
    quotes doubled. We don't accept other types — a malformed value
    raises ResolverError so we never quote-inject something weird into
    the SQL.
    """
    if isinstance(v, bool):
        # bool is an int subclass — handle before numeric branch.
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        escaped = v.replace("'", "''")
        return f"'{escaped}'"
    raise ResolverError(f"unsupported filter literal type: {type(v).__name__}")
