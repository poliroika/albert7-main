from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


ACTIVE_RUNTIME_FILES = [
    "umbrella/deep_agent_tools/phase_control_actions.py",
    "umbrella/deep_agent_tools/phase_control_base.py",
    "umbrella/deep_agent_tools/phase_control_retry.py",
    "umbrella/deep_agent_tools/phase_control_tools.py",
    "umbrella/deep_agent_tools/phase_contract_handlers.py",
    "umbrella/orchestrator/runner.py",
    "umbrella/orchestrator/worker.py",
    "ouroboros/ouroboros/loop.py",
]


def test_phase_control_legacy_not_imported_by_runtime() -> None:
    for rel in ACTIVE_RUNTIME_FILES:
        assert "phase_control_legacy" not in _read(rel), rel


def test_typed_control_plane_laws_are_documented() -> None:
    text = _read("docs/typed-control-plane.md")
    for law in (
        "No Text As Control State",
        "LLM Output Is Proposal",
        "One Owner Per Artifact",
        "RecoveryDecision Is Control Plane",
        "Memory Is Advisory",
        "Review Comments Are Diagnostics",
        "Regex Is Parser Or Lint Only",
        "ProofSpec Owns Completion",
        "Skills Are Not Domains",
        "Model Runtime Guards Require Typed Context",
    ):
        assert law in text


def test_no_contract_migration_metadata_in_active_plan_mutation() -> None:
    forbidden = (
        "contract_migration_reason",
        "contract_migration_files",
        "contract_migration_id",
        "contract_migration_token",
        "plan_mutation_ticket",
    )
    for rel in ACTIVE_RUNTIME_FILES:
        text = _read(rel)
        for marker in forbidden:
            assert marker not in text, f"{marker} leaked into {rel}"


def test_no_regex_controls_recovery_route() -> None:
    text = _read("umbrella/deep_agent_tools/phase_control_retry.py")
    lint_start = text.index("def _bad_generated_success_test_text_lints")
    patch_start = text.index("def _plan_revision_patch_from_typed_contract_issues")
    lint_block = text[lint_start:patch_start]
    assert "loop_back_target" not in lint_block
    assert "requires_plan_mutation" not in lint_block
    assert "RecoveryDecision" not in lint_block


def test_free_text_required_plan_changes_are_not_blocking_control() -> None:
    handlers = _read("umbrella/deep_agent_tools/phase_contract_handlers.py")
    policy = _read("umbrella/deep_agent_tools/phase_contract_policy.py")
    assert "def _plan_revision_contract_issues" not in handlers
    assert "review revision appears unaddressed" not in handlers
    assert "missing keyword(s)" not in handlers
    revision_start = policy.index("def _phase_plan_revision_contract_issues")
    revision_fn = policy[revision_start:policy.index("__all__", revision_start)]
    assert "_typed_revision_compliance_issues" in revision_fn
    assert "_phase_plan_revision_items" not in revision_fn
    assert "review revision appears unaddressed" not in revision_fn
    assert "missing keyword(s)" not in revision_fn


def test_plan_revision_route_requires_real_contract_delta_path() -> None:
    retry = _read("umbrella/deep_agent_tools/phase_control_retry.py")
    assert "def _is_plan_revision_delta_path" in retry
    assert "exceptions_for_missing_conftest_fix" not in retry


def test_internal_route_not_dashboard_stop() -> None:
    runner = _read("umbrella/orchestrator/runner.py")
    assert "_write_task_scoped_stop_request" not in runner
    assert '"internal_recovery_route": True' not in runner


def test_prompt_tool_surface_note_matches_active_schemas() -> None:
    loop = _read("ouroboros/ouroboros/loop.py")
    assert "pre-loaded" not in loop
    assert "Active tool schemas this round" in loop


def test_review_artifact_single_authority() -> None:
    actions = _read("umbrella/deep_agent_tools/phase_control_actions.py")
    assert "source_path=\".memory/drive/state/phase_control_signals.jsonl\"" not in actions
    assert "phase_control_signal.json#" in actions


def test_prompt_tool_surface_matches_phase_manifest_for_light_review() -> None:
    from umbrella.orchestrator.worker import _effective_phase_allowed_tools

    manifest = SimpleNamespace(
        id="research_review",
        allowed_tools=[
            "read_file",
            "submit_micro_review",
            "get_gmas_context",
            "search_gmas_knowledge",
        ],
        forbidden_tools=[],
    )

    allowed = _effective_phase_allowed_tools(
        manifest,
        active_subtask=None,
        gmas_prewrite_required=False,
        research_depth="light",
        subtask_memory_scope_payload=None,
        palace_rules=[],
    )

    assert "get_gmas_context" not in allowed
    assert "search_gmas_knowledge" not in allowed
