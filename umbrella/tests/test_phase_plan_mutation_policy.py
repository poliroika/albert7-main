import json
from types import SimpleNamespace

from umbrella.deep_agent_tools.phase_control_actions import _mutate_phase_plan


def _write_plan(tmp_path, command):
    drive = tmp_path / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    plan = {
        "plan_id": "plan-1",
        "workspace_id": "calculator",
        "run_id": "run-1",
        "version": 1,
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "calculator-gui",
                        "title": "GUI",
                        "status": "pending",
                        "proof": {
                            "execution": {
                                "kind": "pytest",
                                "command": command,
                                "timeout_sec": 60,
                                "shell": False,
                                "subdir": "",
                            },
                            "oracle": {
                                "oracle_type": "unit_assertions",
                                "required_properties": ["no_test_tampering"],
                                "negative_cases_required": True,
                                "input_sensitivity_required": False,
                            },
                            "scope": {
                                "files_under_test": ["src/calculator/gui.py"],
                                "changed_files_expected": [
                                    "src/calculator/gui.py",
                                    "tests/test_calculator_gui.py",
                                ],
                                "pytest_targets": ["tests/test_calculator_gui.py"],
                            },
                            "anti_gaming": {
                                "allows_mock": False,
                                "allows_snapshot_update": False,
                                "allows_test_only_change": False,
                                "requires_real_runtime": True,
                            },
                        },
                    }
                ],
            }
        ],
    }
    (state / "phase_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )
    return drive


def _ctx(drive):
    return SimpleNamespace(
        drive_root=drive,
        task_id="run-1:execute:123",
        context_overlays={"phase_node": {"id": "execute", "manifest_id": "execute"}},
        loop_state_view={"phase_label": "execute"},
    )


def test_mutate_phase_plan_blocks_pytest_selection_weakening(tmp_path) -> None:
    drive = _write_plan(
        tmp_path, ["python", "-m", "pytest", "tests/test_calculator_gui.py", "-q"]
    )
    result = _mutate_phase_plan(
        _ctx(drive),
        patch={
            "subtasks": [
                {
                    "id": "calculator-gui",
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": [
                                "python",
                                "-m",
                                "pytest",
                                "tests/test_calculator_gui.py",
                                "-q",
                                "-k",
                                "not TestGuiStructure",
                            ],
                            "timeout_sec": 60,
                            "shell": False,
                            "subdir": "",
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["no_test_tampering"],
                        },
                    },
                }
            ]
        },
    )

    assert result.startswith("ERROR: cannot mutate phase plan")
    assert "proof_selection_filter_forbidden" in result or "-k" in result


def test_mutate_phase_plan_blocks_pytest_node_target_narrowing(tmp_path) -> None:
    drive = _write_plan(
        tmp_path, ["python", "-m", "pytest", "tests/test_calculator_gui.py", "-q"]
    )
    result = _mutate_phase_plan(
        _ctx(drive),
        patch={
            "subtasks": [
                {
                    "id": "calculator-gui",
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": [
                                "python",
                                "-m",
                                "pytest",
                                "tests/test_calculator_gui.py::TestCalculatorGUIBasicOperations",
                                "-q",
                            ],
                            "timeout_sec": 60,
                            "shell": False,
                            "subdir": "",
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["no_test_tampering"],
                        },
                        "scope": {
                            "pytest_targets": [
                                "tests/test_calculator_gui.py::TestCalculatorGUIBasicOperations"
                            ],
                        },
                    },
                }
            ]
        },
    )

    assert result.startswith("ERROR: cannot mutate phase plan")
    assert "proof_selection_narrowing_forbidden" in result


def test_mutate_phase_plan_rejects_unknown_file_exists_proof_kind(tmp_path) -> None:
    drive = _write_plan(
        tmp_path, ["python", "-m", "pytest", "tests/test_calculator_gui.py", "-q"]
    )
    result = _mutate_phase_plan(
        _ctx(drive),
        patch={
            "subtasks": [
                {
                    "id": "calculator-gui",
                    "proof": {
                        "execution": {
                            "kind": "file_exists",
                            "command": [
                                "python",
                                "-c",
                                "from pathlib import Path; assert Path('src/calculator/gui.py').exists()",
                            ],
                        },
                    },
                }
            ]
        },
    )

    assert result.startswith("ERROR: cannot mutate phase plan")
    assert "unknown_proof_kind" in result


def test_mutate_phase_plan_blocks_pytest_to_build_downgrade(tmp_path) -> None:
    drive = _write_plan(
        tmp_path, ["python", "-m", "pytest", "tests/test_calculator_gui.py", "-q"]
    )
    result = _mutate_phase_plan(
        _ctx(drive),
        patch={
            "subtasks": [
                {
                    "id": "calculator-gui",
                    "proof": {
                        "execution": {
                            "kind": "build",
                            "command": [
                                "python",
                                "-m",
                                "compileall",
                                "src",
                            ],
                        },
                    },
                }
            ]
        },
    )

    assert result.startswith("ERROR: cannot mutate phase plan")
    assert "proof_kind_downgrade_forbidden" in result


def test_mutate_phase_plan_blocks_mocked_proof_when_contract_disallows_mocks(
    tmp_path,
) -> None:
    drive = _write_plan(
        tmp_path, ["python", "-m", "pytest", "tests/test_calculator_gui.py", "-q"]
    )
    result = _mutate_phase_plan(
        _ctx(drive),
        patch={
            "subtasks": [
                {
                    "id": "calculator-gui",
                    "proof": {
                        "execution": {
                            "kind": "command",
                            "command": [
                                "python",
                                "-c",
                                "from unittest.mock import Mock; app = build(Mock())",
                            ],
                            "timeout_sec": 60,
                            "shell": False,
                            "subdir": "",
                        },
                        "oracle": {
                            "oracle_type": "build",
                            "required_properties": ["build_succeeds"],
                        },
                    },
                }
            ]
        },
    )

    assert result.startswith("ERROR: cannot mutate phase plan")
    assert "allows_mock=false" in result


def test_mutate_phase_plan_allows_nonselective_pytest_flag(tmp_path) -> None:
    drive = _write_plan(
        tmp_path, ["python", "-m", "pytest", "tests/test_calculator_gui.py", "-q"]
    )
    result = _mutate_phase_plan(
        _ctx(drive),
        patch={
            "subtasks": [
                {
                    "id": "calculator-gui",
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": [
                                "python",
                                "-m",
                                "pytest",
                                "tests/test_calculator_gui.py",
                                "-q",
                                "--tb=short",
                            ],
                            "timeout_sec": 60,
                            "shell": False,
                            "subdir": "",
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["no_test_tampering"],
                        },
                    },
                }
            ]
        },
    )

    assert result.startswith("PhasePlan mutated")
