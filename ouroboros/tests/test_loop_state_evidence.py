"""Unit tests for ``_update_state_from_tool_calls`` and related state.

These cover the Tier 1.3 / 2.4 / 3.1 evidence-binding logic that the
completion gates depend on. They run without an LLM — we synthesise
``tool_calls`` and trace entries the same way the loop produces them.
"""

import json


from ouroboros.loop import (
    DISCOVERY_TOOL_NAMES,
    _CompletionToolImpasseState,
    _LoopState,
    _maybe_trip_completion_impasse,
    _update_state_from_tool_calls,
)


def _trace_entry(name: str, result):
    return {"tool": name, "args": {}, "result": result}


def test_verify_state_prefers_full_result_over_truncated_trace_preview():
    state = _LoopState(round_idx=21)
    payload = {
        "passed": True,
        "pass_rate": 1.0,
        "skipped": False,
        "results": [],
        "summary": "PASS",
        "verify_run_id": "verify-full-json",
        "failed_step_count": 0,
    }
    state.llm_trace = {
        "assistant_notes": [],
        "tool_calls": [
            {
                "tool": "run_workspace_verify",
                "args": {},
                "result": '{"passed": true, "results": [ ... truncated',
                "result_full": json.dumps(payload),
            }
        ],
    }

    _update_state_from_tool_calls(
        state, [{"function": {"name": "run_workspace_verify"}}]
    )

    assert state.last_verify_run_id == "verify-full-json"
    assert state.last_verify_passed is True


