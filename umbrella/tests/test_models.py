"""
Tests for umbrella.policies.models.
"""

from pathlib import Path

from umbrella.policies.models import (
    PolicyDecision,
    EditSurfaceCategory,
    SelfImprovementTrigger,
    EscalationReason,
    SystemBoundaryPolicy,
    EditSurfacePolicy,
    SelfImprovementPolicy,
    EscalationPolicy,
    WorkspaceMutationPolicy,
    FrameworkBoundaryPolicy,
)


class TestPolicyDecision:
    """Tests for PolicyDecision dataclass."""

    def test_allowed_true(self):
        decision = PolicyDecision(
            allowed=True, reason="Test reason", policy_id="test_policy"
        )
        assert decision.allowed is True
        assert decision.reason == "Test reason"
        assert decision.policy_id == "test_policy"
        assert decision.escalation_required is False
        assert bool(decision) is True

    def test_not_allowed_false(self):
        decision = PolicyDecision(
            allowed=False, reason="Not allowed", policy_id="test_policy"
        )
        assert decision.allowed is False
        assert bool(decision) is False

    def test_escalation_required(self):
        decision = PolicyDecision(
            allowed=True,
            reason="Test reason",
            policy_id="test_policy",
            escalation_required=True,
            escalation_reason=EscalationReason.FRAMEWORK_MODIFICATION,
        )
        assert decision.escalation_required is True
        assert decision.escalation_reason == EscalationReason.FRAMEWORK_MODIFICATION

    def test_metadata(self):
        decision = PolicyDecision(
            allowed=True,
            reason="Test reason",
            policy_id="test_policy",
            metadata={"key": "value"},
        )
        assert decision.metadata == {"key": "value"}


class TestEditSurfaceCategory:
    """Tests for EditSurfaceCategory enum."""

    def test_category_values(self):
        assert EditSurfaceCategory.FRAMEWORK.value == "framework"
        assert EditSurfaceCategory.MANAGER.value == "manager"
        assert EditSurfaceCategory.WORKSPACE_SEED.value == "workspace_seed"
        assert EditSurfaceCategory.WORKSPACE_INSTANCE.value == "workspace_instance"
        assert EditSurfaceCategory.ARTIFACTS.value == "artifacts"
        assert EditSurfaceCategory.INTEGRATION.value == "integration"
        assert EditSurfaceCategory.REPO_DOCS.value == "repo_docs"
        assert EditSurfaceCategory.UNKNOWN.value == "unknown"


class TestSelfImprovementTrigger:
    """Tests for SelfImprovementTrigger enum."""

    def test_trigger_values(self):
        assert SelfImprovementTrigger.REPEATED_FAILURES.value == "repeated_failures"
        assert (
            SelfImprovementTrigger.NO_WORKSPACE_PROGRESS.value
            == "no_workspace_progress"
        )
        assert SelfImprovementTrigger.RETRIEVAL_WEAKNESS.value == "retrieval_weakness"
        assert SelfImprovementTrigger.CAPABILITY_GAP.value == "capability_gap"
        assert SelfImprovementTrigger.EXPLICIT_REQUEST.value == "explicit_request"


class TestEscalationReason:
    """Tests for EscalationReason enum."""

    def test_reason_values(self):
        assert EscalationReason.FRAMEWORK_MODIFICATION.value == "framework_modification"
        assert EscalationReason.RISKY_PROMPT_CHANGE.value == "risky_prompt_change"
        assert (
            EscalationReason.SEED_PROMOTION_WITHOUT_EVIDENCE.value
            == "seed_promotion_without_evidence"
        )
        assert EscalationReason.UNSAFE_MASS_EDIT.value == "unsafe_mass_edit"
        assert (
            EscalationReason.SELF_IMPROVEMENT_TRIGGERED.value
            == "self_improvement_triggered"
        )
        assert (
            EscalationReason.UNCLEAR_PATH_CLASSIFICATION.value
            == "unclear_path_classification"
        )
        assert EscalationReason.POLICY_VIOLATION.value == "policy_violation"


