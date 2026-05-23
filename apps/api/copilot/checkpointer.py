"""LangGraph AsyncPostgresSaver wiring (week 5).

The checkpointer is what makes ``conversation_id`` work: it loads the
prior state of a thread before a graph invocation and persists the
state diff after each node runs. We reuse the existing Postgres
container — no separate Redis / Elasticache to operate.

Why async (``AsyncPostgresSaver`` + ``AsyncConnectionPool``)
-----------------------------------------------------------
FastAPI runs request handlers on an asyncio event loop and our agent
graph is invoked via ``await graph.ainvoke(...)``. LangGraph's async
entry-point dispatches to the checkpointer's ``aget_tuple`` /
``aput`` family. The synchronous ``PostgresSaver`` does not implement
those, so under ``ainvoke`` it raises ``NotImplementedError`` the
moment a thread loads — silently breaking every multi-turn ``/ask``
call. Using the async variant matches the rest of the stack and
keeps the request handler non-blocking.

Why not the in-memory ``MemorySaver``
-------------------------------------
``MemorySaver`` is per-process and disappears at restart. That's fine
for unit tests but useless once the API is exposed to a second
caller, behind a load balancer, or restarted at deploy. Postgres
gives us:

* multi-replica safety (rows are the source of truth);
* free durability (we already back up the DB);
* a simple ops story (``\\dt`` shows ``checkpoints``,
  ``checkpoint_writes``, etc., and you can ``SELECT`` to debug what
  the agent thought a thread looked like at any point in time).

Connection pooling
------------------
``AsyncPostgresSaver`` accepts an ``AsyncConnectionPool``. Each
``/ask`` request fires several reads/writes against the checkpoint
table — opening a connection per call would be wasteful. The pool
integrates cleanly with FastAPI lifespan: opened on startup, closed
on shutdown.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from copilot.config import get_settings

log = logging.getLogger(__name__)

# Two separate pools, sized for the very different access patterns of
# the two consumers:
#
# * SAVER pool — used by ``AsyncPostgresSaver``. Each call borrows a
#   connection, runs one short SQL (UPSERT / SELECT against the
#   checkpoint tables), and returns it. Many short ops per ``ainvoke``.
#   A small pool comfortably serves dozens of concurrent turns.
#
# * LOCK pool — used by ``conversation_lock``. Each call borrows ONE
#   connection and holds it for the entire turn (≈5-10 s of LLM time),
#   because ``pg_advisory_lock`` is session-scoped and releasing the
#   connection would release the lock. Pool size therefore caps the
#   number of concurrent turns the server can accept; bump in
#   production to whatever your peak concurrency budget allows.
#
# Sharing one pool between these two consumers led to PoolTimeout
# starvation: when N concurrent /ask calls each held a lock connection,
# the saver could not borrow a free connection for its writes inside
# ``graph.ainvoke``. Separate pools eliminate that contention entirely.
_SAVER_POOL_MAX_SIZE = 10
_LOCK_POOL_MAX_SIZE = 10


def _checkpointer_dsn() -> str:
    """Return a libpq-style DSN for the checkpointer.

    ``AsyncPostgresSaver`` talks to psycopg directly, so it wants the
    plain ``postgresql://`` URL — neither the ``+psycopg`` SQLAlchemy
    flavour we use in ``db.py`` nor the ``+asyncpg`` async flavour.
    We accept both common shapes from ``.env`` and normalise here.
    """
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL not configured")
    url = settings.database_url
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def _make_pool(max_size: int) -> AsyncConnectionPool:
    """Construct an async psycopg pool against the configured DSN.

    ``open=False`` is required for ``AsyncConnectionPool``: opening
    must happen inside a running event loop via ``await pool.open()``,
    which we do from the helpers below the first time the pool is
    needed.
    """
    from psycopg.rows import dict_row

    return AsyncConnectionPool(
        conninfo=_checkpointer_dsn(),
        max_size=max_size,
        min_size=1,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    )


@lru_cache(maxsize=1)
def get_saver_pool() -> AsyncConnectionPool:
    """Process-wide pool used exclusively by ``AsyncPostgresSaver``.

    Separated from the lock pool so the advisory-lock connections
    (which are held for the duration of a turn) cannot starve the
    saver's short reads/writes against the checkpoint tables.
    """
    return _make_pool(_SAVER_POOL_MAX_SIZE)


@lru_cache(maxsize=1)
def get_lock_pool() -> AsyncConnectionPool:
    """Process-wide pool used exclusively by ``conversation_lock``.

    Each in-flight turn borrows one connection from here and holds it
    until ``pg_advisory_unlock`` runs. The pool size therefore directly
    caps how many concurrent turns the server will accept before back-
    pressuring; raise ``_LOCK_POOL_MAX_SIZE`` in production if needed.
    """
    return _make_pool(_LOCK_POOL_MAX_SIZE)


async def get_checkpointer() -> AsyncPostgresSaver:
    """Return an ``AsyncPostgresSaver`` bound to the saver pool.

    Opens the saver pool lazily on first call (within the running
    event loop). Subsequent calls return a fresh saver wrapping the
    same pool — saver objects are cheap stateless wrappers.

    The ``type: ignore`` is needed because ``AsyncConnectionPool`` is
    generic in psycopg's connection type and our pool's parametric
    type does not exactly match what ``AsyncPostgresSaver`` advertises;
    the runtime API contract is identical.
    """
    pool = get_saver_pool()
    if pool.closed:
        await pool.open()
    return AsyncPostgresSaver(pool)  # type: ignore[arg-type]


async def setup_checkpointer() -> None:
    """Create the checkpointer tables if they do not already exist.

    Safe to call on every startup: ``setup()`` issues ``CREATE TABLE
    IF NOT EXISTS`` statements internally. Logs a one-line confirmation
    so it is obvious in the deploy log when the migration ran.
    """
    saver = await get_checkpointer()
    await saver.setup()
    log.info("langgraph checkpointer tables verified")


async def dispose_checkpointer() -> None:
    """Close both pools. Call from FastAPI shutdown hook."""
    if get_saver_pool.cache_info().currsize:
        await get_saver_pool().close()
        get_saver_pool.cache_clear()
    if get_lock_pool.cache_info().currsize:
        await get_lock_pool().close()
        get_lock_pool.cache_clear()


def _lock_key(thread_id: str) -> int:
    """Derive a deterministic 64-bit signed integer from ``thread_id``.

    ``pg_advisory_lock`` takes a ``bigint`` argument; ``thread_id`` is a
    UUID / arbitrary string. We hash with blake2b (digest_size=8) into
    the signed-64-bit range that Postgres accepts. blake2b is preferred
    over Python's built-in ``hash()`` because the latter is randomised
    per-process (PEP 456), which would mean each replica computes a
    different lock key for the same conversation_id and the lock would
    not actually serialise cross-replica writes.
    """
    digest = hashlib.blake2b(thread_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@asynccontextmanager
async def conversation_lock(thread_id: str) -> AsyncIterator[None]:
    """Serialise concurrent writes for a single ``conversation_id``.

    Why this exists
    ---------------
    LangGraph's checkpoint write is "last writer wins": two requests
    on the same ``thread_id`` that interleave will both read the same
    baseline state, both compute on top of it, and both INSERT a new
    checkpoint. The later commit becomes the canonical state and the
    earlier turn's contributions to ``dialogue`` / ``attempts`` are
    silently lost. See ADR 0005 §"Concurrent writes to the same
    conversation" for the full design discussion.

    How
    ---
    We acquire a Postgres **advisory session lock** keyed on
    ``hash(thread_id)``. Same key → callers block until the holder
    releases; different keys are independent (so unrelated
    conversations stay fully parallel). The lock is session-scoped
    rather than transaction-scoped because we hold it for the whole
    graph invocation, which spans many short transactions managed by
    ``AsyncPostgresSaver``.

    Constraints
    -----------
    The connection is held from this pool for the entire turn
    (≈5-10 s). With ``_POOL_MAX_SIZE = 10`` we comfortably support
    ~5 concurrent turns on different conversations. Increase the pool
    size before raising expected concurrency.

    Failure modes
    -------------
    If acquiring the lock itself fails (e.g. Postgres restart), the
    error propagates to the caller — losing the lock guarantee is
    strictly worse than failing the request, since silently corrupting
    a conversation is the bug we are preventing.
    """
    key = _lock_key(thread_id)
    pool = get_lock_pool()
    if pool.closed:
        await pool.open()
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (key,))
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
