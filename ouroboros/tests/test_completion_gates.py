"""Unit tests for the planner / subtask / remediation completion gates.

These cover the Tier 1.3 + Tier 3.1 + Tier 3.2 control-flow invariants
that block premature plan/subtask/remediation closure when discovery or
verification evidence is missing. Tests exercise the gate helpers
directly to avoid the cost of spinning up the full loop.
"""

from types import SimpleNamespace


from ouroboros.tools.control import (
    _check_discovery_gate,
    _check_planner_discovery_gate,
    _behavior_evidence_warning,
    _validate_delivery_contract,
    _discovery_plan_completion_warning,
    _check_verify_evidence_gate,
)


def _ctx_with_view(view):
    """Build a minimal ``ToolContext``-like object exposing ``loop_state_view``."""

    return SimpleNamespace(loop_state_view=view)


def test_check_discovery_gate_silent_when_subtask_is_not_domain_unknown():
    ctx = _ctx_with_view({"current_subtask_discovery_calls": 0})
    subtask = SimpleNamespace(tags=["normal"])
    assert _check_discovery_gate(ctx, current_subtask=subtask) == ""


def test_check_discovery_gate_blocks_domain_unknown_with_no_discovery():
    ctx = _ctx_with_view({"current_subtask_discovery_calls": 0})
    subtask = SimpleNamespace(tags=["domain_unknown"])
    msg = _check_discovery_gate(ctx, current_subtask=subtask)
    assert "domain_unknown" in msg
    assert "mark_subtask_complete" in msg


def test_check_discovery_gate_passes_after_any_discovery_call():
    ctx = _ctx_with_view({"current_subtask_discovery_calls": 1})
    subtask = SimpleNamespace(tags=["domain_unknown"])
    assert _check_discovery_gate(ctx, current_subtask=subtask) == ""


def test_check_discovery_gate_silent_when_view_missing():
    ctx = SimpleNamespace()  # no loop_state_view at all
    subtask = SimpleNamespace(tags=["domain_unknown"])
    assert _check_discovery_gate(ctx, current_subtask=subtask) == ""


def test_planner_discovery_gate_default_on(monkeypatch):
    monkeypatch.delenv("OUROBOROS_REQUIRE_PLANNER_DISCOVERY", raising=False)
    ctx = _ctx_with_view({"planner_discovery_calls": 0, "phase_label": "planner"})
    msg = _check_planner_discovery_gate(ctx)
    assert "propose_task_plan" in msg
    assert "propose_discovery_plan" in msg


def test_planner_discovery_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REQUIRE_PLANNER_DISCOVERY", "0")
    ctx = _ctx_with_view({"planner_discovery_calls": 0, "phase_label": "planner"})
    assert _check_planner_discovery_gate(ctx) == ""


