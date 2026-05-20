import os
import pathlib
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ouroboros.memory_hooks import (
    _extract_task_brief,
    _guess_initial_workspace,
    _short_args_repr,
)
from ouroboros.loop import (
    _RepeatedReadGuardState,
    _execute_single_tool,
    _format_llm_unavailable_message,
    _looks_like_pseudo_tool_call_text,
    _looks_like_tool_failure,
    _maybe_inject_no_write_tool_nudge,
    _maybe_inject_repeated_read_guard,
    _maybe_inject_self_check,
    _periodic_recall_enabled_for_phase,
    _reject_tool_calls_under_no_write_enforcement,
    _resolve_llm_loop_retries,
    _rewrite_forbidden_tool_call_if_safe,
    _select_forced_progress_tool,
    _should_abort_no_write_tool_churn,
    _subtask_allows_read_only_progress,
    _successful_terminating_tools,
    _summarize_recent_actions,
)


class TestLoopSelfCheck(unittest.TestCase):
    def test_self_check_reinforces_completion_contract(self):
        messages = []
        progress = []

        _maybe_inject_self_check(
            round_idx=50,
            max_rounds=200,
            messages=messages,
            accumulated_usage={},
            emit_progress=progress.append,
        )

        self.assertEqual(len(messages), 1)
        content = messages[0]["content"]
        self.assertIn("completion contract", content)
        self.assertIn("Only stop", content)
        self.assertNotIn("Should I just STOP", content)

    def test_self_check_renders_unlimited_rounds_safely(self):
        """When MAX_ROUNDS is 0 (unlimited mode), the checkpoint reminder must
        render as ``∞`` instead of a misleading negative ``Rounds remaining``.
        """
        messages = []

        _maybe_inject_self_check(
            round_idx=50,
            max_rounds=0,
            messages=messages,
            accumulated_usage={},
            emit_progress=lambda _msg: None,
        )

        self.assertEqual(len(messages), 1)
        content = messages[0]["content"]
        self.assertIn("round 50/∞", content)
        self.assertIn("Rounds remaining: ∞", content)
        self.assertNotIn("Rounds remaining: -", content)

    def test_self_check_does_not_suggest_unavailable_compaction_tool(self):
        messages = []

        _maybe_inject_self_check(
            round_idx=50,
            max_rounds=120,
            messages=messages,
            accumulated_usage={},
            emit_progress=lambda _msg: None,
            available_tool_names={"read_file", "apply_workspace_patch"},
        )

        self.assertEqual(len(messages), 1)
        content = messages[0]["content"]
        self.assertNotIn("call `compact_context`", content)
        self.assertIn("do not call unavailable context tools", content)

    def test_auth_error_message_does_not_suggest_mock_or_rephrasing(self):
        text = _format_llm_unavailable_message(
            "gemma-4",
            3,
            "AuthenticationError(\"Error code: 401 - {'error': 'Unauthorized'}\")",
        )

        self.assertIn("LLM authentication failed", text)
        self.assertIn("did not fall back to mocks", text)
        self.assertNotIn("rephrasing", text.lower())

    def test_llm_loop_retries_default_is_short_and_visible(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_resolve_llm_loop_retries(), 3)

    def test_llm_loop_retries_env_is_clamped(self):
        with patch.dict(os.environ, {"OUROBOROS_LLM_LOOP_RETRIES": "0"}):
            self.assertEqual(_resolve_llm_loop_retries(), 1)
        with patch.dict(os.environ, {"OUROBOROS_LLM_LOOP_RETRIES": "5"}):
            self.assertEqual(_resolve_llm_loop_retries(), 5)

    def test_tool_timeout_is_failed_tool_result(self):
        self.assertTrue(
            _looks_like_tool_failure(
                "⚠️ TOOL_TIMEOUT (run_workspace_command): exceeded 600s limit"
            )
        )

    def test_detects_xml_like_pseudo_tool_call_text(self):
        text = (
            "<tool_call>update_workspace_seed"
            "<arg_key>file_path</arg_key><arg_value>main.py</arg_value>"
            "</tool_call>"
        )
        self.assertTrue(_looks_like_pseudo_tool_call_text(text))

    def test_detects_function_style_pseudo_tool_call_text(self):
        text = 'update_workspace_seed({"workspace_id":"x","file_path":"main.py"})'
        self.assertTrue(_looks_like_pseudo_tool_call_text(text))

    def test_plain_status_text_is_not_pseudo_tool_call(self):
        text = "Готово: обновил план и дальше запускаю тесты."
        self.assertFalse(_looks_like_pseudo_tool_call_text(text))

    def test_periodic_recall_defaults_off_for_all_phases(self):
        """Periodic recall is opt-in; prompts/gates should make the agent call memory."""

        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_periodic_recall_enabled_for_phase("planner"))
            self.assertFalse(_periodic_recall_enabled_for_phase("subtask_1"))
            self.assertFalse(_periodic_recall_enabled_for_phase("subtask_42"))
            self.assertFalse(_periodic_recall_enabled_for_phase("remediation"))
            self.assertFalse(_periodic_recall_enabled_for_phase("review_1"))
            self.assertFalse(_periodic_recall_enabled_for_phase("final_aggregation"))
            self.assertFalse(_periodic_recall_enabled_for_phase("linear"))

    def test_periodic_recall_env_forces_on_for_all_phases(self):
        with patch.dict(os.environ, {"OUROBOROS_ENABLE_PERIODIC_RECALL": "1"}):
            self.assertTrue(_periodic_recall_enabled_for_phase("subtask_2"))
            self.assertTrue(_periodic_recall_enabled_for_phase("review_1"))
            self.assertTrue(_periodic_recall_enabled_for_phase("final_aggregation"))

    def test_periodic_recall_env_forces_off_for_all_phases(self):
        with patch.dict(os.environ, {"OUROBOROS_ENABLE_PERIODIC_RECALL": "0"}):
            self.assertFalse(_periodic_recall_enabled_for_phase("planner"))
            self.assertFalse(_periodic_recall_enabled_for_phase("subtask_2"))
            self.assertFalse(_periodic_recall_enabled_for_phase("remediation"))


