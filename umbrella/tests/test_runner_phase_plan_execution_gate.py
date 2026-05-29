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


def _write_capability_declaration(drive: Path) -> None:
    state = drive / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "capabilities": {
                    "python": {"available": True, "source": "probe"},
                    "subprocess": {"available": True, "source": "probe"},
                    "tkinter": {"available": True, "source": "declared"},
                },
                "notes": "Python runtime capabilities are available for phase plan validation.",
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


def test_execution_items_from_plan_canonicalizes_legacy_phase_alias() -> None:
    plan = {
        "phases": [
            {
                "id": "build-core",
                "title": "Build core",
                "goal": "Implement core behavior.",
            }
        ]
    }

    items = PhaseRunner._execution_items_from_plan(plan)

    assert [item["id"] for item in items] == ["build-core"]


def test_execution_floor_rejects_backend_src_layout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "workspaces" / "civilization"
    workspace.mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='civ'\n", encoding="utf-8")
    (workspace / "workspace.toml").write_text(
        "[policies]\ngreenfield_python_src_layout = true\n",
        encoding="utf-8",
    )
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    _write_capability_declaration(drive)
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


def test_execution_floor_reads_submitted_capability_declaration(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "workspaces" / "civilization"
    (workspace / "src" / "civ").mkdir(parents=True)
    (workspace / "src" / "civ" / "core.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_core.py").write_text(
        "from src.civ.core import add\n\n"
        "def test_add_behavior():\n"
        "    assert add(1, 2) == 3\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    drive = workspace / ".memory" / "drive"
    drive.mkdir(parents=True)
    _write_capability_declaration(drive)
    plan = {
        "subtasks": [
            {
                "id": "core",
                "files_to_create": ["src/civ/core.py", "tests/test_core.py"],
                "proof": {
                    "execution": {
                        "kind": "pytest",
                        "command": ["python", "-m", "pytest", "tests/test_core.py", "-q"],
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": [
                            "distinct_inputs_distinct_outputs",
                            "no_test_tampering",
                        ],
                        "negative_cases_required": True,
                    },
                    "scope": {
                        "files_under_test": ["src/civ/core.py"],
                        "changed_files_expected": ["src/civ/core.py", "tests/test_core.py"],
                        "pytest_targets": ["tests/test_core.py"],
                    },
                    "anti_gaming": {
                        "requires_real_runtime": True,
                        "allows_mock": False,
                    },
                    "generated_test_contract": {
                        "interface_model": {
                            "api": "src.civ.core.add",
                            "valid_values": ["(1, 2)", "(2, 3)"],
                        },
                        "oracle_claims": [
                            {
                                "claim_id": "add_two_positive_pairs",
                                "source": "task_requirement",
                                "subject": "add",
                                "input_values": ["(1, 2)", "(2, 3)"],
                                "accepted": True,
                                "expected_behavior": "returns numeric sums",
                                "test_refs": ["tests/test_core.py"],
                            }
                        ],
                    },
                    "required_capabilities": ["python"],
                },
            }
        ],
    }
    _write_plan_artifact(
        drive,
        filename="phase_plan_submitted_latest.json",
        run_id="run-cap",
        plan=plan,
    )

    runner = PhaseRunner(repo_root=repo, workspace_id="civilization", drive_root=drive)

    assert runner._latest_phase_plan_execution_floor_failure(run_id="run-cap") == ""


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


def test_fresh_pending_subtask_review_is_not_auto_closed_by_green_proof(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
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
                completion={"verification_report": {"passed": True}},
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
        status="pending",
        parent_phase_id="execute",
        overlay={"subtask_id": "s1", "execute_phase_id": "execute"},
    )
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="civilization",
        run_id="run-fresh-review",
        nodes=[execute, review],
    )

    runner._close_recovered_subtask_reviews(plan)

    assert review.status == "pending"


def test_recovered_revised_subtask_review_can_be_auto_closed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
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
                review_verdict="revise",
                completion={"verification_report": {"passed": True}},
            ),
        ],
    )
    review = PhaseNode(
        id="subtask_review:s1",
        manifest_id="subtask_review",
        status="pending",
        parent_phase_id="execute",
        overlay={"subtask_id": "s1", "execute_phase_id": "execute"},
    )
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="civilization",
        run_id="run-recovered-review",
        nodes=[execute, review],
    )

    runner._close_recovered_subtask_reviews(plan)

    assert review.status == "done"


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


def test_verify_loopback_invalidates_final_review_before_repair(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="civilization", drive_root=drive)
    execute = PhaseNode(id="execute", manifest_id="execute", status="done")
    final_review = PhaseNode(
        id="final_review",
        manifest_id="final_review",
        status="done",
    )
    verify = PhaseNode(id="verify", manifest_id="verify", status="running")
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="civilization",
        run_id="run-f",
        nodes=[execute, final_review, verify],
    )

    runner._invalidate_after_verify_loopback(
        plan=plan,
        source_phase=verify,
        loop_back_target="execute",
    )

    assert final_review.status == "pending"
    assert final_review.overlay["invalidated_by_verify_loopback"] is True
    assert verify.status == "pending"


def test_generic_mini_review_after_uses_existing_next_review(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="civilization", drive_root=drive)
    research = PhaseNode(id="research", manifest_id="research")
    review = PhaseNode(id="research_review", manifest_id="research_review")
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="civilization",
        run_id="run-g",
        nodes=[research, review],
    )

    scheduled = runner._schedule_generic_review_phase(
        plan=plan,
        phase_node=research,
        review_manifest_id="research_review",
        run_id="run-g",
    )

    assert scheduled == "research_review"
    assert [node.id for node in plan.nodes] == ["research", "research_review"]


