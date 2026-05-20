"""Tests for ``ouroboros.tool_args_repair`` (P0-1).

The repair layer is the single point of resilience between strict
JSON parsers and the slightly-broken payloads that LLMs occasionally
emit for ``tool_calls[].function.arguments``.  Each branch of the
fallback ladder gets exercised here so regressions show up as failing
tests rather than as silent ``unrepairable`` warnings in production
logs.
"""

import json

import pytest

from ouroboros.tool_args_repair import repair_tool_arguments


class TestStrictPath:
    def test_well_formed_json_returns_ok(self) -> None:
        args, note = repair_tool_arguments("save_umbrella_memory", '{"a": 1, "b": "c"}')
        assert args == {"a": 1, "b": "c"}
        assert note == "ok"

    def test_already_dict_passthrough(self) -> None:
        args, note = repair_tool_arguments("save_umbrella_memory", {"a": 1})
        assert args == {"a": 1}
        assert note == "already_dict"

    def test_none_arguments_become_empty_dict(self) -> None:
        args, note = repair_tool_arguments("save_umbrella_memory", None)
        assert args == {}
        assert note == "empty"

    def test_whitespace_only_treated_as_empty(self) -> None:
        args, note = repair_tool_arguments("save_umbrella_memory", "   \n\t ")
        assert args == {}
        assert note == "empty"

    def test_top_level_array_is_unrepairable(self) -> None:
        args, note = repair_tool_arguments("save_umbrella_memory", "[1, 2, 3]")
        assert args == {}
        assert note.startswith("unrepairable")
        assert "list" in note


class TestPhasePlanRepair:
    def test_propose_phase_plan_content_alias_maps_to_plan(self) -> None:
        args, note = repair_tool_arguments(
            "propose_phase_plan",
            {
                "content": {
                    "subtasks": [
                        {
                            "id": "domain",
                            "success_test": "python -m pytest tests/test_domain.py -q",
                        }
                    ]
                },
                "summary": "planner draft",
            },
        )

        assert "content" not in args
        assert args["plan"]["subtasks"][0]["id"] == "domain"
        assert args["notes"] == "planner draft"
        assert "alias_content_to_plan" in note
        assert "alias_summary_to_notes" in note


class TestControlCharRepair:
    def test_raw_newline_inside_string_gets_escaped(self) -> None:
        # The raw newline inside the value is what trips strict json.loads.
        broken = '{"text": "line1\nline2"}'
        with pytest.raises(json.JSONDecodeError):
            json.loads(broken)
        args, note = repair_tool_arguments("save_umbrella_memory", broken)
        assert args == {"text": "line1\nline2"}
        assert note == "fixed_control_chars_in_json_strings"

    def test_raw_tab_and_carriage_return(self) -> None:
        broken = '{"x": "a\tb\rc"}'
        args, note = repair_tool_arguments("save_umbrella_memory", broken)
        assert args == {"x": "a\tb\rc"}
        assert note == "fixed_control_chars_in_json_strings"

    def test_escapes_outside_strings_left_alone(self) -> None:
        # Newlines between key/value tokens are valid JSON whitespace;
        # nothing should be rewritten and json.loads should succeed first.
        args, note = repair_tool_arguments("save_umbrella_memory", '{\n  "a": 1\n}')
        assert args == {"a": 1}
        assert note == "ok"


class TestTrailingCommaRepair:
    def test_trailing_comma_in_object(self) -> None:
        args, note = repair_tool_arguments("x", '{"a": 1, "b": 2,}')
        assert args == {"a": 1, "b": 2}
        assert note == "removed_trailing_commas"

    def test_trailing_comma_in_array(self) -> None:
        args, note = repair_tool_arguments("x", '{"items": [1, 2, 3,]}')
        assert args == {"items": [1, 2, 3]}
        assert note == "removed_trailing_commas"


