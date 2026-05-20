import tempfile
from pathlib import Path

from umbrella.control_plane import (
    ActionType,
    DecisionContext,
    ManagerPhase,
    ManagerState,
    TaskBrief,
    TaskClass,
    PromptRiskLevel,
    apply_prompt_patch,
    create_human_checkpoint_request,
    decide_next_action,
    get_prompt_surface,
    identify_prompt_surfaces,
    propose_prompt_patch,
    record_human_checkpoint_decision,
    record_prompt_version,
    render_prompt_diff,
    requires_human_checkpoint,
    resume_after_human_checkpoint,
    should_patch_manager,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _decision_context(task_id: str = "prompt_task") -> DecisionContext:
    brief = TaskBrief(
        task_id=task_id,
        original_input="Improve manager prompt behavior",
        task_class=TaskClass.SYSTEM_DESIGN,
        summary="Improve manager prompt behavior",
    )
    state = ManagerState(task_id=task_id)
    return DecisionContext(
        task_id=task_id,
        task_brief=brief,
        manager_state=state,
        workspace_id="agent_research",
    )


def test_identify_prompt_surfaces_includes_required_manager_files():
    repo_root = _repo_root()
    surfaces = identify_prompt_surfaces(repo_root)
    surface_ids = {surface.id for surface in surfaces}

    assert "ouroboros_system_prompt" in surface_ids
    assert "ouroboros_bible" in surface_ids
    assert "ouroboros_context_assembly" in surface_ids
    assert "ouroboros_task_planner_prompts" in surface_ids
    assert "umbrella_delivery_critic" in surface_ids
    assert "umbrella_workspace_task_wrapper" in surface_ids
    assert "umbrella_human_gate_policy" in surface_ids


def test_risky_system_prompt_change_requires_human_checkpoint():
    repo_root = _repo_root()
    surface = get_prompt_surface(
        surface_id="ouroboros_system_prompt", repo_root=repo_root
    )
    current_text = (repo_root / surface.path).read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmpdir:
        proposal = propose_prompt_patch(
            surface,
            repo_root=repo_root,
            version_store_dir=Path(tmpdir) / "versions",
            task_id="task_prompt_risk",
            rationale="Broaden top-level manager behavior after repeated routing failures",
            expected_behavioral_effect="Change global manager behavior so it can choose a different default strategy",
            evidence=["Repeated prompt-level routing errors across runs"],
            proposed_content=current_text + "\n\nAdditional global instruction.\n",
        )

    assert proposal.risk_level == PromptRiskLevel.HIGH_FOUNDATIONAL_CHANGE
    assert requires_human_checkpoint(proposal) is True
    assert proposal.base_version_id is not None
    assert proposal.candidate_version_id is not None


def test_prompt_versions_are_recorded_with_snapshot_files():
    repo_root = _repo_root()
    surface = get_prompt_surface(
        surface_id="ouroboros_context_assembly", repo_root=repo_root
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        record = record_prompt_version(
            surface,
            Path(tmpdir) / "versions",
            repo_root=repo_root,
            task_id="task_versioning",
            label="baseline",
        )

        assert record.surface_id == surface.id
        assert record.snapshot_path.exists()
        assert record.content_hash


def test_render_prompt_diff_shows_line_changes():
    diff_text = render_prompt_diff(
        "line one\nline two\n",
        "line one\nline two changed\n",
        surface_label="test_prompt",
    )

    assert "--- test_prompt:before" in diff_text
    assert "+++ test_prompt:after" in diff_text
    assert "-line two" in diff_text
    assert "+line two changed" in diff_text


def test_prompt_rewrite_is_selected_only_with_prompt_evidence():
    context = _decision_context("task_prompt_choice")
    context.no_progress_iterations = 6
    context.active_gaps = 2
    context.prompt_gap_signals = [
        "SYSTEM.md keeps biasing the manager toward self-patching before workspace iteration"
    ]

    decision = should_patch_manager(context)
    assert decision.action.action_type == ActionType.REWRITE_PROMPT_STACK

    no_prompt_context = _decision_context("task_general_self_improve")
    no_prompt_context.no_progress_iterations = 6
    no_prompt_context.active_gaps = 2

    no_prompt_decision = should_patch_manager(no_prompt_context)
    assert no_prompt_decision.action.action_type == ActionType.SELF_IMPROVE


def test_decide_next_action_can_route_to_prompt_rewrite_when_prompt_gap_is_detected():
    context = _decision_context("task_prompt_route")
    context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE
    context.last_run_outcome = "partial"
    context.no_progress_iterations = 6
    context.active_gaps = 1
    context.prompt_gap_signals = [
        "Prompt routing failed across repeated manager iterations"
    ]

    action = decide_next_action(context)

    assert action.action_type == ActionType.REWRITE_PROMPT_STACK


def test_apply_prompt_patch_writes_to_file_and_creates_version_record():
    """Test that apply_prompt_patch actually writes content to file."""
    repo_root = _repo_root()
    surface = get_prompt_surface(
        surface_id="ouroboros_context_assembly", repo_root=repo_root
    )
    original_text = (repo_root / surface.path).read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Create a proposal with modified content
        modified_text = original_text + "\n# TEST MODIFICATION\n"
        proposal = propose_prompt_patch(
            surface,
            repo_root=repo_root,
            version_store_dir=tmp_path / "versions",
            task_id="task_apply_patch",
            rationale="Test modification",
            expected_behavioral_effect="Verify apply works",
            proposed_content=modified_text,
        )

        # Apply the patch
        version_record = apply_prompt_patch(
            proposal,
            repo_root,
            tmp_path / "versions",
        )

        # Verify the file was modified
        current_text = (repo_root / surface.path).read_text(encoding="utf-8")
        assert "# TEST MODIFICATION" in current_text

        # Verify version record was created
        assert version_record.surface_id == surface.id
        assert version_record.label == "applied"
        assert version_record.snapshot_path.exists()

        # Restore original content
        (repo_root / surface.path).write_text(original_text, encoding="utf-8")
