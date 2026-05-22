"""Tests for no-rollback sandbox exit behavior."""

import subprocess
import shutil
from pathlib import Path

import pytest

from umbrella.control_plane.sandbox_self_edit import (
    SandboxSession,
    exit_sandbox,
)


@pytest.fixture(autouse=True)
def _require_git() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not available on PATH")


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )


def _init_repo(repo: Path) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "tests@example.com")
    _run(repo, "config", "user.name", "Tests")
    (repo / "umbrella").mkdir()
    (repo / "umbrella" / "core.py").write_text("X = 1\n", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "baseline")
    return _run(repo, "rev-parse", "HEAD").stdout.strip()


def _commit(repo: Path, rel_paths: list[str], msg: str) -> str:
    for rel in rel_paths:
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", msg)
    return _run(repo, "rev-parse", "HEAD").stdout.strip()


def test_git_clean_fd_removes_untracked_workspace_sources(tmp_path: Path) -> None:
    """Document why phase_run must not use rollback sandbox.

    ``exit_sandbox`` (git_stash mode) runs ``git clean -fd`` on the repo root,
    which deletes untracked generated files under ``workspaces/<id>/``.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    ws = repo / "workspaces" / "civilization"
    (ws / "src" / "civilization").mkdir(parents=True)
    source = ws / "src" / "civilization" / "state.py"
    source.write_text("class GameState: pass\n", encoding="utf-8")
    assert source.is_file()

    _run(repo, "clean", "-fd")

    assert not source.is_file()


def test_exit_sandbox_does_not_reset_or_cherry_pick(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    baseline = _init_repo(repo)
    candidate_sha = _commit(repo, ["umbrella/core.py"], "candidate only")
    session = SandboxSession(
        session_id="s1",
        task_id="t1",
        repo_root=repo,
        snapshot_method="git_stash",
        baseline_sha=baseline,
        workspace_id="wid",
        original_branch="main",
    )

    exit_sandbox(session)

    assert _run(repo, "rev-parse", "HEAD").stdout.strip() == candidate_sha
    assert session.rollback_ok is True