def test_generic_mini_review_after_inserts_missing_review(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    drive = repo / "workspaces" / "civilization" / ".memory" / "drive"
    drive.mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="civilization", drive_root=drive)
    research = PhaseNode(id="research", manifest_id="research")
    execute = PhaseNode(id="execute", manifest_id="execute")
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="civilization",
        run_id="run-h",
        nodes=[research, execute],
    )

    scheduled = runner._schedule_generic_review_phase(
        plan=plan,
        phase_node=research,
        review_manifest_id="research_review",
        run_id="run-h",
    )

    assert scheduled == "research_review:research"
    assert [node.id for node in plan.nodes] == [
        "research",
        "research_review:research",
        "execute",
    ]


def test_research_finding_floor_depends_on_depth(tmp_path: Path) -> None:
    from umbrella.phases.loader import load_manifest

    manifest = load_manifest(
        Path(__file__).parent.parent / "phases" / "manifests" / "research.yaml"
    )
    none_node = PhaseNode(
        id="research",
        manifest_id="research",
        overlay={"research_depth": "none"},
    )
    light_node = PhaseNode(
        id="research",
        manifest_id="research",
        overlay={"research_depth": "light"},
    )
    full_node = PhaseNode(
        id="research",
        manifest_id="research",
        overlay={"research_depth": "full"},
    )

    runner = PhaseRunner(
        repo_root=tmp_path,
        workspace_id="ws",
        drive_root=tmp_path / "drive",
    )
    assert (
        runner._research_summary_min_valid_findings_for_manifest(
            manifest,
            phase_node=none_node,
        )
        == 0
    )
    assert (
        runner._research_summary_min_valid_findings_for_manifest(
            manifest,
            phase_node=light_node,
        )
        == 1
    )
    assert (
        runner._research_summary_min_valid_findings_for_manifest(
            manifest,
            phase_node=full_node,
        )
        == 3
    )


def test_phase_completion_failure_preserves_phase_impasse(tmp_path: Path) -> None:
    from umbrella.phases.loader import load_manifest

    manifest = load_manifest(
        Path(__file__).parent.parent / "phases" / "manifests" / "research.yaml"
    )
    drive = tmp_path / "drive"
    drive.mkdir(parents=True)
    runner = PhaseRunner(
        repo_root=tmp_path,
        workspace_id="ws",
        drive_root=drive,
    )
    node = PhaseNode(
        id="research",
        manifest_id="research",
        status="running",
        started_at=100.0,
    )
    plan = PhasePlan(
        plan_id="plan",
        workspace_id="ws",
        run_id="run-impasse",
        nodes=[node],
    )

    failure = runner._phase_completion_failure(
        phase_node=node,
        plan=plan,
        manifest=manifest,
        outcome={
            "task_id": "run-impasse:research",
            "run_id": "run-impasse",
            "result": (
                "phase_impasse: completion tool failed repeatedly with the "
                "same control-plane error"
            ),
        },
    )

    assert failure.startswith("phase_impasse:")
    assert "completion tool failed repeatedly" in failure
    assert "missing required call" not in failure


def test_research_exit_palace_write_floor_uses_effective_depth(
    tmp_path: Path,
) -> None:
    from umbrella.phases.loader import load_manifest

    manifest = load_manifest(
        Path(__file__).parent.parent / "phases" / "manifests" / "research.yaml"
    )
    drive = tmp_path / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    task_id = "run-light:research:123"
    finding_id = "finding-1"
    (state / "phase_control_signals.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "signal_id": "cap-1",
                    "created_at": 110.0,
                    "kind": "submit_capability_declaration",
                    "payload": {"status": "submitted"},
                    "task_id": task_id,
                    "run_id": "run-light",
                    "phase": "research",
                },
                {
                    "signal_id": "summary-1",
                    "created_at": 111.0,
                    "kind": "submit_research_summary",
                    "payload": {
                        "architecture_id": "arch",
                        "findings_ids": [finding_id],
                        "coverage_status": "complete",
                    },
                    "task_id": task_id,
                    "run_id": "run-light",
                    "phase": "research",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (logs / "tools.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "task_id": task_id,
                    "tool": "palace_add",
                    "result_preview": json.dumps(
                        {
                            "saved": True,
                            "id": finding_id,
                            "store": "palace.run",
                            "kind": "research_finding",
                        }
                    ),
                },
                {
                    "task_id": task_id,
                    "tool": "submit_research_summary",
                    "args": {
                        "architecture_id": "arch",
                        "findings_ids": [finding_id],
                    },
                    "result_preview": "OK: Research summary submitted",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (state / "research_summary_latest.json").write_text(
        json.dumps(
            {
                "run_id": "run-light",
                "task_id": task_id,
                "architecture_id": "arch",
                "findings_ids": [finding_id],
                "coverage_status": "complete",
            }
        ),
        encoding="utf-8",
    )
    runner = PhaseRunner(
        repo_root=tmp_path,
        workspace_id="ws",
        drive_root=drive,
    )
    node = PhaseNode(
        id="research",
        manifest_id="research",
        status="running",
        overlay={"research_depth": "light"},
        started_at=100.0,
    )
    plan = PhasePlan(
        plan_id="plan",
        workspace_id="ws",
        run_id="run-light",
        nodes=[node],
    )

    assert (
        runner._phase_completion_failure(
            phase_node=node,
            plan=plan,
            manifest=manifest,
            outcome={"task_id": task_id, "run_id": "run-light"},
        )
        == ""
    )
