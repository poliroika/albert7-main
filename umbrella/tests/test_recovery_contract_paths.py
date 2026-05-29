import json
from pathlib import Path
from types import SimpleNamespace

from umbrella.deep_agent_tools.phase_control_retry import (
    _phase_subtask_retry_watcher_review_payload,
)


def _execute_context(tmp_path: Path) -> SimpleNamespace:
    repo = tmp_path
    workspace = repo / "workspaces" / "demo"
    drive = workspace / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    return SimpleNamespace(
        host_repo_root=repo,
        repo_dir=repo,
        drive_root=drive,
        task_id="task:execute",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
    )


def _write_execute_plan(ctx: SimpleNamespace, proof: dict) -> None:
    plan = {
        "version": 1,
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "calculator-gui",
                        "status": "pending",
                        "proof": proof,
                    }
                ],
            }
        ],
    }
    (ctx.drive_root / "state" / "phase_plan.json").write_text(
        json.dumps(plan),
        encoding="utf-8",
    )


def _write_failed_proof_log(ctx: SimpleNamespace, result: dict) -> None:
    row = {
        "task_id": ctx.task_id,
        "tool": "run_subtask_proof",
        "args": {"subtask_id": "calculator-gui"},
        "result_preview": json.dumps(result),
    }
    (ctx.drive_root / "logs" / "tools.jsonl").write_text(
        json.dumps(row) + "\n",
        encoding="utf-8",
    )


def test_headless_real_root_recovery_uses_scope_pytest_targets(tmp_path: Path) -> None:
    ctx = _execute_context(tmp_path)
    _write_execute_plan(
        ctx,
        {
            "execution": {
                "kind": "pytest",
                "command": ["python", "-m", "pytest", "tests/test_gui.py"],
            },
            "scope": {"pytest_targets": ["tests/test_calculator_gui.py"]},
        },
    )
    _write_failed_proof_log(
        ctx,
        {
            "passed": False,
            "exit_code": 1,
            "subtask_id": "calculator-gui",
            "command": ["python", "-m", "pytest", "tests/test_gui.py"],
            "shell_result": {"output": "_tkinter.TclError at root = tk.Tk()"},
            "proof_ref": {"ref_type": "ledger_event", "ref_id": "proof-1"},
        },
    )

    payload = _phase_subtask_retry_watcher_review_payload(
        ctx,
        reason="headless proof tried to create a real Tk root",
        contract_issues=[
            {
                "code": "headless_proof_uses_real_gui_root",
                "target_subtask_id": "calculator-gui",
                "contract_path": "proof.pytest_targets[0]",
                "message": "Generated headless proof uses a real GUI root.",
                "evidence_refs": ["ledger_event:proof-1"],
            }
        ],
    )

    assert payload["recovery_decision"]["kind"] == "proof_execution_infra"
    assert payload["loop_back_target"] == "plan"
    [change] = payload["required_plan_changes"]
    assert change["path"] == "proof.scope.pytest_targets"
    assert "proof.pytest_targets" not in json.dumps(payload)


def test_bad_oracle_required_property_delta_is_canonicalized(tmp_path: Path) -> None:
    ctx = _execute_context(tmp_path)
    _write_execute_plan(
        ctx,
        {
            "execution": {
                "kind": "pytest",
                "command": ["python", "-m", "pytest", "tests/test_model.py"],
            },
            "oracle": {
                "required_properties": ["distinct_inputs_distinct_outputs"],
            },
        },
    )
    _write_failed_proof_log(
        ctx,
        {
            "passed": False,
            "exit_code": 1,
            "subtask_id": "calculator-gui",
            "command": ["python", "-m", "pytest", "tests/test_model.py"],
            "shell_result": {"output": "assert 9 != 9"},
            "proof_ref": {"ref_type": "ledger_event", "ref_id": "proof-1"},
        },
    )

    payload = _phase_subtask_retry_watcher_review_payload(
        ctx,
        reason="typed bad oracle",
        contract_issues=[
            {
                "code": "bad_generated_oracle",
                "target_subtask_id": "calculator-gui",
                "contract_path": "proof.required_properties",
                "invalid_values": ["distinct_inputs_distinct_outputs"],
                "required_deltas": [
                    {
                        "op": "remove",
                        "path": "proof.required_properties",
                        "values": ["distinct_inputs_distinct_outputs"],
                    }
                ],
                "evidence_refs": ["ledger_event:proof-1"],
            }
        ],
    )

    assert payload["recovery_decision"]["kind"] == "plan_contract_revision"
    [delta] = payload["plan_revision_patch"]["required_deltas"]
    assert delta["path"] == "proof.oracle.required_properties"
    [change] = payload["required_plan_changes"]
    assert change["path"] == "proof.oracle.required_properties"


def test_invalid_required_delta_blocks_recovery_contract_not_plan(
    tmp_path: Path,
) -> None:
    ctx = _execute_context(tmp_path)
    _write_execute_plan(
        ctx,
        {
            "execution": {
                "kind": "pytest",
                "command": ["python", "-m", "pytest", "tests/test_model.py"],
            },
            "oracle": {
                "required_properties": ["distinct_inputs_distinct_outputs"],
            },
        },
    )
    _write_failed_proof_log(
        ctx,
        {
            "passed": False,
            "exit_code": 1,
            "subtask_id": "calculator-gui",
            "command": ["python", "-m", "pytest", "tests/test_model.py"],
            "shell_result": {"output": "assert 9 != 9"},
        },
    )

    payload = _phase_subtask_retry_watcher_review_payload(
        ctx,
        reason="typed bad oracle with invalid delta",
        contract_issues=[
            {
                "code": "bad_generated_oracle",
                "target_subtask_id": "calculator-gui",
                "contract_path": "proof.required_properties",
                "invalid_values": ["distinct_inputs_distinct_outputs"],
                "required_deltas": [
                    {
                        "op": "remove",
                        "path": "proof.required_properties[0]",
                        "values": ["distinct_inputs_distinct_outputs"],
                    }
                ],
            }
        ],
    )

    assert payload["status"] == "invalid_recovery_contract"
    assert payload["recovery_decision"]["kind"] == "recovery_contract_invalid"
    assert payload["loop_back_target"] == "none"
    assert "plan_revision_patch" not in payload
