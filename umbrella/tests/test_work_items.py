import json
from types import SimpleNamespace

from umbrella.contracts.work_items import (
    WorkItem,
    load_active_work_item,
    materialize_work_items_from_phase_exit,
    reclassify_active_work_item,
    save_active_work_item,
    work_item_tool_filter,
)
from umbrella.context.compiler import compile_phase_context
from umbrella.context.render import bundle_to_overlay_dict
from umbrella.deep_agent_tools.phase_control_actions import (
    _mark_subtask_complete,
    _run_subtask_proof,
)
from umbrella.deep_agent_tools.workspace_ops import _execute_subtask_write_scope_block
from umbrella.orchestrator.runner import PhaseRunner
from umbrella.phases.base import PhaseNode, PhasePlan, SubtaskCard


def _execute_ctx(tmp_path, *, task_id: str = "run-1:execute"):
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    ctx = SimpleNamespace(
        host_repo_root=tmp_path,
        repo_dir=tmp_path,
        drive_root=drive,
        umbrella_managed=True,
        umbrella_phase_id="execute",
        context_overlays={
            "phase_manifest": {"id": "execute"},
            "phase_node": {"id": "execute", "manifest_id": "execute"},
        },
        current_task_type="phase_run",
        task_id=task_id,
    )
    return ctx, drive


