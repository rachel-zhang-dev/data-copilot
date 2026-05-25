"""Analyst agent — runs after a successful SQL answer to surface
anomalies / follow-up suggestions / optional drill-downs.

Public surface:

* ``analyst_node``  — single LangGraph node called by the supervisor.
* ``parse_response`` — internal JSON parser exported for tests.
"""

from copilot.agents.analyst.nodes import analyst_node, parse_response

__all__ = ["analyst_node", "parse_response"]
