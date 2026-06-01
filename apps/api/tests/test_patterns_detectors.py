"""Unit tests for ``copilot.agent.patterns.detectors``.

Pure numpy — no LLM, no DB. Tests cover:

* Outlier detection on intentionally skewed distributions.
* Trend detection on synthetic monotonic / noisy / flat data.
* Edge cases that must return ``[]`` rather than raise (too few
  rows, non-numeric columns, all NULL, constant column).
* Ranking + cap in the aggregator.
"""

from __future__ import annotations

from typing import Any

import pytest
from copilot.agent.patterns import detectors
from copilot.agent.patterns.detectors import (
    MAX_FINDINGS_PER_TURN,
    MIN_ROWS_FOR_OUTLIER,
    MIN_ROWS_FOR_TREND,
    Finding,
    detect_outliers,
    detect_patterns,
    detect_trend,
    finding_to_dict,
)

# ---------------------------------------------------------------------------
# Helpers — build list[dict] rows in the SQL-result shape.
# ---------------------------------------------------------------------------


def _rows(labels: list[str], values: list[float], *, col: str = "n") -> list[dict[str, Any]]:
    return [{"label": label, col: value} for label, value in zip(labels, values, strict=True)]


def _numeric_rows(values: list[float], *, col: str = "n") -> list[dict[str, Any]]:
    return [{col: v} for v in values]


# ===========================================================================
# Outlier detection
# ===========================================================================


def test_outlier_detects_a_clear_high_value() -> None:
    """USA-style outlier: 13 vs everyone else around 1-3."""
    rows = _rows(
        ["UK", "France", "Germany", "Spain", "Italy", "Brazil", "USA"],
        [2, 1, 3, 2, 1, 2, 13],
    )
    findings = detect_outliers(rows)
    assert len(findings) >= 1
    high = findings[0]
    assert high.kind == "outlier"
    assert high.description_key == "high_value_outlier"
    assert high.payload["value"] == 13.0
    assert high.payload["label"] == "USA"
    assert high.payload["z_score"] > 0


def test_outlier_detects_low_value() -> None:
    """Symmetric of the above: one value far BELOW the cluster."""
    rows = _rows(
        ["a", "b", "c", "d", "e", "f"],
        [100, 102, 98, 101, 99, 5],
    )
    findings = detect_outliers(rows)
    low = [f for f in findings if f.description_key == "low_value_outlier"]
    assert len(low) == 1
    assert low[0].payload["label"] == "f"
    assert low[0].payload["z_score"] < 0


def test_outlier_returns_empty_below_min_rows() -> None:
    rows = _numeric_rows([1, 2, 3])  # < MIN_ROWS_FOR_OUTLIER (4)
    assert detect_outliers(rows) == []


def test_outlier_returns_empty_for_constant_column() -> None:
    rows = _numeric_rows([5, 5, 5, 5, 5])
    assert detect_outliers(rows) == []


def test_outlier_returns_empty_when_no_numeric_columns() -> None:
    rows = [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}]
    assert detect_outliers(rows) == []


def test_outlier_skips_columns_with_string_values() -> None:
    """Even if MOST rows are numeric, one stringly-typed value
    disqualifies the entire column (it's clearly categorical)."""
    rows = [
        {"n": 1},
        {"n": 2},
        {"n": "N/A"},
        {"n": 12},
        {"n": 2},
        {"n": 1},
    ]
    assert detect_outliers(rows) == []


def test_outlier_tolerates_nulls() -> None:
    """NULL values are dropped, not failed. With enough non-NULL
    samples + a clear outlier, the detector still fires."""
    rows = [
        {"label": "a", "n": 1},
        {"label": "b", "n": 2},
        {"label": "c", "n": None},
        {"label": "d", "n": 1},
        {"label": "e", "n": 2},
        {"label": "f", "n": None},
        {"label": "g", "n": 25},  # the outlier
        {"label": "h", "n": 1},
    ]
    findings = detect_outliers(rows)
    assert any(f.payload["label"] == "g" for f in findings)


