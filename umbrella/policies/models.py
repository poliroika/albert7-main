"""
Policy data models for the umbrella integration layer.

Defines explicit types for system boundaries, edit surfaces,
self-improvement rules, and escalation requirements.
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class EditSurfaceCategory(str, Enum):
    """Categories of editable surfaces in the repository."""

    FRAMEWORK = "framework"  # gmas/ - read-only by default
    MANAGER = "manager"  # ouroboros/ - mutable under self-improvement rules
    WORKSPACE_SEED = "workspace_seed"  # Seed workspaces - require promotion
    WORKSPACE_INSTANCE = (
        "workspace_instance"  # Task-specific instances - freely mutable
    )
    ARTIFACTS = "artifacts"  # Generated outputs, logs, reports
    INTEGRATION = "integration"  # umbrella/ - new integration layer
    REPO_DOCS = "repo_docs"  # deep_coding_tasks/ and similar repo-local specs
    UNKNOWN = "unknown"  # Unrecognized paths


class SelfImprovementTrigger(str, Enum):
    """Valid triggers for ouroboros self-improvement."""

    REPEATED_FAILURES = "repeated_failures"  # Same blocker across multiple tasks
    NO_WORKSPACE_PROGRESS = (
        "no_workspace_progress"  # Multiple workspace iterations without improvement
    )
    RETRIEVAL_WEAKNESS = (
        "retrieval_weakness"  # Systemic low retrieval confidence on gmas
    )
    CAPABILITY_GAP = "capability_gap"  # Demonstrated manager-level capability gap
    EXPLICIT_REQUEST = "explicit_request"  # Human-approved explicit request


class EscalationReason(str, Enum):
    """Reasons requiring human escalation."""

    FRAMEWORK_MODIFICATION = "framework_modification"  # Attempting to modify gmas/
    RISKY_PROMPT_CHANGE = "risky_prompt_change"  # Changing ouroboros prompt stack
    SEED_PROMOTION_WITHOUT_EVIDENCE = (
        "seed_promotion_without_evidence"  # Promoting without proof
    )
    UNSAFE_MASS_EDIT = "unsafe_mass_edit"  # Large-scale changes without review
    SELF_IMPROVEMENT_TRIGGERED = (
        "self_improvement_triggered"  # Self-improvement cycle started
    )
    UNCLEAR_PATH_CLASSIFICATION = (
        "unclear_path_classification"  # Cannot determine edit surface
    )
    POLICY_VIOLATION = "policy_violation"  # General policy violation


@dataclass(frozen=True)
class PolicyDecision:
    """Result of a policy check."""

    allowed: bool
    reason: str
    policy_id: str
    escalation_required: bool = False
    escalation_reason: EscalationReason | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class EditSurfacePolicy:
    """Policy for what surfaces can be edited and under what conditions."""

    # Read-only surfaces (never auto-editable)
    framework_readonly: bool = True

    # Manager surfaces (editable under self-improvement)
    manager_editable_under_self_improvement: bool = True

    # Workspace seed surfaces (require promotion process)
    workspace_seed_requires_promotion: bool = True

    # Workspace instances (freely mutable)
    workspace_instance_editable: bool = True

    # Artifacts (freely mutable)
    artifacts_editable: bool = True

    # Integration layer (freely mutable)
    integration_editable: bool = True

    def get_mutability(self, category: EditSurfaceCategory) -> PolicyDecision:
        """Get the mutability decision for a surface category."""
        if category == EditSurfaceCategory.FRAMEWORK:
            return PolicyDecision(
                allowed=False,
                reason="Framework (gmas/) is read-only by default",
                policy_id="edit_surface.framework_readonly",
                escalation_required=True,
                escalation_reason=EscalationReason.FRAMEWORK_MODIFICATION,
            )
        elif category == EditSurfaceCategory.MANAGER:
            return PolicyDecision(
                allowed=self.manager_editable_under_self_improvement,
                reason="Manager (ouroboros/) is editable only under self-improvement rules",
                policy_id="edit_surface.manager_conditional",
                escalation_required=True,
                escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
            )
        elif category == EditSurfaceCategory.WORKSPACE_SEED:
            return PolicyDecision(
                allowed=False,  # Not directly editable - requires promotion
                reason="Workspace seeds require promotion process for changes",
                policy_id="edit_surface.seed_requires_promotion",
                escalation_required=self.workspace_seed_requires_promotion,
                escalation_reason=EscalationReason.SEED_PROMOTION_WITHOUT_EVIDENCE,
            )
        elif category == EditSurfaceCategory.WORKSPACE_INSTANCE:
            return PolicyDecision(
                allowed=self.workspace_instance_editable,
                reason="Task-specific workspace instances are freely mutable",
                policy_id="edit_surface.instance_editable",
            )
        elif category == EditSurfaceCategory.ARTIFACTS:
            return PolicyDecision(
                allowed=self.artifacts_editable,
                reason="Generated artifacts are freely mutable",
                policy_id="edit_surface.artifacts_editable",
            )
        elif category == EditSurfaceCategory.INTEGRATION:
            return PolicyDecision(
                allowed=self.integration_editable,
                reason="Integration layer (umbrella/) is freely mutable",
                policy_id="edit_surface.integration_editable",
            )
        elif category == EditSurfaceCategory.REPO_DOCS:
            return PolicyDecision(
                allowed=True,
                reason="Repository task documentation (e.g. deep_coding_tasks/) is editable",
                policy_id="edit_surface.repo_docs_editable",
            )
        else:
            return PolicyDecision(
                allowed=False,
                reason=f"Unknown surface category: {category}",
                policy_id="edit_surface.unknown",
                escalation_required=True,
                escalation_reason=EscalationReason.UNCLEAR_PATH_CLASSIFICATION,
            )


@dataclass
class SelfImprovementPolicy:
    """Policy governing when ouroboros can improve itself."""

    # Minimum repeated failures before self-improvement
    min_repeated_failures: int = 3

    # Minimum workspace iterations without progress
    min_stalled_iterations: int = 5

    # Minimum retrieval confidence threshold for weakness detection
    retrieval_confidence_threshold: float = 0.3

    # Whether explicit human request always allows self-improvement
    explicit_request_allows: bool = True

    # Valid triggers
    valid_triggers: list[SelfImprovementTrigger] = field(
        default_factory=lambda: list(SelfImprovementTrigger)
    )

    def can_trigger(
        self, trigger: SelfImprovementTrigger, context: dict[str, Any]
    ) -> PolicyDecision:
        """Check if a self-improvement trigger is valid given context."""

        if trigger not in self.valid_triggers:
            return PolicyDecision(
                allowed=False,
                reason=f"Invalid self-improvement trigger: {trigger}",
                policy_id="self_improvement.invalid_trigger",
            )

        if trigger == SelfImprovementTrigger.EXPLICIT_REQUEST:
            if self.explicit_request_allows:
                return PolicyDecision(
                    allowed=True,
                    reason="Explicit human request for self-improvement",
                    policy_id="self_improvement.explicit_request",
                    escalation_required=True,
                    escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
                )

        if trigger == SelfImprovementTrigger.REPEATED_FAILURES:
            failure_count = context.get("failure_count", 0)
            if failure_count >= self.min_repeated_failures:
                return PolicyDecision(
                    allowed=True,
                    reason=f"Repeated failures ({failure_count}) exceed threshold ({self.min_repeated_failures})",
                    policy_id="self_improvement.repeated_failures",
                    escalation_required=True,
                    escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
                    metadata={"failure_count": failure_count},
                )
            else:
                return PolicyDecision(
                    allowed=False,
                    reason=f"Repeated failures ({failure_count}) below threshold ({self.min_repeated_failures})",
                    policy_id="self_improvement.repeated_failures_below_threshold",
                )

        if trigger == SelfImprovementTrigger.NO_WORKSPACE_PROGRESS:
            stalled_iterations = context.get("stalled_iterations", 0)
            if stalled_iterations >= self.min_stalled_iterations:
                return PolicyDecision(
                    allowed=True,
                    reason=f"Stalled workspace iterations ({stalled_iterations}) exceed threshold ({self.min_stalled_iterations})",
                    policy_id="self_improvement.no_workspace_progress",
                    escalation_required=True,
                    escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
                    metadata={"stalled_iterations": stalled_iterations},
                )
            else:
                return PolicyDecision(
                    allowed=False,
                    reason=f"Stalled iterations ({stalled_iterations}) below threshold ({self.min_stalled_iterations})",
                    policy_id="self_improvement.stalled_below_threshold",
                )

        if trigger == SelfImprovementTrigger.RETRIEVAL_WEAKNESS:
            retrieval_confidence = context.get("retrieval_confidence", 1.0)
            if retrieval_confidence < self.retrieval_confidence_threshold:
                return PolicyDecision(
                    allowed=True,
                    reason=f"Retrieval confidence ({retrieval_confidence:.2f}) below threshold ({self.retrieval_confidence_threshold})",
                    policy_id="self_improvement.retrieval_weakness",
                    escalation_required=True,
                    escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
                    metadata={"retrieval_confidence": retrieval_confidence},
                )
            else:
                return PolicyDecision(
                    allowed=False,
                    reason=f"Retrieval confidence ({retrieval_confidence:.2f}) above threshold ({self.retrieval_confidence_threshold})",
                    policy_id="self_improvement.retrieval_ok",
                )

        if trigger == SelfImprovementTrigger.CAPABILITY_GAP:
            has_gap = context.get("capability_gap_detected", False)
            if has_gap:
                return PolicyDecision(
                    allowed=True,
                    reason="Demonstrated manager-level capability gap",
                    policy_id="self_improvement.capability_gap",
                    escalation_required=True,
                    escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
                )
            else:
                return PolicyDecision(
                    allowed=False,
                    reason="No demonstrated capability gap",
                    policy_id="self_improvement.no_capability_gap",
                )

        return PolicyDecision(
            allowed=False,
            reason=f"Unhandled trigger: {trigger}",
            policy_id="self_improvement.unhandled",
        )


@dataclass
class EscalationPolicy:
    """Policy for when human escalation is required."""

    # Always escalate for these reasons
    always_escalate: list[EscalationReason] = field(
        default_factory=lambda: [
            EscalationReason.FRAMEWORK_MODIFICATION,
            EscalationReason.SEED_PROMOTION_WITHOUT_EVIDENCE,
            EscalationReason.UNSAFE_MASS_EDIT,
        ]
    )

    # File count threshold for mass edit detection
    mass_edit_threshold: int = 10

    # Whether to escalate on self-improvement
    escalate_self_improvement: bool = True

    def requires_escalation(
        self, reason: EscalationReason, context: dict[str, Any]
    ) -> PolicyDecision:
        """Check if a situation requires human escalation."""

        if reason in self.always_escalate:
            return PolicyDecision(
                allowed=False,  # Not allowed without human approval
                reason=f"Human escalation required for: {reason.value}",
                policy_id=f"escalation.{reason.value}",
                escalation_required=True,
                escalation_reason=reason,
            )

        if reason == EscalationReason.SELF_IMPROVEMENT_TRIGGERED:
            if self.escalate_self_improvement:
                return PolicyDecision(
                    allowed=True,  # Allowed but requires escalation
                    reason="Self-improvement requires human notification",
                    policy_id="escalation.self_improvement",
                    escalation_required=True,
                    escalation_reason=reason,
                )

        if reason == EscalationReason.UNSAFE_MASS_EDIT:
            file_count = context.get("file_count", 0)
            if file_count >= self.mass_edit_threshold:
                return PolicyDecision(
                    allowed=False,
                    reason=f"Mass edit ({file_count} files) exceeds threshold ({self.mass_edit_threshold})",
                    policy_id="escalation.mass_edit",
                    escalation_required=True,
                    escalation_reason=reason,
                    metadata={"file_count": file_count},
                )

        return PolicyDecision(
            allowed=True,
            reason="No escalation required",
            policy_id="escalation.not_required",
        )


@dataclass
class WorkspaceMutationPolicy:
    """Policy for workspace mutations (seed vs instance)."""

    # Seed workspaces require evidence for promotion
    seed_requires_evidence: bool = True

    # Minimum eval score for promotion
    min_promotion_score: float = 0.7

    # Allowed mutations for instances
    allowed_instance_mutations: list[str] = field(
        default_factory=lambda: [
            "graph",  # graph/topology changes
            "agents",  # agent definitions
            "prompts",  # prompt modifications
            "tools",  # tool configurations
            "models",  # model settings
            "evals",  # evaluation configs
            "experiments",  # experiment scripts
            "reports",  # generated reports
            "runs",  # run logs
            "snapshots",  # workspace snapshots
        ]
    )

    def can_mutate_instance(self, mutation_type: str) -> PolicyDecision:
        """Check if a mutation type is allowed for workspace instances."""
        if mutation_type in self.allowed_instance_mutations:
            return PolicyDecision(
                allowed=True,
                reason=f"Mutation type '{mutation_type}' is allowed for workspace instances",
                policy_id="workspace_mutation.instance_allowed",
            )
        return PolicyDecision(
            allowed=False,
            reason=f"Mutation type '{mutation_type}' is not in allowed list",
            policy_id="workspace_mutation.instance_denied",
        )

    def can_promote_to_seed(self, evidence: dict[str, Any]) -> PolicyDecision:
        """Check if an instance can be promoted to seed."""
        if not self.seed_requires_evidence:
            return PolicyDecision(
                allowed=True,
                reason="Seed promotion does not require evidence",
                policy_id="workspace_mutation.promotion_no_evidence",
            )

        eval_score = evidence.get("eval_score", 0.0)
        if eval_score >= self.min_promotion_score:
            return PolicyDecision(
                allowed=True,
                reason=f"Evaluation score ({eval_score:.2f}) meets threshold ({self.min_promotion_score})",
                policy_id="workspace_mutation.promotion_qualified",
                escalation_required=True,
                escalation_reason=EscalationReason.SEED_PROMOTION_WITHOUT_EVIDENCE,
                metadata={"eval_score": eval_score},
            )

        return PolicyDecision(
            allowed=False,
            reason=f"Evaluation score ({eval_score:.2f}) below threshold ({self.min_promotion_score})",
            policy_id="workspace_mutation.promotion_unqualified",
            escalation_required=True,
            escalation_reason=EscalationReason.SEED_PROMOTION_WITHOUT_EVIDENCE,
        )


@dataclass
class FrameworkBoundaryPolicy:
    """Policy for gmas framework boundaries."""

    # gmas is read-only by default
    gmas_readonly: bool = True

    # Path patterns that define the framework
    framework_paths: list[str] = field(default_factory=lambda: ["gmas/"])

    # If modification is required, this requires explicit human approval
    requires_human_approval: bool = True

    # Documentation-first retrieval preference
    documentation_first_retrieval: bool = True

    # BM25-first hybrid retrieval
    bm25_first_retrieval: bool = True

    def is_framework_path(self, path: Path) -> bool:
        """Check if a path is within the framework."""
        path_str = str(path).replace("\\", "/")
        for pattern in self.framework_paths:
            if path_str.startswith(pattern) or f"/{pattern}" in path_str:
                return True
        return False

    def can_modify(self, path: Path, human_approved: bool = False) -> PolicyDecision:
        """Check if a framework path can be modified."""
        if not self.is_framework_path(path):
            return PolicyDecision(
                allowed=True,
                reason="Path is not within framework boundaries",
                policy_id="framework_boundary.not_framework",
            )

        if self.gmas_readonly and not human_approved:
            return PolicyDecision(
                allowed=False,
                reason="Framework (gmas/) is read-only without explicit human approval",
                policy_id="framework_boundary.readonly",
                escalation_required=True,
                escalation_reason=EscalationReason.FRAMEWORK_MODIFICATION,
            )

        if self.requires_human_approval and not human_approved:
            return PolicyDecision(
                allowed=False,
                reason="Framework modification requires human approval",
                policy_id="framework_boundary.requires_approval",
                escalation_required=True,
                escalation_reason=EscalationReason.FRAMEWORK_MODIFICATION,
            )

        return PolicyDecision(
            allowed=True,
            reason="Framework modification approved by human",
            policy_id="framework_boundary.approved",
            escalation_required=True,
            escalation_reason=EscalationReason.FRAMEWORK_MODIFICATION,
        )


@dataclass
class SandboxSelfEditPolicy:
    """Policy for temporary self-edits that auto-rollback after the task ends.

    When the agent detects a capability gap mid-task, it can temporarily
    modify its own code (ouroboros/, umbrella/) to unblock itself.  All
    changes are rolled back once the task completes, keeping the repo in
    its original state.
    """

    enabled: bool = True
    allowed_surfaces: list[str] = field(
        default_factory=lambda: ["ouroboros", "umbrella"]
    )
    rollback_on_task_end: bool = True
    max_changed_files: int = 15
    require_capability_gap: bool = True
    snapshot_method: str = "git_stash"  # git_stash | git_branch | copy

    def can_sandbox_edit(
        self,
        surface: str,
        capability_gap_detected: bool = False,
        changed_files_count: int = 0,
    ) -> PolicyDecision:
        if not self.enabled:
            return PolicyDecision(
                allowed=False,
                reason="Sandbox self-edit is disabled by policy",
                policy_id="sandbox_self_edit.disabled",
            )
        if surface not in self.allowed_surfaces:
            return PolicyDecision(
                allowed=False,
                reason=f"Surface '{surface}' is not in sandbox-allowed list: {self.allowed_surfaces}",
                policy_id="sandbox_self_edit.surface_denied",
            )
        if self.require_capability_gap and not capability_gap_detected:
            return PolicyDecision(
                allowed=False,
                reason="Sandbox self-edit requires a demonstrated capability gap",
                policy_id="sandbox_self_edit.no_gap",
            )
        if changed_files_count >= self.max_changed_files:
            return PolicyDecision(
                allowed=False,
                reason=f"Sandbox self-edit limit reached ({changed_files_count}/{self.max_changed_files})",
                policy_id="sandbox_self_edit.file_limit",
                escalation_required=True,
                escalation_reason=EscalationReason.UNSAFE_MASS_EDIT,
            )
        return PolicyDecision(
            allowed=True,
            reason=f"Sandbox self-edit allowed for '{surface}' (rollback_on_task_end={self.rollback_on_task_end})",
            policy_id="sandbox_self_edit.allowed",
            escalation_required=False,
            metadata={"surface": surface, "snapshot_method": self.snapshot_method},
        )


@dataclass
class SystemBoundaryPolicy:
    """Complete system boundary policy combining all sub-policies."""

    edit_surface: EditSurfacePolicy = field(default_factory=EditSurfacePolicy)
    self_improvement: SelfImprovementPolicy = field(
        default_factory=SelfImprovementPolicy
    )
    escalation: EscalationPolicy = field(default_factory=EscalationPolicy)
    workspace_mutation: WorkspaceMutationPolicy = field(
        default_factory=WorkspaceMutationPolicy
    )
    framework_boundary: FrameworkBoundaryPolicy = field(
        default_factory=FrameworkBoundaryPolicy
    )
    sandbox_self_edit: SandboxSelfEditPolicy = field(
        default_factory=SandboxSelfEditPolicy
    )

    # Workspace-first optimization policy
    workspace_first: bool = True

    # Standalone workspace requirement
    standalone_workspace_required: bool = True

    # Documentation-first retrieval preference
    documentation_first_retrieval: bool = True

    # BM25-first hybrid retrieval
    bm25_first_retrieval: bool = True

    @property
    def version(self) -> str:
        """Policy version identifier."""
        return "0.2.0"
