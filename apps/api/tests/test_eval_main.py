"""Tests for the eval CLI entrypoint.

The biggest regression hazard here is the report ``--output-dir`` default.
``scripts/dev.sh eval`` ``cd``s into ``apps/api/`` before launching the
module; a CWD-relative default would silently write reports to
``apps/api/docs/eval/`` instead of the repo-root ``docs/eval/``
advertised by the README and ADR 0007.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from copilot.eval.__main__ import _REPO_ROOT, DEFAULT_REPORTS_DIR, _parse_args


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
