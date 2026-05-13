"""
Tests for control plane module.

Covers:
- Task classification
- Workspace-first routing (default: patch workspace, not manager)
- Self-improvement gate rejection without evidence
- Escalation for blocking constraints
- Decision tracing and explainability
- State machine transitions
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from umbrella.control_plane.models import (
    TaskBrief,
    TaskClass,
    ManagerState,
    ManagerPhase,
    DecisionContext,
    NextAction,
    ActionType,
    DecisionRecord,
    DecisionRationale,
    EscalationReason,
    EscalationStatus,
)
from umbrella.control_plane.state import ManagerStateMachine
from umbrella.control_plane.decision_policy import (
    classify_task,
    select_seed_workspace,
    decide_next_action,
)
from umbrella.control_plane.self_improvement import (
    SelfImprovementGate,
    check_self_improvement_eligibility,
    prepare_self_improvement,
    execute_self_improvement,
)
from umbrella.control_plane.escalation import (
    BlockingConstraints,
    HumanEscalation,
    check_blocking_constraints,
    escalate_to_human,
    get_pending_escalations,
)
from umbrella.control_plane.tracing import (
    DecisionTrace,
    TraceManager,
    trace_decision,
    get_decision_history,
)
from umbrella.control_plane.engine import ControlPlaneEngine, create_engine
from umbrella.control_plane.task_updates import queue_runtime_task_update
from umbrella.evals.models import (
    EvaluationRecord,
    OutputQualityRating,
    StabilityRating,
    TaskSuccessRating,
)
from umbrella.evals.comparisons import compare_runs
from umbrella.retrieval.models import RetrievalCard, RetrievalHit, HitType, SourceType
from umbrella.control_plane.workspace_patching import WorkspacePatchResult
from umbrella.workspace_runtime.models import (
    ArtifactRef,
    ArtifactType,
    PreparedWorkspace,
    WorkspaceRunResult,
    WorkspaceRunStatus,
)
from umbrella.workspace_registry.models import WorkspaceRef as RegistryWorkspaceRef


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def temp_dir():
    """Temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def task_brief():
    """Standard task brief for testing."""
    return TaskBrief(
        task_id="test_task_1",
        original_input="Implement a feature from the article",
        task_class=TaskClass.CODE_FROM_ARTICLE,
        summary="Implement a feature from the article",
        requirements=[],
        constraints=[],
        success_criteria=[],
    )


@pytest.fixture
def manager_state():
    """Fresh manager state for testing."""
    return ManagerState(task_id="test_task_1")


@pytest.fixture
def decision_context(task_brief, manager_state):
    """Basic decision context for testing."""
    return DecisionContext(
        task_id="test_task_1",
        task_brief=task_brief,
        manager_state=manager_state,
        workspace_id="agent_research",
    )


@pytest.fixture
def mock_registry():
    """Mock workspace registry."""
    registry = MagicMock()
    registry.get_workspace.return_value = MagicMock(id="agent_research")
    registry.get_seed_profile.return_value = None
    registry.select_best.return_value = None
    return registry


@pytest.fixture
def engine(temp_dir, mock_registry, monkeypatch):
    """Control plane engine for testing."""
    from umbrella.control_plane import engine as engine_module

    def fake_prepare(instance):
        return PreparedWorkspace(instance=instance, ready=True, config_valid=True)

    def fake_run(instance, request, prepare=False):
        run_dir = instance.path / "runs" / "fake_run"
        report_path = instance.path / "reports" / "latest_report.md"
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            "# Synthetic Report\n\nWorkspace run completed successfully.\n",
            encoding="utf-8",
        )

        result = WorkspaceRunResult(
            run_id="run_test_001",
            workspace_id=instance.workspace_id,
            task_id=request.task_id,
            status=WorkspaceRunStatus.COMPLETED,
            final_answer="Synthetic success",
            summary="Synthetic success",
            duration_seconds=0.25,
            total_tokens=128,
        )
        result.run_dir = run_dir
        result.artifacts = [
            ArtifactRef(
                artifact_id="report_001",
                artifact_type=ArtifactType.REPORT,
                path=report_path,
                description="Synthetic report",
            )
        ]
        result.metrics["retrieval_context_injected"] = bool(
            request.metadata.get("retrieval_context")
        )
        result.metrics["retrieval_hits_used"] = int(
            request.metadata.get("retrieval_hit_count", 0)
        )
        return result

    def fake_evaluate_run(
        result,
        instance_path,
        task_class=None,
        previous_evals=None,
        repo_root=None,
        min_article_word_count=1500,
        required_artifact_types=None,
        task_input=None,
        **kwargs,
    ):
        return EvaluationRecord(
            task_id=result.task_id or "test_task",
            workspace_id=result.workspace_id,
            run_id=result.run_id,
            instance_path=instance_path,
            task_success=TaskSuccessRating.COMPLETE,
            output_quality=OutputQualityRating.GOOD,
            stability=StabilityRating.STABLE,
            total_tokens=result.total_tokens,
            total_duration_seconds=result.duration_seconds,
            retrieval_was_useful=bool(result.metrics.get("retrieval_context_injected")),
            retrieval_hits_used=int(result.metrics.get("retrieval_hits_used", 0)),
            artifact_count=len(result.artifacts),
            overall_score=0.9,
            evidence=["Synthetic evaluation"],
        )

    monkeypatch.setattr(engine_module, "prepare_instance", fake_prepare)
    monkeypatch.setattr(engine_module, "run_workspace", fake_run)
    monkeypatch.setattr(engine_module, "evaluate_run", fake_evaluate_run)

    engine = ControlPlaneEngine(
        workspace_registry=mock_registry,
        workspaces_root=temp_dir / "workspaces",
        control_state_dir=temp_dir / "control",
    )
    engine.runtime_config.human_review_timeout_seconds = 0
    engine.retrieval = MagicMock()
    engine.retrieval.search.return_value = RetrievalCard(
        query="synthetic query",
        recommended_pattern="Inject retrieval context into the workspace query.",
        key_symbols=["gmas.GraphBuilder"],
        key_files=["gmas/README.md"],
        example_usage=["Use retrieval before execution"],
        hits=[
            RetrievalHit(
                hit_id="hit_001",
                hit_type=HitType.DOCUMENT_CHUNK,
                score=0.95,
                source_id="gmas_readme",
                source_type=SourceType.DOCUMENTATION,
                title="GMAS README",
                excerpt="Use retrieval before execution.",
            )
        ],
        confidence=0.9,
    )
    return engine


