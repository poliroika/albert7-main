"""
Tests for umbrella.policies.engine.
"""

import pytest
from pathlib import Path

from umbrella.policies.engine import (
    PolicyEngine,
    can_edit_path,
    should_prefer_workspace_patch,
    can_trigger_self_improvement,
    requires_human_escalation,
    classify_path,
)
from umbrella.policies.models import (
    EditSurfaceCategory,
    SelfImprovementTrigger,
    EscalationReason,
)
from umbrella.policies.defaults import load_default_policy


class TestClassifyPath:
    """Tests for classify_path function."""

    def test_classify_gmas_path(self):
        assert classify_path(Path("gmas")) == EditSurfaceCategory.FRAMEWORK
        assert classify_path(Path("gmas/")) == EditSurfaceCategory.FRAMEWORK
        assert (
            classify_path(Path("gmas/src/core/graph.py"))
            == EditSurfaceCategory.FRAMEWORK
        )

    def test_classify_ouroboros_path(self):
        assert classify_path(Path("ouroboros")) == EditSurfaceCategory.MANAGER
        assert classify_path(Path("ouroboros/")) == EditSurfaceCategory.MANAGER

    def test_classify_umbrella_path(self):
        assert classify_path(Path("umbrella")) == EditSurfaceCategory.INTEGRATION
        assert classify_path(Path("umbrella/")) == EditSurfaceCategory.INTEGRATION

    def test_classify_workspace_seed_path(self):
        assert (
            classify_path(Path("workspaces/agent_research"))
            == EditSurfaceCategory.WORKSPACE_SEED
        )
        assert (
            classify_path(Path("workspaces/agent_research/"))
            == EditSurfaceCategory.WORKSPACE_SEED
        )

    def test_classify_workspace_instance_path(self):
        assert (
            classify_path(Path("workspaces/instances/task_123"))
            == EditSurfaceCategory.WORKSPACE_INSTANCE
        )
        assert (
            classify_path(Path("workspaces/task_abc"))
            == EditSurfaceCategory.WORKSPACE_INSTANCE
        )

    def test_classify_artifacts_path(self):
        assert (
            classify_path(Path("workspaces/agent_research/runs"))
            == EditSurfaceCategory.ARTIFACTS
        )
        assert (
            classify_path(Path("workspaces/agent_research/runs/"))
            == EditSurfaceCategory.ARTIFACTS
        )

    def test_unknown_path(self):
        assert classify_path(Path("random_file.txt")) == EditSurfaceCategory.UNKNOWN

    def test_classify_deep_coding_tasks(self):
        assert (
            classify_path(Path("deep_coding_tasks/01_policy.md"))
            == EditSurfaceCategory.REPO_DOCS
        )

    def test_classify_absolute_repo_path(self):
        repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / "workspaces" / "agent_research" / "workspace.toml"
        assert (
            classify_path(path, repo_root=repo_root)
            == EditSurfaceCategory.WORKSPACE_SEED
        )


class TestCanEditPath:
    """Tests for can_edit_path function."""

    def test_gmas_read_only(self):
        result = can_edit_path(Path("gmas/src/core/graph.py"))
        assert result.allowed is False
        assert "framework" in result.reason.lower()

    def test_workspace_seed_requires_promotion(self):
        result = can_edit_path(Path("workspaces/agent_research/workspace.toml"))
        assert result.allowed is False
        assert "seed" in result.reason.lower()

    def test_workspace_instance_editable(self):
        result = can_edit_path(Path("workspaces/task_abc/workspace.toml"))
        assert result.allowed is True

    def test_artifacts_editable(self):
        result = can_edit_path(Path("workspaces/agent_research/runs/run_001.log"))
        assert result.allowed is True

    def test_unknown_path_rejected(self):
        result = can_edit_path(Path("random_file.txt"))
        assert result.allowed is False

    def test_deep_coding_tasks_editable(self):
        result = can_edit_path(
            Path("deep_coding_tasks/01_policy_and_repository_boundaries.md")
        )
        assert result.allowed is True

    def test_manager_path_requires_escalation(self):
        result = can_edit_path(Path("ouroboros/ouroboros/agent.py"))
        assert result.allowed is False
        assert result.escalation_required is True
        assert result.escalation_reason == EscalationReason.SELF_IMPROVEMENT_TRIGGERED

    def test_manager_path_allows_explicit_self_improvement_action(self):
        result = can_edit_path(
            Path("ouroboros/ouroboros/agent.py"),
            action="self_improvement",
        )
        assert result.allowed is True
        assert result.escalation_required is True


class TestShouldPreferWorkspacePatch:
    """Tests for should_prefer_workspace_patch function."""

    def test_prefer_workspace_by_default(self):
        result = should_prefer_workspace_patch({})
        assert result.allowed is True

    def test_prefer_workspace_for_framework_target(self):
        result = should_prefer_workspace_patch({"target": "framework"})
        assert result.allowed is True