def test_planner_discovery_gate_on_blocks_when_no_discovery(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REQUIRE_PLANNER_DISCOVERY", "1")
    ctx = _ctx_with_view(
        {
            "planner_discovery_calls": 0,
            "phase_label": "planner",
            "discovery_plan_proposed": True,
        }
    )
    msg = _check_planner_discovery_gate(ctx)
    assert "propose_task_plan" in msg
    assert "memory" in msg.lower() or "get_umbrella_memory" in msg


def test_planner_discovery_gate_on_passes_after_discovery(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REQUIRE_PLANNER_DISCOVERY", "1")
    ctx = _ctx_with_view(
        {
            "planner_discovery_calls": 2,
            "phase_label": "planner",
            "discovery_plan_proposed": True,
        }
    )
    assert _check_planner_discovery_gate(ctx) == ""


def test_planner_discovery_gate_on_silent_when_view_missing(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REQUIRE_PLANNER_DISCOVERY", "1")
    ctx = SimpleNamespace()
    assert _check_planner_discovery_gate(ctx) == ""


def test_verify_evidence_gate_silent_when_no_work_yet():
    ctx = _ctx_with_view(
        {
            "last_verify_run_id": "",
            "last_verify_round": -1,
            "last_verify_passed": False,
            "last_verify_failed_count": 0,
            "last_write_round": -1,
            "round_idx": 3,
        }
    )
    assert _check_verify_evidence_gate(ctx, gate_kind="mark_subtask_complete") == ""


def test_verify_evidence_gate_blocks_when_writes_unverified():
    ctx = _ctx_with_view(
        {
            "last_verify_run_id": "",
            "last_verify_round": -1,
            "last_verify_passed": False,
            "last_verify_failed_count": 0,
            "last_write_round": 5,
            "round_idx": 7,
        }
    )
    msg = _check_verify_evidence_gate(ctx, gate_kind="mark_subtask_complete")
    assert "run_workspace_verify" in msg


def test_verify_evidence_gate_blocks_when_verify_is_stale():
    # verify ran in round 4 but writes happened in round 6 — stale.
    ctx = _ctx_with_view(
        {
            "last_verify_run_id": "verify-x-1",
            "last_verify_round": 4,
            "last_verify_passed": True,
            "last_verify_failed_count": 0,
            "last_write_round": 6,
            "round_idx": 7,
        }
    )
    msg = _check_verify_evidence_gate(ctx, gate_kind="mark_subtask_complete")
    lowered = msg.lower()
    assert (
        "rerun" in lowered
        or "after the last" in lowered
        or "workspace was modified" in lowered
    )


def test_verify_evidence_gate_blocks_when_failed_steps_present():
    ctx = _ctx_with_view(
        {
            "last_verify_run_id": "verify-x-2",
            "last_verify_round": 8,
            "last_verify_passed": False,
            "last_verify_failed_count": 2,
            "last_write_round": 5,
            "round_idx": 9,
        }
    )
    msg = _check_verify_evidence_gate(ctx, gate_kind="mark_remediation_complete")
    assert "failed" in msg.lower() or "fix" in msg.lower()


def test_verify_evidence_gate_passes_when_fresh_and_green():
    ctx = _ctx_with_view(
        {
            "last_verify_run_id": "verify-x-3",
            "last_verify_round": 9,
            "last_verify_passed": True,
            "last_verify_failed_count": 0,
            "last_write_round": 7,
            "round_idx": 10,
        }
    )
    assert _check_verify_evidence_gate(ctx, gate_kind="mark_remediation_complete") == ""


def test_discovery_plan_completion_warns_when_declared_source_unused():
    ctx = _ctx_with_view(
        {
            "phase_label": "subtask_1",
            "discovery_plan": {
                "phases": [
                    {"phase": "subtask", "sources": ["github", "mcp"], "max_calls": 2}
                ]
            },
            "subtask_discovery_calls_by_tool": {"github_project_search": 1},
        }
    )

    msg = _discovery_plan_completion_warning(ctx)
    assert "declared_discovery_not_used" in msg
    assert "mcp_discover" in msg


def test_discovery_plan_completion_silent_when_declared_source_used():
    ctx = _ctx_with_view(
        {
            "phase_label": "subtask_1",
            "discovery_plan": {
                "phases": [{"phase": "subtask", "sources": ["web"], "max_calls": 1}]
            },
            "subtask_discovery_calls_by_tool": {"deep_search": 1},
        }
    )

    assert _discovery_plan_completion_warning(ctx) == ""


def test_discovery_plan_completion_accepts_mcp_install_as_mcp_action():
    ctx = _ctx_with_view(
        {
            "phase_label": "subtask_1",
            "discovery_plan": {
                "phases": [{"phase": "subtask", "sources": ["mcp"], "max_calls": 1}]
            },
            "subtask_discovery_calls_by_tool": {"mcp_install": 1},
        }
    )

    assert _discovery_plan_completion_warning(ctx) == ""


def test_delivery_contract_rejects_import_only_proof():
    msg = _validate_delivery_contract(
        {
            "outcome": "user can run the CLI",
            "proof": "python -c 'import app; inspect.signature(app.main)'",
        }
    )

    assert "import/compile/signature-only" in msg


def test_delivery_contract_accepts_behavior_smoke_proof():
    msg = _validate_delivery_contract(
        {
            "outcome": "user can run the CLI",
            "proof": "acceptance_command: python -m app --input demo --output out.json",
            "expected_result": "out.json is created",
        }
    )

    assert msg == ""


def test_behavior_evidence_warning_blocks_import_only_implementation():
    cur = SimpleNamespace(
        title="Implement CLI",
        description="Build the runnable command",
        success_check="CLI exists",
    )

    msg = _behavior_evidence_warning(
        cur,
        evidence_text="module imports; inspect.signature(main)",
        summary="Implemented CLI imports.",
    )

    assert "missing_behavior_evidence" in msg


def test_behavior_evidence_warning_accepts_smoke_artifact_evidence():
    cur = SimpleNamespace(
        title="Implement CLI",
        description="Build the runnable command",
        success_check="CLI exists",
    )

    msg = _behavior_evidence_warning(
        cur,
        evidence_text="acceptance_command: python -m app --input demo --output out.json exit_code=0; created out.json",
        summary="Smoke command passed.",
    )

    assert msg == ""
