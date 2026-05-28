"""Regression: phase-exit contract validation must use completion changed_files for diff_hash."""

import json
import time
from pathlib import Path

from umbrella.contracts import (
    ContractCompiler,
    ContractValidator,
    diff_hash,
    hash_value,
    workspace_hash,
)
from umbrella.enforcement.ledger import append_supervisor_ledger_event
from umbrella.orchestrator.runner import PhaseRunner
from umbrella.phases.base import PhaseNode, PhasePlan, SubtaskCard


def _seed_completion_signal(
    drive: Path,
    *,
    repo_root: Path,
    workspace_id: str,
    run_id: str,
    task_id: str,
    subtask_id: str = "project-setup",
    changed_files: list[str],
    diff_h: str,
    ws_hash: str,
) -> None:
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    report_hash = hash_value({"passed": True, "subtask": subtask_id})
    ledger_result = {
        "report_hash": report_hash,
        "passed": True,
        "workspace_hash": ws_hash,
        "diff_hash": diff_h,
    }
    event = append_supervisor_ledger_event(
        repo_root=repo_root,
        workspace_id=workspace_id,
        actor="verifier",
        phase="execute",
        tool="run_subtask_proof",
        result=ledger_result,
    )
    completion = {
        "subtask_id": subtask_id,
        "status": "done",
        "changed_files": changed_files,
        "completed_claims": [
            {
                "claim_id": f"{subtask_id}.proof",
                "text": "Subtask proof passed.",
                "proof_refs": [
                    {
                        "ref_type": "ledger_event",
                        "ref_id": event.event_id,
                        "hash": event.event_hash,
                        "produced_by": "verifier",
                        "phase": "execute",
                        "subtask_id": subtask_id,
                    }
                ],
            }
        ],
        "evidence_refs": [],
        "verification_report": {
            "report_id": event.event_id,
            "report_hash": report_hash,
            "workspace_hash": ws_hash,
            "diff_hash": diff_h,
            "produced_after_event_id": "",
            "verifier_id": "run_subtask_proof",
            "passed": True,
            "ledger_hash": event.event_hash,
        },
    }
    row = {
        "signal_id": f"sig-complete-{subtask_id}-{time.time_ns()}",
        "created_at": time.time(),
        "kind": "mark_subtask_complete",
        "payload": {"completion_contract": completion},
        "actor": "worker",
        "task_id": task_id,
        "run_id": run_id,
        "phase": "execute",
    }
    path = state / "phase_control_signals.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_contract_validation_context_uses_completion_changed_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='calc'\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "calc").mkdir()
    (workspace / "src" / "calc" / "__init__.py").write_text("", encoding="utf-8")

    drive = workspace / ".memory" / "drive"
    run_id = "run_ctx"
    task_id = f"{run_id}:execute"
    changed = ["pyproject.toml", "src/calc/__init__.py"]
    ws_hash = workspace_hash(workspace)
    diff_h = diff_hash(workspace, changed)

    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=task_id,
        changed_files=changed,
        diff_h=diff_h,
        ws_hash=ws_hash,
    )

    bundle = ContractCompiler.from_run(
        repo_root=repo,
        drive_root=drive,
        workspace_id=workspace_id,
        run_id=run_id,
    )
    assert len(bundle.completions) == 1

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    context = runner._contract_validation_context(bundle)
    issues = ContractValidator.validate(bundle, context=context)
    assert not any(issue.code == "diff_hash_mismatch" for issue in issues)
    assert context.current_diff_hash == diff_h


def test_plan_exit_not_blocked_by_stale_execute_workspace_hash(tmp_path: Path) -> None:
    """After execute writes files, plan exit must not loop on old completion proof."""
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "pkg.py").write_text("x = 1\n", encoding="utf-8")

    drive = workspace / ".memory" / "drive"
    run_id = "run_plan_stale_ws"
    changed = ["pkg.py"]
    ws_hash = workspace_hash(workspace)
    diff_h = diff_hash(workspace, changed)

    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute",
        changed_files=changed,
        diff_h=diff_h,
        ws_hash=ws_hash,
    )

    (workspace / "src").mkdir()
    (workspace / "src" / "calc").mkdir(parents=True)
    (workspace / "src" / "calc" / "core.py").write_text("pass\n", encoding="utf-8")
    assert workspace_hash(workspace) != ws_hash

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    plan_failure = runner._phase_contract_decision_failure(
        phase="plan",
        manifest=type("M", (), {"id": "plan"})(),
        run_id=run_id,
    )
    assert plan_failure == ""

    execute_failure = runner._phase_contract_decision_failure(
        phase="execute",
        manifest=type("M", (), {"id": "execute"})(),
        run_id=run_id,
    )
    assert execute_failure == ""