class TestPythonLiteralFallback:
    def test_single_quoted_dict_recovered(self) -> None:
        args, note = repair_tool_arguments("x", "{'a': 1, 'b': 'c'}")
        assert args == {"a": 1, "b": "c"}
        assert note == "python_literal_fallback"

    def test_python_true_false_none(self) -> None:
        args, note = repair_tool_arguments(
            "x", "{'flag': True, 'missing': None, 'off': False}"
        )
        assert args == {"flag": True, "missing": None, "off": False}
        assert note == "python_literal_fallback"

    def test_garbage_falls_back_to_empty_with_reason(self) -> None:
        args, note = repair_tool_arguments("x", '{"a": 1, "b":')
        assert args == {}
        assert note.startswith("unrepairable: ")


class TestKeyCoercion:
    def test_int_keys_from_python_literal_become_strings(self) -> None:
        args, note = repair_tool_arguments("x", "{1: 'a', 2: 'b'}")
        assert args == {"1": "a", "2": "b"}
        assert note == "python_literal_fallback"


class TestRunWorkspaceCommandNormalization:
    def test_command_list_is_mapped_to_argv(self) -> None:
        args, note = repair_tool_arguments(
            "run_workspace_command",
            '{"workspace_id":"terminal_bench","command":["bash","-lc","cd /app && ls"]}',
        )
        assert args == {
            "workspace_id": "terminal_bench",
            "argv": ["bash", "-lc", "cd /app && ls"],
        }
        assert note == "mapped_command_list_to_argv"

    def test_command_string_is_split_into_argv(self) -> None:
        args, note = repair_tool_arguments(
            "run_workspace_command",
            '{"workspace_id":"terminal_bench","command":"bash -lc \\"cd /app && ls -la\\""}',
        )
        assert args == {
            "workspace_id": "terminal_bench",
            "argv": ["bash", "-lc", "cd /app && ls -la"],
        }
        assert note == "mapped_command_string_to_argv"

    def test_existing_argv_string_is_split(self) -> None:
        args, note = repair_tool_arguments(
            "run_workspace_command",
            {"workspace_id": "terminal_bench", "argv": "python -m pytest"},
        )
        assert args == {
            "workspace_id": "terminal_bench",
            "argv": ["python", "-m", "pytest"],
        }
        assert note == "split_argv_string"

    def test_legacy_args_list_is_mapped_to_argv(self) -> None:
        args, note = repair_tool_arguments(
            "run_workspace_command",
            {"workspace_id": "terminal_bench", "args": ["python", "-m", "pytest"]},
        )
        assert args == {
            "workspace_id": "terminal_bench",
            "argv": ["python", "-m", "pytest"],
        }
        assert note == "mapped_args_list_to_argv"

    def test_python_c_payload_quotes_are_unwrapped(self) -> None:
        args, note = repair_tool_arguments(
            "run_workspace_command",
            {
                "workspace_id": "terminal_bench",
                "argv": ["python", "-c", "\"print('hello')\""],
            },
        )
        assert args == {
            "workspace_id": "terminal_bench",
            "argv": ["python", "-c", "print('hello')"],
        }
        assert note == "unwrapped_interpreter_payload_quotes"

    def test_well_formed_argv_list_emits_no_repair_note(self) -> None:
        """A perfectly fine argv list of strings must not look 'repaired'."""
        args, note = repair_tool_arguments(
            "run_workspace_command",
            '{"workspace_id":"news_cards_ai","argv":["python","-m","pytest","-q"]}',
        )
        assert args == {
            "workspace_id": "news_cards_ai",
            "argv": ["python", "-m", "pytest", "-q"],
        }
        assert note == "ok"

    def test_dict_argv_list_emits_no_repair_note(self) -> None:
        """An already-dict payload with a healthy argv list stays 'already_dict'."""
        args, note = repair_tool_arguments(
            "run_workspace_command",
            {
                "workspace_id": "news_cards_ai",
                "argv": ["uv", "run", "python", "src/app/main.py"],
            },
        )
        assert args == {
            "workspace_id": "news_cards_ai",
            "argv": ["uv", "run", "python", "src/app/main.py"],
        }
        assert note == "already_dict"

    def test_argv_list_with_non_string_elements_is_normalized(self) -> None:
        """Numeric / non-string entries in argv still trigger normalization."""
        args, note = repair_tool_arguments(
            "run_workspace_command",
            {"workspace_id": "x", "argv": ["python", "-c", 42]},
        )
        assert args == {"workspace_id": "x", "argv": ["python", "-c", "42"]}
        assert note == "normalized_argv_list"

    def test_powershell_command_payload_quotes_are_unwrapped(self) -> None:
        args, note = repair_tool_arguments(
            "run_workspace_command",
            {
                "workspace_id": "terminal_bench",
                "argv": ["powershell", "-Command", '"Get-ChildItem *.docx"'],
            },
        )
        assert args == {
            "workspace_id": "terminal_bench",
            "argv": ["powershell", "-Command", "Get-ChildItem *.docx"],
        }
        assert note == "unwrapped_interpreter_payload_quotes"


