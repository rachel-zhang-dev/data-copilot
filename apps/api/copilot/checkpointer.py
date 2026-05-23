"""LangGraph PostgresSaver wiring (week 5).

The checkpointer is what makes ``conversation_id`` work: it loads the
prior state of a thread before a graph invocation and persists the
state diff after each node runs. We reuse the existing Postgres
container — no separate Redis / Elasticache to operate.

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
``PostgresSaver`` accepts a raw ``psycopg.Connection`` *or* a
``ConnectionPool``. We use the pool because:

* Each ``/ask`` request fires several reads/writes against the
  checkpoint table — opening a connection per call is wasteful.
* ``ConnectionPool`` integrates cleanly with FastAPI lifespan: open
  on startup, close on shutdown.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from copilot.config import get_settings

log = logging.getLogger(__name__)


def _checkpointer_dsn() -> str:
    """Return a libpq-style DSN for the checkpointer.

    ``PostgresSaver`` talks to psycopg directly, so it wants the
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


@lru_cache(maxsize=1)
def get_pool() -> ConnectionPool:
    """Process-wide connection pool used exclusively by the checkpointer.

    We deliberately keep this separate from the SQLAlchemy engine pool
    in ``db.py``. Mixing them would mean a single pool serving two
    different access patterns (SA-typed reads vs. raw psycopg writes),
    which complicates lifecycle and tuning. Two small pools is simpler.

    ``autocommit=True`` and ``row_factory=dict_row`` match what
    ``PostgresSaver`` expects internally; without ``dict_row`` it
    cannot decode its own row format.
    """
    from psycopg.rows import dict_row

    pool = ConnectionPool(
        conninfo=_checkpointer_dsn(),
        max_size=5,
        min_size=1,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=True,
    )
    return pool


def get_checkpointer() -> PostgresSaver:
    """Return a ``PostgresSaver`` bound to the shared pool.

    Call this lazily — it does not run any DDL until either
    ``setup()`` is invoked (idempotent) or the first save happens.

    The ``type: ignore`` is needed because ``ConnectionPool`` is
    generic in psycopg's connection type and our pool's parametric
    type does not exactly match what ``PostgresSaver`` advertises;
    the run-time API contract is the same.
    """
    return PostgresSaver(get_pool())  # type: ignore[arg-type]


def setup_checkpointer() -> None:
    """Create the checkpointer tables if they do not already exist.

    Safe to call on every startup: ``setup()`` issues ``CREATE TABLE
    IF NOT EXISTS`` statements internally. Logs a one-line confirmation
    so it is obvious in the deploy log when the migration ran.
    """
    saver = get_checkpointer()
    saver.setup()
    log.info("langgraph checkpointer tables verified")


def dispose_checkpointer() -> None:
    """Close the connection pool. Call from FastAPI shutdown hook."""
    if get_pool.cache_info().currsize:
        get_pool().close()
        get_pool.cache_clear()
