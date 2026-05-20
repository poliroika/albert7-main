"""Unit tests for ``ouroboros.preflight_recovery``.

These pin two specific production failure modes:

1. GLM-4.7 occasionally bakes pseudo-XML into ``fn_name`` itself, e.g.
   ``update_workspace_seed</arg_value>flag</arg_key><arg_value>false</arg_value>``.
   ``extract_pseudo_xml_args`` MUST recover both the clean tool name
   and any embedded args so the call can still be routed.

2. After repeated preflight errors on the same (tool, phase), the
   loop escalates the error message returned to the LLM by attaching
   real successful examples from the SAME run. ``recent_successful_args``
   reads ``tools.jsonl`` and returns the most recent N successful arg
   dicts; ``format_examples_for_prompt`` renders them compactly.
"""

import json
from pathlib import Path


from ouroboros.preflight_recovery import (
    PreflightErrorTracker,
    extract_pseudo_xml_args,
    format_examples_for_prompt,
    recent_successful_args,
)


class TestExtractPseudoXmlArgs:
    def test_clean_fn_name_returns_unchanged(self) -> None:
        name, args = extract_pseudo_xml_args("update_workspace_seed", '{"a": 1}')
        assert name == "update_workspace_seed"
        assert args == {}

    def test_xml_in_fn_name_is_stripped(self) -> None:
        polluted = "update_workspace_seed</arg_value>allow_large_overwrite</arg_key><arg_value>false</arg_value>"
        name, args = extract_pseudo_xml_args(polluted, "{}")
        assert name == "update_workspace_seed"
        assert args == {"allow_large_overwrite": False}

    def test_xml_in_args_is_recovered(self) -> None:
        # GLM sometimes places the entire XML mess inside the JSON args
        # field as a string.
        raw_args = (
            "<arg_key>workspace_id</arg_key><arg_value>news_cards_ai</arg_value>"
            "<arg_key>file_path</arg_key><arg_value>src/main.py</arg_value>"
        )
        name, args = extract_pseudo_xml_args("update_workspace_seed", raw_args)
        assert name == "update_workspace_seed"
        assert args == {"workspace_id": "news_cards_ai", "file_path": "src/main.py"}

    def test_tool_call_name_inside_args_overrides_clean_outer_name(self) -> None:
        raw_args = (
            "<tool_call>palace_add"
            "<arg_key>title</arg_key><arg_value>finding</arg_value>"
            "<arg_key>content</arg_key><arg_value>details</arg_value>"
        )

        name, args = extract_pseudo_xml_args("submit_research_summary", raw_args)

        assert name == "palace_add"
        assert args == {"title": "finding", "content": "details"}

    def test_xml_split_across_name_and_args(self) -> None:
        polluted_name = (
            "update_workspace_seed</arg_value>foo</arg_key><arg_value>1</arg_value>"
        )
        raw_args = "<arg_key>bar</arg_key><arg_value>true</arg_value>"
        name, args = extract_pseudo_xml_args(polluted_name, raw_args)
        assert name == "update_workspace_seed"
        assert args == {"foo": 1, "bar": True}

    def test_json_value_inside_arg_value_is_decoded(self) -> None:
        raw_args = '<arg_key>steps</arg_key><arg_value>[{"title":"x","success_check":"y"}]</arg_value>'
        name, args = extract_pseudo_xml_args("propose_task_plan", raw_args)
        assert name == "propose_task_plan"
        assert args == {"steps": [{"title": "x", "success_check": "y"}]}

    def test_paren_call_form_is_stripped(self) -> None:
        # Some models emit ``foo(bar=1)`` in fn_name. Strip the paren tail.
        name, args = extract_pseudo_xml_args("propose_task_plan(steps=[])", "{}")
        assert name == "propose_task_plan"
        assert args == {}

    def test_empty_inputs_safe(self) -> None:
        name, args = extract_pseudo_xml_args(None, None)
        assert name == ""
        assert args == {}

    def test_loop_recovers_xml_args_when_tool_name_is_already_clean(
        self, tmp_path: Path
    ) -> None:
        from ouroboros.loop import _recover_pseudo_xml_tool_name

        tc = {
            "function": {
                "name": "apply_workspace_patch",
                "arguments": (
                    "<arg_key>workspace_id</arg_key><arg_value>mini_game</arg_value>"
                    "<arg_key>patch</arg_key><arg_value>*** Begin Patch\n"
                    "*** Add File: README.md\n+hello\n*** End Patch</arg_value>"
                ),
            }
        }

        name = _recover_pseudo_xml_tool_name(
            tc,
            drive_logs=tmp_path,
            task_id="run-1:execute",
            phase_label="execute",
        )

        assert name == "apply_workspace_patch"
        assert json.loads(tc["function"]["arguments"]) == {
            "workspace_id": "mini_game",
            "patch": "*** Begin Patch\n*** Add File: README.md\n+hello\n*** End Patch",
        }

    def test_loop_rewrites_to_xml_tool_name_when_args_contain_tool_call(
        self, tmp_path: Path
    ) -> None:
        from ouroboros.loop import _recover_pseudo_xml_tool_name

        tc = {
            "function": {
                "name": "submit_research_summary",
                "arguments": (
                    "<tool_call>palace_add"
                    "<arg_key>title</arg_key><arg_value>finding</arg_value>"
                    "<arg_key>content</arg_key><arg_value>details</arg_value>"
                ),
            }
        }

        name = _recover_pseudo_xml_tool_name(
            tc,
            drive_logs=tmp_path,
            task_id="run-1:research",
            phase_label="research",
        )

        assert name == "palace_add"
        assert json.loads(tc["function"]["arguments"]) == {
            "title": "finding",
            "content": "details",
        }