def _create_real_instance(
    engine: ControlPlaneEngine,
    task_input: str = "Test task",
    task_id: str = "test_1",
):
    """Run the selection step so later phases have a real instance to work with."""
    task = engine.start_task(task_input, task_id)
    result = engine.process_task_step()
    assert result.outcome == "success"
    assert engine.active_instance is not None
    return task


# =============================================================================
# Task Classification Tests
# =============================================================================


class TestTaskClassification:
    """Tests for task classification."""

    def test_classify_research_task(self):
        """Research tasks are classified correctly."""
        brief = classify_task("Write a research article about AI", "task_1")
        assert brief.task_class == TaskClass.RESEARCH
        assert brief.task_id == "task_1"
        assert "research" in brief.original_input.lower()

    def test_classify_code_from_article_task(self):
        """Code from article tasks are classified correctly."""
        # Code classification comes before research when both keywords present
        brief = classify_task("Implement code from paper about deep learning", "task_2")
        assert brief.task_class == TaskClass.CODE_FROM_ARTICLE

    def test_classify_system_design_task(self):
        """System design tasks are classified correctly."""
        brief = classify_task("Design the architecture for a new system", "task_3")
        assert brief.task_class == TaskClass.SYSTEM_DESIGN

    def test_classify_unknown_task(self):
        """Unknown tasks default to UNKNOWN."""
        brief = classify_task("Do something random", "task_4")
        assert brief.task_class == TaskClass.UNKNOWN

    def test_task_brief_summary_truncation(self):
        """Long summaries are truncated."""
        long_input = "A" * 300
        brief = classify_task(long_input, "task_5")
        assert len(brief.summary) <= 203  # 200 + "..."

    def test_select_seed_workspace_for_research(self):
        """Research tasks get agent_research workspace."""
        brief = TaskBrief(
            task_id="task_1",
            original_input="Research something",
            task_class=TaskClass.RESEARCH,
            summary="Research",
        )
        workspace_id = select_seed_workspace(brief, None)
        assert workspace_id == "agent_research"

    def test_select_seed_workspace_for_code(self):
        """Code tasks also get agent_research workspace."""
        brief = TaskBrief(
            task_id="task_1",
            original_input="Implement from article",
            task_class=TaskClass.CODE_FROM_ARTICLE,
            summary="Code",
        )
        workspace_id = select_seed_workspace(brief, None)
        assert workspace_id == "agent_research"

    def test_select_seed_workspace_uses_registry_result_when_available(self):
        """Selection can target a non-default seed when the registry matches it."""
        brief = TaskBrief(
            task_id="task_2",
            original_input="Implement code from article",
            task_class=TaskClass.CODE_FROM_ARTICLE,
            summary="Code",
        )
        registry = MagicMock()
        registry.select_best.return_value = MagicMock(workspace_id="code_lab")

        workspace_id = select_seed_workspace(brief, registry)

        assert workspace_id == "code_lab"


# =============================================================================
# Workspace-First Routing Tests
# =============================================================================


class TestWorkspaceFirstRouting:
    """Tests for workspace-first decision policy."""

    def test_success_routes_to_lesson_recorded(self, decision_context):
        """Successful runs route to lesson recording."""
        decision_context.last_run_outcome = "success"
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE
        action = decide_next_action(decision_context)
        assert action.action_type == ActionType.RECORD_LESSON

    def test_failure_routes_to_workspace_patch(self, decision_context):
        """Failures route to workspace patch by default."""
        decision_context.last_run_outcome = "failure"
        decision_context.error_signatures = ["tool_error"]
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE
        action = decide_next_action(decision_context)
        assert action.action_type == ActionType.PATCH_WORKSPACE

    def test_workspace_error_patches_workspace_config(self, decision_context):
        """Workspace-level errors patch workspace config."""
        decision_context.last_run_outcome = "failure"
        decision_context.error_signatures = ["tool_not_found"]
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE
        action = decide_next_action(decision_context)
        assert action.action_type == ActionType.PATCH_WORKSPACE
        # Should patch config, not manager

    def test_partial_result_continues_workspace_iteration(self, decision_context):
        """Partial results continue workspace iteration."""
        decision_context.last_run_outcome = "partial"
        decision_context.no_progress_iterations = 1
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE
        action = decide_next_action(decision_context)
        assert action.action_type == ActionType.PATCH_WORKSPACE

    def test_low_quality_success_respects_runtime_threshold(self, decision_context):
        """Low-score runs only get downgraded when the active threshold requires it."""
        decision_context.last_run_outcome = "success"
        decision_context.last_eval_score = 0.44
        decision_context.quality_completion_threshold = 0.80
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE

        action = decide_next_action(decision_context)

        assert action.action_type == ActionType.PATCH_WORKSPACE

    def test_low_quality_success_can_complete_when_threshold_disabled(
        self, decision_context
    ):
        """Disabling the quality gate should keep successful runs on the happy path."""
        decision_context.last_run_outcome = "success"
        decision_context.last_eval_score = 0.44
        decision_context.quality_completion_threshold = 0.0
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE

        action = decide_next_action(decision_context)

        assert action.action_type == ActionType.RECORD_LESSON

    def test_completion_gate_signal_prevents_false_partial_downgrade(
        self, decision_context
    ):
        """Engine-approved completions should not be downgraded again by raw score alone."""
        decision_context.last_run_outcome = "success"
        decision_context.last_eval_score = 0.84
        decision_context.quality_completion_threshold = 0.85
        decision_context.completion_gate_passed = True
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE

        action = decide_next_action(decision_context)

        assert action.action_type == ActionType.RECORD_LESSON

    def test_repeated_partial_considers_manager_intervention(self, decision_context):
        """Repeated partial results may trigger manager intervention."""
        decision_context.last_run_outcome = "partial"
        decision_context.no_progress_iterations = 5
        decision_context.active_gaps = 1
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE
        action = decide_next_action(decision_context)
        # With no progress and active gaps, might consider self-improvement or escalation
        assert action.action_type in (
            ActionType.PATCH_WORKSPACE,
            ActionType.SELF_IMPROVE,
            ActionType.ESCALATE_TO_HUMAN,
        )

    def test_workspace_level_issue_detected(self, decision_context):
        """Workspace-level issues are correctly identified."""
        decision_context.error_signatures = ["file_not_found", "syntax_error"]
        decision_context.last_run_outcome = "failure"
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE
        action = decide_next_action(decision_context)
        assert action.action_type == ActionType.PATCH_WORKSPACE

    def test_manager_patch_not_default_without_evidence(self, decision_context):
        """Manager patches require evidence, not default."""
        decision_context.last_run_outcome = "failure"
        decision_context.no_progress_iterations = 1  # Low iterations
        decision_context.active_gaps = 0  # No gaps
        decision_context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE
        action = decide_next_action(decision_context)
        assert action.action_type == ActionType.PATCH_WORKSPACE
        assert action.action_type != ActionType.SELF_IMPROVE


