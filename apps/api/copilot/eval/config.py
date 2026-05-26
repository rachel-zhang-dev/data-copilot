"""Experiment configuration.

An ``ExperimentConfig`` is the dial-set we change between A and B in
each comparison. The runner threads the config through ``build_graph``
and the dialogue helpers via the feature flags wired up in week 6
(see ``copilot.agent.graph.build_graph`` and
``copilot.agent.nodes._format_history_block``).

Keeping this dataclass small forces every new knob to be introduced
deliberately rather than accumulated as scattered global state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from copilot.agent.state import ErrorClass


@dataclass(frozen=True)
class ExperimentConfig:
    """Feature flags + identifying label for one run of the eval set."""

    label: str
    """Short identifier used in report tables and LangSmith tags."""

    schema_rag_enabled: bool = True
    """When False, ``retrieve_schema_node`` returns the full DDL
    instead of using vector search + FK expansion. Used by the A1
    experiment to quantify the value of week-3 RAG."""

    retry_budget_override: dict[ErrorClass, int] | None = None
    """When set, replaces ``RETRY_BUDGET`` for the duration of the
    run. ``{}``-equivalent: passing all-zero values turns off
    self-healing. Used by A2."""

    dialogue_context_enabled: bool = True
    """When False, ``_format_history_block`` returns the empty string
    so ``generate_sql`` cannot see prior turns. Used by A3."""

    analyst_enabled: bool = True
    """When False, the week-12.5 supervisor short-circuits after the
    SQL Specialist and never invokes the Analyst. Used by A4 to
    measure the Analyst's token-cost / latency contribution and to
    verify ``success_rate`` stays flat (the Analyst is additive, not
    gating)."""

    coverage_check_enabled: bool = True
    """When False, ``coverage_check_node`` is skipped — the data
    branch behaves exactly like it did before Phase 1.1 (always
    generate SQL, never refuse). Used by A5 (the coverage_check A/B)
    to quantify the gate's impact on ``unanswerable`` / ``schema_explore``
    cases without harming the success rate on the original 32."""

    patterns_detection_enabled: bool = True
    """When False, ``detect_patterns_node`` short-circuits before any
    detector runs — the insight envelope keeps only the legacy
    bullets from ``summarize_result``. Used by A6 (the
    patterns_detection A/B) to quantify how often the detector adds
    user-visible value on the ``has_pattern`` category."""

    notes: str = ""
    """Free-form description of what this run is supposed to test;
    surfaces in the markdown report header."""

    extra_tags: tuple[str, ...] = field(default_factory=tuple)
    """Optional LangSmith tags applied to every run produced under
    this config. Useful for slicing results in the LangSmith UI."""


# Convenience presets ------------------------------------------------------
# Each preset is one side of an A/B; they are imported by the
# experiment drivers in ``copilot.eval.experiments``.

BASELINE_FULL = ExperimentConfig(
    label="full_features",
    notes="All week-3/4/5 features enabled — the production default.",
    extra_tags=("baseline",),
)

WITHOUT_SCHEMA_RAG = ExperimentConfig(
    label="schema_rag_off",
    schema_rag_enabled=False,
    notes="Bypasses retrieve_schema; full DDL is dumped into the prompt.",
    extra_tags=("a1", "schema_rag_off"),
)

WITHOUT_SELF_HEALING = ExperimentConfig(
    label="self_healing_off",
    retry_budget_override={"execution_failed": 0, "unsafe_sql": 0, "fatal": 0},
    notes="No retries — first SQL failure terminates the turn.",
    extra_tags=("a2", "self_healing_off"),
)

WITHOUT_DIALOGUE_CONTEXT = ExperimentConfig(
    label="dialogue_context_off",
    dialogue_context_enabled=False,
    notes="generate_sql does not see previous turns; follow-ups are blind.",
    extra_tags=("a3", "dialogue_context_off"),
)

WITHOUT_ANALYST = ExperimentConfig(
    label="analyst_off",
    analyst_enabled=False,
    notes="Supervisor short-circuits after SQL; no Analyst follow-ups or drill-downs.",
    extra_tags=("a4", "analyst_off"),
)

WITHOUT_COVERAGE_CHECK = ExperimentConfig(
    label="coverage_check_off",
    coverage_check_enabled=False,
    notes=(
        "Phase 1.1 gate disabled — data branch always proceeds to "
        "generate_sql. unanswerable / schema_explore categories will "
        "score badly; existing 32 cases should stay green."
    ),
    extra_tags=("a5", "coverage_check_off"),
)

WITHOUT_PATTERNS_DETECTION = ExperimentConfig(
    label="patterns_detection_off",
    patterns_detection_enabled=False,
    notes=(
        "Phase 1.2 detector disabled — no pattern bullets are merged "
        "into ``insight.bullets`` and ``patterns`` stays empty. "
        "``has_pattern`` cases will fail their assertion; everything "
        "else should stay flat."
    ),
    extra_tags=("a6", "patterns_detection_off"),
)