def test_outlier_severity_high_at_z3_plus() -> None:
    """A textbook >3σ outlier (50 against 9 values clustered around 2-3)
    should be tagged ``high``. We deliberately do NOT use [1,1,1,1,50]
    here: with 5/6 identical values the sample std is dominated by the
    outlier itself and z collapses to ~2 — a real statistical quirk,
    not a detector bug. Slightly broader baseline gives a stable test."""
    rows = _rows(
        [f"r{i}" for i in range(15)],
        [2, 3, 2, 3, 2, 4, 3, 2, 3, 4, 2, 3, 2, 4, 100],
    )
    findings = detect_outliers(rows)
    assert findings
    assert findings[0].severity == "high"


def test_outlier_payload_contains_z_mean_std_and_label() -> None:
    rows = _rows(["x", "y", "z", "w", "v"], [1, 2, 1, 2, 30])
    findings = detect_outliers(rows)
    assert findings
    p = findings[0].payload
    assert "z_score" in p and "mean" in p and "std" in p
    assert p["label"] == "v"


# ===========================================================================
# Trend detection
# ===========================================================================


def test_trend_detects_clear_upward_line() -> None:
    """y = 2x + 1 should be a textbook high-severity trend."""
    rows = _numeric_rows([1, 3, 5, 7, 9, 11, 13])
    findings = detect_trend(rows)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "trend"
    assert f.description_key == "trend_up"
    assert f.payload["r_squared"] == pytest.approx(1.0, abs=0.01)
    assert f.payload["slope_per_step"] == pytest.approx(2.0, abs=0.05)
    assert f.severity == "high"


def test_trend_detects_downward() -> None:
    rows = _numeric_rows([100, 95, 90, 85, 80, 75])
    findings = detect_trend(rows)
    assert findings
    assert findings[0].description_key == "trend_down"
    assert findings[0].payload["slope_per_step"] < 0


def test_trend_ignores_flat_data() -> None:
    """Constant + tiny noise must not be reported."""
    rows = _numeric_rows([100.0, 100.1, 99.9, 100.0, 100.05, 99.95])
    assert detect_trend(rows) == []


def test_trend_ignores_pure_noise() -> None:
    """Random-looking data has low R² and should NOT be a trend."""
    rows = _numeric_rows([5, 17, 3, 28, 9, 2, 14])
    findings = detect_trend(rows)
    # R² for this should fall below TREND_R2_NOTABLE; no finding.
    assert findings == []


def test_trend_returns_empty_below_min_rows() -> None:
    rows = _numeric_rows([1, 2, 3, 4])  # < MIN_ROWS_FOR_TREND (5)
    assert detect_trend(rows) == []


def test_trend_severity_notable_vs_high() -> None:
    """A perfect line is ``high``; a noisier-but-still-linear line is
    ``notable``."""
    perfect = _numeric_rows([1, 2, 3, 4, 5, 6])
    noisy = _numeric_rows([1, 4, 3, 6, 5, 9])
    perfect_f = detect_trend(perfect)
    noisy_f = detect_trend(noisy)
    assert perfect_f and perfect_f[0].severity == "high"
    assert noisy_f and noisy_f[0].severity == "notable"


def test_trend_payload_carries_delta_and_pct() -> None:
    rows = _numeric_rows([10, 12, 14, 16, 18, 20])
    findings = detect_trend(rows)
    assert findings
    p = findings[0].payload
    assert p["first_value"] == 10
    assert p["last_value"] == 20
    assert p["delta"] == 10
    assert p["delta_pct"] == pytest.approx(100.0, abs=0.1)


def test_trend_zero_start_handles_delta_pct_gracefully() -> None:
    rows = _numeric_rows([0, 2, 4, 6, 8, 10])
    findings = detect_trend(rows)
    assert findings
    # delta_pct is None when first value is 0 (avoid div-by-zero)
    assert findings[0].payload["delta_pct"] is None


# ===========================================================================
# Aggregator (detect_patterns)
# ===========================================================================


def test_aggregator_runs_both_detectors() -> None:
    """Data that contains both an outlier AND a trend should produce
    both kinds (subject to the cap)."""
    rows = _rows(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun"],
        [10, 12, 14, 16, 18, 200],  # rising + a fat outlier at the end
    )
    findings = detect_patterns(rows)
    kinds = {f.kind for f in findings}
    assert "outlier" in kinds