class TestLoopMemoryHelpers(unittest.TestCase):
    """Pure-function helpers behind the memory_hooks integration in
    ``run_llm_loop``. Kept here so failures localise to the loop module.
    """

    def test_guess_initial_workspace_finds_explicit_token(self):
        msgs = [{"role": "user", "content": "Please work on workspace_id=JKX."}]
        self.assertEqual(_guess_initial_workspace(msgs), "JKX")

    def test_guess_initial_workspace_finds_path(self):
        msgs = [{"role": "system", "content": "cd workspaces/JKX/ and start."}]
        self.assertEqual(_guess_initial_workspace(msgs), "JKX")

    def test_guess_initial_workspace_prefers_launcher_label_over_example_path(self):
        msgs = [
            {
                "role": "system",
                "content": (
                    "Example: inspect workspaces/agent_research/tools.py first.\n"
                    "Workspace: `workspaces/news_cards_ai`"
                ),
            }
        ]
        self.assertEqual(_guess_initial_workspace(msgs), "news_cards_ai")

    def test_guess_initial_workspace_returns_empty_when_absent(self):
        msgs = [{"role": "user", "content": "just a generic message"}]
        self.assertEqual(_guess_initial_workspace(msgs), "")

    def test_guess_initial_workspace_handles_non_string_content(self):
        msgs = [{"role": "user", "content": None}, {"role": "system", "content": 42}]
        self.assertEqual(_guess_initial_workspace(msgs), "")

    def test_extract_task_brief_caps_length(self):
        long_msg = {"role": "user", "content": "x" * 5000}
        out = _extract_task_brief([long_msg])
        self.assertLessEqual(len(out), 2000)

    def test_extract_task_brief_prefers_user_over_system(self):
        msgs = [
            {"role": "system", "content": "# I Am Ouroboros\nconstitution"},
            {"role": "user", "content": "Build the news cards app"},
        ]
        self.assertEqual(_extract_task_brief(msgs), "Build the news cards app")

    def test_summarize_recent_actions_uses_last_three_notes(self):
        trace = {"assistant_notes": ["a1", "a2", "a3", "a4", "a5"]}
        out = _summarize_recent_actions(trace)
        self.assertEqual(out, "a3 | a4 | a5")

    def test_summarize_recent_actions_handles_empty_trace(self):
        self.assertEqual(_summarize_recent_actions({}), "")
        self.assertEqual(_summarize_recent_actions({"assistant_notes": []}), "")

    def test_short_args_repr_prefers_known_keys(self):
        out = _short_args_repr(
            {"workspace_id": "JKX", "file_path": "a/b.py", "garbage": "x"}
        )
        self.assertIn("workspace_id=JKX", out)
        self.assertIn("file_path=a/b.py", out)

    def test_short_args_repr_falls_back_to_json(self):
        out = _short_args_repr({"unknown_key": "value"})
        self.assertIn("unknown_key", out)


