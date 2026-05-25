"""Multi-agent layer (week 12.5).

Adds a Supervisor that orchestrates two specialised agents on top of
the single LangGraph state machine ADR 0001 originally committed to:

* **SQL Specialist** — the existing 12-node graph from
  ``copilot.agent``, wrapped as a callable sub-graph node.
* **Analyst** — new three-node agent that runs *after* a successful
  data answer, surfaces anomalies / follow-ups / optional drill-
  downs.

The Supervisor itself is a thin LangGraph state machine with
deterministic (rule-based) routing — see ``supervisor.py``. See
``docs/decisions/0014-multi-agent-supervisor-analyst.md`` for the
full design rationale (why supervisor+worker, why rule-based, why
Pydantic envelopes, drill-down hop budget).
"""

from copilot.agents.messages import (
    AnalystAnomaly,
    AnalystFollowup,
    AnalystRequest,
    AnalystResponse,
    DrillDownRequest,
)
from copilot.agents.state import SupervisorState
from copilot.agents.supervisor import build_supervisor_graph

__all__ = [
    "AnalystAnomaly",
    "AnalystFollowup",
    "AnalystRequest",
    "AnalystResponse",
    "DrillDownRequest",
    "SupervisorState",
    "build_supervisor_graph",
]
