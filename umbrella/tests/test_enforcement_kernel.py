from pathlib import Path

from umbrella.enforcement import (
    append_supervisor_ledger_event,
    check_post_tool_diff,
    check_workspace_paths,
    diff_snapshots,
    snapshot_workspace,
)


def test_kernel_blocks_supervisor_and_verifier_policy_paths() -> None:
    issues = check_workspace_paths(
        "apply_workspace_patch",
        "execute",
        [".memory/drive/logs/tools.jsonl", "workspace.toml", "src/app.py"],
    )
    codes = {issue.code for issue in issues}
    assert "supervisor_path_write_denied" in codes
    assert "verifier_policy_write_requires_supervisor_approval" in codes
    assert not any(issue.path == "src/app.py" for issue in issues)


def test_shell_post_diff_reports_workspace_mutation(tmp_path: Path) -> None:
    before = snapshot_workspace(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    changes = diff_snapshots(before, snapshot_workspace(tmp_path))

    issues = check_post_tool_diff("run_workspace_command", "execute", changes)

    assert any(issue.code == "shell_tool_workspace_mutation" for issue in issues)


def test_internal_memory_logs_not_workspace_mutation(tmp_path: Path) -> None:
    before = snapshot_workspace(tmp_path)
    log_dir = tmp_path / ".memory" / "drive" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "tools.jsonl").write_text("{}\n", encoding="utf-8")
    state_dir = tmp_path / ".memory" / "drive" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "phase_control_signals.jsonl").write_text("{}\n", encoding="utf-8")
    changes = diff_snapshots(before, snapshot_workspace(tmp_path))

    issues = check_post_tool_diff("run_subtask_proof", "execute", changes)

    assert not issues


def test_src_change_after_proof_is_workspace_mutation(tmp_path: Path) -> None:
    before = snapshot_workspace(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    changes = diff_snapshots(before, snapshot_workspace(tmp_path))

    issues = check_post_tool_diff("run_subtask_proof", "execute", changes)

    assert any(issue.code == "shell_tool_workspace_mutation" for issue in issues)


def test_tests_change_after_failed_proof_still_blocked(tmp_path: Path) -> None:
    before = snapshot_workspace(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")
    changes = diff_snapshots(before, snapshot_workspace(tmp_path))

    issues = check_post_tool_diff("run_subtask_proof", "execute", changes)

    assert any(issue.path == "tests/test_app.py" for issue in issues)


def test_diff_snapshots_ignores_capture_content_payload(tmp_path: Path) -> None:
    (tmp_path / ".memory" / "drive" / "logs").mkdir(parents=True)
    (tmp_path / ".memory" / "drive" / "logs" / "events.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('unchanged')\n", encoding="utf-8")

    before = snapshot_workspace(tmp_path, capture_content=True)
    after = snapshot_workspace(tmp_path)

    assert diff_snapshots(before, after) == []


def test_supervisor_ledger_hash_chain(tmp_path: Path) -> None:
    first = append_supervisor_ledger_event(
        repo_root=tmp_path,
        workspace_id="demo",
        actor="agent",
        phase="execute",
        tool="apply_workspace_patch",
        args={"paths": ["src/app.py"]},
        result={"status": "applied"},
        touched_files=["src/app.py"],
    )
    second = append_supervisor_ledger_event(
        repo_root=tmp_path,
        workspace_id="demo",
        actor="verifier",
        phase="verify",
        tool="run_workspace_verify",
        args={},
        result={"passed": True},
        touched_files=[],
    )

    assert second.prev_hash == first.event_hash
    ledger = tmp_path / ".umbrella" / "supervisor_ledger" / "demo.jsonl"
    assert ledger.exists()
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2