class TestRepeatedReadGuard(unittest.TestCase):
    def _tool_call(self, name: str, args: dict) -> dict:
        import json

        return {
            "function": {
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False),
            }
        }

    def test_repeated_workspace_read_injects_guard_message(self):
        state = _RepeatedReadGuardState()
        messages = []
        tool_calls = [
            self._tool_call(
                "run_workspace_command",
                {
                    "workspace_id": "JKX",
                    "argv": ["cmd", "/c", "type", "web_server.py"],
                },
            )
        ]

        for _ in range(4):
            _maybe_inject_repeated_read_guard(tool_calls, messages, state)

        self.assertEqual(len(messages), 1)
        self.assertIn("PROGRESS_GUARD", messages[0]["content"])
        self.assertIn("web_server.py", messages[0]["content"])

    def test_different_file_breaks_repeated_read_streak(self):
        state = _RepeatedReadGuardState()
        messages = []
        first = [
            self._tool_call(
                "read_workspace_file",
                {"workspace_id": "JKX", "file_path": "TASK_MAIN.md"},
            )
        ]
        second = [
            self._tool_call(
                "read_workspace_file",
                {"workspace_id": "JKX", "file_path": "web_server.py"},
            )
        ]

        for _ in range(3):
            _maybe_inject_repeated_read_guard(first, messages, state)
        _maybe_inject_repeated_read_guard(second, messages, state)
        _maybe_inject_repeated_read_guard(second, messages, state)

        self.assertEqual(messages, [])


class TestEnforcementToolRejection(unittest.TestCase):
    def test_rejects_when_forced_tool_missing(self):
        import json

        tc = {
            "id": "call_x",
            "function": {
                "name": "read_workspace_file",
                "arguments": json.dumps({"workspace_id": "w", "file_path": "a.md"}),
            },
        }
        messages: list = []
        llm_trace: dict = {"tool_calls": []}
        rejected = _reject_tool_calls_under_no_write_enforcement(
            forced_tool="update_workspace_seed",
            tool_calls=[tc],
            messages=messages,
            llm_trace=llm_trace,
            emit_progress=lambda _m: None,
            phase_label="subtask_1",
        )
        self.assertTrue(rejected)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "tool")
        self.assertEqual(messages[0]["tool_call_id"], "call_x")
        self.assertIn("TOOL_REJECTED_UNDER_ENFORCEMENT", messages[0]["content"])
        self.assertIn("update_workspace_seed", messages[0]["content"])

    def test_passes_through_when_forced_tool_present(self):
        import json

        read_tc = {
            "id": "call_r",
            "function": {
                "name": "read_workspace_file",
                "arguments": json.dumps({"workspace_id": "w", "file_path": "a.md"}),
            },
        }
        write_tc = {
            "id": "call_w",
            "function": {
                "name": "update_workspace_seed",
                "arguments": json.dumps(
                    {"workspace_id": "w", "file_path": "x.py", "new_content": "x=1\n"}
                ),
            },
        }
        messages: list = []
        llm_trace: dict = {"tool_calls": []}
        rejected = _reject_tool_calls_under_no_write_enforcement(
            forced_tool="update_workspace_seed",
            tool_calls=[read_tc, write_tc],
            messages=messages,
            llm_trace=llm_trace,
            emit_progress=lambda _m: None,
            phase_label="subtask_1",
        )
        self.assertFalse(rejected)
        self.assertEqual(messages, [])


