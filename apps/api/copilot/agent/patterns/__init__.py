"""Pattern-detection package (Phase 1.2 / ADR 0017).

Pure-statistics detectors that run on every successful data turn and
surface findings via ``state.patterns`` and merged ``insight.bullets``.

Public API:

* ``Finding``                 — typed envelope every detector produces.
* ``detect_outliers``         — IQR-based outlier flagging on numeric cols.
* ``detect_trend``            — OLS slope + R² on monotonic / temporal x.
* ``detect_patterns``         — runs every detector and returns the merged list.
* ``detect_patterns_node``    — LangGraph node (in ``node.py``).

All detectors are deterministic, LLM-free, and side-effect-free —
suitable for unit-testing without any infrastructure.
"""

from copilot.agent.patterns.detectors import (
    Finding,
    detect_outliers,
    detect_patterns,
    detect_trend,
)

__all__ = [
    "Finding",
    "detect_outliers",
    "detect_patterns",
    "detect_trend",
]
