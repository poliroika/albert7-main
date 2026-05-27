"""Stale revise micro-review must not block phase exit after subtask recovery."""

import json
import time
from pathlib import Path

from umbrella.contracts import ContractCompiler, ContractValidator
from umbrella.contracts.models import ReviewContract
from umbrella.contracts.subtask_recovery import review_superseded_by_recovery
from umbrella.orchestrator.runner import PhaseRunner
from umbrella.phases.base import PhaseNode


def _write_signals(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _write_plan(path: Path, *, subtask_id: str, completed_at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    plan = {
        "plan_id": "plan-1",
        "workspace_id": "calc",
        "run_id": "run_stale_review",
        "version": 1,
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": subtask_id,
                        "title": "Domain",
                        "goal": "logic",
                        "status": "done",
                        "completion": {
                            "completed_at": completed_at,
                            "verification_report": {"passed": True},
                        },
                    }
                ],
            }
        ],
        "edits_log": [],
    }
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")


def test_compile_ignores_superseded_revise_review(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    drive = repo / "workspaces" / workspace_id / ".memory" / "drive"
    run_id = "run_stale_review"
    subtask_id = "domain-logic"
    review_at = 100.0
    recovery_at = 200.0

    _write_plan(drive / "state" / "phase_plan.json", subtask_id=subtask_id, completed_at=recovery_at)
    _write_signals(
        drive / "state" / "phase_control_signals.jsonl",
        [
            {
                "kind": "submit_micro_review",
                "created_at": review_at,
                "payload": {
                    "verdict": "revise",
                    "issues": [
                        {
                            "code": "proof_scope_mismatch",
                            "severity": "blocking",
                            "phase": "execute",
                            "subtask_id": subtask_id,
                            "message": "stale test bug",
                        }
                    ],
                },
            },
            {
                "kind": "mark_subtask_complete",
                "created_at": recovery_at,
                "payload": {
                    "completion_contract": {
                        "subtask_id": subtask_id,
                        "status": "done",
                        "verification_report": {"passed": True},
                    }
                },
            },
        ],
    )

    bundle = ContractCompiler.from_run(
        repo_root=repo,
        drive_root=drive,
        workspace_id=workspace_id,
        run_id=run_id,
    )
    assert bundle.reviews == ()
    issues = ContractValidator.validate(bundle)
    blocking = [i for i in issues if i.severity == "blocking" and i.code == "proof_scope_mismatch"]
    assert not blocking


def test_review_superseded_helper() -> None:
    review = ReviewContract.from_mapping(
        {
            "verdict": "revise",
            "issues": [
                {
                    "code": "proof_scope_mismatch",
                    "severity": "blocking",
                    "subtask_id": "domain-logic",
                    "message": "x",
                }
            ],
        }
    )
    assert review_superseded_by_recovery(
        review,
        recovery_at={"domain-logic": 200.0},
        review_created_at=100.0,
    )


def test_phase_loop_back_target_ignores_superseded_revise_review(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    drive = repo / "workspaces" / workspace_id / ".memory" / "drive"
    task_id = "run_stale_review:execute"
    subtask_id = "domain-logic"

    _write_plan(drive / "state" / "phase_plan.json", subtask_id=subtask_id, completed_at=200.0)
    _write_signals(
        drive / "state" / "phase_control_signals.jsonl",
        [
            {
                "kind": "submit_micro_review",
                "created_at": 100.0,
                "task_id": task_id,
                "payload": {
                    "verdict": "revise",
                    "issues": [
                        {
                            "code": "proof_scope_mismatch",
                            "severity": "blocking",
                            "subtask_id": subtask_id,
                            "message": "stale",
                        }
                    ],
                },
            },
            {
                "kind": "mark_subtask_complete",
                "created_at": 200.0,
                "task_id": task_id,
                "payload": {
                    "completion_contract": {
                        "subtask_id": subtask_id,
                        "verification_report": {"passed": True},
                    }
                },
            },
        ],
    )

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    execute = PhaseNode(id="execute", manifest_id="execute", status="running", started_at=50.0)
    target = runner._phase_loop_back_target(
        phase_node=execute,
        outcome={"task_id": task_id},
    )
    assert target == ""


def test_phase_contract_decision_failure_clear_after_superseded_revise(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace_id = "calc"
    drive = repo / "workspaces" / workspace_id / ".memory" / "drive"
    run_id = "run_stale_review"
    subtask_id = "domain-logic"

    _write_plan(drive / "state" / "phase_plan.json", subtask_id=subtask_id, completed_at=200.0)
    _write_signals(
        drive / "state" / "phase_control_signals.jsonl",
        [
            {
                "kind": "submit_micro_review",
                "created_at": 100.0,
                "payload": {
                    "verdict": "revise",
                    "issues": [
                        {
                            "code": "proof_scope_mismatch",
                            "severity": "blocking",
                            "subtask_id": subtask_id,
                            "message": "stale",
                        }
                    ],
                },
            },
        ],
    )

    runner = PhaseRunner(repo_root=repo, workspace_id=workspace_id, drive_root=drive)
    failure = runner._phase_contract_decision_failure(
        phase="execute",
        manifest=type("M", (), {"id": "execute"})(),
        run_id=run_id,
    )
    assert failure == ""


def test_clear_stale_execute_retry_overlay() -> None:
    from umbrella.phases.base import SubtaskCard

    execute = PhaseNode(
        id="execute",
        manifest_id="execute",
        status="running",
        overlay={
            "retry_reason": "contract decision loop_back to execute: stale",
            "retry_context": {"retry_reason": "stale"},
        },
        subtasks=[
            SubtaskCard(
                id="a",
                title="A",
                goal="g",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="done",
            )
        ],
    )
    PhaseRunner._clear_stale_execute_retry_overlay(execute)
    assert execute.overlay is None or "retry_reason" not in (execute.overlay or {})