# =============================================================================
# Self-Improvement Gate Tests
# =============================================================================


class TestSelfImprovementGate:
    """Tests for self-improvement eligibility gate."""

    def test_gate_rejects_without_evidence(self, decision_context):
        """Gate rejects self-improvement without sufficient evidence."""
        decision_context.no_progress_iterations = 0  # Below threshold (2)
        decision_context.active_gaps = 0
        decision_context.cost_so_far_usd = 1.0

        gate = SelfImprovementGate()
        is_eligible, reason = gate.is_eligible(decision_context)

        assert not is_eligible
        assert "Not eligible" in reason

    def test_gate_rejects_low_iterations(self, decision_context):
        """Gate rejects when iterations below threshold."""
        decision_context.no_progress_iterations = 1  # Below 2
        decision_context.active_gaps = 2
        decision_context.cost_so_far_usd = 15.0

        gate = SelfImprovementGate()
        is_eligible, reason = gate.is_eligible(decision_context)

        assert not is_eligible
        assert "iterations without progress" in reason.lower()

    def test_gate_rejects_at_iteration_limit(self, decision_context):
        """Gate rejects when total iterations are at the hard limit."""
        decision_context.no_progress_iterations = 10
        decision_context.total_iterations = 50  # At the limit

        gate = SelfImprovementGate()
        is_eligible, reason = gate.is_eligible(decision_context)

        assert not is_eligible
        assert "at limit" in reason.lower()

    def test_gate_accepts_with_sufficient_evidence(self, decision_context):
        """Gate accepts when iteration threshold met."""
        decision_context.no_progress_iterations = 3  # Above 2
        decision_context.active_gaps = 0  # Gaps no longer required
        decision_context.cost_so_far_usd = 0.0  # Cost no longer a gate

        gate = SelfImprovementGate()
        is_eligible, reason = gate.is_eligible(decision_context)

        assert is_eligible
        assert "Eligible" in reason

    def test_check_eligibility_returns_suggestions(self, decision_context):
        """Eligibility check returns helpful suggestions."""
        decision_context.no_progress_iterations = 0

        is_eligible, reason, suggestions = check_self_improvement_eligibility(
            decision_context
        )

        assert not is_eligible
        assert len(suggestions) > 0
        assert any("iterations" in s.lower() for s in suggestions)

    def test_checkpoint_creation(self, decision_context, temp_dir):
        """Checkpoints are created correctly."""
        checkpoint = prepare_self_improvement(
            decision_context,
            "prompt_optimization",
            "Fix the prompts",
            temp_dir / "checkpoints",
        )

        assert checkpoint.task_id == "test_task_1"
        assert checkpoint.self_improvement_type == "prompt_optimization"
        assert checkpoint.self_improvement_plan == "Fix the prompts"

        # Check file was created
        checkpoint_file = temp_dir / "checkpoints" / f"{checkpoint.id}.json"
        assert checkpoint_file.exists()

    def test_execute_self_improvement_returns_result(self, decision_context):
        """Self-improvement execution returns proper result when repo_root is missing."""
        checkpoint = prepare_self_improvement(
            decision_context,
            "test_type",
            "Test plan",
            Path("/tmp/test_checkpoints"),
        )

        result = execute_self_improvement(checkpoint, "test_type", "Test plan")

        assert result.outcome == "failure"
        assert result.action.action_type == ActionType.SELF_IMPROVE
        assert (
            "no repo_root" in result.summary.lower()
            or "requires" in result.summary.lower()
        )


# =============================================================================
# Escalation Tests
# =============================================================================