class TestCompletionToolAcceptance(unittest.TestCase):
    def _tool_call(self, name: str) -> dict:
        return {"function": {"name": name, "arguments": "{}"}}

    def test_rejected_completion_tool_does_not_count_as_terminating(self):
        accepted, rejected = _successful_terminating_tools(
            tool_calls=[self._tool_call("mark_subtask_complete")],
            trace_tool_calls=[
                {
                    "tool": "mark_subtask_complete",
                    "result_full": "WARNING: no fresh verify evidence",
                    "is_error": False,
                }
            ],
            terminating_tools=frozenset({"mark_subtask_complete"}),
        )

        self.assertEqual(accepted, set())
        self.assertIn("mark_subtask_complete", rejected)

    def test_ok_completion_tool_counts_as_terminating(self):
        accepted, rejected = _successful_terminating_tools(
            tool_calls=[self._tool_call("mark_subtask_complete")],
            trace_tool_calls=[
                {
                    "tool": "mark_subtask_complete",
                    "result_full": "OK: subtask marked done",
                    "is_error": False,
                }
            ],
            terminating_tools=frozenset({"mark_subtask_complete"}),
        )

        self.assertEqual(accepted, {"mark_subtask_complete"})
        self.assertEqual(rejected, {})


class TestProgressToolSelection(unittest.TestCase):
    def test_forced_progress_prefers_write_tool_over_verify(self):
        self.assertEqual(
            _select_forced_progress_tool(
                frozenset({"apply_workspace_patch", "run_workspace_verify"})
            ),
            "apply_workspace_patch",
        )

    def test_forced_progress_falls_back_to_legacy_write_tool(self):
        self.assertEqual(
            _select_forced_progress_tool(
                frozenset({"update_workspace_seed", "run_workspace_verify"})
            ),
            "update_workspace_seed",
        )


class TestReadOnlySubtaskHeuristic(unittest.TestCase):
    def test_diagnostic_subtask_can_progress_without_write_nudge(self):
        subtask = SimpleNamespace(
            title="Diagnose failing verifier",
            description="Inspect logs and extract the failure cause.",
            success_check="evidence summarized",
            tags=[],
        )

        self.assertTrue(_subtask_allows_read_only_progress(subtask))


class TestNoWriteToolGuard(unittest.TestCase):
    def _tool_call(self, name: str) -> dict:
        import json

        return {"function": {"name": name, "arguments": json.dumps({})}}

    def test_delivery_phase_tool_churn_injects_guard_message(self):
        messages = []
        nudges, last_round = _maybe_inject_no_write_tool_nudge(
            require=True,
            phase_write_tool_calls=0,
            nudges_so_far=0,
            last_nudge_round=0,
            round_idx=12,
            rounds_in_phase=8,
            phase_label="linear",
            workspace_id="news_cards_ai",
            tool_calls=[self._tool_call("read_workspace_file")],
            messages=messages,
        )

        self.assertEqual(nudges, 1)
        self.assertEqual(last_round, 12)
        self.assertEqual(len(messages), 1)
        self.assertIn("NO_WRITE_TOOL_GUARD", messages[0]["content"])
        self.assertIn("update_workspace_seed", messages[0]["content"])

    def test_tool_churn_guard_stays_quiet_after_workspace_write(self):
        messages = []

        nudges, last_round = _maybe_inject_no_write_tool_nudge(
            require=True,
            phase_write_tool_calls=1,
            nudges_so_far=0,
            last_nudge_round=0,
            round_idx=12,
            rounds_in_phase=20,
            phase_label="linear",
            workspace_id="news_cards_ai",
            tool_calls=[self._tool_call("read_workspace_file")],
            messages=messages,
        )

        self.assertEqual((nudges, last_round), (0, 0))
        self.assertEqual(messages, [])

    def test_tool_churn_abort_waits_until_after_last_nudge_is_seen(self):
        tool_calls = [self._tool_call("update_scratchpad")]

        self.assertFalse(
            _should_abort_no_write_tool_churn(
                require=True,
                phase_write_tool_calls=0,
                nudges_so_far=2,
                nudge_injected_this_round=True,
                tool_calls=tool_calls,
            )
        )
        self.assertTrue(
            _should_abort_no_write_tool_churn(
                require=True,
                phase_write_tool_calls=0,
                nudges_so_far=2,
                nudge_injected_this_round=False,
                tool_calls=tool_calls,
            )
        )

    def test_tool_churn_abort_stays_quiet_after_workspace_write(self):
        self.assertFalse(
            _should_abort_no_write_tool_churn(
                require=True,
                phase_write_tool_calls=1,
                nudges_so_far=2,
                nudge_injected_this_round=False,
                tool_calls=[self._tool_call("update_scratchpad")],
            )
        )

    def test_tool_churn_guard_does_not_depend_on_verify_gate_resets(self):
        """Regression: run_workspace_verify used to reset VerifyGate edits to 0
        and mistakenly re-trigger no-write churn abort in the same phase.
        """
        messages = []
        nudges, last_round = _maybe_inject_no_write_tool_nudge(
            require=True,
            phase_write_tool_calls=1,
            nudges_so_far=1,
            last_nudge_round=8,
            round_idx=12,
            rounds_in_phase=12,
            phase_label="subtask_1",
            workspace_id="python_arcade_friend",
            tool_calls=[self._tool_call("run_workspace_verify")],
            messages=messages,
        )
        self.assertEqual((nudges, last_round), (1, 8))
        self.assertEqual(messages, [])


