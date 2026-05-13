"""CLI-level tests for Umbrella app semantics."""

from pathlib import Path
import json

from umbrella.app import (
    _exit_code_for_status,
    _normalize_runtime_limit,
    _resolve_app_live_mode,
    _resolve_live_task_id,
    _resolve_task_request,
    inject_runtime_instruction,
    run_demo,
)
from umbrella.integration.runner import ManagerRunResult


def test_exit_code_for_status_is_honest():
    assert _exit_code_for_status("complete") == 0
    assert _exit_code_for_status("success") == 0
    assert _exit_code_for_status("partial") == 2
    assert _exit_code_for_status("failed") == 1


def test_workspace_improvement_demo_fails_if_no_files_changed(monkeypatch):
    class StubRunner:
        def run_workspace_improvement_cycle(self, **kwargs):
            return {
                "baseline": ManagerRunResult(
                    task_id="baseline", status="complete", duration_seconds=1.0
                ),
                "improved": ManagerRunResult(
                    task_id="improved", status="complete", duration_seconds=0.5
                ),
                "changed_files": [],
                "instance_path": "C:/tmp/fake_instance",
            }

    monkeypatch.setattr("umbrella.app.create_demo_runner", lambda: StubRunner())

    result = run_demo(
        demo_scenario="workspace_improvement_cycle",
        workspace_id="agent_research",
        repo_root=Path.cwd(),
        control_state_dir=None,
        workspaces_root=None,
        max_iterations=1,
        max_duration_seconds=30.0,
        use_live_llm=False,
        output_path=None,
    )

    assert result["status"] == "failed"
    assert result["changed_files"] == []


def test_app_auto_enables_live_from_env(monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".env").write_text("LLM_API_KEY=test-key\n", encoding="utf-8")

    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    resolved_live, reason = _resolve_app_live_mode(repo_root)

    assert resolved_live is True
    assert reason == "auto-enabled from .env"


def test_normalize_runtime_limit_uses_none_for_zero_or_negative():
    assert _normalize_runtime_limit(0) is None
    assert _normalize_runtime_limit(-1) is None
    assert _normalize_runtime_limit(5) == 5


def test_resolve_task_request_from_workspace_path(tmp_path: Path):
    repo_root = tmp_path / "repo"
    workspace_root = repo_root / "workspaces" / "demo_ws"
    workspace_root.mkdir(parents=True)
    (workspace_root / "workspace.toml").write_text(
        'workspace_id = "demo_ws"\ntask_main_file = "TASK_MAIN.md"\n',
        encoding="utf-8",
    )
    (workspace_root / "TASK_MAIN.md").write_text(
        "# TASK_MAIN\n\n"
        "## 1. Objective\n\n"
        "Run the demo workspace.\n\n"
        "## 2. Final Deliverable\n\n"
        "Working output.\n\n"
        "## 3. Success Criteria\n\n"
        "- It runs.\n\n"
        "## 4. Constraints\n\n"
        "- Stay in scope.\n\n"
        "## 5. Starting Point\n\n"
        "The workspace already exists.\n\n"
        "## 6. Human Checkpoints\n\n"
        "- Ask when blocked.\n\n"
        "## 7. Long-Run Policy\n\n"
        "- Keep going.\n",
        encoding="utf-8",
    )

    resolved = _resolve_task_request(str(workspace_root), repo_root)

    assert resolved.source == "workspace_path"
    assert resolved.workspace_id == "demo_ws"
    assert resolved.workspace_path == workspace_root.resolve()
    assert resolved.task_file == (workspace_root / "TASK_MAIN.md").resolve()
    assert "Run the demo workspace" in resolved.task_input


def test_resolve_task_request_requires_task_main_for_workspace_path(tmp_path: Path):
    repo_root = tmp_path / "repo"
    workspace_root = repo_root / "workspaces" / "demo_ws"
    workspace_root.mkdir(parents=True)
    (workspace_root / "workspace.toml").write_text(
        'workspace_id = "demo_ws"\ntask_main_file = "TASK_MAIN.md"\n',
        encoding="utf-8",
    )

    try:
        _resolve_task_request(str(workspace_root), repo_root)
    except FileNotFoundError as exc:
        assert "TASK_MAIN.md" in str(exc)
    else:
        raise AssertionError("Expected FileNotFoundError when TASK_MAIN.md is missing")


def test_resolve_live_task_id_uses_latest_active_checkpoint(tmp_path: Path):
    checkpoint_dir = tmp_path / ".umbrella" / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    older = {
        "id": "task_old",
        "status": "active",
        "state": {
            "task_id": "task_old",
            "phase": "knowledge_retrieved",
            "updated_at": 10.0,
        },
    }
    newer = {
        "id": "task_new",
        "status": "active",
        "state": {"task_id": "task_new", "phase": "decision_made", "updated_at": 20.0},
    }
    (checkpoint_dir / "task_old.json").write_text(json.dumps(older), encoding="utf-8")
    (checkpoint_dir / "task_new.json").write_text(json.dumps(newer), encoding="utf-8")

    resolved = _resolve_live_task_id(tmp_path / ".umbrella", "current")

    assert resolved == "task_new"


def test_inject_runtime_instruction_queues_pending_update(tmp_path: Path):
    checkpoint_dir = tmp_path / ".umbrella" / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    payload = {
        "id": "task_live",
        "status": "active",
        "state": {
            "task_id": "task_live",
            "phase": "knowledge_retrieved",
            "updated_at": 42.0,
        },
    }
    (checkpoint_dir / "task_live.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    result = inject_runtime_instruction(
        instruction="Also update the interface and workspaces.",
        repo_root=tmp_path,
        task_id="current",
    )

    pending_dir = tmp_path / ".umbrella" / "task_updates" / "task_live" / "pending"
    pending_files = list(pending_dir.glob("*.json"))

    assert result["status"] == "queued"
    assert result["task_id"] == "task_live"
    assert len(pending_files) == 1
    assert "interface" in pending_files[0].read_text(encoding="utf-8")