class TestArgumentsEnvelopeUnwrap:
    """DeepSeek-V*-Flash & some Qwen variants wrap the real call args
    inside ``{"arguments": "<inner json string>"}``. The repair layer
    must strip that envelope before downstream tool dispatch."""

    def test_single_envelope_stringified_inner_is_unwrapped(self) -> None:
        envelope = json.dumps({"arguments": json.dumps({"a": 1, "b": "c"})})
        args, note = repair_tool_arguments("save_umbrella_memory", envelope)
        assert args == {"a": 1, "b": "c"}
        assert note.endswith("unwrapped_arguments_envelope")

    def test_single_envelope_dict_inner_is_unwrapped(self) -> None:
        args, note = repair_tool_arguments(
            "save_umbrella_memory",
            {"arguments": {"a": 1, "b": "c"}},
        )
        assert args == {"a": 1, "b": "c"}
        assert note.endswith("unwrapped_arguments_envelope")

    def test_double_wrapped_envelope_is_unwrapped(self) -> None:
        inner = json.dumps({"a": 1, "b": "c"})
        outer = json.dumps({"arguments": inner})
        envelope = json.dumps({"arguments": outer})
        args, note = repair_tool_arguments("save_umbrella_memory", envelope)
        assert args == {"a": 1, "b": "c"}
        assert note.endswith("unwrapped_arguments_envelope")

    def test_envelope_composes_with_run_workspace_command_normalization(self) -> None:
        inner = json.dumps(
            {
                "workspace_id": "x",
                "argv": ["powershell", "-Command", '"Get-ChildItem *.docx"'],
            }
        )
        envelope = json.dumps({"arguments": inner})
        args, note = repair_tool_arguments("run_workspace_command", envelope)
        assert args == {
            "workspace_id": "x",
            "argv": ["powershell", "-Command", "Get-ChildItem *.docx"],
        }
        assert "unwrapped_arguments_envelope" in note
        assert "unwrapped_interpreter_payload_quotes" in note

    def test_arguments_key_with_non_dict_non_string_value_is_left_alone(self) -> None:
        args, note = repair_tool_arguments(
            "save_umbrella_memory",
            {"arguments": 42},
        )
        assert args == {"arguments": 42}
        assert note == "already_dict"