class TestEscalation:
    """Tests for human escalation."""

    def test_blocking_constraints_default(self):
        """Default constraints allow autonomous self-improvement but block GM changes."""
        constraints = BlockingConstraints()
        assert not constraints.gm_modification_allowed
        assert not constraints.seed_promotion_allowed
        assert constraints.policy_change_allowed
        assert constraints.self_improve_allowed

    def test_self_improvement_blocked_when_explicitly_disabled(self):
        """Self-improvement blocked when explicitly disabled."""
        constraints = BlockingConstraints(self_improve_allowed=False)
        is_allowed, reason = constraints.check_action_allowed(ActionType.SELF_IMPROVE)
        assert not is_allowed
        assert "not allowed" in reason.lower()

    def test_self_improvement_allowed_by_default(self):
        """Self-improvement allowed by default for autonomous long-running behavior."""
        constraints = BlockingConstraints()
        is_allowed, reason = constraints.check_action_allowed(ActionType.SELF_IMPROVE)
        assert is_allowed
        assert reason is None

    def test_workspace_patch_allowed_by_default(self):
        """Workspace patches are allowed by default."""
        constraints = BlockingConstraints()
        is_allowed, reason = constraints.check_action_allowed(
            ActionType.PATCH_WORKSPACE
        )
        assert is_allowed
        assert reason is None

    def test_check_blocking_constraints_public_api(self, decision_context):
        """Public API for checking blocking constraints allows self-improvement."""
        is_blocked, reasons = check_blocking_constraints(
            decision_context, ActionType.SELF_IMPROVE
        )
        assert not is_blocked
        assert len(reasons) == 0

    def test_escalate_to_human_creates_record(self, decision_context, temp_dir):
        """Escalation creates a proper record."""
        escalation = escalate_to_human(
            temp_dir / "escalations",
            decision_context,
            EscalationReason.POLICY_VIOLATION,
            "Would violate policy constraints",
            ActionType.SELF_IMPROVE,
        )

        assert escalation.task_id == "test_task_1"
        assert escalation.reason == EscalationReason.POLICY_VIOLATION
        assert escalation.status == EscalationStatus.PENDING

        # Check file was created
        escalation_file = temp_dir / "escalations" / f"{escalation.id}.json"
        assert escalation_file.exists()

    def test_get_pending_escalations(self, decision_context, temp_dir):
        """Can retrieve pending escalations."""
        # Create multiple escalations
        escalate_to_human(
            temp_dir / "escalations",
            decision_context,
            EscalationReason.POLICY_VIOLATION,
            "First escalation",
        )
        escalate_to_human(
            temp_dir / "escalations",
            decision_context,
            EscalationReason.SAFETY_CONCERN,
            "Second escalation",
        )

        pending = get_pending_escalations(temp_dir / "escalations", "test_task_1")
        assert len(pending) == 2

    def test_resolve_escalation(self, decision_context, temp_dir):
        """Escalations can be resolved."""
        escalation_mgr = HumanEscalation(temp_dir / "escalations")

        escalation = escalation_mgr.escalate_to_human(
            decision_context,
            EscalationReason.POLICY_VIOLATION,
            "Need human input",
            ActionType.SELF_IMPROVE,
        )

        resolved = escalation_mgr.resolve_escalation(
            escalation.id,
            "Approved to proceed",
            "human",
        )

        assert resolved.status == EscalationStatus.RESOLVED
        assert resolved.human_response == "Approved to proceed"


# =============================================================================
# Decision Tracing Tests
# =============================================================================


class TestDecisionTracing:
    """Tests for decision tracing and explainability."""

    def test_trace_adds_decision(self, decision_context):
        """Decisions are added to trace."""
        trace = DecisionTrace(task_id="test_task_1")

        decision = DecisionRecord(
            id="dec_1",
            task_id="test_task_1",
            context_snapshot=decision_context,
            action=NextAction(
                action_type=ActionType.PATCH_WORKSPACE, description="Patch workspace"
            ),
            rationale=DecisionRationale(
                action_taken=ActionType.PATCH_WORKSPACE,
                reason="Test decision",
                confidence=0.8,
                evidence=[],
                alternatives_considered=[],
                why_not_alternatives={},
            ),
        )

        trace.add_decision(decision)
        assert len(trace.decisions) == 1

    def test_trace_summary(self, decision_context):
        """Trace provides human-readable summary."""
        trace = DecisionTrace(task_id="test_task_1")

        decision = DecisionRecord(
            id="dec_1",
            task_id="test_task_1",
            context_snapshot=decision_context,
            action=NextAction(
                action_type=ActionType.PATCH_WORKSPACE, description="Patch workspace"
            ),
            rationale=DecisionRationale(
                action_taken=ActionType.PATCH_WORKSPACE,
                reason="Test decision",
                confidence=0.8,
                evidence=["evidence1"],
                alternatives_considered=[],
                why_not_alternatives={},
            ),
            created_at=time.time(),
        )
        trace.add_decision(decision)

        summary = trace.summary()
        assert "test_task_1" in summary
        assert "Total decisions: 1" in summary

    def test_trace_manager_persists_trace(self, temp_dir):
        """Trace manager saves and loads traces."""
        manager = TraceManager(temp_dir / "traces")

        trace = manager.get_or_create_trace("task_1")
        trace.summary = lambda: "Test summary"

        manager.save_trace("task_1")

        # Load trace back
        loaded = manager.load_trace("task_1")
        assert loaded is not None
        assert loaded.task_id == "task_1"

    def test_trace_decision_public_api(self, decision_context, temp_dir):
        """Public API for tracing decisions works."""
        decision = DecisionRecord(
            id="dec_1",
            task_id="test_task_1",
            context_snapshot=decision_context,
            action=NextAction(
                action_type=ActionType.PATCH_WORKSPACE, description="Patch workspace"
            ),
            rationale=DecisionRationale(
                action_taken=ActionType.PATCH_WORKSPACE,
                reason="Test",
                confidence=0.8,
                evidence=[],
                alternatives_considered=[],
                why_not_alternatives={},
            ),
        )

        trace_decision(decision, trace_dir=temp_dir / "traces")

        history = get_decision_history("test_task_1", trace_dir=temp_dir / "traces")
        assert len(history) == 1

    def test_explain_decision(self, decision_context, temp_dir):
        """Decisions can be explained in human-readable form."""
        import uuid

        # Use a unique task_id for this test to avoid conflicts
        unique_task_id = f"test_explain_{uuid.uuid4().hex[:8]}"

        # Use the public API to trace the decision
        decision = DecisionRecord(
            id="dec_1",
            task_id=unique_task_id,
            context_snapshot=decision_context,
            action=NextAction(
                action_type=ActionType.PATCH_WORKSPACE, description="Patch workspace"
            ),
            rationale=DecisionRationale(
                action_taken=ActionType.PATCH_WORKSPACE,
                reason="Workspace issue detected",
                confidence=0.85,
                evidence=["Error in tool"],
                alternatives_considered=[ActionType.SELF_IMPROVE],
                why_not_alternatives={
                    ActionType.SELF_IMPROVE.value: "No manager gap evidence"
                },
            ),
            created_at=time.time(),
        )

        # Trace the decision using the public API
        trace_decision(decision, trace_dir=temp_dir / "traces")

        # Get the decision back from history
        history = get_decision_history(unique_task_id, trace_dir=temp_dir / "traces")
        assert len(history) == 1
        assert history[0].rationale.reason == "Workspace issue detected"
        assert history[0].rationale.confidence == 0.85

    def test_finalize_trace_removes_from_memory(self, temp_dir):
        """Finalized traces are removed from memory."""
        manager = TraceManager(temp_dir / "traces")
        trace = manager.get_or_create_trace("task_1")

        manager.finalize_trace("task_1")

        # Should be removed from active traces
        assert "task_1" not in manager._active_traces


