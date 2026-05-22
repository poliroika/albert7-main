from types import SimpleNamespace

from umbrella.deep_agent_tools.phase_control_actions import (
    _apply_phase_plan_subtask_patch,
    _merge_phase_plan_string_list,
    _phase_plan_string_items,
)


def test_phase_plan_string_items_flattens_nested_lists() -> None:
    assert _phase_plan_string_items(["a", ["b", "c"]]) == ["a", "b", "c"]


def test_merge_phase_plan_string_list_dedupes() -> None:
    merged = _merge_phase_plan_string_list(["src/a.py"], ["src/b.py", "src/a.py"])
    assert merged == ["src/a.py", "src/b.py"]


def test_apply_phase_plan_subtask_patch_replace_files_to_create() -> None:
    plan = {
        "nodes": [
            {
                "id": "execute",
                "subtasks": [
                    {
                        "id": "scaffold",
                        "files_to_create": ["backend/src/app.py"],
                        "files_to_change": [],
                        "files_affected": [],
                    }
                ],
            }
        ]
    }
    ctx = SimpleNamespace()
    applied, issue = _apply_phase_plan_subtask_patch(
        ctx,
        plan,
        [
            {
                "id": "scaffold",
                "replace_files_to_create": [
                    "src/civilization/backend/app.py",
                    "tests/test_app.py",
                ],
            }
        ],
    )
    assert issue is None
    assert applied == ["subtasks.scaffold"]
    subtask = plan["nodes"][0]["subtasks"][0]
    assert subtask["files_to_create"] == [
        "src/civilization/backend/app.py",
        "tests/test_app.py",
    ]


def test_apply_phase_plan_subtask_patch_remove_files_to_change() -> None:
    plan = {
        "nodes": [
            {
                "id": "execute",
                "subtasks": [
                    {
                        "id": "scaffold",
                        "files_to_create": [],
                        "files_to_change": ["backend/src/app.py", "README.md"],
                        "files_affected": [],
                    }
                ],
            }
        ]
    }
    ctx = SimpleNamespace()
    applied, issue = _apply_phase_plan_subtask_patch(
        ctx,
        plan,
        [{"id": "scaffold", "remove_files_to_change": ["backend/src/app.py"]}],
    )
    assert issue is None
    assert applied
    assert plan["nodes"][0]["subtasks"][0]["files_to_change"] == ["README.md"]
