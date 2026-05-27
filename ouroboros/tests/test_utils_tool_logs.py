import json

from ouroboros.utils import sanitize_tool_args_for_log


def test_sanitize_tool_args_preserves_phase_mutation_file_lists() -> None:
    logged = sanitize_tool_args_for_log(
        "mutate_phase_plan",
        {
            "patch": {
                "subtasks": [
                    {
                        "id": "implement_game_state",
                        "contract_migration_reason": (
                            "Generated test expectation was internally wrong."
                        ),
                        "contract_migration_files": ["tests/test_game_state.py"],
                    }
                ]
            }
        },
    )

    assert logged["patch"]["subtasks"][0]["contract_migration_files"] == [
        "tests/test_game_state.py"
    ]
    assert "_depth_limit" not in json.dumps(logged)


def test_sanitize_tool_args_preserves_phase_plan_nested_proof_contract() -> None:
    logged = sanitize_tool_args_for_log(
        "propose_phase_plan",
        {
            "plan": {
                "subtasks": [
                    {
                        "id": "gui-proof",
                        "title": "GUI proof",
                        "goal": "Prove display behavior through headless adapter.",
                        "files_to_create": ["src/calculator/app.py", "tests/test_app.py"],
                        "proof": {
                            "harness_profile": "desktop_gui_headless",
                            "execution": {
                                "kind": "pytest",
                                "command": [
                                    "python",
                                    "-m",
                                    "pytest",
                                    "tests/test_app.py",
                                    "-q",
                                ],
                                "shell": False,
                            },
                            "oracle": {
                                "oracle_type": "unit_assertions",
                                "required_properties": [
                                    "button_callbacks_update_display",
                                    "no_test_tampering",
                                ],
                            },
                            "scope": {
                                "files_under_test": ["src/calculator/app.py"],
                                "changed_files_expected": [
                                    "src/calculator/app.py",
                                    "tests/test_app.py",
                                ],
                                "pytest_targets": ["tests/test_app.py"],
                            },
                            "memory_scope": {
                                "assets": ["8577d303-c6da-42f7-9f42-171beb36bd9e"],
                                "phase_id": "plan",
                            },
                            "allowed_skills": ["architecture-author"],
                            "required_capabilities": ["python", "subprocess"],
                        },
                    }
                ]
            }
        },
    )

    proof = logged["plan"]["subtasks"][0]["proof"]

    assert proof["execution"]["kind"] == "pytest"
    assert proof["execution"]["command"][-1] == "-q"
    assert "no_test_tampering" in proof["oracle"]["required_properties"]
    assert proof["scope"]["pytest_targets"] == ["tests/test_app.py"]
    assert proof["memory_scope"]["assets"] == ["8577d303-c6da-42f7-9f42-171beb36bd9e"]
    assert "_depth_limit" not in json.dumps(logged)


def test_sanitize_tool_args_still_depth_limits_deep_payloads() -> None:
    logged = sanitize_tool_args_for_log(
        "any_tool",
        {"a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}},
    )

    assert logged["a"]["b"]["c"]["d"]["e"]["f"] == {"_depth_limit": True}