class TestUpdateWorkspaceSeedNormalization:
    def test_unwraps_new_content_nested_object(self) -> None:
        args, note = repair_tool_arguments(
            "update_workspace_seed",
            {
                "workspace_id": "python_ui_space_runner",
                "file_path": "README.md",
                "new_content": {"new_content": "# Title\n"},
            },
        )
        assert args == {
            "workspace_id": "python_ui_space_runner",
            "file_path": "README.md",
            "new_content": "# Title\n",
        }
        assert "unwrapped_new_content_dict" in note

    def test_maps_common_aliases(self) -> None:
        # Aliases are now CONSUMED (not duplicated) so the canonical keys
        # match the tool function signature exactly. Without the move the
        # ``lambda ctx, **kw: update_workspace_seed(ctx, **kw)`` dispatch
        # would TypeError on the unknown ``workspace=``/``path=``/
        # ``content=`` keyword arguments.
        args, note = repair_tool_arguments(
            "update_workspace_seed",
            {
                "workspace": "python_ui_space_runner",
                "path": "notes.txt",
                "content": "hello",
            },
        )
        assert args == {
            "workspace_id": "python_ui_space_runner",
            "file_path": "notes.txt",
            "new_content": "hello",
        }
        assert "mapped_workspace_to_workspace_id" in note
        assert "mapped_path_to_file_path" in note
        assert "mapped_content_to_new_content" in note

    def test_maps_more_filename_and_content_aliases(self) -> None:
        args, note = repair_tool_arguments(
            "update_workspace_seed",
            {
                "ws": "py_ui",
                "filename": "src/main.py",
                "code": "print('hi')",
            },
        )
        assert args == {
            "workspace_id": "py_ui",
            "file_path": "src/main.py",
            "new_content": "print('hi')",
        }
        assert "mapped_filename_to_file_path" in note
        assert "mapped_code_to_new_content" in note
        assert "mapped_ws_to_workspace_id" in note

    def test_unwraps_edit_envelope(self) -> None:
        args, note = repair_tool_arguments(
            "update_workspace_seed",
            {
                "workspace_id": "py_ui",
                "edit": {"file_path": "src/foo.py", "content": "x = 1\n"},
            },
        )
        assert args["workspace_id"] == "py_ui"
        assert args["file_path"] == "src/foo.py"
        assert args["new_content"] == "x = 1\n"
        assert "unwrapped_edit_envelope_path" in note
        assert "unwrapped_edit_envelope_content" in note

    def test_joins_new_content_list(self) -> None:
        args, note = repair_tool_arguments(
            "update_workspace_seed",
            {
                "workspace_id": "py_ui",
                "file_path": "a.txt",
                "new_content": ["line1", "line2", "line3"],
            },
        )
        assert args["new_content"] == "line1\nline2\nline3"
        assert "joined_new_content_list" in note


class TestReadWorkspaceFileNormalization:
    def test_alias_path_to_file_path(self) -> None:
        args, note = repair_tool_arguments(
            "read_workspace_file",
            {"workspace_id": "py_ui", "path": "src/foo.py"},
        )
        assert args == {"workspace_id": "py_ui", "file_path": "src/foo.py"}
        assert "mapped_path_to_file_path" in note

    def test_alias_filename_to_file_path(self) -> None:
        args, note = repair_tool_arguments(
            "read_workspace_file",
            {"ws": "py_ui", "filename": "README.md"},
        )
        assert args == {"workspace_id": "py_ui", "file_path": "README.md"}
        assert "mapped_filename_to_file_path" in note
        assert "mapped_ws_to_workspace_id" in note

    def test_canonical_passthrough(self) -> None:
        args, note = repair_tool_arguments(
            "read_workspace_file",
            {"workspace_id": "py_ui", "file_path": "x.txt", "max_chars": 100},
        )
        assert args == {"workspace_id": "py_ui", "file_path": "x.txt", "max_chars": 100}
        assert note == "already_dict"


class TestListWorkspaceFilesNormalization:
    def test_alias_dir_to_subdir(self) -> None:
        args, note = repair_tool_arguments(
            "list_workspace_files",
            {"workspace": "py_ui", "dir": "src"},
        )
        assert args == {"workspace_id": "py_ui", "subdir": "src"}
        assert "mapped_workspace_to_workspace_id" in note
        assert "mapped_dir_to_subdir" in note


