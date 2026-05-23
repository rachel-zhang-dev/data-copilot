"""Markdown rendering of eval results.

Pure function: ``Comparison`` -> ``str``. Easy to snapshot-test and
to commit into ``docs/eval/`` so the historical state of the agent
is preserved alongside the code.
"""

from __future__ import annotations

from datetime import UTC, datetime

from copilot.eval.experiments._common import Comparison
from copilot.eval.runner import ExperimentResult


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _signed_pp(delta: float) -> str:
    """Format a probability delta as signed percentage points."""
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta * 100:.1f} pp"


def _signed_int(delta: float) -> str:
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.0f}"


def _signed_float(delta: float, digits: int = 2) -> str:
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.{digits}f}"


def render_single(result: ExperimentResult) -> str:
    """Render one ExperimentResult as a standalone markdown report.

    Used for ad-hoc one-off runs that aren't part of an A/B.
    """
    lines = [
        f"# Eval — {result.config.label}",
        f"> Generated {datetime.now(tz=UTC).isoformat(timespec='seconds')}",
        "",
        result.config.notes or "",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| cases | {result.total} |",
        f"| success_rate | {_pct(result.success_rate)} |",
        f"| avg_attempts | {result.avg_attempts:.2f} |",
        f"| avg_latency_ms | {result.avg_latency_ms:.0f} |",
        f"| avg_total_tokens | {result.avg_total_tokens:.0f} |",
        "",
        _render_by_category_single(result),
        "",
        _render_failures(result, max_samples=10),
    ]
    return "\n".join(lines)


def render_comparison(comp: Comparison) -> str:
    """Render an A/B Comparison as markdown.

    Layout: header, summary delta table, per-category delta table,
    sample failures from baseline that the treatment fixed.
    """
    b, t = comp.baseline, comp.treatment

    lines: list[str] = []
    lines.append(f"# Eval A/B — {comp.name}")
    lines.append(
        f"> Generated {datetime.now(tz=UTC).isoformat(timespec='seconds')} · "
        f"{b.total} cases per side"
    )
    lines.append("")
    lines.append(f"- **baseline**: `{b.config.label}` — {b.config.notes}")
    lines.append(f"- **treatment**: `{t.config.label}` — {t.config.notes}")
    lines.append("")

    # Summary delta table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | baseline | treatment | Δ |")
    lines.append("|---|---|---|---|")
    lines.append(
        f"| success_rate | {_pct(b.success_rate)} | {_pct(t.success_rate)} | "
        f"**{_signed_pp(comp.success_rate_delta)}** |"
    )
    lines.append(
        f"| avg_attempts | {b.avg_attempts:.2f} | {t.avg_attempts:.2f} | "
        f"{_signed_float(comp.avg_attempts_delta)} |"
    )
    lines.append(
        f"| avg_latency_ms | {b.avg_latency_ms:.0f} | {t.avg_latency_ms:.0f} | "
        f"{_signed_int(comp.avg_latency_ms_delta)} |"
    )
    lines.append(
        f"| avg_total_tokens | {b.avg_total_tokens:.0f} | {t.avg_total_tokens:.0f} | "
        f"{_signed_int(comp.avg_total_tokens_delta)} |"
    )
    lines.append("")

    lines.append(_render_by_category_comparison(comp))
    lines.append("")
    lines.append(_render_fixed_by_treatment(comp, max_samples=10))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_by_category_single(result: ExperimentResult) -> str:
    by_cat = result.by_category()
    if not by_cat:
        return "_(no cases ran)_"
    lines = [
        "## By category",
        "",
        "| Category | n | success_rate | avg_attempts |",
        "|---|---|---|---|",
    ]
    for cat in sorted(by_cat):
        d = by_cat[cat]
        lines.append(
            f"| {cat} | {int(d['n'])} | {_pct(d['success_rate'])} | {d['avg_attempts']:.2f} |"
        )
    return "\n".join(lines)


def _render_by_category_comparison(comp: Comparison) -> str:
    b_by = comp.baseline.by_category()
    t_by = comp.treatment.by_category()
    cats = sorted(set(b_by) | set(t_by))
    if not cats:
        return ""

    lines = [
        "## By category",
        "",
        "| Category | n | baseline | treatment | Δ |",
        "|---|---|---|---|---|",
    ]
    for cat in cats:
        b = b_by.get(cat)
        t = t_by.get(cat)
        if b is None or t is None:
            continue
        delta = t["success_rate"] - b["success_rate"]
        bold = "**" if abs(delta) >= 0.10 else ""  # call out 10pp+ shifts
        lines.append(
            f"| {cat} | {int(b['n'])} | {_pct(b['success_rate'])} | "
            f"{_pct(t['success_rate'])} | {bold}{_signed_pp(delta)}{bold} |"
        )
    return "\n".join(lines)


def _render_failures(result: ExperimentResult, *, max_samples: int) -> str:
    fails = result.failures()[:max_samples]
    if not fails:
        return "## Failures\n\n_(none)_"
    lines = ["## Failures (sample)", ""]
    for o in fails:
        lines.append(f"- **{o.case.id}** ({o.case.category})")
        lines.append(f"  question: {o.case.question}")
        lines.append(f"  sql: `{o.run.sql}`")
        for reason in o.grade.fail_reasons():
            lines.append(f"  - ✗ {reason}")
        lines.append("")
    return "\n".join(lines)


def _render_fixed_by_treatment(comp: Comparison, *, max_samples: int) -> str:
    """Cases that baseline failed but treatment passed — the most
    interesting evidence in any A/B."""
    b_pass = {o.case.id: o for o in comp.baseline.outcomes}
    t_pass = {o.case.id: o for o in comp.treatment.outcomes}

    fixed = [
        cid
        for cid in b_pass
        if cid in t_pass and not b_pass[cid].grade.passed and t_pass[cid].grade.passed
    ]
    regressed = [
        cid
        for cid in b_pass
        if cid in t_pass and b_pass[cid].grade.passed and not t_pass[cid].grade.passed
    ]

    lines: list[str] = []

    lines.append("## Fixed by treatment")
    lines.append("")
    if not fixed:
        lines.append("_(none)_")
    else:
        for cid in fixed[:max_samples]:
            b = b_pass[cid]
            t = t_pass[cid]
            lines.append(f"- **{cid}** ({b.case.category})")
            lines.append(f"  question: {b.case.question}")
            lines.append(f"  baseline sql: `{b.run.sql}`")
            lines.append(f"  treatment sql: `{t.run.sql}`")
            lines.append("")

    lines.append("## Regressions (treatment broke what baseline got right)")
    lines.append("")
    if not regressed:
        lines.append("_(none)_")
    else:
        for cid in regressed[:max_samples]:
            b = b_pass[cid]
            t = t_pass[cid]
            lines.append(f"- **{cid}** ({b.case.category})")
            lines.append(f"  question: {b.case.question}")
            lines.append(f"  baseline sql: `{b.run.sql}` (passed)")
            lines.append(f"  treatment sql: `{t.run.sql}` (failed)")
            for reason in t.grade.fail_reasons():
                lines.append(f"  - ✗ {reason}")
            lines.append("")

    return "\n".join(lines)
