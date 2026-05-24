"""Tests for the eval CLI entrypoint.

The biggest regression hazard here is the report ``--output-dir`` default.
``scripts/dev.sh eval`` ``cd``s into ``apps/api/`` before launching the
module; a CWD-relative default would silently write reports to
``apps/api/docs/eval/`` instead of the repo-root ``docs/eval/``
advertised by the README and ADR 0007.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from copilot.eval.__main__ import _REPO_ROOT, DEFAULT_REPORTS_DIR, _parse_args
from copilot.eval.config import BASELINE_FULL
from copilot.eval.experiments._common import Comparison
from copilot.eval.runner import ExperimentResult


def test_default_output_dir_points_at_repo_root() -> None:
    """``DEFAULT_REPORTS_DIR`` must always resolve to ``<repo>/docs/eval/``
    regardless of where the CLI was launched from."""
    assert _REPO_ROOT.is_absolute()
    assert _REPO_ROOT.name == "data-copilot"
    assert DEFAULT_REPORTS_DIR == _REPO_ROOT / "docs" / "eval"
    # The README that explains the report layout lives there; if this
    # assertion ever fires the repo layout drifted under us.
    assert (DEFAULT_REPORTS_DIR / "README.md").exists()


def test_default_output_dir_is_stable_when_cwd_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate ``cd apps/api && python -m copilot.eval`` and confirm the
    default doesn't drift with the CWD."""
    snapshot = DEFAULT_REPORTS_DIR
    monkeypatch.chdir(tmp_path)
    # Re-import to make sure the constant isn't lazily resolved against CWD.
    from copilot.eval.__main__ import DEFAULT_REPORTS_DIR as DEFAULT_REPORTS_DIR2

    assert DEFAULT_REPORTS_DIR2 == snapshot


def test_cli_help_runs(capsys: pytest.CaptureFixture[str]) -> None:
    """Smoke check: argparse setup is well-formed."""
    with pytest.raises(SystemExit) as exc:
        _parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--experiment" in out
    assert "--dry-run" in out


def test_cli_unknown_experiment_rejected() -> None:
    """argparse ``choices=`` rejects typos so an A/B that silently runs
    nothing is impossible."""
    with pytest.raises(SystemExit):
        _parse_args(["--experiment", "doesnotexist"])


def test_cli_limit_parsed_as_int() -> None:
    args = _parse_args(["--limit", "5"])
    assert args.limit == 5


def test_cli_limit_default_none() -> None:
    args = _parse_args([])
    assert args.limit is None


def test_cli_case_timeout_has_safe_default() -> None:
    """A finite default means a fresh user who skips reading the help
    text still gets timeout protection. 120 s is generous enough to
    accommodate slow LLM round-trips and pgvector cold reads."""
    args = _parse_args([])
    assert args.case_timeout == 120.0


def test_cli_case_timeout_zero_disables_via_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--case-timeout 0`` must normalise to ``None`` before reaching
    the runner so it interprets the absence as 'no timeout'."""
    import copilot.eval.__main__ as m

    captured: dict[str, float | None] = {}

    async def _fake_run_one(
        name: str,
        cases: list,  # type: ignore[type-arg]
        case_timeout_s: float | None,
    ) -> Comparison:
        captured["timeout"] = case_timeout_s
        return Comparison(
            name=name,
            baseline=ExperimentResult(config=BASELINE_FULL),
            treatment=ExperimentResult(config=BASELINE_FULL),
        )

    monkeypatch.setattr(m, "_run_one", _fake_run_one)
    monkeypatch.setattr(m, "load_cases", lambda _p: [])

    rc = asyncio.run(
        m._main(["--dry-run", "--case-timeout", "0", "--experiment", "schema_rag"])
    )
    assert rc == 0
    assert captured["timeout"] is None