# =============================================================================
# State Machine Tests
# =============================================================================


class TestStateMachine:
    """Tests for manager state machine."""

    def test_initial_state(self):
        """State machine starts in TASK_RECEIVED."""
        state = ManagerState(task_id="test_1")
        assert state.phase == ManagerPhase.TASK_RECEIVED

    def test_valid_transition(self):
        """Valid transitions succeed."""
        state = ManagerState(task_id="test_1")
        machine = ManagerStateMachine(state)

        transition = machine.transition_to(
            ManagerPhase.WORKSPACE_SELECTED, reason="Workspace selected"
        )
        assert transition.to_phase == ManagerPhase.WORKSPACE_SELECTED
        assert state.phase == ManagerPhase.WORKSPACE_SELECTED

    def test_invalid_transition_raises(self):
        """Invalid transitions raise ValueError."""
        state = ManagerState(task_id="test_1")
        machine = ManagerStateMachine(state)

        # TASK_RECEIVED cannot directly go to WORKSPACE_RUNNING
        # Only WORKSPACE_SELECTED, TASK_COMPLETE, or TASK_BLOCKED are valid from TASK_RECEIVED
        with pytest.raises(ValueError, match="Invalid transition"):
            machine.transition_to(
                ManagerPhase.WORKSPACE_RUNNING, reason="Cannot jump directly to running"
            )

    def test_iteration_count_increments(self):
        """Iteration count increments on certain transitions."""
        state = ManagerState(task_id="test_1")
        machine = ManagerStateMachine(state)

        initial_count = state.iteration_count
        # INSTANCE_PREPARED increments count
        machine.transition_to(ManagerPhase.WORKSPACE_SELECTED, reason="Test")
        machine.transition_to(ManagerPhase.INSTANCE_PREPARED, reason="Test")

        # INSTANCE_PREPARED should increment count
        assert state.iteration_count >= initial_count + 1

    def test_can_transition_to_check(self):
        """can_transition_to validates transitions."""
        state = ManagerState(task_id="test_1")
        machine = ManagerStateMachine(state)

        assert machine.can_transition_to(ManagerPhase.WORKSPACE_SELECTED)
        assert not machine.can_transition_to(ManagerPhase.INSPECTION_COMPLETE)

    def test_last_decision_storage(self):
        """Last decision is stored in state."""
        state = ManagerState(task_id="test_1")
        action = NextAction(
            action_type=ActionType.PATCH_WORKSPACE, description="Test patch"
        )

        state.last_decision = action
        assert state.last_decision == action
        assert state.last_decision.action_type == ActionType.PATCH_WORKSPACE


# =============================================================================
# Engine Integration Tests
# =============================================================================