class TestProposeTaskPlanNormalization:
    """LLMs frequently call ``propose_task_plan`` with the wrong arg name
    (``subtasks``/``plan``/``tasks``/``items``) or wrap a single subtask
    as a dict. The normalizer collapses all of those to ``steps=[…]``
    so the strict preflight does not reject the call."""

    def test_alias_subtasks_to_steps(self) -> None:
        args, note = repair_tool_arguments(
            "propose_task_plan",
            {
                "subtasks": [
                    {"title": "A", "description": "do a", "success_check": "exists"},
                ]
            },
        )
        assert "steps" in args
        assert isinstance(args["steps"], list) and args["steps"]
        assert args["steps"][0]["title"] == "A"
        assert "alias_subtasks_to_steps" in note

    def test_alias_plan_to_steps(self) -> None:
        args, note = repair_tool_arguments(
            "propose_task_plan",
            '{"plan": [{"title": "A", "description": "do a"}]}',
        )
        assert args["steps"][0]["title"] == "A"
        assert "alias_plan_to_steps" in note

    def test_single_subtask_dict_wrapped_as_list(self) -> None:
        args, note = repair_tool_arguments(
            "propose_task_plan",
            {"steps": {"title": "A", "description": "do a", "success_check": "x"}},
        )
        assert isinstance(args["steps"], list) and len(args["steps"]) == 1
        assert args["steps"][0]["title"] == "A"
        assert "wrapped_single_step_dict" in note

    def test_outer_args_look_like_single_subtask(self) -> None:
        args, note = repair_tool_arguments(
            "propose_task_plan",
            {"title": "A", "description": "do a"},
        )
        assert "steps" in args and len(args["steps"]) == 1
        assert args["steps"][0]["title"] == "A"
        assert "wrapped_outer_args_as_single_step" in note

    def test_bare_string_titles_get_wrapped(self) -> None:
        args, _note = repair_tool_arguments(
            "propose_task_plan",
            {"steps": ["Implement", "Test"]},
        )
        assert [s["title"] for s in args["steps"]] == ["Implement", "Test"]
        for s in args["steps"]:
            assert s["description"] == s["title"]

    def test_per_step_alias_keys_get_normalized(self) -> None:
        args, note = repair_tool_arguments(
            "propose_task_plan",
            {
                "steps": [
                    {"name": "A", "details": "do a", "acceptance_criteria": "x"},
                ]
            },
        )
        s = args["steps"][0]
        assert s["title"] == "A"
        assert s["description"] == "do a"
        assert s["success_check"] == "x"
        assert "step_alias_name_to_title" in note
        assert "step_alias_details_to_description" in note
        assert "step_alias_acceptance_criteria_to_success_check" in note

    def test_arguments_envelope_then_alias(self) -> None:
        envelope = json.dumps(
            {
                "arguments": json.dumps(
                    {"subtasks": [{"title": "A", "description": "x"}]}
                )
            }
        )
        args, note = repair_tool_arguments("propose_task_plan", envelope)
        assert args["steps"][0]["title"] == "A"
        assert "unwrapped_arguments_envelope" in note
        assert "alias_subtasks_to_steps" in note


class TestReviseRemainingPlanNormalization:
    def test_canonical_steps_passthrough(self) -> None:
        # Canonical schema must round-trip without mutation so existing
        # planner orchestration tests are not broken.
        args, note = repair_tool_arguments(
            "revise_remaining_plan",
            {"steps": [{"title": "A", "description": "x"}], "reason": "fix"},
        )
        assert args == {
            "steps": [{"title": "A", "description": "x"}],
            "reason": "fix",
        }
        assert note == "already_dict"

    def test_alias_subtasks_to_steps(self) -> None:
        args, note = repair_tool_arguments(
            "revise_remaining_plan",
            {"subtasks": [{"title": "A", "description": "x"}], "reason": "fix"},
        )
        assert args["steps"][0]["title"] == "A"
        assert args["reason"] == "fix"
        assert "alias_subtasks_to_steps" in note

    def test_alias_tail_with_reason_alias_why(self) -> None:
        args, note = repair_tool_arguments(
            "revise_remaining_plan",
            {"tail": [{"title": "A", "description": "x"}], "why": "scope shrunk"},
        )
        assert args["steps"][0]["title"] == "A"
        assert args["reason"] == "scope shrunk"
        assert "alias_tail_to_steps" in note
        assert "alias_why_to_reason" in note

    def test_legacy_replacement_alias_still_normalized(self) -> None:
        # Older models that learned the function-arg name from older
        # docstrings sometimes emit ``replacement_steps_for_remaining``.
        # We collapse it back to the schema's ``steps`` key.
        args, note = repair_tool_arguments(
            "revise_remaining_plan",
            {
                "replacement_steps_for_remaining": [{"title": "A", "description": "x"}],
                "reason": "fix",
            },
        )
        assert args["steps"][0]["title"] == "A"
        assert "alias_replacement_steps_for_remaining_to_steps" in note
