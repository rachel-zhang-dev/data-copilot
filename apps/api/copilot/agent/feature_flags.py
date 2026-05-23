"""Runtime feature flags for the agent (week 6).

These exist for the eval harness — flipping them lets us run an A/B
on whether schema RAG / dialogue context / self-healing actually
help. Production never touches them; the defaults match the
production behaviour.

Three toggles:

* ``SCHEMA_RAG_ENABLED``        — ``retrieve_schema_node`` reads this.
                                  When False it short-circuits to
                                  ``get_schema_ddl()`` (week-2 behaviour).
* ``DIALOGUE_CONTEXT_ENABLED``  — ``_format_history_block`` reads this.
                                  When False, ``generate_sql`` does not
                                  see prior turns.
* ``RETRY_BUDGET`` lives in ``nodes.py`` and is patched by the runner
  directly (kept there so existing unit tests that monkeypatch
  ``nodes.RETRY_BUDGET`` keep working).

The ``override`` context manager bundles all three so the eval runner
can flip them atomically and restore on exit, including on exceptions.
This is intentionally global mutable state — the eval runs each case
sequentially and never concurrently flips flags.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from copilot.agent import nodes as _nodes_mod
from copilot.agent.state import ErrorClass

SCHEMA_RAG_ENABLED: bool = True
DIALOGUE_CONTEXT_ENABLED: bool = True


@contextmanager
def override(
    *,
    schema_rag_enabled: bool | None = None,
    dialogue_context_enabled: bool | None = None,
    retry_budget: dict[ErrorClass, int] | None = None,
) -> Iterator[None]:
    """Flip flags for the duration of the ``with`` block.

    Any argument left as ``None`` keeps its current value. On exit
    (including via exception) all three flags are restored, even if
    only some were set.
    """
    global SCHEMA_RAG_ENABLED, DIALOGUE_CONTEXT_ENABLED

    prev_rag = SCHEMA_RAG_ENABLED
    prev_dlg = DIALOGUE_CONTEXT_ENABLED
    prev_budget = dict(_nodes_mod.RETRY_BUDGET)

    if schema_rag_enabled is not None:
        SCHEMA_RAG_ENABLED = schema_rag_enabled
    if dialogue_context_enabled is not None:
        DIALOGUE_CONTEXT_ENABLED = dialogue_context_enabled
    if retry_budget is not None:
        # Mutate in place so existing references to the dict (e.g. in
        # tests that imported the module attribute) see the change.
        _nodes_mod.RETRY_BUDGET.clear()
        _nodes_mod.RETRY_BUDGET.update(retry_budget)

    try:
        yield
    finally:
        SCHEMA_RAG_ENABLED = prev_rag
        DIALOGUE_CONTEXT_ENABLED = prev_dlg
        _nodes_mod.RETRY_BUDGET.clear()
        _nodes_mod.RETRY_BUDGET.update(prev_budget)
