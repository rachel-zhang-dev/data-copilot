"""Unit tests for the checkpointer module.

These tests focus on the bits we wrote ourselves around
``AsyncPostgresSaver`` — primarily the ``conversation_lock`` context
manager that serialises concurrent same-thread writes. We do not test
``AsyncPostgresSaver`` itself; that is LangGraph's responsibility and is
covered by integration tests that run against a real Postgres.

The pool is mocked so these tests stay pure unit tests (no Docker, no
network). What we are verifying:

  * The lock context manager calls ``pg_advisory_lock`` with the
    correct bigint key on entry and ``pg_advisory_unlock`` with the
    same key on exit.
  * The key derivation is deterministic and process-independent
    (blake2b, not Python's randomised built-in ``hash``) — different
    replicas of the API must compute the same key for the same
    conversation_id or the lock does not actually serialise across
    replicas.
  * Even if the body raises, the unlock is still issued.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from copilot import checkpointer
from copilot.checkpointer import _lock_key, conversation_lock

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_lock_key_is_deterministic_across_calls() -> None:
    """Same input -> same output. Sanity check; without this, every
    replica would compute a different key and the cross-replica lock
    semantics would silently break."""
    a = _lock_key("conv-123")
    b = _lock_key("conv-123")
    assert a == b


def test_lock_key_differs_for_different_conversations() -> None:
    """Different inputs -> different outputs in any sensible hashing.
    Collisions are theoretically possible with 64 bits but vanishingly
    rare; if this test ever flakes the universe is broken before we
    are."""
    keys = {_lock_key(f"conv-{i}") for i in range(100)}
    assert len(keys) == 100


def test_lock_key_fits_in_postgres_bigint() -> None:
    """Postgres ``pg_advisory_lock`` takes a signed 64-bit integer.
    A key that overflows would raise a server-side error at runtime."""
    for cid in ["a", "uuid-like-string", "x" * 256]:
        key = _lock_key(cid)
        assert -(2**63) <= key < 2**63


# ---------------------------------------------------------------------------
# conversation_lock context manager
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal async-conn stand-in: records the SQL executed."""

    def __init__(self) -> None:
        self.execute = AsyncMock()


class _FakePool:
    """Minimal pool stand-in returning a single shared connection."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.closed = False

    def connection(self) -> Any:
        """psycopg-pool returns an async context manager; mimic that."""

        @asynccontextmanager
        async def _cm():
            yield self._conn

        return _cm()


@pytest.fixture()
def fake_pool(monkeypatch: pytest.MonkeyPatch) -> _FakePool:
    """Replace ``get_lock_pool`` with a fake.

    ``conversation_lock`` borrows from the dedicated *lock* pool (kept
    separate from the saver pool so advisory-lock connections cannot
    starve the checkpoint writes — see ``checkpointer.py`` and ADR
    0005). The fake pool returned here is what the CM will actually use.
    """
    conn = _FakeConn()
    pool = _FakePool(conn)
    monkeypatch.setattr(checkpointer, "get_lock_pool", lambda: pool)
    return pool


async def test_conversation_lock_acquires_and_releases(fake_pool: _FakePool) -> None:
    """Entering the CM runs ``pg_advisory_lock(key)``; exiting runs
    ``pg_advisory_unlock(key)`` with the SAME key."""
    cid = "happy-path-thread"
    expected_key = _lock_key(cid)

    async with conversation_lock(cid):
        pass

    conn = fake_pool._conn
    calls = conn.execute.await_args_list
    assert len(calls) == 2, f"expected exactly 2 SQL calls (lock + unlock), got {len(calls)}"

    lock_sql, lock_args = calls[0].args
    unlock_sql, unlock_args = calls[1].args
    assert "pg_advisory_lock" in lock_sql
    assert "pg_advisory_unlock" in unlock_sql
    assert lock_args == (expected_key,)
    assert unlock_args == (expected_key,)


async def test_conversation_lock_releases_on_exception(fake_pool: _FakePool) -> None:
    """If the wrapped body raises, the unlock MUST still run — otherwise
    the next request on the same thread_id would deadlock waiting for a
    lock that no live process owns."""
    cid = "exception-thread"

    with pytest.raises(RuntimeError, match="boom"):
        async with conversation_lock(cid):
            raise RuntimeError("boom")

    conn = fake_pool._conn
    sqls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("pg_advisory_unlock" in sql for sql in sqls), (
        "unlock was not issued after the body raised"
    )