class TestControlPlaneEngine:
    """Tests for control plane engine integration."""

    def test_engine_initialization(self, temp_dir, mock_registry):
        """Engine initializes correctly."""
        engine = ControlPlaneEngine(
            workspace_registry=mock_registry,
            workspaces_root=temp_dir / "workspaces",
            control_state_dir=temp_dir / "control",
        )

        assert engine.workspace_registry == mock_registry
        assert engine.workspaces_root == temp_dir / "workspaces"
        assert engine.control_state_dir == temp_dir / "control"

    def test_start_task_creates_task(self, engine):
        """Starting a task creates a task object."""
        task = engine.start_task("Implement a feature", "task_1")

        assert task.id == "task_1"
        assert task.status == "active"
        assert task.state.phase == ManagerPhase.WORKSPACE_SELECTED
        assert task.state.current_workspace_id == "agent_research"

    def test_start_task_auto_generates_id(self, engine):
        """Task ID is auto-generated if not provided."""
        task = engine.start_task("Implement a feature")

        assert task.id.startswith("task_")
        assert task.status == "active"

    def test_process_task_step_no_active_task(self, engine):
        """Processing without active task returns cancelled result."""
        result = engine.process_task_step()

        assert result.outcome == "cancelled"
        assert "No active task" in result.summary

    def test_process_workspace_selected_phase(self, engine):
        """Workspace selected phase transitions to instance prepared."""
        task = engine.start_task("Test task", "test_1")
        assert task.state.phase == ManagerPhase.WORKSPACE_SELECTED
        result = engine.process_task_step()
        assert result.outcome == "success"
        assert engine.active_instance is not None
        assert task.state.phase == ManagerPhase.INSTANCE_PREPARED

    def test_process_instance_prepared_phase(self, engine):
        """Instance prepared phase processes correctly."""
        task = _create_real_instance(engine)
        result = engine.process_task_step()
        assert result.outcome == "success"
        assert engine.last_run_result is not None
        assert engine.last_run_result.status == WorkspaceRunStatus.COMPLETED
        assert task.status == "complete"

    def test_default_run_skips_proactive_gmas_retrieval(self, engine):
        """Legacy proactive GMAS retrieval stays disabled unless the runtime flag enables it."""
        task = _create_real_instance(engine)

        result = engine.process_task_step()

        assert result.outcome == "success"
        assert engine.last_run_result is not None
        assert engine.last_run_result.metrics["retrieval_context_injected"] is False
        assert task.state.retrieval_summary is None
        assert task.state.retrieval_hit_count == 0

    def test_runtime_flag_can_reenable_proactive_gmas_retrieval(self, engine):
        """Legacy proactive retrieval can still be enabled explicitly."""
        engine.runtime_config.auto_retrieve_gmas_context = True
        task = _create_real_instance(engine)

        result = engine.process_task_step()

        assert result.outcome == "success"
        assert engine.last_run_result is not None
        assert engine.last_run_result.metrics["retrieval_context_injected"] is True
        assert (
            task.state.retrieval_summary
            == "Inject retrieval context into the workspace query."
        )
        assert task.state.retrieval_hit_count == 1

    def test_runtime_task_update_is_applied_before_next_phase(self, engine):
        """Human runtime updates should be injected into the task and TASK_MAIN."""
        task = _create_real_instance(engine)
        assert task.state.current_instance_path is not None

        queue_runtime_task_update(
            engine.control_state_dir,
            task.id,
            "Also update the workspace UI and workspaces integration.",
        )

        result = engine.process_task_step(task)
        task_main = (task.state.current_instance_path / "TASK_MAIN.md").read_text(
            encoding="utf-8"
        )

        assert result.outcome == "success"
        assert result.action.action_type == ActionType.UPDATE_TASK_SCOPE
        assert "workspace UI" in task.brief.original_input
        assert task.state.runtime_update_count == 1
        assert "workspace UI" in task_main

    def test_runtime_task_update_retargets_stale_decision_to_rerun(self, engine):
        """A live scope change should prevent execution of an outdated completion decision."""
        task = _create_real_instance(engine)
        task.state.phase = ManagerPhase.DECISION_MADE
        task.state.last_decision = NextAction(
            action_type=ActionType.COMPLETE_TASK,
            description="Task complete",
        )

        queue_runtime_task_update(
            engine.control_state_dir,
            task.id,
            "Actually also update the interface before finishing.",
        )

        result = engine.process_task_step(task)

        assert result.action.action_type == ActionType.UPDATE_TASK_SCOPE
        assert task.state.phase == ManagerPhase.KNOWLEDGE_RETRIEVED
        assert task.state.last_decision is not None
        assert task.state.last_decision.action_type == ActionType.RUN_WORKSPACE

    def test_workspace_run_phase(self, engine):
        """Workspace run phase completes the run."""
        task = _create_real_instance(engine)
        engine.state_machine.transition_to(
            ManagerPhase.KNOWLEDGE_RETRIEVED, reason="Test"
        )
        engine.state_machine.transition_to(
            ManagerPhase.WORKSPACE_RUNNING, reason="Test"
        )
        result = engine.process_task_step()
        assert result.outcome == "success"
        assert engine.last_run_result is not None
        assert engine.last_run_result.status == WorkspaceRunStatus.COMPLETED
        assert task.status == "complete"

    def test_run_complete_phase_makes_decision(self, engine):
        """Run complete phase makes a decision."""
        task = _create_real_instance(engine)
        result = engine.process_task_step()
        assert result.outcome == "success"
        assert task.state.last_decision is not None
        assert task.state.last_decision.action_type == ActionType.RECORD_LESSON

    def test_decision_made_phase_executes_action(self, engine):
        """Decision made phase executes the action."""
        task = _create_real_instance(engine)
        task.state.phase = ManagerPhase.DECISION_MADE
        task.state.last_decision = NextAction(
            action_type=ActionType.RUN_WORKSPACE, description="Run workspace"
        )
        result = engine.process_task_step()
        assert result.outcome == "success"
        assert task.status == "complete"

    def test_complete_task(self, engine):
        """Task completion works correctly."""
        task = _create_real_instance(engine)
        task.state.phase = ManagerPhase.DECISION_MADE
        task.state.last_decision = NextAction(
            action_type=ActionType.COMPLETE_TASK,
            description="Task complete",
        )
        result = engine.process_task_step()
        assert result.outcome == "success"
        assert task.status == "complete"

    def test_lesson_recorded_does_not_complete_on_partial_eval(self, engine):
        """Low-quality/partial evals must re-run instead of completing."""
        task = _create_real_instance(engine)
        task.state.phase = ManagerPhase.LESSON_RECORDED
        engine.last_eval_record = EvaluationRecord(
            task_id=task.id,
            workspace_id="agent_research",
            run_id="run_partial",
            instance_path=task.state.current_instance_path or Path("."),
            task_success=TaskSuccessRating.PARTIAL,
            output_quality=OutputQualityRating.POOR,
            stability=StabilityRating.UNKNOWN,
            total_tokens=10,
            total_duration_seconds=1.0,
            overall_score=0.53,
        )
        engine.last_run_result = WorkspaceRunResult(
            workspace_id="agent_research",
            task_id=task.id,
            status=WorkspaceRunStatus.COMPLETED,
        )

        result = engine.process_task_step()

        assert result.action.action_type == ActionType.RUN_WORKSPACE
        assert task.status == "active"

    def test_lesson_recorded_requires_human_review_before_completion(
        self, engine, monkeypatch
    ):
        """High-quality completion should wait for explicit human approval when review is enabled."""
        task = _create_real_instance(engine)
        task.state.phase = ManagerPhase.LESSON_RECORDED
        engine.runtime_config.human_review_timeout_seconds = 60.0
        engine.last_eval_record = EvaluationRecord(
            task_id=task.id,
            workspace_id="agent_research",
            run_id="run_complete",
            instance_path=task.state.current_instance_path or Path("."),
            task_success=TaskSuccessRating.COMPLETE,
            output_quality=OutputQualityRating.GOOD,
            stability=StabilityRating.UNKNOWN,
            total_tokens=10,
            total_duration_seconds=1.0,
            overall_score=0.91,
        )
        engine.last_run_result = WorkspaceRunResult(
            workspace_id="agent_research",
            task_id=task.id,
            status=WorkspaceRunStatus.COMPLETED,
        )

        class _ReviewResult:
            response = "skip"
            timed_out = False

        monkeypatch.setattr(
            "umbrella.control_plane.terminal_check.TerminalCheckpointAdapter.request_review",
            lambda *args, **kwargs: _ReviewResult(),
        )

        result = engine.process_task_step()

        assert result.outcome == "partial"
        assert result.action.action_type == ActionType.WAIT_FOR_INPUT

    def test_completion_quality_ok_allows_near_threshold_first_success(self, engine):
        """A first successful run should not fail completion only because stability is still unknown."""
        task = _create_real_instance(engine)
        engine.runtime_config.quality_completion_threshold = 0.85
        engine.last_eval_record = EvaluationRecord(
            task_id=task.id,
            workspace_id="agent_research",
            run_id="run_near_threshold",
            instance_path=task.state.current_instance_path or Path("."),
            task_success=TaskSuccessRating.COMPLETE,
            output_quality=OutputQualityRating.GOOD,
            stability=StabilityRating.UNKNOWN,
            total_tokens=10,
            total_duration_seconds=1.0,
            overall_score=0.84,
        )

        quality_ok, reason = engine._completion_quality_ok()

        assert quality_ok is True
        assert reason == ""

    def test_fail_task(self, engine):
        """Task failure works correctly."""
        task = _create_real_instance(engine)
        task.state.phase = ManagerPhase.DECISION_MADE
        task.state.last_decision = NextAction(
            action_type=ActionType.FAIL_TASK,
            description="Task failed",
        )
        result = engine.process_task_step()
        assert result.outcome == "failure"
        assert task.status == "failed"

    def test_escalated_phase_waits_for_input(self, engine):
        """Escalated phase waits for human input."""
        task = engine.start_task("Test task", "test_1")
        # Set up for escalation through proper state flow
        engine.state_machine.transition_to(
            ManagerPhase.INSTANCE_PREPARED, reason="Test"
        )
        engine.state_machine.transition_to(
            ManagerPhase.KNOWLEDGE_RETRIEVED, reason="Test"
        )
        engine.state_machine.transition_to(
            ManagerPhase.WORKSPACE_RUNNING, reason="Test"
        )
        engine.state_machine.transition_to(ManagerPhase.RUN_COMPLETE, reason="Test")
        engine.state_machine.transition_to(
            ManagerPhase.INSPECTION_COMPLETE, reason="Test"
        )
        engine.state_machine.transition_to(
            ManagerPhase.DECISION_MADE, reason="Decision made"
        )
        # Now escalate
        engine.state_machine.transition_to(ManagerPhase.ESCALATED, reason="Escalated")
        result = engine.process_task_step()
        # Should wait for input
        assert result.outcome == "partial"
        assert result.action.action_type == ActionType.WAIT_FOR_INPUT

    def test_create_engine_factory(self, temp_dir, mock_registry):
        """Factory function creates configured engine."""
        engine = create_engine(
            workspace_registry=mock_registry,
            workspaces_root=temp_dir / "workspaces",
            control_state_dir=temp_dir / "control",
        )

        assert isinstance(engine, ControlPlaneEngine)
        assert engine.workspace_registry == mock_registry