def _write_phase_plan(drive, *, status: str = "running") -> None:
    (drive / "state" / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": status,
                        "subtasks": [
                            {
                                "id": "project-setup",
                                "status": "pending",
                                "files_to_change": ["pyproject.toml"],
                                "proof": {
                                    "execution": {
                                        "kind": "command",
                                        "command": ["python", "-c", "import demo"],
                                    },
                                    "oracle": {
                                        "required_properties": ["module_imports"]
                                    },
                                    "scope": {
                                        "changed_files_expected": ["pyproject.toml"]
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_final_review_loop_back_materializes_packaging_work_item() -> None:
    decision = {
        "phase_id": "final_review",
        "task_id": "run-1:final_review",
        "outcome": "loop_back",
        "target_phase": "execute",
        "required_changes": [
            {
                "id": "fix-import",
                "change_type": "create",
                "target_phase": "execute",
                "file": "workspace.toml",
                "message": "No module named 'calculator'",
            }
        ],
        "evidence_refs": [
            {
                "ref_type": "verification_report",
                "ref_id": "verify-1",
                "hash": "ledger-hash",
                "produced_by": "verifier",
            }
        ],
        "source_tool_call_id": "decision-1",
    }

    items = materialize_work_items_from_phase_exit(decision)

    assert len(items) == 1
    item = items[0]
    assert item.kind == "packaging_import_repair"
    assert item.required_changes[0]["id"] == "fix-import"
    assert "workspace.toml" in item.allowed_files
    assert item.evidence_refs[0]["ref_id"] == "verify-1"
    assert item.proof_contract["execution"]["command"] == [
        "python",
        "-c",
        "import calculator",
    ]


def test_phase_runner_loopback_adds_repair_subtask_and_active_work_item(tmp_path) -> None:
    repo = tmp_path
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    runner = PhaseRunner(repo_root=repo, workspace_id="demo", drive_root=drive)
    execute = PhaseNode(
        id="execute",
        manifest_id="execute",
        status="done",
        subtasks=[
            SubtaskCard(
                id="project-setup",
                title="Project setup",
                goal="setup",
                allowed_tools=frozenset(),
                allowed_skills=frozenset(),
                status="done",
            )
        ],
    )
    final_review = PhaseNode(id="final_review", manifest_id="final_review", status="running")
    plan = PhasePlan(
        plan_id="p1",
        workspace_id="demo",
        run_id="run-1",
        nodes=[execute, final_review],
    )
    (drive / "state" / "phase_exit_decision_latest.json").write_text(
        json.dumps(
            {
                "phase_id": "final_review",
                "task_id": "task-1",
                "outcome": "loop_back",
                "target_phase": "execute",
                "required_changes": [
                    {
                        "id": "fix-import",
                        "change_type": "create",
                        "file": "workspace.toml",
                        "message": "No module named 'calculator'",
                    }
                ],
                "source_tool_call_id": "decision-1",
            }
        ),
        encoding="utf-8",
    )

    result, _ = runner._finish_phase_loop_back(
        phase_node=final_review,
        plan=plan,
        run_id="run-1",
        outcome={"task_id": "task-1"},
        loop_back_target="execute",
        retry_reason="final_review loopback",
    )

    assert result.loop_back_target == "execute"
    repair_subtasks = [card for card in execute.subtasks or [] if card.status == "pending"]
    assert repair_subtasks
    assert repair_subtasks[0].id.startswith("repair-packaging-import")
    active = load_active_work_item(drive)
    assert active is not None
    assert active.active_subtask_id == repair_subtasks[0].id


def test_work_item_tool_filter_removes_execute_write_and_complete_without_active_item() -> None:
    filtered = work_item_tool_filter(
        {
            "allow": [
                "read_file",
                "apply_workspace_patch",
                "run_subtask_proof",
                "mark_subtask_complete",
            ],
            "deny": [],
            "required": ["mark_subtask_complete"],
        },
        work_item=None,
    )

    assert "read_file" in filtered["allow"]
    assert "apply_workspace_patch" not in filtered["allow"]
    assert "run_subtask_proof" not in filtered["allow"]
    assert "mark_subtask_complete" not in filtered["allow"]


def test_work_item_reclassified_from_implementation_to_packaging_import(tmp_path) -> None:
    ctx, drive = _execute_ctx(tmp_path)
    save_active_work_item(
        drive,
        WorkItem(
            id="execute:calculator-core",
            kind="implementation_repair",
            source_phase="execute",
            target_phase="execute",
            active_subtask_id="calculator-core",
            allowed_files=("src/calculator/core.py", "tests/test_calculator_core.py"),
            proof_contract={"execution": {"kind": "pytest"}},
            tool_envelope={"allowed_tools": ["apply_workspace_patch"]},
        ),
    )

    item = reclassify_active_work_item(
        drive,
        {
            "reason_code": "package_import_env_mismatch",
            "evidence_refs": ["ledger_event:proof-1"],
            "message": "ModuleNotFoundError: No module named 'calculator'",
        },
        decision_id="decision-1",
    )

    assert item is not None
    assert item.id != "execute:calculator-core"
    assert item.kind == "packaging_import_repair"
    assert item.allowed_files == (
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "workspace.toml",
    )
    assert "src/" in item.forbidden_files
    assert load_active_work_item(drive).id == item.id


def test_reclassified_work_item_updates_tool_envelope(tmp_path) -> None:
    _ctx, drive = _execute_ctx(tmp_path)
    save_active_work_item(
        drive,
        WorkItem(
            id="execute:calculator-core",
            kind="implementation_repair",
            source_phase="execute",
            target_phase="execute",
            active_subtask_id="calculator-core",
            allowed_files=("src/calculator/core.py",),
            proof_contract={},
            tool_envelope={"allowed_tools": ["apply_workspace_patch"]},
        ),
    )

    item = reclassify_active_work_item(
        drive,
        {"reason_code": "package_import_env_mismatch"},
        decision_id="decision-1",
    )

    assert item is not None
    assert item.tool_envelope["policy"]["allowed_files"] == list(item.allowed_files)
    assert "apply_plan_revision_patch" not in item.tool_envelope["allowed_tools"]


def test_blocked_work_item_tool_surface_only_required_actions() -> None:
    work_item = WorkItem(
        id="work:1",
        kind="proof_contract_repair",
        source_phase="execute",
        target_phase="execute",
        active_subtask_id="calculator-core",
        allowed_files=(),
        proof_contract={},
        tool_envelope={
            "allowed_tools": [
                "read_file",
                "apply_workspace_patch",
                "apply_plan_revision_patch",
                "run_subtask_proof",
                "mark_subtask_complete",
                "request_watcher_review",
            ]
        },
    )

    filtered = work_item_tool_filter(
        {
            "allow": [
                "read_file",
                "apply_workspace_patch",
                "apply_plan_revision_patch",
                "run_subtask_proof",
                "mark_subtask_complete",
                "request_watcher_review",
            ],
            "deny": [],
            "required": ["mark_subtask_complete"],
        },
        work_item=work_item,
        control_decision={
            "kind": "blocked_no_valid_next_action",
            "allowed_next_tools": ["apply_plan_revision_patch"],
        },
    )

    assert filtered["allow"] == ["apply_plan_revision_patch"]
    assert "mark_subtask_complete" in filtered["deny"]
    assert "request_watcher_review" in filtered["deny"]
    assert "run_subtask_proof" in filtered["deny"]


def test_compile_phase_context_carries_active_work_item(tmp_path) -> None:
    manifest = SimpleNamespace(id="execute")
    phase_node = SimpleNamespace(id="execute")
    active_work_item = {
        "id": "work:1",
        "kind": "implementation_repair",
        "active_subtask_id": "project-setup",
        "allowed_files": ["pyproject.toml"],
    }

    bundle = compile_phase_context(
        workspace_root=tmp_path,
        workspace_id="demo",
        run_id="run-1",
        task_id="task-1",
        manifest=manifest,
        phase_node=phase_node,
        tool_filter={"allow": ["read_file"], "deny": [], "required": []},
        active_subtask={"id": "project-setup", "files_to_change": ["pyproject.toml"]},
        active_work_item=active_work_item,
    )
    overlay = bundle_to_overlay_dict(bundle)

    assert overlay["active_work_item_id"] == "work:1"
    assert overlay["active_work_item"]["active_subtask_id"] == "project-setup"


def test_mark_subtask_complete_rejects_model_supplied_state_fields(tmp_path) -> None:
    ctx, drive = _execute_ctx(tmp_path)
    _write_phase_plan(drive)

    result = _mark_subtask_complete(
        ctx,
        subtask_id="execute",
        summary="done",
        evidence=["ledger_event:fake"],
    )
    payload = json.loads(result)

    assert payload["error"] == "MODEL_SUPPLIED_COMPLETION_FIELDS_REJECTED"
    assert "subtask_id" in payload["rejected_fields"]


def test_run_subtask_proof_empty_args_returns_active_work_item_message(tmp_path) -> None:
    ctx, drive = _execute_ctx(tmp_path)
    _write_phase_plan(drive)
    save_active_work_item(
        drive,
        WorkItem(
            id="work:1",
            kind="implementation_repair",
            source_phase="execute",
            target_phase="execute",
            active_subtask_id="project-setup",
            allowed_files=("pyproject.toml",),
            proof_contract={},
        ),
    )

    result = _run_subtask_proof(ctx)
    payload = json.loads(result)

    assert payload["error"] == "ACTIVE_SUBTASK_ID_REQUIRED"
    assert payload["active_subtask_id"] == "project-setup"


def test_work_item_write_scope_blocks_outside_allowed_files(tmp_path) -> None:
    ctx, drive = _execute_ctx(tmp_path)
    save_active_work_item(
        drive,
        WorkItem(
            id="work:1",
            kind="implementation_repair",
            source_phase="execute",
            target_phase="execute",
            active_subtask_id="project-setup",
            allowed_files=("pyproject.toml",),
            proof_contract={},
        ),
    )

    block = _execute_subtask_write_scope_block(
        ctx,
        planned=[{"path": "tests/test_gui.py", "action": "update"}],
    )

    assert block is not None
    assert block["reason"] == "work_item_scope_mismatch"
    assert block["outside_allowed_files"] == ["tests/test_gui.py"]


def test_work_item_write_scope_requires_state_when_overlay_declares_active_item(tmp_path) -> None:
    ctx, _drive = _execute_ctx(tmp_path)
    ctx.context_overlays["active_work_item_id"] = "work:missing"

    block = _execute_subtask_write_scope_block(
        ctx,
        planned=[{"path": "pyproject.toml", "action": "update"}],
    )

    assert block is not None
    assert block["reason"] == "active_work_item_required"
