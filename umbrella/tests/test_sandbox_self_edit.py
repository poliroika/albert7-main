"""Tests for ``umbrella.control_plane.sandbox_self_edit``.

Live sandbox rollback is disabled: entering a sandbox creates an authorization
session for ``sandbox_self_edit``, but it must not stash, branch, reset,
checkout, clean, or otherwise undo agent edits at task end.

Each test creates a throwaway git repository in ``tmp_path`` and drives
the real ``git`` subprocess so we validate actual git semantics (the
same the production code relies on).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from umbrella.control_plane.sandbox_self_edit import (
    capture_candidate_diff,
    enter_sandbox,
    exit_sandbox,
    recover_orphan_sandbox_stashes,
    resolve_snapshot_method,
)


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available on PATH"
)


def _git(
    repo_root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=check,
    )


def _init_repo(repo_root: Path) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    _git(repo_root, "init", "-q", "-b", "main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test")
    (repo_root / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-q", "-m", "init")


def _stash_list(repo_root: Path) -> list[str]:
    result = _git(repo_root, "stash", "list", check=False)
    return [l for l in result.stdout.splitlines() if l.strip()]


class TestExitSandboxHappyPath:
    def test_default_session_does_not_stash_or_rollback(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)

        (repo / "README.md").write_text("user edit\n", encoding="utf-8")
        (repo / "new_untracked.txt").write_text("untracked work\n", encoding="utf-8")

        session = enter_sandbox(repo, task_id="happy-path")
        assert session.snapshot_method == "none"
        assert session.stash_ref is None
        assert (repo / "README.md").read_text(encoding="utf-8") == "user edit\n"
        assert (repo / "new_untracked.txt").exists()

        (repo / "umbrella").mkdir()
        (repo / "umbrella" / "self_edit.py").write_text("# keep me\n", encoding="utf-8")

        result = exit_sandbox(session)

        assert result.rollback_ok is True
        assert result.error == ""
        assert _stash_list(repo) == []
        assert (repo / "README.md").read_text(encoding="utf-8") == "user edit\n"
        assert (repo / "new_untracked.txt").exists()
        assert (repo / "umbrella" / "self_edit.py").exists()


class TestCopySnapshotMode:
    def test_requested_copy_snapshot_is_ignored_and_edits_persist(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "umbrella").mkdir()
        (repo / "umbrella" / "core.py").write_text("local fix\n", encoding="utf-8")

        session = enter_sandbox(repo, task_id="copy-mode", snapshot_method="copy")

        assert session.snapshot_method == "none"
        assert session.snapshot_dir is None
        assert session.stash_ref is None
        assert (repo / "umbrella" / "core.py").read_text(
            encoding="utf-8"
        ) == "local fix\n"

        (repo / "umbrella" / "core.py").write_text("agent scratch\n", encoding="utf-8")
        (repo / "ouroboros").mkdir()
        (repo / "ouroboros" / "temp.py").write_text("print('temp')\n", encoding="utf-8")

        result = exit_sandbox(session)

        assert result.rollback_ok is True
        assert (repo / "umbrella" / "core.py").read_text(
            encoding="utf-8"
        ) == "agent scratch\n"
        assert (repo / "ouroboros" / "temp.py").exists()


class TestCandidateCaptureIsReadOnly:
    """Candidate diff capture must not create commits now that rollback is off."""

    def test_exit_resets_head_back_to_baseline(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        baseline = _git(repo, "rev-parse", "HEAD").stdout.strip()

        session = enter_sandbox(repo, task_id="snapshot-readonly")
        session.baseline_sha = baseline
        session.original_branch = "main"

        ws_dir = repo / "workspaces" / "demo"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "product.py").write_text(
            "print('ouroboros product')\n", encoding="utf-8"
        )
        agent_dir = repo / "umbrella"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "self_edit.py").write_text("# agent self-edit\n", encoding="utf-8")

        diff = capture_candidate_diff(session)
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == baseline

        result = exit_sandbox(session)

        assert result.rollback_ok is True
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == baseline, (
            "capture/exit must not create candidate-snapshot commits"
        )
        assert (ws_dir / "product.py").exists()
        assert (agent_dir / "self_edit.py").exists()


class TestExitSandboxFailedPop:
    def test_exit_does_not_pop_or_touch_existing_stashes(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)

        (repo / "README.md").write_text("user edit\n", encoding="utf-8")
        session = enter_sandbox(repo, task_id="conflict")
        assert session.stash_ref is None

        _git(repo, "stash", "push", "-m", "manual", "--include-untracked", check=False)
        before = _stash_list(repo)

        result = exit_sandbox(session)

        assert result.rollback_ok is True
        assert _stash_list(repo) == before


class TestOrphanRecovery:
    def test_dirty_agent_surface_switches_git_stash_to_copy(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "umbrella").mkdir()
        (repo / "umbrella" / "core.py").write_text("local edit\n", encoding="utf-8")

        assert resolve_snapshot_method(repo, "git_stash") == "copy"
        assert resolve_snapshot_method(repo, "git_branch") == "git_branch"

    def test_applies_orphan_stash_on_clean_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)

        (repo / "README.md").write_text("stashed change\n", encoding="utf-8")
        _git(
            repo,
            "stash",
            "push",
            "-m",
            "umbrella-sandbox-sandbox_orphan",
            "--include-untracked",
        )
        assert any("umbrella-sandbox-" in line for line in _stash_list(repo))
        assert (repo / "README.md").read_text(encoding="utf-8") == "baseline\n"

        applied = recover_orphan_sandbox_stashes(repo)

        assert any("umbrella-sandbox-sandbox_orphan" in msg for msg in applied)
        assert (repo / "README.md").read_text(encoding="utf-8") == "stashed change\n"
        assert any("umbrella-sandbox-" in line for line in _stash_list(repo))

    def test_skips_when_worktree_dirty(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)

        (repo / "README.md").write_text("stashed change\n", encoding="utf-8")
        _git(
            repo,
            "stash",
            "push",
            "-m",
            "umbrella-sandbox-sandbox_orphan_dirty",
            "--include-untracked",
        )

        (repo / "local.txt").write_text("local dirty\n", encoding="utf-8")

        applied = recover_orphan_sandbox_stashes(repo)

        assert applied == []
        assert any("umbrella-sandbox-" in line for line in _stash_list(repo))
        assert (repo / "README.md").read_text(encoding="utf-8") == "baseline\n"

    def test_ignores_non_sandbox_stashes(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)

        (repo / "README.md").write_text("user stash\n", encoding="utf-8")
        _git(repo, "stash", "push", "-m", "my-manual-stash", "--include-untracked")

        applied = recover_orphan_sandbox_stashes(repo)

        assert applied == []
        assert any("my-manual-stash" in line for line in _stash_list(repo))

    def test_does_not_touch_stash_of_live_sibling_session(self, tmp_path: Path) -> None:
        """If another process still has an active sandbox session with
        ``exited_at is None`` and a matching ``stash_ref``, recovery must
        leave that stash alone. Otherwise two parallel Umbrella runs on the
        same clone silently corrupt each other's work.
        """
        import json

        repo = tmp_path / "repo"
        _init_repo(repo)

        (repo / "README.md").write_text("sibling wip\n", encoding="utf-8")
        _git(
            repo,
            "stash",
            "push",
            "-m",
            "umbrella-sandbox-sandbox_sibling_live",
            "--include-untracked",
        )

        session_dir = repo / ".umbrella" / "sandbox_sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "sandbox_sibling_live.json").write_text(
            json.dumps(
                {
                    "session_id": "sandbox_sibling_live",
                    "task_id": "t1",
                    "started_at": 0.0,
                    "snapshot_method": "git_stash",
                    "original_branch": "main",
                    "original_sha": "",
                    "original_head_detached": False,
                    "stash_ref": "umbrella-sandbox-sandbox_sibling_live",
                    "rollback_ok": False,
                    "exited_at": None,
                    "exit_reason": "",
                    "edited_files": [],
                    "error": "",
                }
            ),
            encoding="utf-8",
        )

        applied = recover_orphan_sandbox_stashes(repo)

        assert applied == []
        assert any(
            "umbrella-sandbox-sandbox_sibling_live" in line
            for line in _stash_list(repo)
        )
        assert (repo / "README.md").read_text(encoding="utf-8") == "baseline\n"
