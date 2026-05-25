"""CLI entrypoint for the eval harness.

Usage::

    uv run python -m copilot.eval                        # run all 3 A/B
    uv run python -m copilot.eval --experiment schema_rag
    uv run python -m copilot.eval --cases path/to/cases.yaml --output-dir docs/eval
    uv run python -m copilot.eval --limit 3              # smoke-run only 3 cases
    uv run python -m copilot.eval --case-timeout 30      # bound per-case wall time

Designed to be wrapped by ``scripts/dev.sh eval`` so the operator
never has to remember the module path.

Note on ``--dry-run``: it only suppresses **file writes**. LLM, embedding
and database calls still happen. Use ``--limit 1`` (or a small subset of
``--experiment``) when you actually want to minimise API spend while
sanity-checking the harness.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from copilot.eval.cases import DEFAULT_CASES_PATH, load_cases
from copilot.eval.experiments import (
    run_analyst_ab,
    run_dialogue_context_ab,
    run_schema_rag_ab,
    run_self_healing_ab,
)
from copilot.eval.experiments._common import Comparison
from copilot.eval.reports.markdown import render_comparison

log = logging.getLogger("copilot.eval")


# Resolve the default reports directory relative to the repo root rather
# than the current working directory. ``scripts/dev.sh eval`` ``cd``s into
# ``apps/api/`` before invoking this module, so a CWD-relative default
# silently wrote reports to ``apps/api/docs/eval/`` instead of the
# repo-root ``docs/eval/`` advertised in the README and ADR 0007.
_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_REPORTS_DIR = _REPO_ROOT / "docs" / "eval"


_EXPERIMENTS = {
    "schema_rag": run_schema_rag_ab,
    "self_healing": run_self_healing_ab,
    "dialogue_context": run_dialogue_context_ab,
    "analyst": run_analyst_ab,
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="copilot.eval",
        description="Run the data-copilot eval harness (A/B experiments).",
    )
    p.add_argument(
        "--experiment",
        choices=sorted(_EXPERIMENTS),
        action="append",
        help=("Which A/B to run (may be passed multiple times). Default: all three."),
    )
    p.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_PATH,
        help="Path to cases.yaml.",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_REPORTS_DIR,
        help=(
            "Directory to write markdown reports into. Defaults to the "
            "repo-root ``docs/eval/`` regardless of the CWD the CLI was "
            "launched from."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print reports to stdout instead of writing files. NOTE: this "
            "does NOT mock LLM / DB / embedding calls — they still happen. "
            "Use --limit to reduce API spend during smoke checks."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Run only the first N cases (after experiment filtering). "
            "Useful for fast iteration on the harness itself; full reports "
            "should run without --limit."
        ),
    )
    p.add_argument(
        "--case-timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help=(
            "Per-case wall-clock budget. A case that exceeds it is recorded "
            "as a runner_timeout failure and the run continues, so a single "
            "hung LLM call cannot stall the entire eval. Pass 0 to disable."
        ),
    )
    return p.parse_args(argv)


async def _run_one(
    name: str, cases: list, case_timeout_s: float | None  # type: ignore[type-arg]
) -> Comparison:
    runner = _EXPERIMENTS[name]
    log.info("=== experiment: %s ===", name)
    return await runner(cases, case_timeout_s=case_timeout_s)


async def _main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    cases = load_cases(args.cases)
    log.info("loaded %d cases from %s", len(cases), args.cases)

    if args.limit is not None:
        if args.limit <= 0:
            raise SystemExit("--limit must be a positive integer")
        if args.limit < len(cases):
            log.info("--limit=%d → truncating from %d cases", args.limit, len(cases))
            cases = cases[: args.limit]

    selected = args.experiment or list(_EXPERIMENTS)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # argparse coerces missing --case-timeout to the default 120.0; an
    # explicit 0 means "no timeout" and is normalised to None for the
    # runner's API.
    case_timeout_s = args.case_timeout if args.case_timeout and args.case_timeout > 0 else None

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    summary_lines: list[str] = [f"# Eval summary — {timestamp} UTC", ""]

    for name in selected:
        comparison = await _run_one(name, cases, case_timeout_s)
        report = render_comparison(comparison)

        if args.dry_run:
            print(report)
            print()
        else:
            out_path = args.output_dir / f"{timestamp}-{name}.md"
            out_path.write_text(report)
            log.info("wrote %s", out_path)

        summary_lines.append(
            f"- **{name}**: success_rate Δ "
            f"{comparison.success_rate_delta * 100:+.1f} pp · "
            f"tokens Δ {comparison.avg_total_tokens_delta:+.0f} · "
            f"latency Δ {comparison.avg_latency_ms_delta:+.0f} ms"
        )

    summary = "\n".join(summary_lines) + "\n"
    if args.dry_run:
        print("---")
        print(summary)
    else:
        (args.output_dir / f"{timestamp}-summary.md").write_text(summary)
        log.info("wrote summary")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(_main()))
