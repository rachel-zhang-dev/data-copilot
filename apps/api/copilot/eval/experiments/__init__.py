"""A/B experiment drivers.

Each driver pairs a baseline config (production defaults) with one
treatment config that flips exactly one feature flag. The driver
calls ``run_eval`` twice and returns a ``Comparison`` of the two
results, which the report renderer turns into a delta table.
"""

from copilot.eval.experiments.analyst import run_analyst_ab
from copilot.eval.experiments.coverage_check import run_coverage_check_ab
from copilot.eval.experiments.critic import run_critic_ab
from copilot.eval.experiments.dialogue_context import run_dialogue_context_ab
from copilot.eval.experiments.investigate_mode import run_investigate_mode_ab
from copilot.eval.experiments.patterns_detection import run_patterns_detection_ab
from copilot.eval.experiments.schema_rag import run_schema_rag_ab
from copilot.eval.experiments.self_healing import run_self_healing_ab

__all__ = [
    "run_analyst_ab",
    "run_coverage_check_ab",
    "run_critic_ab",
    "run_dialogue_context_ab",
    "run_investigate_mode_ab",
    "run_patterns_detection_ab",
    "run_schema_rag_ab",
    "run_self_healing_ab",
]