# =============================================================================
# End-to-End Workflow Tests
# =============================================================================


class TestEndToEndWorkflows:
    """End-to-end workflow tests."""

    def test_full_task_lifecycle_success(self, engine):
        """Full task lifecycle from start to completion."""
        task = engine.start_task("Write a research article")
        assert task.state.phase == ManagerPhase.WORKSPACE_SELECTED

        result = engine.process_task_step()
        assert result.outcome == "success"
        assert task.status == "active"

        result = engine.process_task_step()
        assert result.outcome == "success"
        assert task.status == "complete"
        assert task.final_artifact_path is not None

    def test_workspace_first_routing_in_action(self, engine):
        """Workspace-first routing is enforced in practice."""
        task = engine.start_task("Implement feature")

        # Build context with failure
        context = engine._build_context(task)
        context.last_run_outcome = "failure"
        context.error_signatures = ["tool_error"]
        context.manager_state.phase = ManagerPhase.INSPECTION_COMPLETE

        action = decide_next_action(context)

        # With default context (no manager gap evidence), should route to workspace patch
        assert action.action_type in (
            ActionType.PATCH_WORKSPACE,
            ActionType.RUN_WORKSPACE,
        )
        assert action.action_type != ActionType.ESCALATE_TO_HUMAN

    def test_self_improvement_gate_in_practice(self, engine):
        """Self-improvement gate requires evidence."""
        task = engine.start_task("Complex task")
        task.state.phase = ManagerPhase.SELF_IMPROVEMENT_PENDING

        result = engine.process_task_step()

        # Should not approve self-improvement without evidence
        assert result.outcome in ("partial", "success")

    def test_self_improvement_records_instance_patch_without_seed_promotion(
        self, engine
    ):
        """Self-improvement should stay local until the normal promotion pipeline approves it."""
        task = _create_real_instance(
            engine, task_input="Tune the workspace prompts", task_id="self_improve_1"
        )
        instance = engine.active_instance
        assert instance is not None
        assert task.state.current_instance_path == instance.path

        seed_prompt_path = (
            engine.workspaces_root / "agent_research" / "prompts" / "system.md"
        )
        seed_prompt_path.parent.mkdir(parents=True, exist_ok=True)
        seed_prompt_path.write_text("seed version\n", encoding="utf-8")

        action = NextAction(
            action_type=ActionType.SELF_IMPROVE,
            description="Tune workspace prompt",
            self_improvement_type="general",
        )

        with patch(
            "umbrella.control_plane.code_improver.improve_system_from_context",
            return_value={
                "applied_count": 1,
                "failed_count": 0,
                "applied": [
                    {
                        "file": "prompts/system.md",
                        "description": "Tighten delivery instructions",
                        "change_type": "prompt",
                    }
                ],
                "failed": [],
                "changed_files": ["prompts/system.md"],
            },
        ):
            result = engine._execute_self_improvement(task, action)

        assert result.outcome == "success"
        assert engine.last_patch_result is not None
        assert task.state.last_patch_summary is not None
        assert "promotion deferred" in task.state.last_patch_summary.lower()
        assert any(
            Path(path).as_posix().endswith("prompts/system.md")
            for path in task.state.last_patch_files
        )
        assert seed_prompt_path.read_text(encoding="utf-8") == "seed version\n"

    def test_self_improvement_passes_error_context_to_optimizer(self, engine):
        """Self-improvement should pass log/error context so the optimizer can fix concrete failures."""
        task = _create_real_instance(
            engine,
            task_input="Fix the failing workspace",
            task_id="self_improve_errors",
        )
        instance = engine.active_instance
        assert instance is not None

        task.state.last_decision = NextAction(
            action_type=ActionType.SELF_IMPROVE,
            description="Fix import and tool configuration issues",
            self_improvement_type="general",
        )
        engine.last_inspection = {
            "manifest": {
                "status": "failed",
                "errors": ["ModuleNotFoundError: missing_tool"],
                "warnings": ["tool budget too low"],
                "final_answer": "",
            },
            "artifacts": {"count": 0, "paths": []},
            "log_summary": {
                "status": "available",
                "error_count": 2,
                "warning_count": 1,
                "tail": [
                    "Traceback (most recent call last):",
                    "ModuleNotFoundError: missing_tool",
                ],
            },
            "error_signatures": ["ModuleNotFoundError", "tool_not_found"],
            "raw_tail": [
                "Traceback (most recent call last):",
                "ModuleNotFoundError: missing_tool",
            ],
        }

        captured: dict[str, object] = {}

        def _capture_improvement_context(*, task_id, instance_path, repo_root, context):
            del task_id, instance_path, repo_root
            captured.update(context)
            return {
                "applied_count": 0,
                "failed_count": 0,
                "applied": [],
                "failed": [],
                "changed_files": [],
            }

        with patch(
            "umbrella.control_plane.code_improver.improve_system_from_context",
            side_effect=_capture_improvement_context,
        ):
            result = engine._execute_self_improvement(
                task,
                NextAction(
                    action_type=ActionType.SELF_IMPROVE,
                    description="Fix import and tool configuration issues",
                    self_improvement_type="general",
                ),
            )

        assert result.outcome == "partial"
        inspection = captured.get("inspection")
        assert isinstance(inspection, dict)
        assert inspection["error_signatures"] == [
            "ModuleNotFoundError",
            "tool_not_found",
        ]
        assert inspection["raw_tail"] == [
            "Traceback (most recent call last):",
            "ModuleNotFoundError: missing_tool",
        ]
        assert captured["last_decision"] == {
            "action_type": "self_improve",
            "description": "Fix import and tool configuration issues",
        }

    def test_consider_promotion_applies_instance_change_back_to_seed(self, engine):
        """A successful promotion must copy the improved file from instance back into the seed workspace."""
        task = _create_real_instance(
            engine, task_input="Improve workspace prompt", task_id="promote_1"
        )
        instance = engine.active_instance
        assert instance is not None

        seed_prompt_path = (
            engine.workspaces_root / "agent_research" / "prompts" / "system.md"
        )
        instance_prompt_path = instance.path / "prompts" / "system.md"
        seed_prompt_path.parent.mkdir(parents=True, exist_ok=True)
        instance_prompt_path.parent.mkdir(parents=True, exist_ok=True)

        seed_prompt_path.write_text("seed version\n", encoding="utf-8")
        instance_prompt_path.write_text("instance improved version\n", encoding="utf-8")

        baseline = EvaluationRecord(
            task_id=task.id,
            workspace_id=instance.workspace_id,
            run_id="run_before",
            instance_path=instance.path,
            task_success=TaskSuccessRating.PARTIAL,
            output_quality=OutputQualityRating.FAIR,
            stability=StabilityRating.UNKNOWN,
            total_tokens=100,
            total_duration_seconds=1.0,
            overall_score=0.40,
        )
        comparison_eval = EvaluationRecord(
            task_id=task.id,
            workspace_id=instance.workspace_id,
            run_id="run_after",
            instance_path=instance.path,
            task_success=TaskSuccessRating.COMPLETE,
            output_quality=OutputQualityRating.GOOD,
            stability=StabilityRating.STABLE,
            total_tokens=120,
            total_duration_seconds=1.0,
            overall_score=0.92,
        )
        comparison = compare_runs(baseline, comparison_eval)

        engine.baseline_eval_record = baseline
        engine.last_eval_record = comparison_eval
        engine.last_patch_result = WorkspacePatchResult(
            applied=True,
            summary="Improved prompt in instance",
            changed_files=[str(instance_prompt_path)],
        )

        run_result = WorkspaceRunResult(
            run_id="run_after",
            workspace_id=instance.workspace_id,
            task_id=task.id,
            status=WorkspaceRunStatus.COMPLETED,
            final_answer="Improved result",
            summary="Improved result",
            duration_seconds=0.5,
            total_tokens=120,
        )

        engine._consider_promotion(task, comparison, run_result)

        assert task.state.promotion_candidate is not None
        assert task.state.promotion_candidate.changed_files == [
            Path("prompts/system.md")
        ]
        assert (
            seed_prompt_path.read_text(encoding="utf-8")
            == "instance improved version\n"
        )

    def test_escalation_for_blocking_constraints(self, engine):
        """Explicitly disabled self-improvement triggers escalation."""
        from umbrella.control_plane.escalation import BlockingConstraints
        import unittest.mock as mock

        task = engine.start_task("High-risk task")
        context = engine._build_context(task)

        blocked_constraints = BlockingConstraints(self_improve_allowed=False)
        with mock.patch(
            "umbrella.control_plane.escalation.BlockingConstraints",
            return_value=blocked_constraints,
        ):
            result = engine._execute_action(
                task,
                NextAction(
                    action_type=ActionType.SELF_IMPROVE, description="Self-improve"
                ),
                context,
            )

        assert result.action.action_type in (
            ActionType.ESCALATE_TO_HUMAN,
            ActionType.WAIT_FOR_INPUT,
        )

    def test_resolve_seed_profile_uses_registry_workspace_ref_when_seed_profile_missing(
        self, temp_dir
    ):
        workspace_root = temp_dir / "workspaces" / "custom_workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        (workspace_root / "workspace.toml").write_text(
            'workspace_id = "custom_workspace"\nname = "Custom Workspace"\n',
            encoding="utf-8",
        )
        (workspace_root / "TASK_MAIN.md").write_text(
            "# Task\n\nCustom task contract.\n", encoding="utf-8"
        )

        registry = MagicMock()
        registry.get_seed_profile.return_value = None
        registry.get_workspace.return_value = RegistryWorkspaceRef(
            workspace_id="custom_workspace",
            name="Custom Workspace",
            description="Convention workspace",
            path=workspace_root,
            task_main_file="TASK_MAIN.md",
        )

        engine = ControlPlaneEngine(
            workspace_registry=registry,
            workspaces_root=temp_dir / "workspaces",
            control_state_dir=temp_dir / "control",
        )

        profile = engine._resolve_seed_profile("custom_workspace")
        assert profile.workspace_id == "custom_workspace"
        assert profile.path == workspace_root
        assert profile.ref.task_main_file == "TASK_MAIN.md"