def test_execute_completion_uses_subtask_diff_not_future_workspace_hash(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "src").mkdir()
    (workspace / "src" / "calc").mkdir()
    (workspace / "src" / "calc" / "core.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )

    drive = workspace / ".memory" / "drive"
    run_id = "run_two_completions"
    core_changed = ["src/calc/core.py"]
    core_ws_hash = workspace_hash(workspace)
    core_diff_h = diff_hash(workspace, core_changed)
    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute:core",
        subtask_id="core",
        changed_files=core_changed,
        diff_h=core_diff_h,
        ws_hash=core_ws_hash,
    )

    (workspace / "src" / "calc" / "gui.py").write_text(
        "class Gui:\n    pass\n",
        encoding="utf-8",
    )
    gui_changed = ["src/calc/gui.py"]
    gui_ws_hash = workspace_hash(workspace)
    assert gui_ws_hash != core_ws_hash
    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute:gui",
        subtask_id="gui",
        changed_files=gui_changed,
        diff_h=diff_hash(workspace, gui_changed),
        ws_hash=gui_ws_hash,
    )

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    execute_failure = runner._phase_contract_decision_failure(
        phase="execute",
        manifest=type("M", (), {"id": "execute"})(),
        run_id=run_id,
    )

    assert execute_failure == ""


def test_execute_review_gate_scopes_contracts_to_latest_completed_subtask(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "src" / "calc").mkdir(parents=True)
    target = workspace / "src" / "calc" / "gui.py"
    target.write_text("class Gui:\n    title = 'window'\n", encoding="utf-8")

    drive = workspace / ".memory" / "drive"
    run_id = "run_overlap_completion_scope"
    changed = ["src/calc/gui.py"]
    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute:window",
        subtask_id="window",
        changed_files=changed,
        diff_h=diff_hash(workspace, changed),
        ws_hash=workspace_hash(workspace),
    )

    target.write_text(
        "class Gui:\n    title = 'window'\n    buttons = []\n",
        encoding="utf-8",
    )
    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute:buttons",
        subtask_id="buttons",
        changed_files=changed,
        diff_h=diff_hash(workspace, changed),
        ws_hash=workspace_hash(workspace),
    )

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    unscoped = runner._phase_contract_decision_failure(
        phase="execute",
        manifest=type("M", (), {"id": "execute"})(),
        run_id=run_id,
    )
    assert "verify_in_place" in unscoped

    execute = PhaseNode(
        id="execute",
        manifest_id="execute",
        status="running",
        started_at=0.0,
        subtasks=[
            SubtaskCard(
                id="window",
                title="Window",
                goal="window",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="done",
            ),
            SubtaskCard(
                id="buttons",
                title="Buttons",
                goal="buttons",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="done",
            ),
        ],
    )
    plan = PhasePlan(
        plan_id="p1",
        workspace_id=workspace_id,
        run_id=run_id,
        nodes=[execute],
    )
    exit_criteria = type(
        "Exit",
        (),
        {
            "required_calls": ("mark_subtask_complete",),
            "required_palace_writes": (),
            "min_palace_writes": (),
        },
    )()
    manifest = type("M", (), {"id": "execute", "exit_criteria": exit_criteria})()

    failure = runner._phase_completion_failure(
        phase_node=execute,
        plan=plan,
        manifest=manifest,
        outcome={"task_id": f"{run_id}:execute:buttons", "run_id": run_id},
    )

    assert failure == ""


