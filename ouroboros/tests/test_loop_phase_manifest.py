"""
Tests that loop.py respects tool_filter from task context_overlays (phase_manifest).
"""
import queue
import pathlib
import json
import pytest


def _make_minimal_task(tool_filter=None, overlays=None):
    task = {
        "id": "test-task-1",
        "type": "phase_run",
        "input": "test input",
        "workspace_id": "test_ws",
    }
    if tool_filter:
        task["tool_filter"] = tool_filter
    if overlays:
        task["context_overlays"] = overlays
    return task


def _write_basic_capability_declaration(state: pathlib.Path) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "capabilities": {
                    "python": {"available": True, "source": "probe"},
                    "subprocess": {"available": True, "source": "probe"},
                },
                "probe_audit": {"python": True, "subprocess": True},
                "notes": "Python and subprocess are available for this phase-plan fixture.",
            }
        ),
        encoding="utf-8",
    )


def _pytest_proof(command: list[str], *, target: str = "tests") -> dict:
    return {
        "execution": {
            "kind": "pytest",
            "command": command,
            "shell": False,
        },
        "oracle": {
            "oracle_type": "unit_assertions",
            "required_properties": ["build_succeeds"],
        },
        "scope": {
            "pytest_targets": [target],
        },
        "anti_gaming": {"requires_real_runtime": True},
        "required_capabilities": ["python", "subprocess"],
    }


def test_tool_filter_allow_restricts_schemas(tmp_path):
    """When tool_filter.allow is set, only those tools appear in tool_schemas."""
    import sys, os
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import run_llm_loop
    import inspect
    sig = inspect.signature(run_llm_loop)
    assert "tool_filter" in sig.parameters, "run_llm_loop must accept tool_filter parameter"


def test_tool_filter_deny_excludes_tools():
    """Verify tool_filter.deny logic."""
    all_schemas = [
        {"type": "function", "function": {"name": "shell"}},
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "apply_workspace_patch"}},
    ]
    denied = {"apply_workspace_patch", "shell"}
    filtered = [s for s in all_schemas if s.get("function", {}).get("name") not in denied]
    assert len(filtered) == 1
    assert filtered[0]["function"]["name"] == "read_file"


def test_tool_filter_allow_logic():
    """Verify tool_filter.allow logic."""
    all_schemas = [
        {"type": "function", "function": {"name": "shell"}},
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "palace_search"}},
    ]
    allowed = {"read_file", "palace_search"}
    filtered = [s for s in all_schemas if s.get("function", {}).get("name") in allowed]
    assert len(filtered) == 2
    names = {s["function"]["name"] for s in filtered}
    assert "shell" not in names


def test_phase_control_tools_registered():
    """phase_control.py exports get_tools() with expected tools."""
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import get_tools
    tools = get_tools()
    names = {t.name for t in tools}
    required = {
        "mutate_phase_plan", "add_phase", "loop_back_to",
        "submit_research_summary", "submit_micro_review", "submit_verification",
        "harness_run", "submit_preflight_report",
    }
    missing = required - names
    assert not missing, f"Missing phase control tools: {missing}"


def test_phase_manifest_tools_are_registered_in_tool_registry(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.registry import ToolRegistry

    registry = ToolRegistry(tmp_path, tmp_path / "drive", tmp_path)
    names = set(getattr(registry, "_entries").keys())
    for expected in {
        "env_check",
        "palace_search",
        "list_files",
        "read_file",
        "shell",
        "terminal_session",
        "run_unit_tests",
        "run_real_e2e",
        "promote_to_durable",
    }:
        assert expected in names


def test_phase_contract_workspace_scope_prefers_active_workspace(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _workspace_id
    from ouroboros.tools.registry import ToolContext

    drive = tmp_path / "workspaces" / "current_ws" / ".memory" / "drive"
    drive.mkdir(parents=True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=drive)
    ctx.loop_state_view = {"active_workspace_id": "current_ws"}

    assert _workspace_id(ctx, "stale_ws") == "current_ws"

    ctx.loop_state_view = {}
    assert _workspace_id(ctx, "fallback_ws") == "current_ws"

    plain_ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path / "drive")
    assert _workspace_id(plain_ctx, "fallback_ws") == "fallback_ws"


def test_run_real_e2e_guard_fails_web_goal_without_http_proof(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _apply_real_e2e_adequacy_guard

    workspace = tmp_path / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    payload = {
        "passed": True,
        "pass_rate": 1.0,
        "failed_step_count": 0,
        "summary": "Verification: PASS",
        "results": [
            {
                "name": "import app",
                "kind": "import_check",
                "status": "passed",
                "optional": False,
            }
        ],
    }

    guarded = _apply_real_e2e_adequacy_guard(
        payload,
        workspace_id="demo",
        workspace_root=workspace,
        goal_text="подними через localhost чтобы потестить web ui",
    )

    assert guarded["passed"] is False
    assert guarded["real_e2e_guard"]["reason"] == "missing_localhost_e2e_evidence"
    assert any(r["name"] == "e2e_guard:localhost_ui" for r in guarded["results"])


def test_run_real_e2e_guard_accepts_http_boot_proof(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _apply_real_e2e_adequacy_guard

    workspace = tmp_path / "workspaces" / "demo"
    workspace.mkdir(parents=True)
    payload = {
        "passed": True,
        "pass_rate": 1.0,
        "failed_step_count": 0,
        "summary": "Verification: PASS",
        "results": [
            {
                "name": "http_boot:app",
                "kind": "http_boot",
                "status": "passed",
                "optional": False,
            }
        ],
    }

    guarded = _apply_real_e2e_adequacy_guard(
        payload,
        workspace_id="demo",
        workspace_root=workspace,
        goal_text="localhost web ui",
    )

    assert guarded["passed"] is True
    assert guarded["real_e2e_guard"]["passed"] is True


def test_run_real_e2e_blocks_after_run_cancel(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools import phase_contract
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-cancel:final_review"
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "stop_requested.json").write_text(
        json.dumps({"run_id": "run-cancel"}),
        encoding="utf-8",
    )

    def fail_verify(*args, **kwargs):
        raise AssertionError("run_workspace_verify should not start after stop")

    monkeypatch.setattr(
        phase_contract.umbrella_tools,
        "run_workspace_verify",
        fail_verify,
    )

    result = phase_contract._run_real_e2e(ctx, workspace_id="mini_game")

    assert "stop_requested" in result


def test_phase_contract_write_tools_block_after_run_cancel(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools import phase_contract
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-cancel:plan"
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "stop_requested.json").write_text(
        json.dumps({"run_id": "run-cancel"}),
        encoding="utf-8",
    )

    def fail_save(*args, **kwargs):
        raise AssertionError("phase write tools should not write after stop")

    monkeypatch.setattr(
        phase_contract.umbrella_tools,
        "save_umbrella_memory",
        fail_save,
    )

    result = phase_contract._propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "build",
                    "success_test": "python -m pytest tests -q",
                }
            ]
        },
    )
    assert "stop_requested" in result
    assert not (state / "phase_plan_proposal_latest.json").exists()

    result = phase_contract._palace_add(ctx, title="note", content="content")
    assert "stop_requested" in result


def test_propose_subtasks_does_not_replace_authoritative_phase_plan(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools import phase_contract
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-plan:plan"
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "capabilities": {
                    "python": {"available": True, "source": "probe"},
                    "subprocess": {"available": True, "source": "probe"},
                },
                "probe_audit": {"python": True, "subprocess": True},
                "notes": "Python and subprocess are available for this phase-plan fixture.",
            }
        ),
        encoding="utf-8",
    )

    phase_result = phase_contract._propose_phase_plan(
        ctx,
        plan={
            "plan_id": "authoritative",
            "subtasks": [
                {
                    "id": "build",
                    "title": "Build",
                    "goal": "Implement the build leaf and prove it with pytest.",
                    "files_to_create": ["src/demo/__init__.py"],
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": ["python", "-m", "pytest", "tests", "-q"],
                            "shell": False,
                        },
                        "oracle": {
                            "oracle_type": "unit_assertions",
                            "required_properties": ["build_succeeds"],
                        },
                        "scope": {
                            "files_under_test": ["src/demo/__init__.py"],
                            "changed_files_expected": ["src/demo/__init__.py"],
                            "pytest_targets": ["tests"],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                        "required_capabilities": ["python", "subprocess"],
                    },
                }
            ],
        },
    )
    assert phase_result.startswith("OK:")
    latest_phase = json.loads(
        (tmp_path / "state" / "phase_plan_proposal_latest.json").read_text(
            encoding="utf-8"
        )
    )

    subtask_result = phase_contract._propose_subtasks(
        ctx,
        steps=[
            {
                "id": "verify",
                "title": "Verify",
                "goal": "Run the API pytest proof.",
                "files_to_create": ["src/demo/api.py"],
                "proof": {
                    "execution": {
                        "kind": "pytest",
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_api.py",
                            "-q",
                        ],
                        "shell": False,
                    },
                    "oracle": {
                        "oracle_type": "unit_assertions",
                        "required_properties": ["build_succeeds"],
                    },
                    "scope": {
                        "files_under_test": ["src/demo/api.py"],
                        "changed_files_expected": ["src/demo/api.py"],
                        "pytest_targets": ["tests/test_api.py"],
                    },
                    "anti_gaming": {"requires_real_runtime": True},
                    "required_capabilities": ["python", "subprocess"],
                },
            }
        ],
    )
    assert subtask_result.startswith("OK:")
    latest_subtasks = tmp_path / "state" / "subtask_proposal_latest.json"
    assert latest_subtasks.exists()
    assert json.loads(
        (tmp_path / "state" / "phase_plan_proposal_latest.json").read_text(
            encoding="utf-8"
        )
    ) == latest_phase


