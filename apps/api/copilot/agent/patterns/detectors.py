"""Pure-stat pattern detectors (Phase 1.2 / ADR 0017).

Each detector receives the SQL result rows + helpful metadata, runs a
**deterministic, numpy-only** computation, and returns a list of
``Finding`` envelopes. The agent's ``detect_patterns_node`` later asks
the LLM to translate those structured findings into natural-language
bullets, but the statistics themselves never go through a model.

Design rules:

* No scipy. Mann-Kendall, OLS, and z-tests are all expressible in
  numpy primitives with at most ~20 lines of code. Skipping scipy
  keeps the production image ~90 MB lighter.
* No pandas. The rows arrive as ``list[dict]`` straight from
  ``run_select``; we never materialise a DataFrame just to slice
  one column.
* Every detector is a pure function with the signature
  ``(rows, ...) -> list[Finding]``. Composability over inheritance.
* Detectors **return an empty list** on any "not applicable" input
  (too few rows, no numeric column, etc.) rather than raising. The
  node treats len(findings) == 0 as "skip the rendering LLM call".

The findings the LLM sees are intentionally bounded: a single turn
emits at most ``MAX_FINDINGS_PER_TURN`` (the most "interesting" ones,
ranked by severity). The renderer is therefore deterministic in shape
and the chip / bullet UI stays compact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


FindingKind = Literal["outlier", "trend"]
"""Phase 1.2 ships two kinds. Future phases may add ``concentration``,
``seasonality``, ``sparse_column``, etc.; keep the literal explicit so
mypy catches typos."""


Severity = Literal["info", "notable", "high"]
"""How loudly the finding should be reported.

* ``info``    — visible, but the renderer may collapse it under
                "and 3 minor observations".
* ``notable`` — surfaced as a regular bullet.
* ``high``    — surfaced prominently; the renderer should put this
                bullet first.
