import json
import time
from pathlib import Path

from umbrella.orchestrator.runner import PhaseRunner
from umbrella.phases.base import PhaseNode, PhasePlan, SubtaskCard


def _write_plan_artifact(
    drive: Path,
    *,
    filename: str,
    run_id: str,
    plan: dict,
) -> None:
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / filename).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "workspace_id": "civilization",
                "plan": plan,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_phase_plan_execution_payload_uses_submitted_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    submitted = {
        "subtasks": [{"id": "scaffold", "files_to_create": ["src/pkg/app.py"]}],
    }
    proposal = {
        "subtasks": [{"id": "scaffold", "files_to_create": ["backend/src/app.py"]}],
    }
    _write_plan_artifact(
        drive,
        filename="phase_plan_submitted_latest.json",
        run_id="run-a",
        plan=submitted,
    )
    _write_plan_artifact(
        drive,
        filename="phase_plan_proposal_latest.json",
        run_id="run-a",
        plan=proposal,
    )
    runner = PhaseRunner(
        repo_root=repo,
        workspace_id="civilization",
        drive_root=drive,
    )
    payload, source = runner._phase_plan_execution_payload(run_id="run-a")
    assert source == "phase_plan_submitted_latest.json"
    assert payload.get("plan") == submitted


def test_phase_plan_review_payload_prefers_submitted_then_proposal(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    _write_plan_artifact(
        drive,
        filename="phase_plan_proposal_latest.json",
        run_id="run-b",
        plan={"subtasks": [{"id": "proposal-only"}]},
    )
    runner = PhaseRunner(repo_root=repo, workspace_id="civilization", drive_root=drive)
    payload, source = runner._phase_plan_review_payload(run_id="run-b")
    assert source == "phase_plan_proposal_latest.json"
    assert payload["plan"]["subtasks"][0]["id"] == "proposal-only"

    _write_plan_artifact(
        drive,
        filename="phase_plan_submitted_latest.json",
        run_id="run-b",
        plan={"subtasks": [{"id": "submitted"}]},
    )
    payload, source = runner._phase_plan_review_payload(run_id="run-b")
    assert source == "phase_plan_submitted_latest.json"
    assert payload["plan"]["subtasks"][0]["id"] == "submitted"


def test_execution_floor_rejects_backend_src_layout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "workspaces" / "civilization"
    workspace.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='civ'\n", encoding="utf-8")
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    bad_plan = {
        "subtasks": [
            {
                "id": "scaffold",
                "files_to_create": ["pyproject.toml", "backend/src/app.py"],
                "proof": {
                    "execution": {
                        "kind": "pytest",
                        "command": ["python", "-m", "pytest", "tests/test_app.py", "-q"],
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": ["x"],
                    },
                    "scope": {
                        "files_under_test": ["backend/src/app.py"],
                        "changed_files_expected": ["backend/src/app.py"],
                        "pytest_targets": ["tests/test_app.py"],
                    },
                    "anti_gaming": {
                        "requires_real_runtime": True,
                        "allows_mock": False,
                    },
                },
            }
        ],
    }
    _write_plan_artifact(
        drive,
        filename="phase_plan_submitted_latest.json",
        run_id="run-c",
        plan=bad_plan,
    )
    runner = PhaseRunner(repo_root=repo, workspace_id="civilization", drive_root=drive)
    failure = runner._latest_phase_plan_execution_floor_failure(run_id="run-c")
    assert failure
    assert "greenfield_python_src_layout_policy" in failure


def test_runner_schedules_subtask_review_after_execute_completion(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="civilization", drive_root=drive)
    execute = PhaseNode(
        id="execute",
        manifest_id="execute",
        status="running",
        subtasks=[
            SubtaskCard(
                id="s1",
                title="First",
                goal="first",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="done",
            ),
            SubtaskCard(
                id="s2",
                title="Second",
                goal="second",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="pending",
            ),
        ],
    )
    final_review = PhaseNode(id="final_review", manifest_id="final_review")
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="civilization",
        run_id="run-d",
        nodes=[execute, final_review],
    )

    scheduled = runner._schedule_subtask_review(
        plan=plan,
        execute_node=execute,
        completed_subtask=execute.subtasks[0],
        review_manifest_id="subtask_review",
        run_id="run-d",
    )

    assert scheduled == "subtask_review:s1"
    assert [node.id for node in plan.nodes] == [
        "execute",
        "subtask_review:s1",
        "final_review",
    ]
    review = plan.get_node("subtask_review:s1")
    assert review is not None
    assert review.parent_phase_id == "execute"
    assert review.overlay["subtask_id"] == "s1"


def test_subtask_review_pass_resumes_execute_when_more_subtasks_remain(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="civilization", drive_root=drive)
    execute = PhaseNode(
        id="execute",
        manifest_id="execute",
        status="done",
        subtasks=[
            SubtaskCard(
                id="s1",
                title="First",
                goal="first",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="done",
            ),
            SubtaskCard(
                id="s2",
                title="Second",
                goal="second",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="pending",
            ),
        ],
    )
    review = PhaseNode(
        id="subtask_review:s1",
        manifest_id="subtask_review",
        status="running",
        parent_phase_id="execute",
        overlay={"subtask_id": "s1", "execute_phase_id": "execute"},
    )
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="civilization",
        run_id="run-e",
        nodes=[execute, review],
    )

    runner._set_reviewed_subtask_verdict(
        plan=plan,
        review_node=review,
        verdict="ok",
    )
    runner._resume_execute_after_subtask_review(plan=plan, review_node=review)

    assert execute.subtasks[0].review_verdict == "ok"
    assert execute.status == "pending"
    assert execute.overlay["last_reviewed_subtask_id"] == "s1"


def test_latest_completed_subtask_reads_mark_subtask_signal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="civilization", drive_root=drive)
    execute = PhaseNode(
        id="execute",
        manifest_id="execute",
        status="running",
        subtasks=[
            SubtaskCard(
                id="s1",
                title="First",
                goal="first",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="done",
            )
        ],
    )
    execute.started_at = time.time() - 1
    (state / "phase_control_signals.jsonl").write_text(
        json.dumps(
            {
                "kind": "mark_subtask_complete",
                "task_id": "task-1",
                "created_at": time.time(),
                "payload": {"subtask_id": "s1", "status": "done"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    card = runner._latest_completed_subtask_from_phase(
        phase_node=execute,
        outcome={"task_id": "task-1"},
    )

    assert card is execute.subtasks[0]