class TestSystemBoundaryPolicy:
    """Tests for SystemBoundaryPolicy dataclass."""

    def test_minimal_policy(self):
        policy = SystemBoundaryPolicy()
        assert policy.workspace_first is True
        assert policy.standalone_workspace_required is True
        assert policy.edit_surface is not None

    def test_full_policy(self):
        policy = SystemBoundaryPolicy(
            workspace_first=False,
            standalone_workspace_required=False,
        )
        assert policy.workspace_first is False
        assert policy.standalone_workspace_required is False

    def test_default_values(self):
        policy = SystemBoundaryPolicy()
        assert policy.workspace_first is True
        assert policy.standalone_workspace_required is True
        assert policy.documentation_first_retrieval is True
        assert policy.bm25_first_retrieval is True


class TestEditSurfacePolicy:
    """Tests for EditSurfacePolicy dataclass."""

    def test_minimal_policy(self):
        policy = EditSurfacePolicy()
        assert policy.framework_readonly is True
        assert policy.manager_editable_under_self_improvement is True

    def test_get_mutability_framework(self):
        policy = EditSurfacePolicy()
        result = policy.get_mutability(EditSurfaceCategory.FRAMEWORK)
        assert result.allowed is False

    def test_get_mutability_instance(self):
        policy = EditSurfacePolicy()
        result = policy.get_mutability(EditSurfaceCategory.WORKSPACE_INSTANCE)
        assert result.allowed is True

    def test_get_mutability_repo_docs(self):
        policy = EditSurfacePolicy()
        result = policy.get_mutability(EditSurfaceCategory.REPO_DOCS)
        assert result.allowed is True


class TestSelfImprovementPolicy:
    """Tests for SelfImprovementPolicy dataclass."""

    def test_minimal_policy(self):
        policy = SelfImprovementPolicy()
        assert policy.min_repeated_failures == 3
        assert policy.explicit_request_allows is True

    def test_can_trigger_repeated_failures(self):
        policy = SelfImprovementPolicy()
        result = policy.can_trigger(
            SelfImprovementTrigger.REPEATED_FAILURES, {"failure_count": 5}
        )
        assert result.allowed is True

    def test_can_trigger_explicit_request(self):
        policy = SelfImprovementPolicy()
        result = policy.can_trigger(SelfImprovementTrigger.EXPLICIT_REQUEST, {})
        assert result.allowed is True


class TestEscalationPolicy:
    """Tests for EscalationPolicy dataclass."""

    def test_minimal_policy(self):
        policy = EscalationPolicy()
        assert len(policy.always_escalate) > 0
        assert EscalationReason.FRAMEWORK_MODIFICATION in policy.always_escalate

    def test_requires_escalation_framework(self):
        policy = EscalationPolicy()
        result = policy.requires_escalation(EscalationReason.FRAMEWORK_MODIFICATION, {})
        assert result.escalation_required is True


class TestWorkspaceMutationPolicy:
    """Tests for WorkspaceMutationPolicy dataclass."""

    def test_minimal_policy(self):
        policy = WorkspaceMutationPolicy()
        assert policy.seed_requires_evidence is True
        assert policy.min_promotion_score == 0.7

    def test_can_mutate_instance(self):
        policy = WorkspaceMutationPolicy()
        result = policy.can_mutate_instance("graph")
        assert result.allowed is True

    def test_cannot_mutate_unknown_type(self):
        policy = WorkspaceMutationPolicy()
        result = policy.can_mutate_instance("unknown_type")
        assert result.allowed is False


class TestFrameworkBoundaryPolicy:
    """Tests for FrameworkBoundaryPolicy dataclass."""

    def test_minimal_policy(self):
        policy = FrameworkBoundaryPolicy()
        assert policy.gmas_readonly is True
        assert policy.requires_human_approval is True

    def test_is_framework_path(self):
        policy = FrameworkBoundaryPolicy()
        assert policy.is_framework_path(Path("gmas/core/graph.py")) is True
        assert policy.is_framework_path(Path("other/file.py")) is False

    def test_can_modify_without_approval(self):
        policy = FrameworkBoundaryPolicy()
        result = policy.can_modify(Path("gmas/core/graph.py"), human_approved=False)
        assert result.allowed is False

    def test_can_modify_with_approval(self):
        policy = FrameworkBoundaryPolicy()
        result = policy.can_modify(Path("gmas/core/graph.py"), human_approved=True)
        assert result.allowed is True
