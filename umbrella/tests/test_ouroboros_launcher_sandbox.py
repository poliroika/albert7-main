"""Launcher sandbox policy: phase_run must not enter rollback sandbox."""

from pathlib import Path
from unittest.mock import patch

import pytest

from umbrella.integration.ouroboros_launcher import (
    OuroborosLauncher,
    _task_requires_product_self_edit_sandbox,
    _unlogged_workspace_source_loss,
    _workspace_source_manifest,
)


def test_task_requires_product_self_edit_sandbox_phase_run_false() -> None:
    assert (
        _task_requires_product_self_edit_sandbox({"type": "phase_run", "id": "t1"})
        is False
    )


def test_task_requires_product_self_edit_sandbox_self_improve_true() -> None:
    assert (
        _task_requires_product_self_edit_sandbox(
            {"type": "other", "self_improve": True, "id": "t2"}
        )
        is True
    )


def test_phase_run_does_not_enter_product_self_edit_sandbox(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "workspaces" / "civ").mkdir(parents=True)
    launcher = OuroborosLauncher(repo_root=repo, workspace_id="civ")

    with patch(
        "umbrella.integration.ouroboros_launcher.enter_sandbox",
        side_effect=AssertionError("enter_sandbox must not run for phase_run"),
    ) as enter_mock:
        session = launcher._enter_task_sandbox(
            {"id": "run:execute", "type": "phase_run"},
            workspace_id="civ",
        )
    assert session is None
    enter_mock.assert_not_called()


def test_unlogged_workspace_source_loss_detects_missing_files(tmp_path: Path) -> None:
    ws = tmp_path / "workspaces" / "civilization"
    (ws / "src" / "pkg").mkdir(parents=True)
    path = ws / "src" / "pkg" / "state.py"
    path.write_text("x = 1\n", encoding="utf-8")
    before = _workspace_source_manifest(ws)
    assert "src/pkg/state.py" in before
    loss = _unlogged_workspace_source_loss(before, {})
    assert loss is not None
    assert loss["reason"] == "unlogged_workspace_source_loss"
    assert "src/pkg/state.py" in loss["missing_files"]


def test_execute_manifest_does_not_expose_sandbox_self_edit() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "umbrella" / "phases" / "manifests" / "execute.yaml").read_text(
        encoding="utf-8"
    )
    assert "sandbox_self_edit" not in text