def test_contract_compiler_keeps_latest_completion_per_subtask(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "src" / "calc").mkdir(parents=True)
    target = workspace / "src" / "calc" / "gui.py"
    target.write_text("value = 1\n", encoding="utf-8")

    drive = workspace / ".memory" / "drive"
    run_id = "run_same_subtask_recovery"
    changed = ["src/calc/gui.py"]
    first_hash = workspace_hash(workspace)
    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute:gui:first",
        subtask_id="gui",
        changed_files=changed,
        diff_h=diff_hash(workspace, changed),
        ws_hash=first_hash,
    )

    target.write_text("value = 2\n", encoding="utf-8")
    second_hash = workspace_hash(workspace)
    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute:gui:second",
        subtask_id="gui",
        changed_files=changed,
        diff_h=diff_hash(workspace, changed),
        ws_hash=second_hash,
    )

    bundle = ContractCompiler.from_run(
        repo_root=repo,
        workspace_id=workspace_id,
        drive_root=drive,
        run_id=run_id,
    )

    assert [completion.subtask_id for completion in bundle.completions] == ["gui"]
    assert bundle.completions[0].verification_report is not None
    assert bundle.completions[0].verification_report.workspace_hash == second_hash


def test_execute_completion_still_rejects_changed_subtask_diff(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "src").mkdir()
    (workspace / "src" / "calc").mkdir()
    target = workspace / "src" / "calc" / "core.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    drive = workspace / ".memory" / "drive"
    run_id = "run_changed_subtask_diff"
    changed = ["src/calc/core.py"]
    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute:core",
        subtask_id="core",
        changed_files=changed,
        diff_h=diff_hash(workspace, changed),
        ws_hash=workspace_hash(workspace),
    )

    target.write_text("def add(a, b):\n    return 42\n", encoding="utf-8")

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    execute_failure = runner._phase_contract_decision_failure(
        phase="execute",
        manifest=type("M", (), {"id": "execute"})(),
        run_id=run_id,
    )

    assert "different workspace hash" in execute_failure or "diff hash" in execute_failure