def test_completion_impasse_writes_artifact_after_repeated_control_error(tmp_path):
    class Tools:
        class Ctx:
            active_plan_id = "plan123"

        _ctx = Ctx()

    guard = _CompletionToolImpasseState()
    tool_calls = [
        {
            "function": {
                "name": "mark_subtask_complete",
                "arguments": json.dumps({"status": "done", "evidence": ["x"]}),
            }
        }
    ]
    trace = [
        {
            "tool": "mark_subtask_complete",
            "result": json.dumps(
                {"status": "control_plane_error", "reason": "active_plan_missing"}
            ),
            "is_error": True,
        }
    ]

    for _ in range(2):
        assert not _maybe_trip_completion_impasse(
            state=guard,
            tool_calls=tool_calls,
            trace_tool_calls=trace,
            terminating_tools=frozenset({"mark_subtask_complete"}),
            phase_label="remediation_1_subtask_1",
            task_id="task123",
            drive_root=tmp_path,
            tools=Tools(),
        )
    message = _maybe_trip_completion_impasse(
        state=guard,
        tool_calls=tool_calls,
        trace_tool_calls=trace,
        terminating_tools=frozenset({"mark_subtask_complete"}),
        phase_label="remediation_1_subtask_1",
        task_id="task123",
        drive_root=tmp_path,
        tools=Tools(),
    )

    assert "phase_impasse" in message
    payload = json.loads(
        (tmp_path / "state" / "phase_impasse.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "phase_impasse"
    assert payload["plan_id"] == "plan123"
    assert payload["tool"] == "mark_subtask_complete"


def test_invalid_recovery_contract_is_recorded_without_immediate_phase_impasse(tmp_path):
    class Tools:
        class Ctx:
            active_plan_id = "plan123"

        _ctx = Ctx()

    guard = _CompletionToolImpasseState()
    tool_calls = [
        {
            "function": {
                "name": "submit_phase_plan",
                "arguments": json.dumps({"plan_id": "p1"}),
            }
        }
    ]
    trace = [
        {
            "tool": "submit_phase_plan",
            "result": json.dumps(
                {
                    "status": "invalid_recovery_contract",
                    "reason": "invalid_contract_path",
                }
            ),
            "is_error": True,
        }
    ]

    message = _maybe_trip_completion_impasse(
        state=guard,
        tool_calls=tool_calls,
        trace_tool_calls=trace,
        terminating_tools=frozenset({"submit_phase_plan"}),
        phase_label="plan",
        task_id="task123",
        drive_root=tmp_path,
        tools=Tools(),
    )

    assert message == ""
    payload = json.loads(
        (tmp_path / "state" / "invalid_recovery_contract.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["status"] == "invalid_recovery_contract"
    assert not (tmp_path / "state" / "phase_impasse.json").exists()

    for _ in range(2):
        message = _maybe_trip_completion_impasse(
            state=guard,
            tool_calls=tool_calls,
            trace_tool_calls=trace,
            terminating_tools=frozenset({"submit_phase_plan"}),
            phase_label="plan",
            task_id="task123",
            drive_root=tmp_path,
            tools=Tools(),
        )

    assert "phase_impasse" in message


def test_write_tool_updates_last_write_round():
    state = _LoopState(round_idx=7)
    state.llm_trace = {"assistant_notes": [], "tool_calls": []}
    tool_calls = [{"function": {"name": "update_workspace_seed"}}]

    _update_state_from_tool_calls(state, tool_calls)

    assert state.last_write_round == 7


def test_discovery_calls_increment_per_subtask_and_planner():
    state = _LoopState(round_idx=3)
    state.llm_trace = {"assistant_notes": [], "tool_calls": []}
    tool_calls = [
        {"function": {"name": "deep_search"}},
        {"function": {"name": "get_umbrella_memory"}},
    ]

    _update_state_from_tool_calls(state, tool_calls)

    assert state.current_subtask_discovery_calls == 2
    assert state.planner_discovery_calls == 2
    # Non-discovery tool must not bump the counter.
    _update_state_from_tool_calls(
        state, [{"function": {"name": "list_workspace_files"}}]
    )
    assert state.current_subtask_discovery_calls == 2


def test_external_discovery_counts_are_bucketed_by_tool():
    state = _LoopState(round_idx=3)
    state.llm_trace = {"assistant_notes": [], "tool_calls": []}

    _update_state_from_tool_calls(
        state,
        [
            {"function": {"name": "deep_search"}},
            {"function": {"name": "github_project_search"}},
            {"function": {"name": "mcp_discover"}},
        ],
    )

    assert state.discovery_calls_by_tool["deep_search"] == 1
    assert state.discovery_calls_by_tool["github_project_search"] == 1
    assert state.discovery_calls_by_tool["mcp_discover"] == 1


def test_discovery_tool_set_is_explicit_about_navigation_vs_research():
    # Navigation tools are intentionally not credited as discovery —
    # otherwise the agent could loop on read_workspace_file and skip
    # external research entirely.
    assert "read_workspace_file" not in DISCOVERY_TOOL_NAMES
    assert "list_workspace_files" not in DISCOVERY_TOOL_NAMES
    # External-research tools must all be present so the gate works
    # regardless of which one the agent picks.
    for name in (
        "deep_search",
        "github_project_search",
        "github_extract_snippets",
        "mcp_discover",
        "web_fetch",
        "get_umbrella_memory",
    ):
        assert name in DISCOVERY_TOOL_NAMES, name


def test_verify_outcome_bound_to_state_when_run_workspace_verify_returns_payload():
    state = _LoopState(round_idx=12)
    payload = {
        "passed": True,
        "pass_rate": 1.0,
        "skipped": False,
        "results": [
            {
                "name": "core",
                "kind": "file_exists",
                "status": "passed",
                "optional": False,
            },
        ],
        "summary": "Verification: **PASS** (1/1 required steps passed)",
    }
    state.llm_trace = {
        "assistant_notes": [],
        "tool_calls": [_trace_entry("run_workspace_verify", json.dumps(payload))],
    }
    tool_calls = [{"function": {"name": "run_workspace_verify"}}]

    _update_state_from_tool_calls(state, tool_calls)

    assert state.last_verify_passed is True
    assert state.last_verify_failed_count == 0
    assert state.last_verify_round == 12
    assert state.last_verify_run_id == "round-12"
    assert "PASS" in state.last_verify_summary


def test_verify_outcome_counts_failed_required_steps_only():
    state = _LoopState(round_idx=4)
    payload = {
        "passed": False,
        "pass_rate": 0.5,
        "skipped": False,
        "results": [
            {"name": "ok", "kind": "shell", "status": "passed", "optional": False},
            {"name": "bad", "kind": "shell", "status": "failed", "optional": False},
            # Optional failure must NOT count — it would be too eager
            # otherwise and block completion on truly optional gates.
            {"name": "soft", "kind": "shell", "status": "failed", "optional": True},
            {"name": "err", "kind": "shell", "status": "error", "optional": False},
        ],
        "summary": "Verification: **FAIL** (1/3 required steps passed)",
    }
    state.llm_trace = {
        "assistant_notes": [],
        "tool_calls": [_trace_entry("run_workspace_verify", json.dumps(payload))],
    }

    _update_state_from_tool_calls(
        state, [{"function": {"name": "run_workspace_verify"}}]
    )

    assert state.last_verify_passed is False
    assert state.last_verify_failed_count == 2  # bad + err, not soft


def test_skipped_verify_does_not_count_as_passing_evidence():
    state = _LoopState(round_idx=2)
    payload = {"passed": False, "skipped": True, "reason": "no steps", "results": []}
    state.llm_trace = {
        "assistant_notes": [],
        "tool_calls": [_trace_entry("run_workspace_verify", json.dumps(payload))],
    }

    _update_state_from_tool_calls(
        state, [{"function": {"name": "run_workspace_verify"}}]
    )

    assert state.last_verify_passed is False
    assert state.last_verify_run_id == ""  # skipped runs do not mint an id
    assert state.last_verify_round == 2  # but we still record that it ran


def test_verify_run_id_from_payload_is_preferred_over_round_derived():
    state = _LoopState(round_idx=15)
    payload = {
        "passed": True,
        "pass_rate": 1.0,
        "skipped": False,
        "results": [
            {
                "name": "core",
                "kind": "file_exists",
                "status": "passed",
                "optional": False,
            }
        ],
        "summary": "PASS",
        "verify_run_id": "verify-ws_demo-1715420400000",
        "failed_step_count": 0,
    }
    state.llm_trace = {
        "assistant_notes": [],
        "tool_calls": [_trace_entry("run_workspace_verify", json.dumps(payload))],
    }

    _update_state_from_tool_calls(
        state, [{"function": {"name": "run_workspace_verify"}}]
    )

    assert state.last_verify_run_id == "verify-ws_demo-1715420400000"
    assert state.last_verify_failed_count == 0


def test_run_real_e2e_updates_dedicated_e2e_state():
    state = _LoopState(round_idx=18)
    payload = {
        "passed": True,
        "pass_rate": 1.0,
        "skipped": False,
        "results": [
            {
                "name": "http_boot:app",
                "kind": "http_boot",
                "status": "passed",
                "optional": False,
            }
        ],
        "summary": "E2E PASS",
        "verify_run_id": "verify-e2e-1",
        "failed_step_count": 0,
    }
    state.llm_trace = {
        "assistant_notes": [],
        "tool_calls": [_trace_entry("run_real_e2e", json.dumps(payload))],
    }

    _update_state_from_tool_calls(
        state,
        [{"function": {"name": "run_real_e2e"}}],
        phase_label="final_review",
    )

    assert state.last_verify_passed is True
    assert state.last_e2e_passed is True
    assert state.last_e2e_run_id == "verify-e2e-1"
    assert state.last_e2e_phase_label == "final_review"


def test_explicit_failed_step_count_is_honoured():
    state = _LoopState(round_idx=4)
    payload = {
        "passed": False,
        "pass_rate": 0.0,
        "skipped": False,
        "results": [],
        "summary": "FAIL",
        "failed_step_count": 3,
    }
    state.llm_trace = {
        "assistant_notes": [],
        "tool_calls": [_trace_entry("run_workspace_verify", json.dumps(payload))],
    }

    _update_state_from_tool_calls(
        state, [{"function": {"name": "run_workspace_verify"}}]
    )

    assert state.last_verify_failed_count == 3


def test_warning_result_text_does_not_corrupt_verify_state():
    state = _LoopState(round_idx=9)
    state.last_verify_run_id = "round-5"
    state.last_verify_passed = True
    state.llm_trace = {
        "assistant_notes": [],
        "tool_calls": [_trace_entry("run_workspace_verify", "⚠️ verify error: boom")],
    }

    _update_state_from_tool_calls(
        state, [{"function": {"name": "run_workspace_verify"}}]
    )

    # Previous good state must be preserved when the latest call errored.
    assert state.last_verify_run_id == "round-5"
    assert state.last_verify_passed is True
