from types import SimpleNamespace
import json

from umbrella.deep_agent_tools.phase_control_actions import _submit_micro_review

_COVERAGE = {
    "policy_conflicts": True,
    "oracle_compatibility": True,
    "proof_strength": True,
    "scope_validity": True,
    "runtime_capabilities": True,
    "test_validity": True,
}


def test_submit_micro_review_normalizes_required_delta_paths(tmp_path) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:plan_review",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "plan", "manifest_id": "plan"}},
    )

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "bad_generated_oracle",
                "severity": "blocking",
                "target_subtask_id": "logic",
                "contract_path": "proof.required_properties",
                "invalid_values": ["distinct_inputs_distinct_outputs"],
                "message": "Remove ungrounded distinct output oracle.",
                "required_deltas": [
                    {
                        "op": "remove",
                        "path": "proof.required_properties",
                        "values": ["distinct_inputs_distinct_outputs"],
                    }
                ],
            }
        ],
        loop_back_target="plan",
        coverage=_COVERAGE,
    )

    assert result.startswith("OK:"), result
    latest = (drive / "state" / "phase_control_signals.jsonl").read_text(
        encoding="utf-8"
    )
    assert "proof.oracle.required_properties" in latest
    assert "proof.required_properties" not in latest


def test_submit_micro_review_canonicalizes_indexed_required_delta_path(tmp_path) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:plan_review",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "plan", "manifest_id": "plan"}},
    )

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "bad_generated_oracle",
                "severity": "blocking",
                "target_subtask_id": "logic",
                "contract_path": "proof.required_properties",
                "invalid_values": ["distinct_inputs_distinct_outputs"],
                "message": "Remove ungrounded distinct output oracle.",
                "required_deltas": [
                    {
                        "op": "remove",
                        "path": "proof.required_properties[0]",
                        "values": ["distinct_inputs_distinct_outputs"],
                    }
                ],
            }
        ],
        loop_back_target="plan",
        coverage=_COVERAGE,
    )

    assert result.startswith("OK:"), result
    latest = (drive / "state" / "phase_control_signals.jsonl").read_text(
        encoding="utf-8"
    )
    assert "proof.oracle.required_properties" in latest
    assert "proof.required_properties[0]" not in latest


def test_submit_micro_review_canonicalizes_generated_oracle_issue_paths(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:plan_review",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "plan", "manifest_id": "plan"}},
    )

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "bad_generated_oracle",
                "severity": "blocking",
                "target_subtask_id": "logic",
                "contract_path": "proof.generated_test_contract.oracle_claims[3]",
                "message": "Generated oracle claims an exact GUI text value.",
                "required_deltas": [
                    {
                        "op": "replace",
                        "path": "divide_returns_quotient.expected_output",
                        "value": "Only assert displayed quotient behavior.",
                    },
                    {
                        "op": "replace",
                        "path": "oracle_claims.*.expected_output",
                        "value": "Use behavior-level oracle claims.",
                    },
                ],
            }
        ],
        loop_back_target="plan",
        coverage=_COVERAGE,
    )

    assert result.startswith("OK:"), result
    rows = [
        json.loads(line)
        for line in (drive / "state" / "phase_control_signals.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    latest = json.dumps(rows[-2]["payload"], sort_keys=True)
    assert "proof.generated_test_contract.oracle_claims" in latest
    assert "proof.generated_test_contract.oracle_claims[3]" not in latest
    assert "divide_returns_quotient.expected_output" not in latest
    assert "oracle_claims.*.expected_output" not in latest


def test_submit_micro_review_compiles_required_plan_changes_to_targets(
    tmp_path,
) -> None:
    drive = tmp_path / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (drive / "state" / "phase_plan_submitted_latest.json").write_text(
        json.dumps(
            {
                "plan": {
                    "subtasks": [
                        {
                            "id": "runtime-smoke",
                            "files_to_create": [],
                            "proof": {
                                "execution": {
                                    "kind": "command",
                                    "command": ["python", "-m", "demo"],
                                },
                                "oracle": {
                                    "required_properties": ["runtime_started"],
                                },
                            },
                        },
                        {
                            "id": "gui-integration",
                            "proof": {
                                "scope": {
                                    "changed_files_expected": [
                                        "src/demo/gui.py",
                                    ],
                                    "pytest_targets": [
                                        "tests/test_gui_headless.py::test_button_events",
                                    ],
                                },
                            },
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    ctx = SimpleNamespace(
        drive_root=drive,
        task_id="run-1:plan_review",
        current_task_type="phase_run",
        context_overlays={"phase_node": {"id": "plan_review", "manifest_id": "plan_review"}},
    )

    result = _submit_micro_review(
        ctx,
        verdict="revise",
        issues=[
            {
                "code": "proof_scope_mismatch",
                "severity": "blocking",
                "subtask_id": "gui-integration",
                "message": "pytest target scope needs revision.",
            },
            {
                "code": "weak_proof",
                "severity": "blocking",
                "subtask_id": "runtime-smoke",
                "message": "Runtime proof needs behavioral evidence.",
            },
            {
                "code": "missing_proof",
                "severity": "blocking",
                "subtask_id": "runtime-smoke",
                "message": "Driver file must be declared.",
            },
        ],
        loop_back_target="plan",
        coverage=_COVERAGE,
        required_plan_changes=[
            {
                "id": "runtime-smoke-proof-fix",
                "path": "proof.execution",
                "op": "replace_applied",
                "value": "Add behavioral GUI verification.",
                "severity": "blocking",
            },
            {
                "id": "runtime-smoke-driver-declaration",
                "path": "files_to_create",
                "op": "contains",
                "value": "tests/helpers/gui_driver.py",
                "severity": "blocking",
            },
            {
                "id": "gui-integration-scope-fix",
                "path": "changed_files_expected",
                "op": "contains",
                "value": "tests/test_gui_headless.py",
                "severity": "blocking",
            },
        ],
    )

    assert result.startswith("OK:"), result
    rows = [
        json.loads(line)
        for line in (drive / "state" / "phase_control_signals.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    payload = rows[-2]["payload"]
    changes = payload["required_plan_changes"]
    assert any(
        item.get("target_subtask_id") == "gui-integration"
        and item.get("path") == "proof.scope.changed_files_expected"
        for item in changes
        if isinstance(item, dict)
    )
    assert any(
        item.get("target_subtask_id") == "runtime-smoke"
        and item.get("path") == "proof.execution"
        and item.get("op") == "semantic_diff"
        and "previous_value" in item
        for item in changes
        if isinstance(item, dict)
    )
    assert any(
        item.get("target_subtask_id") == "runtime-smoke"
        and item.get("path") == "files_to_create"
        for item in changes
        if isinstance(item, dict)
    )