class TestRecentSuccessfulArgs:
    def _write_log(self, drive_logs: Path, entries: list[dict]) -> None:
        path = drive_logs / "tools.jsonl"
        path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
            encoding="utf-8",
        )

    def test_missing_log_returns_empty(self, tmp_path: Path) -> None:
        assert recent_successful_args(tmp_path, "update_workspace_seed") == []

    def test_returns_most_recent_first(self, tmp_path: Path) -> None:
        self._write_log(
            tmp_path,
            [
                {
                    "ts": "1",
                    "tool": "update_workspace_seed",
                    "task_id": "t1",
                    "args": {
                        "workspace_id": "ws",
                        "file_path": "a.py",
                        "new_content": "old",
                    },
                    "result_preview": "ok",
                },
                {
                    "ts": "2",
                    "tool": "update_workspace_seed",
                    "task_id": "t1",
                    "args": {
                        "workspace_id": "ws",
                        "file_path": "b.py",
                        "new_content": "newer",
                    },
                    "result_preview": "ok",
                },
                {
                    "ts": "3",
                    "tool": "update_workspace_seed",
                    "task_id": "t1",
                    "args": {
                        "workspace_id": "ws",
                        "file_path": "c.py",
                        "new_content": "newest",
                    },
                    "result_preview": "ok",
                },
            ],
        )
        examples = recent_successful_args(tmp_path, "update_workspace_seed", n=2)
        assert len(examples) == 2
        assert examples[0]["file_path"] == "c.py"  # newest first
        assert examples[1]["file_path"] == "b.py"

    def test_filters_by_task_id(self, tmp_path: Path) -> None:
        self._write_log(
            tmp_path,
            [
                {
                    "ts": "1",
                    "tool": "update_workspace_seed",
                    "task_id": "OTHER",
                    "args": {
                        "workspace_id": "ws",
                        "file_path": "a.py",
                        "new_content": "x",
                    },
                    "result_preview": "ok",
                },
                {
                    "ts": "2",
                    "tool": "update_workspace_seed",
                    "task_id": "t1",
                    "args": {
                        "workspace_id": "ws",
                        "file_path": "b.py",
                        "new_content": "y",
                    },
                    "result_preview": "ok",
                },
            ],
        )
        examples = recent_successful_args(
            tmp_path, "update_workspace_seed", n=2, task_id="t1"
        )
        assert len(examples) == 1
        assert examples[0]["file_path"] == "b.py"

    def test_skips_error_results_and_empty_args(self, tmp_path: Path) -> None:
        self._write_log(
            tmp_path,
            [
                # error result preview — must be skipped
                {
                    "ts": "1",
                    "tool": "update_workspace_seed",
                    "task_id": "t1",
                    "args": {
                        "workspace_id": "ws",
                        "file_path": "a.py",
                        "new_content": "x",
                    },
                    "result_preview": "⚠️ TOOL_PREFLIGHT_ERROR ...",
                },
                # empty args — must be skipped
                {
                    "ts": "2",
                    "tool": "update_workspace_seed",
                    "task_id": "t1",
                    "args": {},
                    "result_preview": "ok",
                },
                # the only valid one
                {
                    "ts": "3",
                    "tool": "update_workspace_seed",
                    "task_id": "t1",
                    "args": {
                        "workspace_id": "ws",
                        "file_path": "good.py",
                        "new_content": "valid",
                    },
                    "result_preview": "ok",
                },
            ],
        )
        examples = recent_successful_args(
            tmp_path, "update_workspace_seed", n=5, task_id="t1"
        )
        assert len(examples) == 1
        assert examples[0]["file_path"] == "good.py"

    def test_filters_by_tool_name(self, tmp_path: Path) -> None:
        self._write_log(
            tmp_path,
            [
                {
                    "ts": "1",
                    "tool": "read_workspace_file",
                    "task_id": "t1",
                    "args": {"workspace_id": "ws", "file_path": "a.py"},
                    "result_preview": "ok",
                },
                {
                    "ts": "2",
                    "tool": "update_workspace_seed",
                    "task_id": "t1",
                    "args": {
                        "workspace_id": "ws",
                        "file_path": "b.py",
                        "new_content": "x",
                    },
                    "result_preview": "ok",
                },
            ],
        )
        examples = recent_successful_args(tmp_path, "update_workspace_seed", n=5)
        assert len(examples) == 1
        assert "new_content" in examples[0]


