# ADR 0002: SQL safety via AST parsing (sqlglot)

> Status: Accepted · Date: 2026-05 (Week 2) · Supersedes: none

## Context

The agent's `generate_sql` node hands an LLM-produced string to the
database. Anything more permissive than "read-only SELECT, bounded row
count" would let a clever prompt drop a table, exfiltrate a credentials
column, or pin a row lock under load. We need a hard policy boundary
that runs on every single query the LLM emits — no exceptions.

Three options were considered:

1. **Regex denylist** — match `INSERT`, `UPDATE`, `DROP`, etc.
2. **Tokenizer** — use `sqlparse` to look at the first token of the
   statement.
3. **Full parser** — use `sqlglot` to build an AST and inspect the
   root node.

## Decision

We use **`sqlglot`** to parse every LLM-produced SQL string and reject
anything whose root is not a `SELECT`. We additionally inject a
`LIMIT` if one is absent, and reject `SELECT ... INTO` and `FOR
UPDATE / SHARE` locks.

The implementation lives in
[`apps/api/copilot/agent/sql_safety.py`](../../apps/api/copilot/agent/sql_safety.py)
and is covered by
[`apps/api/tests/test_sql_safety.py`](../../apps/api/tests/test_sql_safety.py).

## Why not regex

A denylist regex has two failure modes the project cannot accept:

1. **String literals trigger false rejections.** A perfectly fine
   query like `SELECT id FROM customers WHERE notes = 'DELETE this row'`
   contains the literal substring `DELETE` and would be blocked by any
   straightforward `\bDELETE\b` pattern.

2. **New keywords are easy to forget.** PostgreSQL has dozens of
   write-capable constructs (`COPY ... FROM`, `VACUUM`, `LISTEN`,
   `NOTIFY`, …). Maintaining a complete denylist is a guaranteed
   future bug.

Parsing the SQL into an AST eliminates both classes of problem by
construction.

## Why not sqlparse

`sqlparse` is a **lexer/tokenizer**, not a true parser. It does not
build an AST and cannot reliably answer "is the root of this statement
a SELECT?" — especially in the presence of CTEs (`WITH ... SELECT`)
or comments. We would end up half-reimplementing a parser on top of it.

## Why sqlglot

* **AST-first.** `isinstance(parsed, exp.Select)` is the entire
  whitelist test.
* **Multi-dialect.** We parse as `postgres` today; future deployments
  on Snowflake / BigQuery only need a dialect change.
* **AST round-trip.** We get free SQL normalisation: re-emitting via
  `parsed.sql()` removes weird whitespace and gives us a single
  canonical form to log.
* **Maintained.** `sqlglot` ships frequent releases and actively tracks
  Postgres syntax additions.

## Consequences

### Good

* The safety policy is one short, testable function with comprehensive
  unit coverage (23 cases at time of writing).
* Adding new rules (e.g. "no `pg_*` system tables") is straightforward
  — just another AST walk.
* The same module will be reused by the Week 4 self-healing loop: if
  validation fails, the error message is rich enough to feed back to
  the LLM for a rewrite.

### Bad / accepted trade-offs

* `sqlglot` is one more dependency (~1 MB installed). Worth it.
* Parsing every query adds ~1-2 ms per request. Negligible next to a
  ~1-3 s LLM call.
* `sqlglot` occasionally lags on bleeding-edge syntax. Acceptable —
  we control the LLM prompt and tell it to stick to standard
  PostgreSQL.

## Alternatives we may revisit later

* **Postgres-level read-only role.** A truly bullet-proof defence is a
  DB user with `SELECT`-only privileges. This is complementary to AST
  validation, not a replacement, and we will configure it in Week 11
  when the deployment story comes online.
* **Resource limits.** Per-query `statement_timeout` and `LIMIT`
  injection. We do the latter now; the former is a 2-line Postgres
  config change deferred to Week 11.