"""


@dataclass(frozen=True)
class Finding:
    """One structured observation produced by a detector.

    ``payload`` carries detector-specific evidence (e.g. the outlier
    value, z-score, slope, R²). The renderer reads it to write a
    sentence that mentions specific numbers; the front-end can also
    use the structured form for future chart annotations without
    re-running statistics.
    """

    kind: FindingKind
    column: str
    """The result-set column the finding describes. ``"*"`` for
    cross-column observations (none in v1 but reserved)."""

    severity: Severity
    description_key: str
    """Stable identifier for the type of observation. The renderer
    uses this as a hint for the natural-language template. Examples:
    ``"high_value_outlier"`` / ``"low_value_outlier"`` / ``"trend_up"`` /
    ``"trend_down"``."""

    payload: dict[str, Any] = field(default_factory=dict)
    """Detector-specific evidence. Always JSON-serialisable so the
    finding can travel verbatim through the SSE stream + AskResponse."""


# ---------------------------------------------------------------------------
# Constants — tunable from one place rather than scattered through code.
# ---------------------------------------------------------------------------


MIN_ROWS_FOR_OUTLIER = 4
"""Below 4 rows the IQR / z-score numerics are essentially undefined.
Bumping this past 4 trades recall for fewer false positives — 4 is
the empirical sweet spot on Northwind-sized result sets."""

MIN_ROWS_FOR_TREND = 5
"""Trend tests need at least 5 points before the slope is meaningful.
Linear regression on 3-4 points is mostly noise."""

OUTLIER_IQR_K = 1.5
"""Tukey's fence multiplier. 1.5 is the textbook default for
"mild outliers"; 3.0 marks "extreme outliers". We report severity
based on which side of 3.0 the observation falls."""

OUTLIER_Z_NOTABLE = 2.0
"""Z-score absolute value above which we mark severity ``notable``."""

OUTLIER_Z_HIGH = 3.0
"""Z-score absolute value above which we mark severity ``high``."""

TREND_R2_NOTABLE = 0.5
"""R² threshold for emitting a trend finding at all. Below this the
relationship is too weak to claim a trend with a straight face."""

TREND_R2_HIGH = 0.85
"""R² threshold for marking severity ``high``. With this much
explained variance the relationship is robust."""

TREND_MIN_RELATIVE_SLOPE = 0.05
"""Absolute(slope) / abs(mean(y)) below which we suppress the
finding even if R² is high. Stops "perfectly flat" data
("99.0, 99.0, 99.0, 99.0, 99.0") from being called a trend."""

MAX_FINDINGS_PER_TURN = 4
"""Cap on the total number of findings the node hands to the
renderer. Ranked by severity then by absolute strength so the most
notable observations always make the cut."""


# ---------------------------------------------------------------------------
# Helpers — extract one numeric column from list[dict] rows
# ---------------------------------------------------------------------------


def _numeric_columns(rows: list[dict[str, Any]]) -> list[str]:
    """Return columns that look numeric across ALL rows.

    A column is numeric if every non-None value coerces to a finite
    float. We deliberately reject columns where even a single value
    is non-numeric — ``None`` is OK (treated as missing), but a
    stringly-typed entry means the column is categorical and stats
    are meaningless.
    """
    if not rows:
        return []
    cols: list[str] = []
    candidates = list(rows[0].keys())
    for col in candidates:
        values = [r.get(col) for r in rows]
        nonnull = [v for v in values if v is not None]
        if not nonnull:
            continue
        try:
            arr = np.asarray(nonnull, dtype=float)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(arr).all():
            continue
        cols.append(col)
    return cols


def _column_values(rows: list[dict[str, Any]], col: str) -> np.ndarray:
    """Pull ``rows[*][col]`` into a 1-D float ndarray, dropping NULLs.

    The caller is responsible for checking the column is numeric;
    we don't re-validate here (kept hot-loop cheap).
    """
    values = [r.get(col) for r in rows if r.get(col) is not None]
    return np.asarray(values, dtype=float)


def _label_for(rows: list[dict[str, Any]], col: str, value: float) -> str | None:
    """Return a human-readable label for the row that produced ``value``.

    Strategy: pick the first non-numeric column (the "dimension" in
    BI-speak) and return its value at the matching row. Falls back
    to a 1-based index when no dimension column exists ("row 7").

    Lives here, not in the renderer, so the renderer prompt sees a
    cleanly-named outlier ("USA: 13 customers") instead of a row
    index ("Row 1 of 21").
    """
    candidates = [
        k for k in rows[0].keys()
        if k != col and isinstance(rows[0].get(k), str)
    ]
    for row in rows:
        if row.get(col) is None:
            continue
        try:
            if float(row[col]) == float(value):
                if candidates:
                    return str(row[candidates[0]])
                idx = rows.index(row) + 1
                return f"row {idx}"
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Outlier detector — Tukey IQR + z-score
# ---------------------------------------------------------------------------


def detect_outliers(rows: list[dict[str, Any]]) -> list[Finding]:
    """Flag rows whose numeric column value falls outside Tukey's IQR
    fence on at least one numeric column.

    We use IQR (robust to skew) for the gating decision and report
    z-score (intuitive for users) alongside in the payload. Severity
    cascades through the constant thresholds above.

    Returns an empty list when:
      * fewer than ``MIN_ROWS_FOR_OUTLIER`` rows, OR
      * no numeric column, OR
      * the column is constant (IQR == 0).
    """
    if len(rows) < MIN_ROWS_FOR_OUTLIER:
        return []

    findings: list[Finding] = []
    for col in _numeric_columns(rows):
        values = _column_values(rows, col)
        if values.size < MIN_ROWS_FOR_OUTLIER:
            continue

        q1, q3 = np.percentile(values, [25, 75])
        iqr = q3 - q1
        if iqr <= 0:
            continue
        lo_fence = q1 - OUTLIER_IQR_K * iqr
        hi_fence = q3 + OUTLIER_IQR_K * iqr

        mean = float(np.mean(values))
        # ``ddof=1`` for sample std — matches every spreadsheet default
        # and avoids inflating z-scores on tiny result sets.
        std = float(np.std(values, ddof=1))
        if std == 0:
            continue

        for v in values:
            if v < lo_fence or v > hi_fence:
                z = (float(v) - mean) / std
                side: str = "high_value_outlier" if z > 0 else "low_value_outlier"
                abs_z = abs(z)
                if abs_z >= OUTLIER_Z_HIGH:
                    severity: Severity = "high"
                elif abs_z >= OUTLIER_Z_NOTABLE:
                    severity = "notable"
                else:
                    severity = "info"

                findings.append(
                    Finding(
                        kind="outlier",
                        column=col,
                        severity=severity,
                        description_key=side,
                        payload={
                            "value": float(v),
                            "z_score": round(z, 2),
                            "mean": round(mean, 4),
                            "std": round(std, 4),
                            "q1": round(float(q1), 4),
                            "q3": round(float(q3), 4),
                            "label": _label_for(rows, col, float(v)),
                        },
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# Trend detector — OLS slope + R² on monotonic x
# ---------------------------------------------------------------------------


def detect_trend(rows: list[dict[str, Any]]) -> list[Finding]:
    """Fit a simple linear regression to each numeric column against
    row order, and report a trend when R² + relative slope are both
    above their thresholds.

    Why row order (not an inferred temporal column): the SQL writer's
    job is to put rows in the order the user asked for. If the
    user asked for monthly orders ordered by month, row order *is*
    the time axis. If they asked for top-10 unordered, row order is
    arbitrary and the R² will collapse — at which point we don't
    report a trend, which is the right behaviour.

    Returns empty list when:
      * fewer than ``MIN_ROWS_FOR_TREND`` rows, OR
      * no numeric column, OR
      * relative slope is negligible (constant), OR
      * R² below ``TREND_R2_NOTABLE``.
    """
    if len(rows) < MIN_ROWS_FOR_TREND:
        return []

    findings: list[Finding] = []
    for col in _numeric_columns(rows):
        y = _column_values(rows, col)
        if y.size < MIN_ROWS_FOR_TREND:
            continue
        x = np.arange(y.size, dtype=float)

        # OLS via numpy. polyfit returns (slope, intercept); residuals
        # is the squared-error sum which we use to derive R².
        slope, intercept = np.polyfit(x, y, deg=1)
        y_pred = slope * x + intercept
        ss_res = float(np.sum((y - y_pred) ** 2))
        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        if ss_tot == 0:
            # constant column — no trend
            continue
        r2 = 1.0 - ss_res / ss_tot

        mean_y = float(np.mean(np.abs(y))) or 1.0
        rel_slope = abs(float(slope)) / mean_y
        if rel_slope < TREND_MIN_RELATIVE_SLOPE:
            continue
        if r2 < TREND_R2_NOTABLE:
            continue

        if r2 >= TREND_R2_HIGH:
            severity: Severity = "high"
        else:
            severity = "notable"

        direction = "trend_up" if slope > 0 else "trend_down"
        findings.append(
            Finding(
                kind="trend",
                column=col,
                severity=severity,
                description_key=direction,
                payload={
                    "slope_per_step": round(float(slope), 4),
                    "r_squared": round(r2, 3),
                    "first_value": round(float(y[0]), 4),
                    "last_value": round(float(y[-1]), 4),
                    "delta": round(float(y[-1] - y[0]), 4),
                    "delta_pct": (
                        round(100.0 * (y[-1] - y[0]) / y[0], 1)
                        if y[0] != 0
                        else None
                    ),
                    "n_points": int(y.size),
                },
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Aggregator — run every detector + cap + rank
# ---------------------------------------------------------------------------


_SEVERITY_RANK: dict[Severity, int] = {"high": 0, "notable": 1, "info": 2}


def _strength(f: Finding) -> float:
    """Per-kind tie-breaker used after severity. Higher = more notable.

    For outliers we use absolute z-score; for trends we use R². Both
    are already in the payload so this is essentially free.
    """
    if f.kind == "outlier":
        return abs(float(f.payload.get("z_score", 0.0)))
    if f.kind == "trend":
        return float(f.payload.get("r_squared", 0.0))
    return 0.0


def detect_patterns(rows: list[dict[str, Any]]) -> list[Finding]:
    """Run every detector and return the top ``MAX_FINDINGS_PER_TURN``
    findings ranked by (severity, strength).

    The cap exists for two reasons:
      1. The renderer LLM call has a fixed prompt budget; raw findings
         > N would balloon costs without proportional value.
      2. The UI's ``insight.bullets`` is already capped at ~4; pushing
         5+ pattern bullets would crowd the legacy bullets out.
    """
    findings = [
        *detect_outliers(rows),
        *detect_trend(rows),
    ]
    findings.sort(key=lambda f: (_SEVERITY_RANK[f.severity], -_strength(f)))
    return findings[:MAX_FINDINGS_PER_TURN]


# ---------------------------------------------------------------------------
# Serialisation helper (used by the node + tests)
# ---------------------------------------------------------------------------


def finding_to_dict(f: Finding) -> dict[str, Any]:
    """JSON-friendly view of a Finding. The node uses this to put
    findings into ``state.patterns`` and the AskResponse payload."""
    return {
        "kind": f.kind,
        "column": f.column,
        "severity": f.severity,
        "description_key": f.description_key,
        "payload": dict(f.payload),
    }