class TestForbiddenToolRewrite(unittest.TestCase):
    def test_review_phase_keeps_forbidden_call_unchanged(self):
        import json

        tc = {
            "id": "call_1",
            "function": {
                "name": "mark_subtask_complete",
                "arguments": json.dumps({"status": "done"}),
            },
        }
        rewritten, note = _rewrite_forbidden_tool_call_if_safe(
            tc,
            allowed_tool_names=frozenset(
                {"revise_remaining_plan", "read_workspace_file"}
            ),
            phase_label="review_2",
        )
        self.assertEqual(rewritten["function"]["name"], "mark_subtask_complete")
        self.assertIsNone(note)


class TestForbiddenToolDelegation(unittest.TestCase):
    class _DummyTools:
        CODE_TOOLS = frozenset()

        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        def available_tools(self):
            return ["schedule_task", "read_workspace_file"]

        def execute(self, name: str, args: dict):
            self.calls.append((name, args))
            if name == "schedule_task":
                return "Scheduled task deadbeef: delegated"
            return "ok"

    def test_subtask_forbidden_call_is_auto_delegated(self):
        import json

        tools = self._DummyTools()
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            tc = {
                "id": "call_1",
                "function": {
                    "name": "update_scratchpad",
                    "arguments": json.dumps({"content": "notes"}),
                },
            }
            out = _execute_single_tool(
                tools=tools,
                tc=tc,
                drive_logs=drive_logs,
                task_id="task1",
                allowed_tool_names=frozenset({"read_workspace_file"}),
                phase_label="subtask_1",
            )
        self.assertFalse(out["is_error"])
        self.assertIn("Auto-delegated via schedule_task", out["result"])
        self.assertTrue(any(name == "schedule_task" for name, _args in tools.calls))

    def test_planner_forbidden_call_stays_hard_error(self):
        import json

        tools = self._DummyTools()
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            tc = {
                "id": "call_2",
                "function": {
                    "name": "update_scratchpad",
                    "arguments": json.dumps({"content": "notes"}),
                },
            }
            out = _execute_single_tool(
                tools=tools,
                tc=tc,
                drive_logs=drive_logs,
                task_id="task2",
                allowed_tool_names=frozenset({"read_workspace_file"}),
                phase_label="planner",
            )
        self.assertTrue(out["is_error"])
        self.assertIn("TOOL_FORBIDDEN_IN_PHASE", out["result"])
        self.assertFalse(any(name == "schedule_task" for name, _args in tools.calls))

    def test_review_forbidden_call_stays_hard_error_without_delegation(self):
        import json

        tools = self._DummyTools()
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            tc = {
                "id": "call_3",
                "function": {
                    "name": "run_workspace_command",
                    "arguments": json.dumps(
                        {"workspace_id": "w1", "argv": ["pytest", "-q"]}
                    ),
                },
            }
            out = _execute_single_tool(
                tools=tools,
                tc=tc,
                drive_logs=drive_logs,
                task_id="task3",
                allowed_tool_names=frozenset({"revise_remaining_plan"}),
                phase_label="review_1",
            )
        self.assertTrue(out["is_error"])
        self.assertIn("TOOL_FORBIDDEN_IN_PHASE", out["result"])
        self.assertFalse(any(name == "schedule_task" for name, _args in tools.calls))

    def test_review_write_call_does_not_clear_remaining_plan(self):
        import json

        tools = self._DummyTools()
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            tc = {
                "id": "call_4",
                "function": {
                    "name": "update_workspace_from_instance",
                    "arguments": json.dumps(
                        {
                            "workspace_id": "w1",
                            "path": "app.py",
                            "content": "print('x')",
                        }
                    ),
                },
            }
            out = _execute_single_tool(
                tools=tools,
                tc=tc,
                drive_logs=drive_logs,
                task_id="task4",
                allowed_tool_names=frozenset({"revise_remaining_plan"}),
                phase_label="review_1",
            )
        self.assertTrue(out["is_error"])
        self.assertIn("TOOL_FORBIDDEN_IN_PHASE", out["result"])
        self.assertFalse(
            any(name == "revise_remaining_plan" for name, _args in tools.calls)
        )