def test_phase_effective_write_count_includes_replace_workspace_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    task_id = "run_write:execute:1"
    (drive / "logs" / "tools.jsonl").write_text(
        json.dumps(
            {
                "tool": "replace_workspace_file",
                "task_id": task_id,
                "result_preview": json.dumps(
                    {"status": "ok", "path": "src/app.py"}
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    assert runner._phase_effective_write_count(task_id=task_id) == 1


def test_execute_no_write_guard_allows_completion_only_retry(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='calc'\n", encoding="utf-8")
    drive = workspace / ".memory" / "drive"
    run_id = "run_execute_retry"
    task_id = f"{run_id}:execute:retry"
    changed = ["pyproject.toml"]
    ws_hash = workspace_hash(workspace)
    diff_h = diff_hash(workspace, changed)
    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=task_id,
        changed_files=changed,
        diff_h=diff_h,
        ws_hash=ws_hash,
    )
    phase_node = PhaseNode(
        id="execute",
        manifest_id="execute",
        status="running",
        started_at=time.time() - 10,
        subtasks=[
            SubtaskCard(
                id="project-setup",
                title="Project setup",
                goal="Set up package",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                files_to_create=changed,
                status="done",
            )
        ],
    )
    outcome = {"task_id": task_id}
    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)

    completed = runner._latest_completed_subtask_from_phase(
        phase_node=phase_node,
        outcome=outcome,
    )
    assert completed is not None
    assert runner._phase_effective_write_count(task_id=task_id) == 0
    assert (
        runner._execute_phase_missing_write_failure(
            phase_node=phase_node,
            outcome=outcome,
            completed_subtask=completed,
        )
        == ""
    )


def test_execute_no_write_guard_still_rejects_noop_execute(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    phase_node = PhaseNode(id="execute", manifest_id="execute", status="running")
    outcome = {"task_id": "run_noop:execute:1"}
    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)

    assert (
        runner._execute_phase_missing_write_failure(
            phase_node=phase_node,
            outcome=outcome,
            completed_subtask=None,
        )
        == "execute phase completed without any effective workspace write tool calls"
    )


def test_execute_exit_supersedes_stale_proof_when_fresh_workspace_verify(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "pkg.py").write_text("x = 1\n", encoding="utf-8")

    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    run_id = "run_execute_fresh_verify"
    changed = ["pkg.py"]
    ws_hash = workspace_hash(workspace)
    diff_h = diff_hash(workspace, changed)

    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute",
        changed_files=changed,
        diff_h=diff_h,
        ws_hash=ws_hash,
    )

    (workspace / "src").mkdir()
    (workspace / "src" / "calc").mkdir(parents=True)
    (workspace / "src" / "calc" / "core.py").write_text("pass\n", encoding="utf-8")
    current_ws = workspace_hash(workspace)
    report_hash = hash_value({"passed": True})
    verify_preview = json.dumps(
        {
            "passed": True,
            "verification_report_ref": {
                "report_id": "evt-fresh",
                "report_hash": report_hash,
                "workspace_hash": current_ws,
                "diff_hash": current_ws,
                "passed": True,
            },
        }
    )
    (drive / "logs" / "tools.jsonl").write_text(
        json.dumps(
            {
                "tool": "run_workspace_verify",
                "task_id": f"{run_id}:execute",
                "result_preview": verify_preview,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    execute_failure = runner._phase_contract_decision_failure(
        phase="execute",
        manifest=type("M", (), {"id": "execute"})(),
        run_id=run_id,
    )
    assert execute_failure == ""


def test_post_execute_review_supersedes_stale_proof_when_fresh_workspace_verify(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "pkg.py").write_text("x = 1\n", encoding="utf-8")

    drive = workspace / ".memory" / "drive"
    (drive / "logs").mkdir(parents=True)
    run_id = "run_post_execute_fresh_verify"
    changed = ["pkg.py"]
    ws_hash = workspace_hash(workspace)
    diff_h = diff_hash(workspace, changed)

    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:execute",
        changed_files=changed,
        diff_h=diff_h,
        ws_hash=ws_hash,
    )

    (workspace / "src").mkdir()
    (workspace / "src" / "calc").mkdir(parents=True)
    (workspace / "src" / "calc" / "core.py").write_text("pass\n", encoding="utf-8")
    current_ws = workspace_hash(workspace)
    report_hash = hash_value({"passed": True})
    verify_preview = json.dumps(
        {
            "passed": True,
            "verification_report_ref": {
                "report_id": "evt-fresh",
                "report_hash": report_hash,
                "workspace_hash": current_ws,
                "diff_hash": current_ws,
                "passed": True,
            },
        }
    )
    (drive / "logs" / "tools.jsonl").write_text(
        json.dumps(
            {
                "tool": "run_workspace_verify",
                "task_id": f"{run_id}:execute",
                "result_preview": verify_preview,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    failure = runner._phase_contract_decision_failure(
        phase="subtask_review:integration-e2e",
        manifest=type("M", (), {"id": "subtask_review"})(),
        run_id=run_id,
    )
    assert failure == ""


def test_phase_contract_decision_failure_clear_with_completion_context(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    workspace = repo / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "pkg.py").write_text("x = 1\n", encoding="utf-8")

    drive = workspace / ".memory" / "drive"
    run_id = "run_plan_exit"
    changed = ["pkg.py"]
    ws_hash = workspace_hash(workspace)
    diff_h = diff_hash(workspace, changed)

    _seed_completion_signal(
        drive,
        repo_root=repo,
        workspace_id=workspace_id,
        run_id=run_id,
        task_id=f"{run_id}:plan",
        changed_files=changed,
        diff_h=diff_h,
        ws_hash=ws_hash,
    )

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    failure = runner._phase_contract_decision_failure(
        phase="plan",
        manifest=type("M", (), {"id": "plan"})(),
        run_id=run_id,
    )
    assert failure == ""


def test_parse_contract_loop_back_target() -> None:
    target = PhaseRunner._parse_contract_loop_back_target(
        "contract decision loop_back to execute: Verification report was produced for a different diff hash.",
        default="plan",
    )
    assert target == "execute"


def test_loop_back_supersede_after_honors_ok_plan_review_and_submit(tmp_path: Path) -> None:
    runner = PhaseRunner(
        repo_root=tmp_path / "repo",
        workspace_id="calc",
        drive_root=tmp_path / "drive",
    )
    records = [
        {
            "kind": "loop_back_to",
            "created_at": 100.0,
            "payload": {"phase": "plan"},
        },
        {
            "kind": "submit_phase_plan",
            "created_at": 200.0,
            "payload": {"plan_id": "p1"},
        },
        {
            "kind": "submit_micro_review",
            "created_at": 250.0,
            "phase": "plan_review",
            "payload": {"verdict": "ok"},
        },
    ]
    assert runner._loop_back_supersede_after(records) == 250.0


def test_plan_review_ok_supersedes_floor_when_main_py_in_plan(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ws = "calc"
    workspace = repo / "workspaces" / ws
    workspace.mkdir(parents=True)
    (workspace / "TASK_MAIN.md").write_text("calc\n", encoding="utf-8")

    drive = workspace / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    run_id = "run_plan_review_floor"
    plan_body = {
        "subtasks": [
            {
                "id": "main-launcher",
                "title": "launcher",
                "goal": "launch",
                "files_to_create": ["main.py", "tests/test_launcher.py"],
                "proof": {
                    "execution": {
                        "kind": "pytest",
                        "command": ["python", "-m", "pytest", "tests/test_launcher.py", "-q"],
                        "shell": False,
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": ["module_imports"],
                    },
                    "scope": {
                        "files_under_test": ["main.py"],
                        "changed_files_expected": ["main.py", "tests/test_launcher.py"],
                    },
                },
            }
        ]
    }
    submitted = {
        "created_at": 300.0,
        "run_id": run_id,
        "workspace_id": ws,
        "plan": plan_body,
    }
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(submitted),
        encoding="utf-8",
    )
    signals = state / "phase_control_signals.jsonl"
    for row in (
        {
            "signal_id": "s1",
            "created_at": 300.0,
            "kind": "submit_phase_plan",
            "run_id": run_id,
            "task_id": f"{run_id}:plan",
            "payload": {"plan_id": "p1"},
        },
        {
            "signal_id": "s2",
            "created_at": 310.0,
            "kind": "submit_micro_review",
            "run_id": run_id,
            "task_id": f"{run_id}:plan_review",
            "phase": "plan_review",
            "payload": {"verdict": "ok", "issues": []},
        },
    ):
        with signals.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    runner = PhaseRunner(repo_root=repo, workspace_id=ws, drive_root=drive)
    assert runner._plan_review_ok_supersedes_plan_floor(run_id=run_id)
    assert runner._latest_phase_plan_execution_floor_failure(run_id=run_id) == ""


def test_sync_execute_subtasks_does_not_clobber_runtime_mutations(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    ws = "calc"
    drive = repo / "workspaces" / ws / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    run_id = "run-sync"
    submitted = {
        "run_id": run_id,
        "workspace_id": ws,
        "plan_id": "p1",
        "created_at": 100.0,
        "plan": {
            "subtasks": [
                {
                    "id": "launch-main",
                    "title": "launcher",
                    "goal": "launch app",
                    "files_to_create": ["main.py", "tests/test_main_entry.py"],
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": [
                                "python",
                                "-m",
                                "pytest",
                                "tests/test_main_entry.py",
                                "-q",
                            ],
                            "shell": False,
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["module_imports"],
                        },
                        "scope": {
                            "files_under_test": ["main.py"],
                            "changed_files_expected": [
                                "main.py",
                                "tests/test_main_entry.py",
                            ],
                        },
                    },
                }
            ]
        },
    }
    (state / "phase_plan_submitted_latest.json").write_text(
        json.dumps(submitted),
        encoding="utf-8",
    )
    plan = PhasePlan(
        plan_id="phase-plan",
        workspace_id=ws,
        run_id=run_id,
        nodes=[
            PhaseNode(id="plan", manifest_id="plan", status="done"),
            PhaseNode(id="plan_review", manifest_id="plan_review", status="done"),
            PhaseNode(id="execute", manifest_id="execute", status="pending"),
        ],
    )
    runner = PhaseRunner(repo_root=repo, workspace_id=ws, drive_root=drive)
    runner._latest_phase_plan_execution_floor_failure = lambda *, run_id: ""

    assert runner._sync_execute_subtasks_from_latest_plan(plan, run_id=run_id)
    execute = plan.get_node("execute")
    assert execute is not None and execute.subtasks
    execute.subtasks[0].files_to_create = [
        "src/calculator/cli.py",
        "tests/test_main_entry.py",
    ]
    execute.overlay = None

    assert not runner._sync_execute_subtasks_from_latest_plan(plan, run_id=run_id)
    assert execute.subtasks[0].files_to_create == [
        "src/calculator/cli.py",
        "tests/test_main_entry.py",
    ]


def test_loop_back_supersede_after_ignores_stale_loop_back_to(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    task_id = "run-1:execute"
    signals = [
        {
            "kind": "loop_back_to",
            "created_at": 100.0,
            "task_id": task_id,
            "payload": {"phase": "plan"},
        },
        {
            "kind": "mark_subtask_complete",
            "created_at": 200.0,
            "task_id": task_id,
            "payload": {},
        },
    ]
    (state / "phase_control_signals.jsonl").write_text(
        "\n".join(json.dumps(row) for row in signals) + "\n",
        encoding="utf-8",
    )

    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    execute = PhaseNode(id="execute", manifest_id="execute", status="running")
    execute.started_at = 50.0
    target = runner._phase_loop_back_target(
        phase_node=execute,
        outcome={"task_id": task_id},
    )
    assert target == ""


def test_loop_back_ignores_stale_attempt_suffixed_review_signals(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    run_id = "run-1"
    signals = [
        {
            "signal_id": "old-review",
            "kind": "submit_micro_review",
            "created_at": 100.0,
            "task_id": f"{run_id}:plan_review:100",
            "phase": "plan_review",
            "payload": {
                "verdict": "revise",
                "issues": [{"code": "weak_proof", "message": "stale"}],
                "loop_back_target": "plan",
            },
        },
        {
            "signal_id": "old-loop",
            "kind": "loop_back_to",
            "created_at": 101.0,
            "task_id": f"{run_id}:plan_review:100",
            "phase": "plan_review",
            "payload": {"phase": "plan"},
        },
        {
            "signal_id": "current-ok",
            "kind": "submit_micro_review",
            "created_at": 200.0,
            "task_id": f"{run_id}:plan_review:200",
            "phase": "plan_review",
            "payload": {"verdict": "ok", "issues": []},
        },
    ]
    (state / "phase_control_signals.jsonl").write_text(
        "\n".join(json.dumps(row) for row in signals) + "\n",
        encoding="utf-8",
    )

    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    plan_review = PhaseNode(id="plan_review", manifest_id="plan_review", status="running")
    plan_review.started_at = 190.0
    target = runner._phase_loop_back_target(
        phase_node=plan_review,
        outcome={"task_id": f"{run_id}:plan_review:200"},
    )
    assert target == ""


def test_revision_contract_preserves_required_plan_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    task_id = "run-1:plan_review:200"
    (state / "phase_control_signals.jsonl").write_text(
        json.dumps(
            {
                "signal_id": "review-revise",
                "kind": "submit_micro_review",
                "created_at": 200.0,
                "task_id": task_id,
                "phase": "plan_review",
                "payload": {
                    "verdict": "revise",
                    "issues": [{"code": "weak_proof", "message": "proof is weak"}],
                    "required_plan_changes": [
                        "Strengthen launcher proof with observable command behavior."
                    ],
                    "loop_back_target": "plan",
                    "notes": "Keep unrelated subtasks unchanged.",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    phase = PhaseNode(id="plan_review", manifest_id="plan_review", status="running")
    phase.started_at = 100.0

    contract = runner._latest_revision_contract(
        phase_node=phase,
        outcome={"task_id": task_id},
    )

    assert contract["issues"] == [{"code": "weak_proof", "message": "proof is weak"}]
    assert contract["loop_back_target"] == "plan"
    assert contract["review_source"] == "submit_micro_review"
    assert contract["review_phase_id"] == "plan_review"
    assert contract["review_artifact_ref"] == "review-revise"
    assert contract["required_plan_changes"] == [
        "Strengthen launcher proof with observable command behavior."
    ]
    assert contract["revisions"] == []
    rendered = json.dumps(contract, ensure_ascii=False, indent=2)
    assert "weak_proof" in rendered
    assert "proof is weak" in rendered
    assert rendered.index('"issues"') < rendered.index('"notes"')


def test_revision_contract_accepts_watcher_plan_contract_issue(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    task_id = "run-1:execute:200"
    change = {
        "target_subtask_id": "logic",
        "reason_code": "bad_generated_oracle",
        "contract_path": "proof.required_properties",
        "invalid_values": ["impossible_oracle"],
        "required_deltas": [
            {
                "op": "remove",
                "path": "proof.required_properties",
                "values": ["impossible_oracle"],
            }
        ],
    }
    (state / "phase_control_signals.jsonl").write_text(
        json.dumps(
            {
                "signal_id": "watcher-contract",
                "kind": "request_watcher_review",
                "created_at": 200.0,
                "task_id": task_id,
                "phase": "execute",
                "payload": {
                    "status": "review_recorded",
                    "verdict": "bad_test_contract",
                    "loop_back_target": "plan",
                    "issues": [
                        {
                            "code": "plan_contract_issue",
                            "severity": "blocking",
                            "target": "logic",
                            "message": "generated oracle contradicts task",
                        }
                    ],
                    "required_plan_changes": [change],
                    "recommendation": "Route to plan contract revision.",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    phase = PhaseNode(id="execute", manifest_id="execute", status="running")
    phase.started_at = 100.0

    contract = runner._latest_revision_contract(
        phase_node=phase,
        outcome={"task_id": task_id},
    )

    assert contract["review_source"] == "request_watcher_review"
    assert contract["loop_back_target"] == "plan"
    assert contract["issues"][0]["code"] == "plan_contract_issue"
    assert contract["required_plan_changes"] == [change]
    assert contract["notes"] == "Route to plan contract revision."


def test_phase_loop_back_target_uses_watcher_plan_contract_issue(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    task_id = "run-1:execute:200"
    (state / "phase_control_signals.jsonl").write_text(
        json.dumps(
            {
                "signal_id": "watcher-contract",
                "kind": "request_watcher_review",
                "created_at": 200.0,
                "task_id": task_id,
                "phase": "execute",
                "payload": {
                    "status": "review_recorded",
                    "verdict": "bad_test_contract",
                    "loop_back_target": "plan",
                    "issues": [{"code": "plan_contract_issue"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    phase = PhaseNode(id="execute", manifest_id="execute", status="running")
    phase.started_at = 100.0
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="demo",
        run_id="run-1",
        nodes=[PhaseNode(id="plan", manifest_id="plan"), phase],
    )

    target = runner._phase_loop_back_target(
        phase_node=phase,
        outcome={"task_id": task_id},
        plan=plan,
    )

    assert target == "plan"


def test_revision_contract_merges_typed_issues(tmp_path: Path) -> None:
    runner = PhaseRunner(
        repo_root=tmp_path / "repo",
        workspace_id="demo",
        drive_root=tmp_path / "drive",
    )
    existing = {
        "source_phase": "subtask_review:s1",
        "source_task_id": "run:review:1",
        "issues": [
            {
                "code": "proof_scope_mismatch",
                "severity": "blocking",
                "message": "old proof missed the runtime target",
            }
        ],
        "revisions": [],
        "required_plan_changes": [],
        "notes": "old note",
    }
    latest = {
        "source_phase": "subtask_review:s1",
        "source_task_id": "run:review:2",
        "issues": [
            {
                "code": "proof_scope_mismatch",
                "severity": "blocking",
                "message": "old proof missed the runtime target",
            },
            {
                "code": "weak_proof",
                "severity": "error",
                "message": "runtime proof duplicated headless checks",
            },
        ],
        "revisions": [],
        "required_plan_changes": [],
        "notes": "new note",
    }

    merged = runner._merged_revision_contract(existing, latest)

    assert [issue["code"] for issue in merged["issues"]] == [
        "proof_scope_mismatch",
        "weak_proof",
    ]
    assert "old note" in merged["notes"]
    assert "new note" in merged["notes"]