class TestCanTriggerSelfImprovement:
    """Tests for can_trigger_self_improvement function."""

    def test_repeated_failures_trigger(self):
        result = can_trigger_self_improvement(
            {
                "intent": SelfImprovementTrigger.REPEATED_FAILURES,
                "failure_count": 5,
            }
        )
        assert result.allowed is True
        assert "repeated_failures" in result.policy_id

    def test_capability_gap_trigger(self):
        result = can_trigger_self_improvement(
            {
                "intent": SelfImprovementTrigger.CAPABILITY_GAP,
                "capability_gap_detected": True,
            }
        )
        assert result.allowed is True
        assert "capability_gap" in result.policy_id

    def test_capability_gap_with_gaps_count(self):
        result = can_trigger_self_improvement(
            {
                "intent": SelfImprovementTrigger.CAPABILITY_GAP,
                "capability_gaps": 2,
            }
        )
        assert result.allowed is True
        assert "capability_gap" in result.policy_id

    def test_no_triggers_rejected(self):
        result = can_trigger_self_improvement(
            {"intent": "invalid_trigger", "triggers": []}
        )
        assert result.allowed is False

    def test_repeated_failures_below_threshold(self):
        result = can_trigger_self_improvement(
            {
                "intent": SelfImprovementTrigger.REPEATED_FAILURES,
                "failure_count": 1,
            }
        )
        assert result.allowed is False


class TestRequiresHumanEscalation:
    """Tests for requires_human_escalation function."""

    def test_framework_modification_requires_escalation(self):
        result = requires_human_escalation(
            {
                "intent": EscalationReason.FRAMEWORK_MODIFICATION,
                "path": Path("gmas/src/core/graph.py"),
            }
        )
        assert result.escalation_required is True
        assert result.allowed is False

    def test_risky_prompt_change_requires_escalation(self):
        result = requires_human_escalation(
            {
                "intent": EscalationReason.RISKY_PROMPT_CHANGE,
                "path": Path("ouroboros/prompts/SYSTEM.md"),
            }
        )
        assert result.escalation_required is True

    def test_seed_promotion_requires_escalation(self):
        result = requires_human_escalation(
            {
                "intent": EscalationReason.SEED_PROMOTION_WITHOUT_EVIDENCE,
                "path": Path("workspaces/agent_research/workspace.toml"),
                "evidence": None,
            }
        )
        assert result.escalation_required is True

    def test_normal_edit_no_escalation(self):
        result = requires_human_escalation(
            {
                "intent": "normal_edit",
                "path": Path("workspaces/task_abc/workspace.toml"),
            }
        )
        assert result.escalation_required is False

    def test_mass_edit_threshold_uses_policy_default(self):
        result = requires_human_escalation(
            {
                "intent": EscalationReason.UNSAFE_MASS_EDIT,
                "file_count": 10,
            }
        )
        assert result.escalation_required is True
        assert result.allowed is False

    def test_mass_edit_below_threshold_is_allowed(self):
        result = requires_human_escalation(
            {
                "intent": EscalationReason.UNSAFE_MASS_EDIT,
                "file_count": 9,
            }
        )
        assert result.allowed is True
        assert result.escalation_required is False


class TestPolicyEngine:
    """Tests for PolicyEngine class."""

    @pytest.fixture
    def engine(self):
        return PolicyEngine(load_default_policy())

    def test_engine_initialization(self, engine):
        assert engine.policy is not None

    def test_engine_classify_path(self, engine):
        assert engine.classify_path(Path("gmas/")) == EditSurfaceCategory.FRAMEWORK
        assert engine.classify_path(Path("ouroboros/")) == EditSurfaceCategory.MANAGER
        assert (
            engine.classify_path(Path("umbrella/")) == EditSurfaceCategory.INTEGRATION
        )

    def test_engine_can_edit_path(self, engine):
        # Framework not editable
        result = engine.can_edit_path(Path("gmas/src/core/graph.py"))
        assert result.allowed is False

        # Integration editable
        result = engine.can_edit_path(Path("umbrella/policies/models.py"))
        assert result.allowed is True

    def test_engine_can_trigger_self_improvement(self, engine):
        result = engine.can_trigger_self_improvement(
            {"intent": SelfImprovementTrigger.REPEATED_FAILURES, "failure_count": 5}
        )
        assert result.allowed is True

    def test_engine_requires_human_escalation(self, engine):
        result = engine.requires_human_escalation(
            {
                "intent": EscalationReason.FRAMEWORK_MODIFICATION,
                "path": Path("gmas/src/core/graph.py"),
            }
        )
        assert result.escalation_required is True
