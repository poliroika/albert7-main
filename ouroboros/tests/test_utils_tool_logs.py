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


def test_sanitize_tool_args_still_depth_limits_deep_payloads() -> None:
    logged = sanitize_tool_args_for_log(
        "any_tool",
        {"a": {"b": {"c": {"d": {"e": {"f": "too deep"}}}}}},
    )

    assert logged["a"]["b"]["c"]["d"]["e"]["f"] == {"_depth_limit": True}