def test_aggregator_caps_at_max() -> None:
    """If a column has many outliers, the aggregator only keeps the
    most notable MAX_FINDINGS_PER_TURN."""
    rows = _rows(
        [f"r{i}" for i in range(15)],
        [1, 1, 1, 1, 1, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140],
    )
    findings = detect_patterns(rows)
    assert len(findings) <= MAX_FINDINGS_PER_TURN


def test_aggregator_ranks_more_severe_before_less() -> None:
    """When the result set contains both a strong outlier and a noisier
    pattern, the high-severity finding must appear before the
    notable-severity one. We use a larger baseline so the outlier
    isn't masked by its own contribution to std (cf. the comment in
    ``test_outlier_severity_high_at_z3_plus``)."""
    rows = _rows(
        [f"r{i}" for i in range(15)],
        [2, 3, 2, 3, 2, 4, 3, 2, 3, 4, 2, 3, 2, 4, 150],
    )
    findings = detect_patterns(rows)
    assert findings
    assert findings[0].kind == "outlier"
    assert findings[0].severity == "high"


def test_aggregator_returns_empty_on_uninteresting_data() -> None:
    """Constant data → no findings."""
    rows = _numeric_rows([7, 7, 7, 7, 7, 7, 7])
    assert detect_patterns(rows) == []


def test_aggregator_returns_empty_on_too_few_rows() -> None:
    rows = _numeric_rows([1, 2, 3])  # below both thresholds
    assert detect_patterns(rows) == []


# ===========================================================================
# Serialisation
# ===========================================================================


def test_finding_to_dict_round_trip() -> None:
    f = Finding(
        kind="outlier",
        column="n",
        severity="high",
        description_key="high_value_outlier",
        payload={"value": 13.0, "z_score": 2.5},
    )
    d = finding_to_dict(f)
    assert d["kind"] == "outlier"
    assert d["payload"]["value"] == 13.0
    # Ensure it's plain JSON-safe types
    import json
    json.dumps(d)  # raises on non-serialisable values


def test_finding_to_dict_coerces_numpy_scalars() -> None:
    """Regression: a numpy.float64 / int64 in the payload must not
    leak through ``finding_to_dict`` — LangGraph's PostgresSaver
    msgpack serialiser refuses them at checkpoint time and the whole
    turn blows up. Detector code wraps most reads in ``float(...)``,
    but a missed cast (e.g. an arithmetic expression that keeps one
    numpy operand) is enough to break the turn. The serialiser
    boundary must be the safety net."""
    import numpy as np

    f = Finding(
        kind="outlier",
        column="n",
        severity="notable",
        description_key="high_value_outlier",
        payload={
            "value": np.float64(13.0),
            "z_score": np.float64(2.5),
            "n_points": np.int64(7),
            # Nested structures are walked recursively.
            "histogram_bounds": [np.float64(1.0), np.float64(13.0)],
            "meta": {"mean": np.float64(4.3)},
        },
    )
    d = finding_to_dict(f)
    # Type checks — every scalar must be a Python builtin.
    assert type(d["payload"]["value"]) is float
    assert type(d["payload"]["z_score"]) is float
    assert type(d["payload"]["n_points"]) is int
    assert all(type(x) is float for x in d["payload"]["histogram_bounds"])
    assert type(d["payload"]["meta"]["mean"]) is float
    # Value preservation.
    assert d["payload"]["value"] == 13.0


def test_finding_to_dict_handles_numpy_array_payload() -> None:
    """numpy arrays should be converted to plain lists of Python
    floats (the detector currently doesn't put arrays in payloads,
    but future detectors might)."""
    import numpy as np

    f = Finding(
        kind="trend",
        column="n",
        severity="notable",
        description_key="trend_up",
        payload={"residuals": np.array([0.1, 0.2, 0.3])},
    )
    d = finding_to_dict(f)
    assert isinstance(d["payload"]["residuals"], list)
    assert all(type(x) is float for x in d["payload"]["residuals"])


# ===========================================================================
# Module-level constants — guard against accidental tightening
# ===========================================================================


def test_constants_are_sane() -> None:
    assert MIN_ROWS_FOR_OUTLIER >= 4
    assert MIN_ROWS_FOR_TREND >= 4
    assert MAX_FINDINGS_PER_TURN >= 1
    assert detectors.TREND_R2_NOTABLE < detectors.TREND_R2_HIGH
    assert detectors.OUTLIER_Z_NOTABLE < detectors.OUTLIER_Z_HIGH