class TestFormatExamplesForPrompt:
    def test_empty_returns_empty(self) -> None:
        assert format_examples_for_prompt([]) == ""

    def test_renders_single_example(self) -> None:
        out = format_examples_for_prompt(
            [{"workspace_id": "ws", "file_path": "a.py"}],
        )
        assert out.startswith("Example #1 (this run):")
        assert '"workspace_id": "ws"' in out
        assert '"file_path": "a.py"' in out

    def test_truncates_long_string_fields(self) -> None:
        big = "x" * 5000
        out = format_examples_for_prompt(
            [{"file_path": "a.py", "new_content": big}],
            max_per_field_chars=50,
        )
        assert "[+4950 chars]" in out
        assert big not in out  # NOT inlined verbatim

    def test_renders_multiple_examples(self) -> None:
        out = format_examples_for_prompt(
            [{"a": 1}, {"b": 2}],
        )
        assert "Example #1" in out
        assert "Example #2" in out


class TestPreflightErrorTracker:
    def test_bump_increments_per_key(self) -> None:
        tracker = PreflightErrorTracker()
        assert tracker.bump("t1", "update_workspace_seed", "subtask_1") == 1
        assert tracker.bump("t1", "update_workspace_seed", "subtask_1") == 2
        # Different phase = different key
        assert tracker.bump("t1", "update_workspace_seed", "subtask_2") == 1

    def test_record_success_clears_task(self) -> None:
        tracker = PreflightErrorTracker()
        tracker.bump("t1", "update_workspace_seed", "subtask_1")
        tracker.bump("t1", "read_workspace_file", "subtask_1")
        tracker.bump("t2", "update_workspace_seed", "subtask_1")  # other task
        tracker.record_success("t1")
        assert tracker.current("t1", "update_workspace_seed", "subtask_1") == 0
        assert tracker.current("t1", "read_workspace_file", "subtask_1") == 0
        # other task is untouched
        assert tracker.current("t2", "update_workspace_seed", "subtask_1") == 1

    def test_reset_drops_specific_key(self) -> None:
        tracker = PreflightErrorTracker()
        tracker.bump("t1", "foo", "subtask_1")
        tracker.bump("t1", "bar", "subtask_1")
        tracker.reset("t1", "foo", "subtask_1")
        assert tracker.current("t1", "foo", "subtask_1") == 0
        assert tracker.current("t1", "bar", "subtask_1") == 1

    def test_no_task_id_does_not_crash(self) -> None:
        tracker = PreflightErrorTracker()
        # empty task_id allowed (falsy is filtered in record_success)
        assert tracker.bump("", "foo", "phase") == 1
        tracker.record_success("")  # no-op
        assert tracker.current("", "foo", "phase") == 1