class TestToolPreflight(unittest.TestCase):
    class _SchemaTools:
        CODE_TOOLS = frozenset()

        def available_tools(self):
            return ["run_workspace_command"]

        def get_schema_by_name(self, name: str):
            if name != "run_workspace_command":
                return None
            return {
                "type": "function",
                "function": {
                    "name": "run_workspace_command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "workspace_id": {"type": "string"},
                            "argv": {"type": "array"},
                        },
                        "required": ["workspace_id", "argv"],
                    },
                },
            }

        def execute(self, name: str, args: dict):
            return "ok"

    def test_preflight_rejects_missing_required_fields(self):
        import json

        tools = self._SchemaTools()
        with tempfile.TemporaryDirectory() as tmp:
            out = _execute_single_tool(
                tools=tools,
                tc={
                    "id": "call_pf_1",
                    "function": {
                        "name": "run_workspace_command",
                        "arguments": json.dumps({"workspace_id": "w1"}),
                    },
                },
                drive_logs=pathlib.Path(tmp),
                task_id="t_pf_1",
                allowed_tool_names=frozenset({"run_workspace_command"}),
                phase_label="subtask_1",
            )
        self.assertTrue(out["is_error"])
        self.assertIn("TOOL_PREFLIGHT_ERROR", out["result"])
        self.assertIn("missing required field", out["result"])

    def test_preflight_rejects_type_mismatch(self):
        import json

        tools = self._SchemaTools()
        with tempfile.TemporaryDirectory() as tmp:
            out = _execute_single_tool(
                tools=tools,
                tc={
                    "id": "call_pf_2",
                    "function": {
                        "name": "run_workspace_command",
                        "arguments": json.dumps(
                            {"workspace_id": 123, "argv": ["pytest", "-q"]}
                        ),
                    },
                },
                drive_logs=pathlib.Path(tmp),
                task_id="t_pf_2",
                allowed_tool_names=frozenset({"run_workspace_command"}),
                phase_label="subtask_1",
            )
        self.assertTrue(out["is_error"])
        self.assertIn("TOOL_PREFLIGHT_ERROR", out["result"])
        self.assertIn("expects string", out["result"])

    def test_preflight_infers_workspace_id_from_drive_root(self):
        import json

        tools = self._SchemaTools()
        with tempfile.TemporaryDirectory() as tmp:
            ws_drive = (
                pathlib.Path(tmp) / "workspaces" / "demo_ws" / ".memory" / "drive"
            )
            ws_drive.mkdir(parents=True, exist_ok=True)
            tools._ctx = SimpleNamespace(drive_root=ws_drive)
            out = _execute_single_tool(
                tools=tools,
                tc={
                    "id": "call_pf_3",
                    "function": {
                        "name": "run_workspace_command",
                        "arguments": json.dumps({"argv": ["pytest", "-q"]}),
                    },
                },
                drive_logs=pathlib.Path(tmp),
                task_id="t_pf_3",
                allowed_tool_names=frozenset({"run_workspace_command"}),
                phase_label="subtask_1",
            )
        self.assertFalse(out["is_error"])


if __name__ == "__main__":
    unittest.main()