def test_propose_subtasks_requires_executable_success_tests(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools import phase_contract
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-plan:plan"

    result = phase_contract._propose_subtasks(
        ctx,
        steps=[
            {
                "id": "verify",
                "success_criteria": "tests pass and UI looks good",
            }
        ],
    )

    assert result.startswith("ERROR: subtask proposal contract rejected")
    assert "missing_proof" in result
    assert not (tmp_path / "state" / "subtask_proposal_latest.json").exists()


def test_phase_filter_preloads_non_core_allowed_tools(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import (
        _apply_tool_filter_in_place,
        _phase_tool_filter_sets,
        _preload_phase_tool_schemas,
        _setup_dynamic_tools,
    )
    from ouroboros.tools.registry import ToolRegistry

    registry = ToolRegistry(tmp_path, tmp_path / "drive", tmp_path)
    schemas = registry.schemas(core_only=True)
    allowed, denied = _phase_tool_filter_sets(
        "phase_run",
        {
            "allow": ["submit_preflight_report", "env_check"],
            "deny": ["run_shell"],
            "required": ["submit_preflight_report"],
        },
    )
    _preload_phase_tool_schemas(
        registry,
        schemas,
        allowed=allowed,
        denied=denied,
        drive_logs=tmp_path,
        task_id="t1",
    )
    messages = []
    _setup_dynamic_tools(
        registry,
        schemas,
        messages,
        phase_allowed_tools=allowed,
        phase_denied_tools=denied,
    )
    _apply_tool_filter_in_place(schemas, allowed=allowed, denied=denied)
    names = {s["function"]["name"] for s in schemas}
    assert "submit_preflight_report" in names
    assert "env_check" in names
    assert "run_shell" not in names
    assert "propose_task_plan" not in names


def test_phase_tool_discovery_lists_only_phase_enableable_tools(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _setup_dynamic_tools
    from ouroboros.tools.registry import ToolRegistry

    registry = ToolRegistry(tmp_path, tmp_path / "drive", tmp_path)
    schemas = registry.schemas(core_only=True)
    messages = []

    _setup_dynamic_tools(
        registry,
        schemas,
        messages,
        phase_allowed_tools={"list_available_tools", "enable_tools", "palace_search"},
        phase_denied_tools=set(),
    )

    listed = registry.execute("list_available_tools", {})
    assert "palace_search" in listed
    assert "update_workspace_seed" not in listed

    denied = registry.execute("enable_tools", {"tools": "update_workspace_seed"})
    assert denied.startswith("ERROR:")
    assert "Not allowed in this phase" in denied


def test_dynamic_tool_note_hidden_when_enable_tools_not_allowed(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import (
        _apply_tool_filter_in_place,
        _phase_tool_filter_sets,
        _preload_phase_tool_schemas,
        _setup_dynamic_tools,
    )
    from ouroboros.tools.registry import ToolRegistry

    registry = ToolRegistry(tmp_path, tmp_path / "drive", tmp_path)
    schemas = registry.schemas(core_only=True)
    allowed, denied = _phase_tool_filter_sets(
        "phase_run",
        {"allow": ["read_file", "submit_micro_review"]},
    )
    _preload_phase_tool_schemas(
        registry,
        schemas,
        allowed=allowed,
        denied=denied,
        drive_logs=tmp_path,
        task_id="t-no-enable",
    )
    messages = []
    _setup_dynamic_tools(
        registry,
        schemas,
        messages,
        phase_allowed_tools=allowed,
        phase_denied_tools=denied,
    )
    _apply_tool_filter_in_place(schemas, allowed=allowed, denied=denied)

    assert not any("enable_tools" in str(msg.get("content") or "") for msg in messages)


def test_dynamic_tool_note_excludes_preloaded_allowed_tools(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import (
        _phase_tool_filter_sets,
        _preload_phase_tool_schemas,
        _setup_dynamic_tools,
    )
    from ouroboros.tools.registry import ToolRegistry

    registry = ToolRegistry(tmp_path, tmp_path / "drive", tmp_path)
    schemas = registry.schemas(core_only=True)
    allowed, denied = _phase_tool_filter_sets(
        "phase_run",
        {"allow": ["list_available_tools", "enable_tools", "palace_search"]},
    )
    _preload_phase_tool_schemas(
        registry,
        schemas,
        allowed=allowed,
        denied=denied,
        drive_logs=tmp_path,
        task_id="t-preloaded",
    )
    messages = []
    _setup_dynamic_tools(
        registry,
        schemas,
        messages,
        phase_allowed_tools=allowed,
        phase_denied_tools=denied,
    )

    assert messages == []
    assert "palace_search" not in registry.execute("list_available_tools", {})


def test_required_phase_completion_nudge_forces_submit_tool():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _maybe_force_required_phase_completion

    messages = []
    forced = _maybe_force_required_phase_completion(
        terminating_tools=frozenset({"submit_research_summary"}),
        tool_calls=[{"function": {"name": "list_available_tools"}}],
        messages=messages,
        rounds_in_phase=4,
        forced_progress_tool_choice=None,
    )

    assert forced == "submit_research_summary"
    assert messages
    assert "REQUIRED_PHASE_COMPLETION_PENDING" in messages[-1]["content"]


def test_required_phase_completion_nudge_waits_for_palace_prerequisite():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _maybe_force_required_phase_completion

    messages = []
    forced = _maybe_force_required_phase_completion(
        terminating_tools=frozenset({"submit_research_summary"}),
        tool_calls=[{"function": {"name": "mcp_discover"}}],
        messages=messages,
        rounds_in_phase=5,
        forced_progress_tool_choice=None,
        trace_tool_calls=[
            {
                "tool": "palace_add",
                "args": {"tags": "research_finding"},
                "result": json.dumps(
                    {
                        "saved": True,
                        "id": "finding-1",
                        "store": "palace.run",
                    }
                ),
            },
            {
                "tool": "palace_add",
                "args": {"tags": "research_finding"},
                "result": json.dumps(
                    {
                        "saved": True,
                        "id": "finding-2",
                        "store": "palace.run",
                    }
                ),
            },
        ],
        completion_prerequisites=(
            {
                "store": "palace.run",
                "tag": "research_finding",
                "n": 3,
                "tools": ["palace_add"],
            },
        ),
    )

    assert forced == "palace_add"
    assert messages
    assert "REQUIRED_MEMORY_WRITES_PENDING" in messages[-1]["content"]
    assert "submit_research_summary" in messages[-1]["content"]
    assert "palace_add" in messages[-1]["content"]
    assert "2/3" in messages[-1]["content"]
    assert "Do not call `palace_add` with `{}`" in messages[-1]["content"]
    assert "non-empty `title` or `content`" in messages[-1]["content"]


def test_required_phase_completion_nudge_forces_submit_after_palace_prerequisite():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _maybe_force_required_phase_completion

    messages = []
    forced = _maybe_force_required_phase_completion(
        terminating_tools=frozenset({"submit_research_summary"}),
        tool_calls=[{"function": {"name": "mcp_discover"}}],
        messages=messages,
        rounds_in_phase=5,
        forced_progress_tool_choice=None,
        trace_tool_calls=[
            {
                "tool": "palace_add",
                "args": {"tags": "research_finding"},
                "result": json.dumps(
                    {
                        "saved": True,
                        "id": f"finding-{idx}",
                        "store": "palace.run",
                    }
                ),
            }
            for idx in range(3)
        ],
        completion_prerequisites=(
            {
                "store": "palace.run",
                "tag": "research_finding",
                "n": 3,
                "tools": ["palace_add"],
            },
        ),
    )

    assert forced == "submit_research_summary"
    assert messages
    assert "REQUIRED_PHASE_COMPLETION_PENDING" in messages[-1]["content"]


def test_rejected_completion_nudge_explains_inline_capability_probe():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _format_rejected_termination_nudge

    message = _format_rejected_termination_nudge(
        {
            "submit_capability_declaration": (
                "ERROR: capability_declaration rejected: capability "
                "`desktop_gui_runtime` is marked unavailable because it still "
                "needs verification. Run a failed same-slug probe "
                "(capabilities.desktop_gui_runtime.probe or "
                "probes.desktop_gui_runtime) before declaring it unavailable."
            )
        }
    )

    assert "Recovery hint" in message
    assert "probes.desktop_gui_runtime" in message
    assert "not a separate shell step" in message


def test_required_phase_completion_nudge_waits_for_prior_tool_call():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _maybe_force_required_phase_completion

    messages = []
    forced = _maybe_force_required_phase_completion(
        terminating_tools=frozenset({"submit_preflight_report"}),
        tool_calls=[
            {"function": {"name": "env_check"}},
            {"function": {"name": "palace_health"}},
            {"function": {"name": "submit_preflight_report"}},
        ],
        messages=messages,
        rounds_in_phase=5,
        forced_progress_tool_choice=None,
        trace_tool_calls=[
            {"tool": "env_check", "result": '{"status":"ok"}'},
            {"tool": "palace_health", "result": '{"status":"ok"}'},
        ],
        completion_prerequisites=(
            {"kind": "tool_call", "tool": "env_check", "n": 1, "tools": ["env_check"]},
            {
                "kind": "tool_call",
                "tool": "read_workspace_charter",
                "n": 1,
                "tools": ["read_workspace_charter"],
            },
        ),
    )

    assert forced == "read_workspace_charter"
    assert messages
    assert "REQUIRED_TOOL_CALLS_PENDING" in messages[-1]["content"]
    assert "submit_preflight_report" in messages[-1]["content"]
    assert "read_workspace_charter" in messages[-1]["content"]


def test_required_phase_completion_nudge_ignores_failed_prior_tool_call():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _maybe_force_required_phase_completion

    messages = []
    forced = _maybe_force_required_phase_completion(
        terminating_tools=frozenset({"submit_preflight_report"}),
        tool_calls=[{"function": {"name": "skill_audit"}}],
        messages=messages,
        rounds_in_phase=5,
        forced_progress_tool_choice=None,
        trace_tool_calls=[
            {"tool": "env_check", "result": '{"status":"ok"}'},
            {"tool": "mcp_health", "result": '{"status":"error"}'},
            {"tool": "skill_audit", "result": '{"status":"ok"}'},
        ],
        completion_prerequisites=(
            {"kind": "tool_call", "tool": "env_check", "n": 1, "tools": ["env_check"]},
            {"kind": "tool_call", "tool": "mcp_health", "n": 1, "tools": ["mcp_health"]},
            {"kind": "tool_call", "tool": "skill_audit", "n": 1, "tools": ["skill_audit"]},
        ),
    )

    assert forced == "mcp_health"
    assert "REQUIRED_TOOL_CALLS_PENDING" in messages[-1]["content"]
    assert "`mcp_health` accepted 0/1" in messages[-1]["content"]


def test_accepted_completion_tool_waits_for_prior_tool_calls(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _LoopState, _handle_phase_tail_after_tool_round

    state = _LoopState(round_idx=5)
    state.llm_trace["tool_calls"] = [
        {"tool": "env_check", "result": '{"status":"ok"}'},
        {
            "tool": "submit_preflight_report",
            "result": "OK: Preflight report: blocked (blockers: 1)",
        },
    ]
    messages = []

    result = _handle_phase_tail_after_tool_round(
        state=state,
        tool_calls=[{"function": {"name": "submit_preflight_report"}}],
        terminating_tools=frozenset({"submit_preflight_report"}),
        messages=messages,
        phase_label="linear",
        budget_remaining_usd=None,
        llm=None,
        max_retries=0,
        drive_logs=tmp_path,
        task_id="phase_web_baf6b5c1:preflight",
        event_queue=None,
        task_type="phase_run",
        drive_root=tmp_path,
        rounds_in_phase=5,
        completion_prerequisites=(
            {"kind": "tool_call", "tool": "env_check", "n": 1, "tools": ["env_check"]},
            {
                "kind": "tool_call",
                "tool": "read_workspace_charter",
                "n": 1,
                "tools": ["read_workspace_charter"],
            },
        ),
    )

    assert result == ("continue", "continue")
    assert "REQUIRED_TOOL_CALLS_PENDING" in messages[-1]["content"]
    assert "read_workspace_charter" in messages[-1]["content"]


def test_failed_verify_counts_as_executed_evidence_not_passed_evidence():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _trace_entry_satisfies_tool_prerequisite

    item = {
        "tool": "run_workspace_verify",
        "result": json.dumps(
            {
                "passed": False,
                "failed_step_count": 2,
                "verify_run_id": "verify-failed",
                "verification_report_ref": {"report_id": "report-failed"},
            }
        ),
    }

    assert _trace_entry_satisfies_tool_prerequisite(
        item,
        tool_name="run_workspace_verify",
        accept="executed_evidence",
    )
    assert not _trace_entry_satisfies_tool_prerequisite(
        item,
        tool_name="run_workspace_verify",
        accept="passed_evidence",
    )

    malformed = {
        "tool": "run_workspace_verify",
        "result": "ERROR: tool exploded before producing a report",
    }
    assert not _trace_entry_satisfies_tool_prerequisite(
        malformed,
        tool_name="run_workspace_verify",
        accept="executed_evidence",
    )

    blocked = {
        "tool": "run_workspace_verify",
        "result": json.dumps({"status": "blocked", "blocker": "env unavailable"}),
    }
    assert not _trace_entry_satisfies_tool_prerequisite(
        blocked,
        tool_name="run_workspace_verify",
        accept="executed_evidence",
    )
    assert _trace_entry_satisfies_tool_prerequisite(
        blocked,
        tool_name="run_workspace_verify",
        accept="blocker_evidence",
    )


def test_final_review_loop_back_accepts_failed_verify_evidence(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _LoopState, _handle_phase_tail_after_tool_round

    state = _LoopState(round_idx=5)
    state.llm_trace["tool_calls"] = [
        {
            "tool": "run_workspace_verify",
            "result": json.dumps(
                {
                    "passed": False,
                    "failed_step_count": 2,
                    "verify_run_id": "verify-failed",
                    "verification_report_ref": {"report_id": "report-failed"},
                    "results": [{"name": "pytest:tests", "status": "failed"}],
                }
            ),
        },
        {
            "tool": "submit_final_review",
            "result": "OK: Final review submitted: loop_back (signal: sig-1)",
        },
    ]
    messages = []

    result = _handle_phase_tail_after_tool_round(
        state=state,
        tool_calls=[
            {
                "function": {
                    "name": "submit_final_review",
                    "arguments": json.dumps({"outcome": "loop_back"}),
                }
            }
        ],
        terminating_tools=frozenset({"submit_final_review"}),
        messages=messages,
        phase_label="final_review",
        budget_remaining_usd=None,
        llm=None,
        max_retries=0,
        drive_logs=tmp_path,
        task_id="phase_web_1:final_review",
        event_queue=None,
        task_type="phase_run",
        drive_root=tmp_path,
        rounds_in_phase=5,
        completion_prerequisites=(
            {
                "kind": "completion_contract",
                "tool": "submit_final_review",
                "outcomes": {
                    "loop_back": {
                        "requires": [
                            {
                                "tool": "run_workspace_verify",
                                "accept": "executed_evidence",
                            }
                        ]
                    }
                },
            },
        ),
    )

    assert result is not None
    assert result[1] == "terminated"
    assert "REQUIRED_TOOL_CALLS_PENDING" not in "\n".join(
        str(msg.get("content") or "") for msg in messages
    )


def test_final_review_ok_rejects_failed_verify_evidence(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _LoopState, _handle_phase_tail_after_tool_round

    state = _LoopState(round_idx=5)
    state.llm_trace["tool_calls"] = [
        {
            "tool": "run_workspace_verify",
            "result": json.dumps(
                {
                    "passed": False,
                    "failed_step_count": 1,
                    "verify_run_id": "verify-failed",
                    "verification_report_ref": {"report_id": "report-failed"},
                }
            ),
        },
        {
            "tool": "submit_final_review",
            "result": "OK: Final review submitted: ok (signal: sig-1)",
        },
    ]
    messages = []

    result = _handle_phase_tail_after_tool_round(
        state=state,
        tool_calls=[
            {
                "function": {
                    "name": "submit_final_review",
                    "arguments": json.dumps({"outcome": "ok"}),
                }
            }
        ],
        terminating_tools=frozenset({"submit_final_review"}),
        messages=messages,
        phase_label="final_review",
        budget_remaining_usd=None,
        llm=None,
        max_retries=0,
        drive_logs=tmp_path,
        task_id="phase_web_1:final_review",
        event_queue=None,
        task_type="phase_run",
        drive_root=tmp_path,
        rounds_in_phase=5,
        completion_prerequisites=(
            {
                "kind": "completion_contract",
                "tool": "submit_final_review",
                "outcomes": {
                    "ok": {
                        "requires": [
                            {
                                "tool": "run_workspace_verify",
                                "accept": "passed_evidence",
                            }
                        ]
                    }
                },
            },
        ),
    )

    assert result == ("continue", "continue")
    assert "REQUIRED_TOOL_CALLS_PENDING" in messages[-1]["content"]
    assert "`run_workspace_verify` accepted 0/1" in messages[-1]["content"]


def test_required_phase_completion_nudge_forces_submit_after_prior_tool_calls():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _maybe_force_required_phase_completion

    messages = []
    forced = _maybe_force_required_phase_completion(
        terminating_tools=frozenset({"submit_preflight_report"}),
        tool_calls=[
            {"function": {"name": "env_check"}},
            {"function": {"name": "read_workspace_charter"}},
        ],
        messages=messages,
        rounds_in_phase=5,
        forced_progress_tool_choice=None,
        trace_tool_calls=[
            {"tool": "env_check", "result": '{"status":"ok"}'},
            {
                "tool": "read_workspace_charter",
                "result": '{"workspace_id":"civilization","files":{"TASK_MAIN.md":"task"}}',
            },
        ],
        completion_prerequisites=(
            {"kind": "tool_call", "tool": "env_check", "n": 1, "tools": ["env_check"]},
            {
                "kind": "tool_call",
                "tool": "read_workspace_charter",
                "n": 1,
                "tools": ["read_workspace_charter"],
            },
        ),
    )

    assert forced == "submit_preflight_report"
    assert messages
    assert "REQUIRED_PHASE_COMPLETION_PENDING" in messages[-1]["content"]


def test_required_phase_completion_nudge_does_not_force_execute_completion():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _maybe_force_required_phase_completion

    messages = []
    forced = _maybe_force_required_phase_completion(
        terminating_tools=frozenset({"mark_subtask_complete"}),
        tool_calls=[{"function": {"name": "read_file"}}],
        messages=messages,
        rounds_in_phase=4,
        forced_progress_tool_choice=None,
    )

    assert forced is None
    assert messages == []


def test_required_phase_completion_nudge_forces_mark_after_typed_proof(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import (
        _maybe_force_required_phase_completion,
        _may_force_mark_subtask_complete,
    )

    drive_root = tmp_path / "drive"
    state_dir = drive_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "subtasks": [
                            {
                                "id": "project-setup",
                                "status": "pending",
                                "proof": {
                                    "execution": {
                                        "kind": "bool",
                                        "command": [
                                            "python",
                                            "-c",
                                            "import demoapp",
                                        ],
                                    }
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    trace_tool_calls = [
        {
            "tool": "run_subtask_proof",
            "args": {"subtask_id": "project-setup"},
            "result": json.dumps(
                {
                    "passed": True,
                    "completion_contract_hint": {
                        "subtask_id": "project-setup",
                        "status": "done",
                    },
                }
            ),
            "is_error": False,
        }
    ]
    assert _may_force_mark_subtask_complete(
        drive_root=drive_root,
        trace_tool_calls=trace_tool_calls,
        phase_write_tool_calls=1,
    )
    messages = []
    forced = _maybe_force_required_phase_completion(
        terminating_tools=frozenset({"mark_subtask_complete"}),
        tool_calls=[{"function": {"name": "read_file"}}],
        messages=messages,
        rounds_in_phase=8,
        forced_progress_tool_choice=None,
        drive_root=drive_root,
        phase_write_tool_calls=1,
        trace_tool_calls=trace_tool_calls,
    )
    assert forced == "mark_subtask_complete"
    assert "mark_subtask_complete(claim=" in messages[-1]["content"]
    assert "completion_contract_hint" not in messages[-1]["content"]


def test_required_phase_completion_nudge_ignores_shell_success_for_typed_proof(tmp_path):
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _maybe_force_required_phase_completion

    drive_root = tmp_path / "drive"
    state_dir = drive_root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "phase_plan.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "execute",
                        "subtasks": [
                            {
                                "id": "runtime-smoke",
                                "status": "pending",
                                "proof": {
                                    "execution": {
                                        "kind": "pytest",
                                        "command": [
                                            "python",
                                            "-m",
                                            "pytest",
                                            "tests/test_guismoke.py",
                                            "-q",
                                            "-s",
                                        ],
                                    }
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    trace_tool_calls = [
        {
            "tool": "shell",
            "args": {
                "command": [
                    "python",
                    "-m",
                    "pytest",
                    "tests/test_guismoke.py",
                    "-q",
                    "-s",
                ]
            },
            "result": '{"exit_code": 0, "output": "2 passed"}',
            "is_error": False,
        },
        {
            "tool": "run_subtask_proof",
            "args": {"subtask_id": "runtime-smoke"},
            "result": json.dumps(
                {
                    "passed": False,
                    "subtask_id": "runtime-smoke",
                    "completion_contract_hint": {"status": "failed"},
                }
            ),
            "is_error": False,
        },
    ]
    messages = []
    forced = _maybe_force_required_phase_completion(
        terminating_tools=frozenset({"mark_subtask_complete"}),
        tool_calls=[{"function": {"name": "read_file"}}],
        messages=messages,
        rounds_in_phase=16,
        forced_progress_tool_choice=None,
        drive_root=drive_root,
        phase_write_tool_calls=1,
        trace_tool_calls=trace_tool_calls,
    )
    assert forced is None
    assert messages == []


def test_phase_required_tools_are_part_of_phase_filter():
    import sys
    import pathlib

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _phase_required_tool_set, _phase_tool_filter_sets

    tool_filter = {
        "allow": ["env_check"],
        "deny": [],
        "required": ["submit_preflight_report"],
    }
    allowed, denied = _phase_tool_filter_sets("phase_run", tool_filter)
    assert denied == set()
    assert "env_check" in allowed
    assert "submit_preflight_report" in allowed
    assert "propose_task_plan" not in allowed
    assert _phase_required_tool_set(tool_filter) == {"submit_preflight_report"}


def test_submit_micro_review_validates_verdict(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_micro_review
    from unittest.mock import MagicMock
    ctx = MagicMock()
    ctx.drive_root = tmp_path
    (tmp_path / "state").mkdir(exist_ok=True)
    result = _submit_micro_review(ctx, verdict="bad_value")
    assert "ERROR" in result


def test_submit_final_review_ok_requires_current_phase_e2e(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_final_review
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {
        "phase_node": {"id": "final_review", "manifest_id": "final_review"}
    }
    ctx.loop_state_view = {
        "phase_label": "final_review",
        "last_verify_passed": True,
        "last_verify_failed_count": 0,
        "last_verify_run_id": "verify-ok",
        "last_e2e_passed": False,
        "last_e2e_failed_count": 0,
        "last_e2e_phase_label": "",
        "last_e2e_run_id": "",
    }

    result = _submit_final_review(ctx, outcome="ok")

    assert result.startswith("ERROR:")
    assert "run_real_e2e" in result


def test_submit_final_review_ok_requires_workspace_verify(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_final_review
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {
        "phase_node": {"id": "final_review", "manifest_id": "final_review"}
    }
    ctx.loop_state_view = {
        "phase_label": "final_review",
        "last_verify_passed": False,
        "last_verify_failed_count": 0,
        "last_verify_run_id": "",
        "last_e2e_passed": True,
        "last_e2e_failed_count": 0,
        "last_e2e_phase_label": "final_review",
        "last_e2e_run_id": "verify-e2e-1",
    }

    result = _submit_final_review(ctx, outcome="ok")

    assert result.startswith("ERROR:")
    assert "run_workspace_verify" in result


def test_submit_final_review_ok_accepts_current_phase_e2e(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_final_review
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {
        "phase_node": {"id": "final_review", "manifest_id": "final_review"}
    }
    ctx.loop_state_view = {
        "phase_label": "final_review",
        "last_verify_passed": True,
        "last_verify_failed_count": 0,
        "last_verify_run_id": "verify-ok",
        "last_e2e_passed": True,
        "last_e2e_failed_count": 0,
        "last_e2e_phase_label": "final_review",
        "last_e2e_run_id": "verify-e2e-1",
    }

    result = _submit_final_review(ctx, outcome="ok", notes="green")

    assert result.startswith("OK:")
    signal = json.loads((tmp_path / "state" / "phase_control_signal.json").read_text())
    assert signal["kind"] == "submit_final_review"
    assert signal["payload"]["outcome"] == "ok"


def test_submit_final_review_ok_accepts_same_round_logged_e2e(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_final_review
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-1:final_review"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {
        "phase_node": {"id": "final_review", "manifest_id": "final_review"}
    }
    ctx.loop_state_view = {
        "phase_label": "final_review",
        "last_e2e_passed": False,
        "last_e2e_failed_count": 0,
        "last_e2e_phase_label": "",
        "last_e2e_run_id": "",
    }
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    payload = {
        "passed": True,
        "verify_run_id": "verify-e2e-logged",
        "failed_step_count": 0,
    }
    (logs / "tools.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": "run-1:final_review",
                        "tool": "run_workspace_verify",
                        "result_preview": json.dumps(
                            {
                                "passed": True,
                                "verify_run_id": "verify-workspace-logged",
                                "failed_step_count": 0,
                            }
                        ),
                    }
                ),
                json.dumps(
                    {
                        "task_id": "run-1:final_review",
                        "tool": "run_real_e2e",
                        "result_preview": json.dumps(payload),
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = _submit_final_review(ctx, outcome="ok", notes="green")

    assert result.startswith("OK:")


def test_submit_final_review_loop_back_writes_phase_exit_decision(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_final_review
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-1:final_review"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {
        "phase_node": {"id": "final_review", "manifest_id": "final_review"}
    }
    ctx.loop_state_view = {
        "phase_label": "final_review",
        "verification_summary": "Verification failed: pytest:tests",
    }
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": ctx.task_id,
                "tool": "run_workspace_verify",
                "result_preview": json.dumps(
                    {
                        "passed": False,
                        "failed_step_count": 1,
                        "summary": "Verification failed: pytest:tests",
                        "verification_report_ref": {
                            "report_id": "verify-ledger-1",
                            "ledger_hash": "ledger-hash-1",
                        },
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _submit_final_review(
        ctx,
        outcome="loop_back",
        issues=[
            {
                "code": "verification_step_failed",
                "severity": "blocking",
                "phase": "final_review",
                "message": "pytest:tests failed",
            }
        ],
        required_changes=[
            {
                "id": "fix-pytest-tests",
                "target_phase": "execute",
                "path": "verification.pytest:tests",
                "op": "repair",
            }
        ],
    )

    assert result.startswith("OK:")
    decision = json.loads(
        (tmp_path / "state" / "phase_exit_decision_latest.json").read_text()
    )
    assert decision["outcome"] == "loop_back"
    assert decision["target_phase"] == "execute"
    assert decision["issues"][0]["code"] == "verification_step_failed"
    assert decision["required_changes"][0]["id"] == "fix-pytest-tests"


def test_submit_verification_pass_rejects_unresolved_limitations(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_verification
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.loop_state_view = {
        "last_verify_run_id": "verify-ok",
        "last_verify_passed": True,
        "last_verify_failed_count": 0,
    }

    result = _submit_verification(
        ctx,
        status="pass",
        details="All green, but actual playable gameplay requires fixing.",
    )

    assert result.startswith("ERROR:")
    assert "unresolved blockers" in result


def test_promote_to_durable_blocks_verify_report_with_limitations(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _promote_to_durable
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.context_overlays = {
        "phase_node": {"id": "verify", "manifest_id": "verify"},
    }

    result = _promote_to_durable(
        ctx,
        workspace_id="mini_game",
        tags="verification_report",
        content="PASS, but runtime game logic errors detected and require fixing.",
    )

    assert result.startswith("ERROR:")
    assert "cannot promote" in result


def test_promote_to_durable_writes_verified_palace_durable_store(
    tmp_path, monkeypatch
):
    import sys
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    (tmp_path / "umbrella").mkdir()
    (tmp_path / "workspaces").mkdir()
    from ouroboros.tools.phase_contract import _promote_to_durable
    from ouroboros.tools.registry import ToolContext
    from umbrella.enforcement.ledger import append_supervisor_ledger_event

    calls = []

    class _FakeMemPalace:
        def __init__(self, repo_root, workspace_id):
            self.repo_root = repo_root
            self.workspace_id = workspace_id

        def add(self, **kw):
            calls.append(kw)
            return "durable-node"

        def close(self):
            return None

    monkeypatch.setattr("umbrella.memory.palace.facade.MemPalace", _FakeMemPalace)
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    ctx = ToolContext(repo_dir=tmp_path, host_repo_root=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-verify:verify"
    ctx.context_overlays = {
        "phase_node": {"id": "verify", "manifest_id": "verify"},
    }

    blocked = _promote_to_durable(
        ctx,
        workspace_id="mini_game",
        tags="verification_report",
        title="Verification report",
        content="PASS: all required checks are green.",
    )
    blocked_payload = json.loads(blocked)
    assert blocked_payload["saved"] is False
    assert not calls

    event = append_supervisor_ledger_event(
        repo_root=tmp_path,
        workspace_id="mini_game",
        actor="verifier",
        phase="verify",
        tool="run_workspace_verify",
        result={"passed": True},
    )
    result = _promote_to_durable(
        ctx,
        workspace_id="mini_game",
        tags="verification_report",
        title="Verification report",
        content="PASS: all required checks are green.",
        evidence_refs=[
            {
                "ref_type": "ledger_event",
                "ref_id": event.event_id,
                "hash": event.event_hash,
                "produced_by": "verifier",
            }
        ],
        trust_level="public_verified",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["durable_store"] == "palace.durable"
    assert payload["durable_node_id"] == "durable-node"
    assert calls
    assert calls[-1]["store"] == "palace.durable"
    assert calls[-1]["scope"] == "cross_run_durable"
    assert calls[-1]["verified"] is True
    assert "verification_report" in calls[-1]["tags"]
    assert calls[-1]["extra"]["trust_level"] == "public_verified"
    assert "ledger_event" in calls[-1]["extra"]["evidence_refs_json"]


def test_submit_preflight_report_ready(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_preflight_report
    from unittest.mock import MagicMock
    ctx = MagicMock()
    ctx.drive_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.host_repo_root = tmp_path
    (tmp_path / "state").mkdir(exist_ok=True)
    result = _submit_preflight_report(
        ctx,
        status="ready",
        blockers=[],
        research_depth="none",
        research_depth_rationale="No external research needed for this fixture.",
    )
    assert "ready" in result
    assert result.startswith("OK:")
    assert "ERROR" not in result


def test_submit_preflight_report_defers_implementation_blockers(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_preflight_report
    from unittest.mock import MagicMock
    ctx = MagicMock()
    ctx.drive_root = tmp_path
    (tmp_path / "state").mkdir(exist_ok=True)

    result = _submit_preflight_report(
        ctx,
        status="blocked",
        blockers=[
            "Import error: cannot import get_available_bot_tools",
            "Previous verification showed HTTP boot failure",
            "api_missing_argument: GameEngine.__init__() missing required 'ai_controller' parameter",
            "pytest_collection_failed: tests cannot import from backend.bots.bot_tools",
            "Cannot proceed with new development until existing codebase is functional",
        ],
    )

    signal = json.loads((tmp_path / "state" / "phase_control_signal.json").read_text())
    assert "ready" in result
    assert signal["payload"]["status"] == "ready"
    assert signal["payload"]["blockers"] == []
    assert len(signal["payload"]["implementation_notes"]) == 5


def test_loop_back_to_marks_phase_pending(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _loop_back_to
    from unittest.mock import MagicMock

    plan = {
        "plan_id": "p1", "workspace_id": "ws1", "run_id": "r1", "version": 1,
        "nodes": [
            {"id": "research", "manifest_id": "research", "status": "done"},
            {"id": "plan", "manifest_id": "plan", "status": "running"},
        ],
        "edits_log": [],
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "phase_plan.json").write_text(json.dumps(plan))

    ctx = MagicMock()
    ctx.drive_root = tmp_path
    result = _loop_back_to(ctx, phase="research", reason="test loop back")
    assert "research" in result
    updated = json.loads((state_dir / "phase_plan.json").read_text())
    research_node = next(n for n in updated["nodes"] if n["id"] == "research")
    assert research_node["status"] == "pending"


def test_loop_back_to_rejects_forward_phase_target(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _loop_back_to
    from unittest.mock import MagicMock

    plan = {
        "plan_id": "p1", "workspace_id": "ws1", "run_id": "r1", "version": 1,
        "nodes": [
            {"id": "plan", "manifest_id": "plan", "status": "done"},
            {"id": "execute", "manifest_id": "execute", "status": "running"},
            {"id": "verify", "manifest_id": "verify", "status": "pending"},
        ],
        "edits_log": [],
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "phase_plan.json").write_text(json.dumps(plan))

    ctx = MagicMock()
    ctx.drive_root = tmp_path
    ctx.task_id = "r1:execute"
    ctx.context_overlays = {"phase_node": {"id": "execute", "manifest_id": "execute"}}

    result = _loop_back_to(ctx, phase="verify", reason="captured forward jump")

    assert result.startswith("ERROR:")
    assert "current or an earlier phase" in result
    updated = json.loads((state_dir / "phase_plan.json").read_text())
    assert updated["nodes"][1]["status"] == "running"
    assert updated["nodes"][2]["status"] == "pending"
    assert not (state_dir / "phase_control_signal.json").exists()


def test_subtask_review_loop_back_to_plan_requires_typed_recovery(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _loop_back_to
    from unittest.mock import MagicMock

    plan = {
        "plan_id": "p1", "workspace_id": "ws1", "run_id": "r1", "version": 1,
        "nodes": [
            {"id": "plan", "manifest_id": "plan", "status": "done"},
            {"id": "execute", "manifest_id": "execute", "status": "done"},
            {
                "id": "subtask_review:s1",
                "manifest_id": "subtask_review",
                "status": "running",
                "parent_phase_id": "execute",
            },
        ],
        "edits_log": [],
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "phase_plan.json").write_text(json.dumps(plan))

    ctx = MagicMock()
    ctx.drive_root = tmp_path
    ctx.task_id = "r1:subtask_review:s1"
    ctx.context_overlays = {
        "phase_node": {
            "id": "subtask_review:s1",
            "manifest_id": "subtask_review",
        }
    }

    result = _loop_back_to(ctx, phase="plan", reason="review prose")

    assert result.startswith("ERROR:")
    assert "RecoveryDecision" in result
    updated = json.loads((state_dir / "phase_plan.json").read_text())
    assert updated["nodes"][0]["status"] == "done"
    assert not (state_dir / "phase_control_signal.json").exists()


def test_mark_subtask_complete_rejects_phase_level_without_active_work_item(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _mark_subtask_complete
    from ouroboros.tools.registry import ToolContext

    plan = {
        "plan_id": "p1", "workspace_id": "ws1", "run_id": "run-1", "version": 1,
        "nodes": [
            {"id": "execute", "manifest_id": "execute", "status": "running"},
        ],
        "edits_log": [],
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "phase_plan.json").write_text(json.dumps(plan))

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-1:execute"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {"phase_node": {"id": "execute", "manifest_id": "execute"}}
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _mark_subtask_complete(ctx, notes="implemented and verified")

    payload = json.loads(result)
    assert payload["error"] == "NO_ACTIVE_WORK_ITEM"
    assert payload["required_next_action"] == "materialize_work_item"
    assert not (state_dir / "phase_control_signal.json").exists()


def test_mark_subtask_complete_rejects_unknown_subtask_when_cards_exist(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _mark_subtask_complete
    from ouroboros.tools.registry import ToolContext

    plan = {
        "plan_id": "p1", "workspace_id": "ws1", "run_id": "run-1", "version": 1,
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [{"id": "build-ui", "status": "running"}],
            },
        ],
        "edits_log": [],
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "phase_plan.json").write_text(json.dumps(plan))

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "run-1:execute"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {"phase_node": {"id": "execute", "manifest_id": "execute"}}

    result = _mark_subtask_complete(ctx, subtask_id="missing")

    payload = json.loads(result)
    assert payload["error"] == "MODEL_SUPPLIED_COMPLETION_FIELDS_REJECTED"
    assert payload["rejected_fields"] == ["subtask_id"]
    assert not (state_dir / "phase_control_signal.json").exists()


def test_mark_subtask_complete_promotes_summary_and_evidence_to_phase_memory(
    tmp_path, monkeypatch
):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _mark_subtask_complete
    from ouroboros.tools.registry import ToolContext
    from umbrella.contracts.hashing import diff_hash, hash_value, workspace_hash
    from umbrella.contracts.work_items import WorkItem, save_active_work_item
    from umbrella.enforcement.ledger import append_supervisor_ledger_event
    from umbrella.memory.palace import facade

    captured_memory: list[dict] = []

    def fake_add(self, **kwargs):
        captured_memory.append(kwargs)
        return "subtask-memory-id"

    monkeypatch.setattr(facade.MemPalace, "add", fake_add)

    plan = {
        "plan_id": "p1", "workspace_id": "ws1", "run_id": "run-1", "version": 1,
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [{"id": "build-core", "status": "running"}],
            },
        ],
        "edits_log": [],
    }
    workspace = tmp_path / "workspaces" / "ws1"
    drive = workspace / ".memory" / "drive"
    state_dir = drive / "state"
    logs_dir = drive / "logs"
    state_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    (state_dir / "phase_plan.json").write_text(json.dumps(plan))

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.host_repo_root = tmp_path
    ctx.drive_root = drive
    ctx.task_id = "run-1:execute"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {"phase_node": {"id": "execute", "manifest_id": "execute"}}
    ctx.loop_state_view = {"phase_label": "linear"}

    ws_hash = workspace_hash(workspace)
    diff_h = diff_hash(workspace, [])
    report_hash = hash_value({"passed": True})
    report_result = {
        "report_hash": report_hash,
        "passed": True,
        "workspace_hash": ws_hash,
        "diff_hash": diff_h,
    }
    event = append_supervisor_ledger_event(
        repo_root=tmp_path,
        workspace_id="ws1",
        actor="verifier",
        phase="execute",
        tool="run_subtask_proof",
        result=report_result,
    )
    proof_ref = {
        "ref_type": "ledger_event",
        "ref_id": event.event_id,
        "hash": event.event_hash,
        "produced_by": "verifier",
        "phase": "execute",
        "subtask_id": "build-core",
    }
    verification_report = {
            "report_id": event.event_id,
            "report_hash": report_hash,
            "workspace_hash": ws_hash,
            "diff_hash": diff_h,
            "produced_after_event_id": "",
            "verifier_id": "verifier",
            "passed": True,
            "ledger_hash": event.event_hash,
    }
    (logs_dir / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "run-1:execute",
                "tool": "run_subtask_proof",
                "args": {"subtask_id": "build-core"},
                "result_preview": json.dumps(
                    {
                        "passed": True,
                        "subtask_id": "build-core",
                        "proof_ref": proof_ref,
                        "verification_report": verification_report,
                        "completion_contract_hint": {"changed_files": []},
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    save_active_work_item(
        drive,
        WorkItem(
            id="work:build-core",
            kind="implementation_repair",
            source_phase="execute",
            target_phase="execute",
            active_subtask_id="build-core",
        ),
    )

    result = _mark_subtask_complete(
        ctx,
        claim="Built the core models.",
        notes="Built the core models.",
    )

    assert result == "OK: Subtask 'build-core' marked complete"
    signal = json.loads((state_dir / "phase_control_signal.json").read_text())
    payload = signal["payload"]
    assert payload["subtask_id"] == "build-core"
    assert payload["status"] == "done"
    assert payload["summary"] == "Built the core models."
    assert payload["evidence"] == [
        f"ledger_event:{event.event_id}",
        f"verification_report:{event.event_id}",
    ]

    updated = json.loads((state_dir / "phase_plan.json").read_text())
    subtask = updated["nodes"][0]["subtasks"][0]
    assert subtask["status"] == "done"
    assert subtask["completion"]["summary"] == "Built the core models."
    assert subtask["completion"]["evidence"] == [
        f"ledger_event:{event.event_id}",
        f"verification_report:{event.event_id}",
    ]
    assert captured_memory
    mirrored = captured_memory[-1]
    assert mirrored["store"] == "palace.subtask"
    assert mirrored["tier"] == "hot"
    assert mirrored["scope"] == "subtask_scoped"
    assert mirrored["subtask_id"] == "build-core"
    assert mirrored["run_id"] == "run-1"
    assert mirrored["verified"] is True
    assert "subtask_complete" in mirrored["tags"]
    assert "Built the core models." in mirrored["content"]
    assert f"verification_report:{event.event_id}" in mirrored["content"]


def test_mark_subtask_complete_rejects_failed_status_for_phase_subtask(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _mark_subtask_complete
    from ouroboros.tools.registry import ToolContext

    plan = {
        "plan_id": "p1", "workspace_id": "ws1", "run_id": "run-1", "version": 1,
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [{"id": "st-001", "status": "pending"}],
            },
        ],
        "edits_log": [],
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "phase_plan.json").write_text(json.dumps(plan))

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "phase_web_c57aad13:execute"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {"phase_node": {"id": "execute", "manifest_id": "execute"}}

    result = _mark_subtask_complete(
        ctx,
        subtask_id="st-001",
        status="failed",
        summary=(
            "Subtask execution failed due to blocking guard condition. "
            "Budget exhausted with 0/3 successful phases."
        ),
        evidence=["Required success_test was passed but phase completion was rejected"],
    )

    payload = json.loads(result)
    assert payload["error"] == "MODEL_SUPPLIED_COMPLETION_FIELDS_REJECTED"
    assert set(payload["rejected_fields"]) == {
        "subtask_id",
        "status",
        "summary",
        "evidence",
    }
    updated = json.loads((state_dir / "phase_plan.json").read_text())
    assert updated["nodes"][0]["subtasks"][0]["status"] == "pending"
    assert not (state_dir / "phase_control_signal.json").exists()


def test_mark_subtask_complete_rejects_untyped_empty_completion_after_success_test(
    tmp_path,
):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _mark_subtask_complete
    from ouroboros.tools.registry import ToolContext

    # Reduced from phase_web_25dbf47b: a first completion call had useful
    # summary/evidence but quoted the subtask id; the retry used only
    # {"subtask_id": "1.1"} and was accepted with empty subtask memory.
    # Phase-run completion now rejects this earlier: it must carry the typed
    # verifier-backed completion contract so proof metadata cannot be dropped.
    plan = {
        "plan_id": "p1",
        "workspace_id": "civilization",
        "run_id": "phase_web_25dbf47b",
        "version": 1,
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "1.1",
                        "status": "pending",
                        "success_test": {
                            "kind": "cmd",
                            "value": "pytest tests/test_backend_init.py -k test_health_endpoint -q",
                        },
                    },
                ],
            },
        ],
        "edits_log": [],
    }
    state_dir = tmp_path / "state"
    logs_dir = tmp_path / "logs"
    state_dir.mkdir()
    logs_dir.mkdir()
    (state_dir / "phase_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (logs_dir / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_25dbf47b:execute",
                "tool": "shell",
                "args": {
                    "argv": [
                        "pytest",
                        "tests/test_backend_init.py",
                        "-k",
                        "test_health_endpoint",
                        "-q",
                    ],
                },
                "result_preview": json.dumps(
                    {
                        "exit_code": 0,
                        "output": "1 passed, 3 deselected in 0.40s",
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "phase_web_25dbf47b:execute"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {"phase_node": {"id": "execute", "manifest_id": "execute"}}

    result = _mark_subtask_complete(ctx, subtask_id="1.1")

    payload = json.loads(result)
    assert payload["error"] == "MODEL_SUPPLIED_COMPLETION_FIELDS_REJECTED"
    assert payload["rejected_fields"] == ["subtask_id"]
    updated = json.loads((state_dir / "phase_plan.json").read_text(encoding="utf-8"))
    assert updated["nodes"][0]["subtasks"][0]["status"] == "pending"
    assert not (state_dir / "phase_control_signal.json").exists()


def test_mark_subtask_complete_rejects_captured_openai_runtime_memory_claim(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _mark_subtask_complete
    from ouroboros.tools.registry import ToolContext

    plan = {
        "plan_id": "p1",
        "workspace_id": "civilization",
        "run_id": "phase_web_eb7e7d72",
        "version": 1,
        "nodes": [
            {
                "id": "execute",
                "manifest_id": "execute",
                "status": "running",
                "subtasks": [
                    {
                        "id": "st_1_3_llm_integration",
                        "status": "pending",
                        "success_test": "pytest tests/test_llm_config.py -v --tb=short",
                    }
                ],
            },
        ],
        "edits_log": [],
    }
    state_dir = tmp_path / "state"
    logs_dir = tmp_path / "logs"
    state_dir.mkdir()
    logs_dir.mkdir()
    (state_dir / "phase_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (logs_dir / "tools.jsonl").write_text(
        json.dumps(
            {
                "task_id": "phase_web_eb7e7d72:execute",
                "tool": "shell",
                "result_preview": json.dumps(
                    {
                        "command": [
                            "python",
                            "-m",
                            "pytest",
                            "tests/test_llm_config.py",
                            "-v",
                            "--tb=short",
                        ],
                        "exit_code": 0,
                        "output": "34 passed in 0.08s",
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "phase_web_eb7e7d72:execute"
    ctx.current_task_type = "phase_run"
    ctx.context_overlays = {"phase_node": {"id": "execute", "manifest_id": "execute"}}
    ctx.loop_state_view = {"phase_label": "linear"}

    result = _mark_subtask_complete(
        ctx,
        subtask_id="st_1_3_llm_integration",
        summary="Implemented LLM provider configuration.",
        evidence=[
            (
                "Implemented support for multiple environment variable sources: "
                "OUROBOROS_* (highest precedence), LLM_*, OPENAI_*"
            ),
            "All 34 tests passed successfully: pytest tests/test_llm_config.py -v --tb=short",
        ],
    )

    payload = json.loads(result)
    assert payload["error"] == "MODEL_SUPPLIED_COMPLETION_FIELDS_REJECTED"
    assert set(payload["rejected_fields"]) == {"subtask_id", "summary", "evidence"}
    updated = json.loads((state_dir / "phase_plan.json").read_text(encoding="utf-8"))
    assert updated["nodes"][0]["subtasks"][0]["status"] == "pending"
    assert not (state_dir / "phase_control_signal.json").exists()


def test_propose_phase_plan_persists_review_artifact(tmp_path, monkeypatch):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from umbrella.memory.palace import facade
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    (tmp_path / "umbrella").mkdir()

    captured: list[dict] = []

    def fake_add(self, **kwargs):
        captured.append(kwargs)
        return "memory-id"

    monkeypatch.setattr(facade.MemPalace, "add", fake_add)

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {
        "active_workspace_id": "ws1",
        "phase_label": "plan",
    }

    result = _propose_phase_plan(
        ctx,
        plan={
            "phases": [
                    {
                        "id": "execute",
                        "title": "Build",
                        "proof": _pytest_proof(
                            ["python", "-m", "pytest", "tests", "-q"],
                        ),
                    }
                ]
            },
        notes="test plan",
    )

    assert result.startswith("OK:")
    latest = json.loads(
        (drive / "state" / "phase_plan_proposal_latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["plan"]["subtasks"][0]["id"] == "execute"
    assert latest["plan_id"] == "phase_plan:execute"
    assert "plan_id: phase_plan:execute" in result
    assert captured
    assert captured[0]["store"] == "palace.run"
    assert captured[0]["tier"] == "hot"
    assert "phase_plan_proposal" in captured[0]["tags"]
    assert "umbrella_plan_candidate" in captured[0]["tags"]
    assert "phase_plan" not in captured[0]["tags"]


def test_propose_phase_plan_accepts_typed_root_test_file(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                    {
                        "id": "e2e",
                        "files": ["test_integration.py"],
                        "proof": _pytest_proof(
                            ["python", "-m", "pytest", "test_integration.py", "-q"],
                            target="test_integration.py",
                        ),
                    }
                ]
            },
    )

    assert result.startswith("OK:")
    assert (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_propose_phase_plan_rejects_stale_optional_param_claim(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    (ws_root / "game_engine.py").write_text(
        "class GameEngine:\n"
        "    def __init__(self, game, ai_controller=None):\n"
        "        self.ai_controller = ai_controller\n",
        encoding="utf-8",
    )

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "repair",
            "overview": "Fix GameEngine.__init__() missing ai_controller parameter.",
            "subtasks": [
                    {
                        "id": "repair-api",
                        "goal": "Fix GameEngine.__init__() missing ai_controller.",
                        "proof": _pytest_proof(
                            ["python", "-m", "pytest", "tests", "-q"],
                        ),
                    }
                ],
            },
    )

    assert result.startswith("ERROR:")
    assert "already shows that parameter" in result


def test_propose_phase_plan_rejects_stale_symbol_mismatch_claim(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    tools = ws_root / "backend" / "bots"
    tools.mkdir(parents=True)
    (tools / "bot_tools.py").write_text(
        "def get_game_state_tool(game_state, player_id):\n    return 'state'\n",
        encoding="utf-8",
    )

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "repair",
            "overview": (
                "pytest fails because tests/test_bot_tools.py expects "
                "get_game_state_tool function but bot_tools.py contains "
                "GetGameStateTool class."
            ),
            "subtasks": [
                {
                    "id": "repair-tools",
                    "goal": "Fix get_game_state_tool export mismatch.",
                    "success_test": "python -m pytest tests -q",
                }
            ],
        },
    )

    assert result.startswith("ERROR:")
    assert "does not contain that class definition" in result


def test_propose_phase_plan_rejects_stub_intent(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "fix-import",
                    "description": "Implement or stub the missing API.",
                    "files": ["src/app.py", "tests/test_app.py"],
                    "verification": "pytest",
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "stub/mock/placeholder" in result


def test_propose_phase_plan_rejects_unknown_declared_tools(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                    {
                        "id": "execute",
                        "allowed_tools": "imaginary_tool, shell",
                        "proof": _pytest_proof(
                            ["python", "-m", "pytest", "tests", "-q"],
                        ),
                    }
                ]
            },
        )

    assert result.startswith("ERROR:")
    assert "unknown phase tool `imaginary_tool`" in result
    assert "unknown phase tool `shell`" not in result
    assert not (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_propose_phase_plan_rejects_unknown_tools_field_names(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                    {
                        "id": "execute",
                        "tools": ["imaginary_tool", "shell"],
                        "proof": _pytest_proof(
                            ["python", "-m", "pytest", "tests", "-q"],
                        ),
                    }
                ]
            },
        )

    assert result.startswith("ERROR:")
    assert "unknown phase tool `imaginary_tool`" in result
    assert "unknown phase tool `shell`" not in result


def test_propose_phase_plan_allows_domain_runtime_tools(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "title": "GMAS game plan",
            "subtasks": [
                {
                    "id": "wire-bots",
                    "goal": "Wire bot agents to the game state.",
                    "proof": _pytest_proof(
                        ["python", "-m", "pytest", "tests/test_bots.py", "-q"],
                        target="tests/test_bots.py",
                    ),
                }
            ],
            "gmas_usage": {
                "agent_roles": [
                    {
                        "id": "diplomat",
                        "tools": ["propose_trade", "declare_war"],
                    },
                    {
                        "id": "economist",
                        "tools": ["allocate_production", "adjust_tax_policy"],
                    },
                ]
            },
        },
    )

    assert result.startswith("OK:")
    latest = drive / "state" / "phase_plan_proposal_latest.json"
    assert latest.exists()
    assert "propose_trade" in latest.read_text(encoding="utf-8")


def test_propose_phase_plan_rejects_parallel_impl_root_without_migration(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    (ws_root / "game_core").mkdir(parents=True)
    (ws_root / "game_core" / "__init__.py").write_text("", encoding="utf-8")
    (ws_root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "backend",
                    "deliverables": ["backend/api.py", "backend/bots/gmas_config.py"],
                    "verification": "python -m pytest tests",
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "new top-level implementation root" in result
    assert not (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_propose_phase_plan_rejects_scaffold_over_existing_impl(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    (ws_root / "frontend" / "src").mkdir(parents=True)
    (ws_root / "frontend" / "src" / "App.tsx").write_text(
        "export default function App(){return null}\n",
        encoding="utf-8",
    )
    (ws_root / "game_core").mkdir()
    (ws_root / "game_core" / "__init__.py").write_text("", encoding="utf-8")
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "setup_project_structure",
                    "description": (
                        "Setup project structure and dependencies. Create full-stack "
                        "project structure with Python backend and React frontend."
                    ),
                    "success_test": (
                        "pyproject.toml has fastapi dependencies; frontend/ and "
                        "backend/ directories created"
                    ),
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "scaffolding/building project structure from scratch" in result
    assert not (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_propose_phase_plan_allows_greenfield_scaffold_with_only_control_files(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    ws_root.mkdir(parents=True)
    (ws_root / "TASK_MAIN.md").write_text("Build a calculator GUI.\n", encoding="utf-8")
    (ws_root / "workspace.toml").write_text(
        "[project]\nname = \"calculator\"\n",
        encoding="utf-8",
    )
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "plan_id": "greenfield-calculator",
            "subtasks": [
                {
                    "id": "project-setup",
                    "title": "Project setup",
                    "goal": "Create the Python package scaffold and import test.",
                    "files_to_create": [
                        "pyproject.toml",
                        "src/calculator/__init__.py",
                        "tests/test_import.py",
                    ],
                    "proof": {
                        "execution": {
                            "kind": "pytest",
                            "command": [
                                "python",
                                "-m",
                                "pytest",
                                "tests/test_import.py",
                                "-q",
                            ],
                            "timeout_sec": 60,
                            "shell": False,
                        },
                        "oracle": {
                            "oracle_type": "import_check",
                            "required_properties": [
                                "module_imports",
                                "no_test_tampering",
                            ],
                        },
                        "scope": {
                            "files_under_test": ["src/calculator/__init__.py"],
                            "changed_files_expected": [
                                "pyproject.toml",
                                "src/calculator/__init__.py",
                                "tests/test_import.py",
                            ],
                            "pytest_targets": ["tests/test_import.py"],
                        },
                        "anti_gaming": {
                            "allows_mock": False,
                            "allows_snapshot_update": False,
                            "allows_test_only_change": False,
                            "requires_real_runtime": False,
                        },
                        "required_capabilities": ["python"],
                        "human_claims": ["Calculator package scaffold imports."],
                        "generated_test_contract": {
                            "interface_model": {
                                "events": [
                                    {
                                        "name": "package_import",
                                        "valid_values": ["calculator"],
                                    }
                                ]
                            },
                            "oracle_claims": [
                                {
                                    "claim_id": "calculator_package_imports",
                                    "source": "task_requirement",
                                    "subject": "package_import",
                                    "input_values": ["calculator"],
                                    "accepted": True,
                                    "expected_behavior": "module imports",
                                    "test_refs": ["tests/test_import.py"],
                                }
                            ],
                        },
                    },
                }
            ],
        },
    )

    assert result.startswith("OK:"), result
    assert (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_propose_phase_plan_allows_repair_plan_for_existing_impl(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    (ws_root / "frontend" / "src").mkdir(parents=True)
    (ws_root / "frontend" / "src" / "App.tsx").write_text(
        "export default function App(){return null}\n",
        encoding="utf-8",
    )
    (ws_root / "game_core").mkdir()
    (ws_root / "game_core" / "__init__.py").write_text("", encoding="utf-8")
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "repair_existing_runtime",
                        "description": (
                            "Fix and integrate existing frontend and game_core runtime "
                            "without scaffolding a replacement project."
                        ),
                        "files": ["frontend/src/App.tsx", "game_core/__init__.py"],
                        "proof": _pytest_proof(
                            ["python", "-m", "pytest", "tests", "-q"],
                        ),
                    }
                ]
            },
    )

    assert result.startswith("OK:")


def test_propose_phase_plan_rejects_new_root_python_file_with_existing_src(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    (ws_root / "src" / "calculator").mkdir(parents=True)
    (ws_root / "src" / "calculator" / "__init__.py").write_text("", encoding="utf-8")
    (ws_root / "pyproject.toml").write_text(
        "[project]\nname = \"calculator\"\n",
        encoding="utf-8",
    )
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "entrypoint",
                    "files_to_create": ["main.py"],
                    "proof": {
                        "execution": {
                            "kind": "command",
                            "command": ["python", "-c", "print('ok')"],
                            "shell": False,
                        },
                        "oracle": {
                            "oracle_type": "build",
                            "required_properties": ["module_imports"],
                        },
                        "scope": {
                            "files_under_test": ["main.py"],
                            "changed_files_expected": ["main.py"],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                        "required_capabilities": ["python"],
                    },
                }
            ]
        },
    )

    assert result.startswith("ERROR:"), result
    assert "creates new top-level Python path" in result
    assert not (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_propose_phase_plan_rejects_desktop_runtime_without_cleanup(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    ws_root.mkdir(parents=True)
    (ws_root / "workspace.toml").write_text(
        "[policies]\ngreenfield_python_src_layout = true\n",
        encoding="utf-8",
    )
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "capability_declaration.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "status": "submitted",
                "capabilities": {
                    "python": {"available": True, "source": "probe"},
                    "subprocess": {"available": True, "source": "probe"},
                    "desktop_gui_runtime": {
                        "available": True,
                        "source": "platform_builtin",
                    },
                },
                "probe_audit": {"python": True, "subprocess": True},
                "notes": "Python subprocess and desktop GUI runtime are available.",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "runtime",
                    "files_to_create": ["src/app/main.py"],
                    "proof": {
                        "execution": {
                            "kind": "command",
                            "command": ["python", "src/app/main.py"],
                            "shell": False,
                        },
                        "oracle": {
                            "oracle_type": "build",
                            "required_properties": ["runtime_started"],
                        },
                        "scope": {
                            "files_under_test": ["src/app/main.py"],
                            "changed_files_expected": ["src/app/main.py"],
                        },
                        "anti_gaming": {"requires_real_runtime": True},
                        "harness_profile": "desktop_gui_runtime",
                        "harness_options": {
                            "managed_runtime": True,
                            "readiness": {"type": "process_alive"},
                        },
                        "required_capabilities": [
                            "python",
                            "subprocess",
                            "desktop_gui_runtime",
                        ],
                    },
                }
            ]
        },
    )

    assert result.startswith("ERROR:"), result
    assert "must include cleanup instructions" in result
    assert not (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_propose_phase_plan_rejects_scaffold_subtask_even_when_other_subtasks_repair(
    tmp_path,
):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    (ws_root / "frontend" / "src").mkdir(parents=True)
    (ws_root / "frontend" / "src" / "App.tsx").write_text(
        "export default function App(){return null}\n",
        encoding="utf-8",
    )
    (ws_root / "game_core").mkdir()
    (ws_root / "game_core" / "__init__.py").write_text("", encoding="utf-8")
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "extend_existing_game_core",
                    "title": "Extend existing game_core models",
                    "success_test": "pytest tests/test_game_models.py",
                },
                {
                    "id": "frontend_setup",
                    "title": "Frontend Project Setup with Vite React TypeScript",
                    "success_test": "cd frontend && npm run build",
                },
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "subtask `frontend_setup` proposes setup/scaffold" in result


def test_propose_phase_plan_validates_phases_as_work_items(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    (ws_root / "frontend" / "src").mkdir(parents=True)
    (ws_root / "frontend" / "src" / "App.tsx").write_text(
        "export default function App(){return null}\n",
        encoding="utf-8",
    )
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "phases": [
                {
                    "id": "frontend_setup",
                    "title": "Frontend Project Setup with Vite React TypeScript",
                    "success_test": "cd frontend && npm run build",
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "subtask `frontend_setup` proposes setup/scaffold" in result


def test_propose_phase_plan_validates_ordered_subtasks_as_work_items(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    (ws_root / "frontend" / "src").mkdir(parents=True)
    (ws_root / "frontend" / "src" / "App.tsx").write_text(
        "export default function App(){return null}\n",
        encoding="utf-8",
    )
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "ordered_subtasks": [
                {
                    "id": "frontend_setup",
                    "title": "Frontend Project Setup with Vite React TypeScript",
                    "success_test": "cd frontend && npm run build",
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "subtask `frontend_setup` proposes setup/scaffold" in result


def test_propose_phase_plan_validates_nested_phase_leaf_subtasks(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    (ws_root / "frontend" / "src").mkdir(parents=True)
    (ws_root / "frontend" / "src" / "App.tsx").write_text(
        "export default function App(){return null}\n",
        encoding="utf-8",
    )
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "phases": [
                {
                    "id": "frontend_phase",
                    "title": "Frontend umbrella phase",
                    "subtasks": [
                        {
                            "id": "frontend_setup",
                            "title": "Frontend Project Setup with Vite React TypeScript",
                            "success_test": "cd frontend && npm run build",
                        }
                    ],
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "subtask `frontend_setup` proposes setup/scaffold" in result


def test_propose_phase_plan_rejects_user_report_success_test(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    ws_root.mkdir(parents=True)
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "manual_play",
                    "title": "Manual play test",
                    "success_test": (
                        "Manual 10-turn gameplay session completes and "
                        "user reports game is playable"
                    ),
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "manual_play" in result
    assert "non-automatable success_test" in result


def test_propose_phase_plan_rejects_manual_browser_success_test(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    ws_root.mkdir(parents=True)
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "ui_check",
                    "success_test": (
                        "Manual verification: load http://localhost:8080 in "
                        "browser and create a game"
                    ),
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "ui_check" in result
    assert "non-automatable success_test" in result


def test_propose_phase_plan_rejects_descriptive_browser_observation_success_test(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    ws_root.mkdir(parents=True)
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "manual_e2e_verification",
                    "success_test": (
                        "Server starts cleanly; browser opens to localhost:5173; "
                        "human player completes 3 turns with AI responses visible; "
                        "browser console has zero errors; WebSocket messages show "
                        "in network inspector"
                    ),
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "manual_e2e_verification" in result
    assert "non-automatable success_test" in result


def test_propose_phase_plan_rejects_vague_documentation_success_test(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    ws_root.mkdir(parents=True)
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "diagnose_contracts",
                    "title": "Diagnose contracts",
                    "success_test": "Documentation of actual signatures and exports",
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "diagnose_contracts" in result
    assert "non-automatable success_test" in result


def test_propose_phase_plan_rejects_nonexistent_read_file_reference(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    ws_root.mkdir(parents=True)
    (ws_root / "main.py").write_text("print('ok')\n", encoding="utf-8")
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "subtasks": [
                {
                    "id": "bad_path",
                    "title": "Inspect wrong path",
                    "files_to_read": ["game_core/game_engine.py"],
                    "success_test": "python -m pytest tests -q",
                }
            ]
        },
    )

    assert result.startswith("ERROR:")
    assert "non-existent file `game_core/game_engine.py`" in result


def test_propose_phase_plan_allows_new_impl_root_with_explicit_migration(tmp_path):
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    (ws_root / "game_core").mkdir(parents=True)
    (ws_root / "game_core" / "__init__.py").write_text("", encoding="utf-8")
    (ws_root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    (tmp_path / "umbrella").mkdir()

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {"active_workspace_id": "ws1", "phase_label": "plan"}

    result = _propose_phase_plan(
        ctx,
        plan={
            "description": (
                "Migrate existing game_core and main.py into backend, then "
                "remove obsolete duplicate code after tests pass."
            ),
            "subtasks": [
                    {
                        "id": "backend",
                        "deliverables": ["backend/api.py", "backend/bots/gmas_config.py"],
                        "proof": _pytest_proof(
                            ["python", "-m", "pytest", "tests", "-q"],
                        ),
                    }
                ],
            },
    )

    assert result.startswith("OK:"), result
    assert (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_submit_phase_plan_defaults_to_latest_proposal(tmp_path, monkeypatch):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from ouroboros.tools.phase_control import _submit_phase_plan
    from umbrella.memory.palace import facade
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    (tmp_path / "umbrella").mkdir()

    monkeypatch.setattr(facade.MemPalace, "add", lambda self, **kwargs: "memory-id")

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {
        "active_workspace_id": "ws1",
        "phase_label": "plan",
    }

    _propose_phase_plan(
        ctx,
        plan={
            "phase_id": "llm_civ_game_implementation",
            "steps": [
                    {
                        "id": "build",
                        "title": "Build",
                        "proof": _pytest_proof(
                            ["python", "-m", "pytest", "tests", "-q"],
                        ),
                    }
                ],
            },
        notes="ready",
    )
    result = _submit_phase_plan(ctx)

    assert "llm_civ_game_implementation" in result
    signal = json.loads(
        (drive / "state" / "phase_control_signal.json").read_text(encoding="utf-8")
    )
    assert signal["kind"] == "submit_phase_plan"
    assert signal["payload"]["plan_id"] == "llm_civ_game_implementation"


def test_submit_phase_plan_invalidates_stale_plan_review_and_downstream(
    tmp_path, monkeypatch
):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from ouroboros.tools.phase_control import _submit_phase_plan
    from umbrella.memory.palace import facade
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    monkeypatch.setattr(facade.MemPalace, "add", lambda self, **kwargs: "memory-id")

    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "plan_id": "p1",
                "workspace_id": "ws1",
                "run_id": "run-1",
                "version": 7,
                "nodes": [
                    {"id": "plan", "manifest_id": "plan", "status": "running"},
                    {
                        "id": "plan_review",
                        "manifest_id": "plan_review",
                        "status": "done",
                        "started_at": 1,
                        "ended_at": 2,
                        "overlay": {"old": True},
                    },
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "started_at": 3,
                        "ended_at": None,
                    },
                    {
                        "id": "final_review",
                        "manifest_id": "final_review",
                        "status": "done",
                        "started_at": 4,
                        "ended_at": 5,
                    },
                    {
                        "id": "verify",
                        "manifest_id": "verify",
                        "status": "done",
                        "started_at": 6,
                        "ended_at": 7,
                    },
                ],
                "edits_log": [],
            }
        )
    )

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {
        "active_workspace_id": "ws1",
        "phase_label": "plan",
    }

    _propose_phase_plan(
        ctx,
        plan={
            "phase_id": "revised_plan",
            "steps": [
                    {
                        "id": "build",
                        "title": "Build",
                        "proof": _pytest_proof(
                            ["python", "-m", "pytest", "tests", "-q"],
                        ),
                    }
                ],
            },
        notes="ready",
    )
    result = _submit_phase_plan(ctx)

    assert result.startswith("OK:"), result
    updated = json.loads((state / "phase_plan.json").read_text(encoding="utf-8"))
    by_id = {node["id"]: node for node in updated["nodes"]}
    for node_id in ("plan_review", "execute", "final_review", "verify"):
        assert by_id[node_id]["status"] == "pending"
        assert by_id[node_id].get("started_at") is None
        assert by_id[node_id].get("ended_at") is None
    assert by_id["plan_review"].get("overlay") == {}
    assert any(
        "invalidate_downstream_review_for_plan_id" in edit.get("patch", {})
        for edit in updated["edits_log"]
    )


def test_mark_subtask_complete_blocks_after_run_cancel(tmp_path):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _mark_subtask_complete
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    state.mkdir(parents=True)
    (state / "phase_plan.json").write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "execute",
                        "manifest_id": "execute",
                        "status": "running",
                        "subtasks": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (state / "stop_requested.json").write_text(
        json.dumps({"run_id": "run-stop", "reason": "cancel"}),
        encoding="utf-8",
    )

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-stop:execute"
    ctx.loop_state_view = {"phase_label": "execute"}

    result = _mark_subtask_complete(ctx, summary="done")

    assert result.startswith("ERROR: stop_requested")
    assert not (state / "phase_control_signal.json").exists()


def test_phase_completion_tracks_missing_required_tools_from_trace():
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import (
        _accepted_terminating_tools_from_trace,
        _missing_terminating_tools_from_trace,
    )

    terminating = frozenset({"propose_phase_plan", "submit_phase_plan"})
    trace = [
        {
            "tool": "propose_phase_plan",
            "result": "OK: phase plan proposal recorded (plan_id: demo)",
        }
    ]

    assert _accepted_terminating_tools_from_trace(trace, terminating) == {
        "propose_phase_plan"
    }
    assert _missing_terminating_tools_from_trace(trace, terminating) == {
        "submit_phase_plan"
    }


def test_accepted_phase_completion_returns_non_empty_final(tmp_path):
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.loop import _handle_phase_tail_after_tool_round

    state = SimpleNamespace(
        llm_trace={
            "tool_calls": [
                {
                    "tool": "submit_micro_review",
                    "result": "OK: Micro-review submitted: ok (signal: s1)",
                }
            ]
        },
        accumulated_usage={},
        last_text="",
    )
    tool_calls = [{"function": {"name": "submit_micro_review"}}]

    result = _handle_phase_tail_after_tool_round(
        state=state,
        tool_calls=tool_calls,
        terminating_tools=frozenset({"submit_micro_review"}),
        messages=[],
        phase_label="plan_review",
        budget_remaining_usd=None,
        llm=None,
        max_retries=0,
        drive_logs=tmp_path,
        task_id="run:plan_review",
        event_queue=None,
        task_type="phase_run",
        drive_root=None,
        rounds_in_phase=1,
        completion_prerequisites=(),
    )

    assert result is not None
    final, reason = result
    assert reason == "terminated"
    assert final is not None
    assert final[0] == "OK: Accepted phase-completion tool(s): submit_micro_review"
    assert state.last_text == final[0]


def test_propose_phase_plan_rejects_json_string_payload(tmp_path, monkeypatch):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from umbrella.memory.palace import facade
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    monkeypatch.setattr(facade.MemPalace, "add", lambda self, **kwargs: "memory-id")

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {
        "active_workspace_id": "ws1",
        "phase_label": "plan",
    }

    result = _propose_phase_plan(
        ctx,
        plan=(
            '{"phases": [{"id": "execute", "title": "Build", '
            '"success_test": "python -m pytest tests -q"}]}'
        ),
        notes="json string",
    )

    assert result.startswith("ERROR:")
    assert "`plan` must be a typed object" in result
    assert not (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_propose_phase_plan_rejects_unparseable_string_without_work_items(
    tmp_path, monkeypatch
):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from umbrella.memory.palace import facade
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    monkeypatch.setattr(facade.MemPalace, "add", lambda self, **kwargs: "memory-id")

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {
        "active_workspace_id": "ws1",
        "phase_label": "plan",
    }

    result = _propose_phase_plan(
        ctx,
        plan='{"phase_id": "demo", "subtasks": [',
        notes="bad string",
    )

    assert result.startswith("ERROR:")
    assert "`plan` must be a typed object" in result
    assert not (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_submit_phase_plan_rejects_malformed_legacy_proposal(tmp_path):
    import sys, json, time
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_control import _submit_phase_plan
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    state = drive / "state"
    logs = drive / "logs"
    state.mkdir(parents=True)
    _write_basic_capability_declaration(state)
    logs.mkdir(parents=True)
    (logs / "tools.jsonl").write_text("", encoding="utf-8")
    payload = {
        "created_at": time.time(),
        "task_id": "run-1:plan",
        "workspace_id": "ws1",
        "run_id": "run-1",
        "plan_id": "bad-plan",
        "plan": {"text": '{"phase_id": "demo", "subtasks": ['},
        "notes": "Payload warning: plan was a string and JSON parsing failed",
    }
    (state / "phase_plan_proposal_latest.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    (state / "phase_plan_proposals.jsonl").write_text(
        json.dumps(payload) + "\n",
        encoding="utf-8",
    )

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {
        "active_workspace_id": "ws1",
        "phase_label": "plan",
    }

    result = _submit_phase_plan(ctx)

    assert result.startswith("ERROR:")
    assert "missing_plan_subtasks" in result


def test_propose_phase_plan_rejects_yamlish_object_string(tmp_path, monkeypatch):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _propose_phase_plan
    from umbrella.memory.palace import facade
    from unittest.mock import MagicMock

    ws_root = tmp_path / "workspaces" / "ws1"
    drive = ws_root / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (tmp_path / "umbrella").mkdir()

    monkeypatch.setattr(facade.MemPalace, "add", lambda self, **kwargs: "memory-id")

    ctx = MagicMock()
    ctx.drive_root = drive
    ctx.host_repo_root = tmp_path
    ctx.repo_dir = tmp_path
    ctx.task_id = "run-1:plan"
    ctx.loop_state_view = {
        "active_workspace_id": "ws1",
        "phase_label": "plan",
    }

    result = _propose_phase_plan(
        ctx,
        plan="""
phases:
  - id: execute
    title: Build
    success_test: python -m pytest tests -q
coordinates:
  tech_stack: [python, react]
  estimated_effort: 3 days
""",
        notes="yamlish string",
    )

    assert result.startswith("ERROR:")
    assert "`plan` must be a typed object" in result
    assert not (drive / "state" / "phase_plan_proposal_latest.json").exists()


def test_env_check_accepts_ouroboros_llm_key_alias(tmp_path, monkeypatch):
    import sys, json
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    from ouroboros.tools.phase_contract import _env_check
    from unittest.mock import MagicMock

    for key in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "LLM_MODEL",
        "OUROBOROS_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OUROBOROS_LLM_API_KEY", "secret")
    monkeypatch.setenv("OUROBOROS_MODEL", "model")

    ctx = MagicMock()
    ctx.repo_dir = tmp_path
    ctx.host_repo_root = tmp_path
    ctx.drive_root = tmp_path / ".memory" / "drive"

    payload = json.loads(_env_check(ctx))

    assert payload["status"] == "ok"
    assert payload["llm_provider_ready"] is True
    assert payload["env_present"]["LLM_API_KEY"] is False
    assert payload["env_present"]["OUROBOROS_LLM_API_KEY"] is True
    assert payload["accepted_api_key_vars"][:2] == [
        "LLM_API_KEY",
        "OUROBOROS_LLM_API_KEY",
    ]
    assert payload["accepted_model_vars"] == ["LLM_MODEL", "OUROBOROS_MODEL"]
    assert payload["advisories"]
    advisory_text = "\n".join(payload["advisories"])
    assert "Generated workspace projects" in advisory_text
    assert "LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL" in advisory_text
    assert "control-plane aliases" in advisory_text
    assert "OUROBOROS_LLM_MODEL" not in advisory_text
    assert "OUROBOROS_LLM_API_KEY/LLM_API_KEY" not in advisory_text
