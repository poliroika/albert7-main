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


def test_inject_lesson_signal_sets_watcher_lesson_overlay(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    lesson = "Use kind=observation for synthesis; cite URLs from deep_search results."
    runner._watcher.write_signal(
        WatcherSignal(
            signal_id="inject-1",
            created_at=time.time(),
            kind="inject_lesson",
            reason="Repeated semantic tool failure during research.",
            trigger="repeat_semantic_failure",
            payload={
                "watcher_lesson": lesson,
                "watcher_semantic_category": "research_memory_provenance_error",
            },
        )
    )

    phase_node = PhaseNode(id="research", manifest_id="research", status="running")
    phase_node.started_at = time.time()
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

    assert result is not None
    assert result.outcome == "loop_back"
    assert envelope is not None and envelope.ok is True
    target = plan.get_node("research")
    assert target is not None
    assert target.overlay.get("watcher_lesson") == lesson


def test_inject_lesson_does_not_interrupt_running_phase(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    runner._watcher.write_signal(
        WatcherSignal(
            signal_id="inject-advisory",
            created_at=time.time(),
            kind="inject_lesson",
            reason="Repeated semantic tool failure during research.",
            trigger="repeat_semantic_failure",
            payload={"watcher_lesson": "Use observation for unsupported synthesis."},
        )
    )

    class FakeHandle:
        worker_pid = None

        def __init__(self) -> None:
            self.calls = 0

        def wait(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                return None
            return {"status": "completed", "task_id": "task-1", "events": []}

    class FakeLauncher:
        def __init__(self) -> None:
            self.handle = FakeHandle()

        def submit_task(self, task, timeout=None):
            return self.handle

    fake = FakeLauncher()
    runner._launcher = fake
    phase_node = PhaseNode(id="research", manifest_id="research", status="running")
    phase_node.started_at = time.time()

    outcome = runner._run_phase_single(
        {"id": "task-1"},
        phase_node,
        run_id="run-1",
    )

    assert outcome["status"] == "completed"
    assert fake.handle.calls == 2


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


def test_force_verify_signal_sets_verification_overlay(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    drive.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    signal = WatcherSignal(
        signal_id="force-1",
        created_at=time.time(),
        kind="force_verify",
        reason="proof is stale",
        trigger="stall",
        payload={},
    )
    phase_node = PhaseNode(id="execute", manifest_id="execute", status="running")
    phase_node.started_at = time.time()
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="demo",
        run_id="run-1",
        nodes=[phase_node],
    )

    result, envelope = runner._apply_pending_watcher_signal(
        signal=signal,
        phase_node=phase_node,
        plan=plan,
        run_id="run-1",
        outcome={"status": "watcher", "task_id": "task-1"},
    )

    assert result is not None
    assert result.outcome == "loop_back"
    assert result.loop_back_target == "execute"
    assert envelope is not None
    assert envelope.ok is True
    assert phase_node.overlay["watcher_force_verify"] is True
    assert "run_subtask_proof" in phase_node.overlay["required_next_actions"]


def test_mutate_phase_plan_signal_routes_to_plan_overlay(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    drive.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    execute = PhaseNode(id="execute", manifest_id="execute", status="running")
    execute.started_at = time.time()
    plan_node = PhaseNode(id="plan", manifest_id="plan", status="done")
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="demo",
        run_id="run-1",
        nodes=[plan_node, execute],
    )
    signal = WatcherSignal(
        signal_id="mutate-1",
        created_at=time.time(),
        kind="mutate_phase_plan",
        reason="subtask proof contract is invalid",
        trigger="repeat_semantic_failure",
        payload={"issue": "proof_contract"},
    )

    result, envelope = runner._apply_pending_watcher_signal(
        signal=signal,
        phase_node=execute,
        plan=plan,
        run_id="run-1",
        outcome={"status": "watcher", "task_id": "task-1"},
    )

    assert result is not None
    assert result.outcome == "loop_back"
    assert result.loop_back_target == "plan"
    assert envelope is not None
    assert envelope.ok is True
    assert plan_node.overlay["watcher_mutate_phase_plan_request"] == {
        "issue": "proof_contract"
    }


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
