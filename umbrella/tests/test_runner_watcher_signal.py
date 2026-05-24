"""Runner applies watcher repair signals via loop_back instead of WORKER_PANIC."""

import json
import time
from pathlib import Path

from umbrella.orchestrator.runner import PhaseRunner
from umbrella.phases.base import PhaseNode, PhasePlan, PhaseResult
from umbrella.phases.base import WatcherSignal
from umbrella.utils.result_envelope import ErrorCode


def test_restart_phase_signal_loops_back_instead_of_worker_panic(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    runner._watcher.write_signal(
        WatcherSignal(
            signal_id="restart-1",
            created_at=time.time(),
            kind="restart_phase",
            reason="Repeated proof failure; restart execute.",
            trigger="repeat_semantic_failure",
            payload={"category": "proof_not_passing"},
        )
    )

    phase_node = PhaseNode(id="execute", manifest_id="execute", status="running")
    phase_node.started_at = time.time()
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="demo",
        run_id="run-1",
        nodes=[phase_node],
    )
    outcome = {"status": "watcher", "task_id": "task-1", "event_count": 0}

    result, envelope = runner._apply_pending_watcher_signal(
        signal=runner._watcher.read_pending_signal(),
        phase_node=phase_node,
        plan=plan,
        run_id="run-1",
        outcome=outcome,
    )

    assert result is not None
    assert isinstance(result, PhaseResult)
    assert result.outcome == "loop_back"
    assert result.loop_back_target == "execute"
    assert envelope is not None
    assert envelope.ok is True
    assert runner._watcher.read_pending_signal() is None
    processed = (state / "watcher_signals.processed.jsonl").read_text(encoding="utf-8")
    assert "restart-1" in processed


def test_abort_phase_signal_uses_watcher_abort_code(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    drive.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    runner._watcher.write_signal(
        WatcherSignal(
            signal_id="abort-1",
            created_at=time.time(),
            kind="abort_phase",
            reason="fake evidence",
            trigger="repeat_semantic_failure",
            payload={"category": "fake_evidence_ref"},
        )
    )

    phase_node = PhaseNode(id="execute", manifest_id="execute", status="running")
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="demo",
        run_id="run-1",
        nodes=[phase_node],
    )

    result, envelope = runner._apply_pending_watcher_signal(
        signal=runner._watcher.read_pending_signal(),
        phase_node=phase_node,
        plan=plan,
        run_id="run-1",
        outcome={"status": "watcher", "task_id": "task-1"},
    )

    assert result is None
    assert envelope is not None
    assert envelope.ok is False
    assert envelope.errors[0].code == ErrorCode.WATCHER_ABORT


def test_write_phase_budget_file_skipped_when_watcher_budget_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    from umbrella.phases.base import Budgets

    monkeypatch.setenv("UMBRELLA_WATCHER_BUDGET_ENABLED", "0")
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    stale = drive / "state" / "execute.budget.json"
    stale.write_text('{"max_seconds": 1}', encoding="utf-8")
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    runner._write_phase_budget_file(
        "execute",
        Budgets(max_seconds=3600, max_tool_calls=500),
    )
    assert not stale.exists()


def test_write_phase_budget_file_from_manifest(tmp_path: Path, monkeypatch) -> None:
    from umbrella.phases.base import Budgets

    monkeypatch.setenv("UMBRELLA_WATCHER_BUDGET_ENABLED", "1")
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    drive.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    runner._write_phase_budget_file(
        "execute",
        Budgets(max_seconds=3600, max_tool_calls=500, max_tokens=60000),
    )
    payload = json.loads((drive / "state" / "execute.budget.json").read_text(encoding="utf-8"))
    assert payload["max_seconds"] == 3600
    assert payload["max_tool_calls"] == 500
