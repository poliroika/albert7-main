"""
Policy engine for making decisions about edit permissions,
self-improvement, and human escalation.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from umbrella.policies.defaults import load_default_policy
from umbrella.policies.models import (
    SystemBoundaryPolicy,
    EditSurfaceCategory,
    SelfImprovementTrigger,
    EscalationReason,
    PolicyDecision,
)

log = logging.getLogger(__name__)
_DEFAULT_POLICY = load_default_policy()
_ARTIFACT_DIRS = {"runs", "reports", "snapshots", "artifacts", "memory", "logs"}
_SELF_IMPROVEMENT_ACTIONS = {"self_improvement", "self_improve", "self_patch"}
_SANDBOX_SELF_EDIT_ACTIONS = {
    "sandbox_self_edit",
    "sandbox_edit",
    "capability_gap_edit",
}


def _effective_policy(policy: SystemBoundaryPolicy | None) -> SystemBoundaryPolicy:
    return policy or _DEFAULT_POLICY


def _normalized_parts(path: Path, repo_root: Path | None = None) -> list[str]:
    raw_path = Path(path)
    if repo_root is not None:
        try:
            raw_path = raw_path.resolve(strict=False).relative_to(
                Path(repo_root).resolve(strict=False)
            )
        except Exception:
            pass

    parts = [
        part.lower()
        for part in raw_path.parts
        if part not in ("", ".", raw_path.anchor)
    ]
    anchors = {"gmas", "ouroboros", "umbrella", "workspaces", "deep_coding_tasks"}
    for index, part in enumerate(parts):
        if part in anchors:
            return parts[index:]
    return parts


def classify_path(
    path: Path,
    repo_root: Path | None = None,
    _policy: SystemBoundaryPolicy | None = None,
) -> EditSurfaceCategory:
    """Classify a path into an edit surface category."""
    del _policy
    parts = _normalized_parts(path, repo_root)
    if not parts:
        return EditSurfaceCategory.UNKNOWN

    if parts[0] == "gmas":
        return EditSurfaceCategory.FRAMEWORK
    if parts[0] == "ouroboros":
        return EditSurfaceCategory.MANAGER
    if parts[0] == "umbrella":
        return EditSurfaceCategory.INTEGRATION
    if parts[0] == "deep_coding_tasks":
        return EditSurfaceCategory.REPO_DOCS

    if parts[0] == "workspaces" and len(parts) >= 2:
        if parts[1] == "instances":
            if len(parts) >= 4 and parts[3] in _ARTIFACT_DIRS:
                return EditSurfaceCategory.ARTIFACTS
            return EditSurfaceCategory.WORKSPACE_INSTANCE
        if len(parts) >= 3 and parts[2] in _ARTIFACT_DIRS:
            return EditSurfaceCategory.ARTIFACTS
        return (
            EditSurfaceCategory.WORKSPACE_SEED
            if parts[1] == "agent_research"
            else EditSurfaceCategory.WORKSPACE_INSTANCE
        )

    return EditSurfaceCategory.UNKNOWN


def can_edit_path(
    path: Path,
    actor: str = "ouroboros",
    action: str = "write",
    repo_root: Path | None = None,
    _policy: SystemBoundaryPolicy | None = None,
) -> PolicyDecision:
    """Check if a path can be edited by the actor/action pair."""
    policy = _effective_policy(_policy)
    category = classify_path(path, repo_root, _policy=policy)

    if category == EditSurfaceCategory.FRAMEWORK:
        return policy.framework_boundary.can_modify(path, human_approved=False)

    if category == EditSurfaceCategory.MANAGER:
        if action.lower() in _SANDBOX_SELF_EDIT_ACTIONS:
            sandbox_decision = policy.sandbox_self_edit.can_sandbox_edit(
                surface="ouroboros",
                capability_gap_detected=True,
            )
            if sandbox_decision.allowed:
                return PolicyDecision(
                    allowed=True,
                    reason=(
                        f"Path '{path}' is manager-owned. Sandbox self-edit allowed; "
                        "changes will be rolled back after task completion."
                    ),
                    policy_id="manager_sandbox_self_edit",
                    escalation_required=False,
                    metadata={"actor": actor, "action": action, "sandbox": True},
                )
        if (
            action.lower() in _SELF_IMPROVEMENT_ACTIONS
            and policy.edit_surface.manager_editable_under_self_improvement
        ):
            return PolicyDecision(
                allowed=True,
                reason=(
                    f"Path '{path}' is manager-owned. Manager edits are allowed only inside an explicit "
                    "self-improvement flow and still require human notification."
                ),
                policy_id="manager_edit_conditional",
                escalation_required=policy.escalation.escalate_self_improvement,
                escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
                metadata={"actor": actor, "action": action},
            )
        return PolicyDecision(
            allowed=False,
            reason=(
                f"Path '{path}' is manager-owned. Use an explicit self-improvement action before patching "
                "manager code."
            ),
            policy_id="manager_edit_requires_self_improvement",
            escalation_required=True,
            escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
        )

    if category == EditSurfaceCategory.INTEGRATION:
        if action.lower() in _SANDBOX_SELF_EDIT_ACTIONS:
            sandbox_decision = policy.sandbox_self_edit.can_sandbox_edit(
                surface="umbrella",
                capability_gap_detected=True,
            )
            if sandbox_decision.allowed:
                return PolicyDecision(
                    allowed=True,
                    reason=(
                        f"Path '{path}' is integration layer. Sandbox self-edit allowed; "
                        "changes will be rolled back after task completion."
                    ),
                    policy_id="integration_sandbox_self_edit",
                    escalation_required=False,
                    metadata={"surface": category.value, "sandbox": True},
                )
        decision = policy.edit_surface.get_mutability(category)
        return PolicyDecision(
            allowed=decision.allowed,
            reason=f"Path '{path}' is classified as integration layer (umbrella/). {decision.reason}",
            policy_id=decision.policy_id,
            escalation_required=decision.escalation_required,
            escalation_reason=decision.escalation_reason,
            metadata={"surface": category.value},
        )

    if category == EditSurfaceCategory.WORKSPACE_INSTANCE:
        decision = policy.edit_surface.get_mutability(category)
        return PolicyDecision(
            allowed=decision.allowed,
            reason=f"Path '{path}' is classified as workspace instance. {decision.reason}",
            policy_id=decision.policy_id,
            escalation_required=decision.escalation_required,
            escalation_reason=decision.escalation_reason,
            metadata={"surface": category.value},
        )

    if category == EditSurfaceCategory.REPO_DOCS:
        return PolicyDecision(
            allowed=True,
            reason=f"Path '{path}' is repository task documentation and is safe to edit.",
            policy_id="repo_docs_edit",
            metadata={"surface": category.value},
        )

    if category == EditSurfaceCategory.WORKSPACE_SEED:
        return PolicyDecision(
            allowed=False,
            reason=f"Path '{path}' is a seed workspace. Seed edits require promotion evidence rather than direct mutation.",
            policy_id="workspace_seed_promotion",
            escalation_required=True,
            escalation_reason=EscalationReason.SEED_PROMOTION_WITHOUT_EVIDENCE,
        )

    if category == EditSurfaceCategory.ARTIFACTS:
        decision = policy.edit_surface.get_mutability(EditSurfaceCategory.ARTIFACTS)
        return PolicyDecision(
            allowed=decision.allowed,
            reason=f"Path '{path}' is classified as artifacts. {decision.reason}",
            policy_id=decision.policy_id,
            escalation_required=decision.escalation_required,
            escalation_reason=decision.escalation_reason,
            metadata={"surface": category.value},
        )

    return PolicyDecision(
        allowed=False,
        reason=f"Unknown surface for path: {path}",
        policy_id="unknown_surface",
        escalation_required=True,
        escalation_reason=EscalationReason.UNCLEAR_PATH_CLASSIFICATION,
    )


def should_prefer_workspace_patch(
    context: dict[str, Any] | None = None,
    _policy: SystemBoundaryPolicy | None = None,
) -> PolicyDecision:
    """Check if a workspace patch should be preferred over self-patching."""
    policy = _effective_policy(_policy)
    return PolicyDecision(
        allowed=policy.workspace_first,
        reason="By default, workspace patches are preferred over self-patches",
        policy_id="workspace_first_default",
        metadata={"context": context} if context is not None else {},
    )


def can_trigger_self_improvement(
    context: dict[str, Any],
    _policy: SystemBoundaryPolicy | None = None,
) -> PolicyDecision:
    """Check if self-improvement can be triggered given context."""
    policy = _effective_policy(_policy)
    intent = context.get("intent")

    if intent == SelfImprovementTrigger.CAPABILITY_GAP:
        has_gap = (
            context.get("capability_gap_detected", False)
            or context.get("capability_gaps", 0) > 0
        )
        if has_gap:
            return PolicyDecision(
                allowed=True,
                reason="Demonstrated manager-level capability gap",
                policy_id="self_improvement.capability_gap",
                escalation_required=True,
                escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
            )
        return PolicyDecision(
            allowed=False,
            reason="No demonstrated capability gap",
            policy_id="self_improvement.no_capability_gap",
        )

    if intent == SelfImprovementTrigger.EXPLICIT_REQUEST:
        return policy.self_improvement.can_trigger(
            SelfImprovementTrigger.EXPLICIT_REQUEST, context
        )

    if intent in {
        SelfImprovementTrigger.REPEATED_FAILURES,
        SelfImprovementTrigger.NO_WORKSPACE_PROGRESS,
        SelfImprovementTrigger.RETRIEVAL_WEAKNESS,
    }:
        return policy.self_improvement.can_trigger(intent, context)

    return PolicyDecision(
        allowed=False,
        reason="No valid self-improvement triggers provided",
        policy_id="self_improvement.no_triggers",
    )


def requires_human_escalation(
    context: dict[str, Any],
    _policy: SystemBoundaryPolicy | None = None,
) -> PolicyDecision:
    """Check if a situation requires human escalation."""
    policy = _effective_policy(_policy)
    intent = context.get("intent")

    if intent == EscalationReason.UNSAFE_MASS_EDIT:
        file_count = int(context.get("file_count", 0))
        if file_count >= policy.escalation.mass_edit_threshold:
            return PolicyDecision(
                allowed=False,
                reason=(
                    f"Mass edit ({file_count} files) exceeds threshold "
                    f"({policy.escalation.mass_edit_threshold})"
                ),
                policy_id="mass_edit_escalation",
                escalation_required=True,
                escalation_reason=EscalationReason.UNSAFE_MASS_EDIT,
                metadata={"file_count": file_count},
            )
        return PolicyDecision(
            allowed=True,
            reason="Mass edit is below the escalation threshold",
            policy_id="mass_edit_below_threshold",
            metadata={"file_count": file_count},
        )

    if intent == EscalationReason.SELF_IMPROVEMENT_TRIGGERED:
        return PolicyDecision(
            allowed=True,
            reason="Self-improvement cycle triggered",
            policy_id="self_improvement_escalation",
            escalation_required=policy.escalation.escalate_self_improvement,
            escalation_reason=EscalationReason.SELF_IMPROVEMENT_TRIGGERED,
        )

    if intent in {
        EscalationReason.FRAMEWORK_MODIFICATION,
        EscalationReason.RISKY_PROMPT_CHANGE,
        EscalationReason.SEED_PROMOTION_WITHOUT_EVIDENCE,
    }:
        return PolicyDecision(
            allowed=False,
            reason=f"Human escalation required for: {intent.value}",
            policy_id=f"escalation.{intent.value}",
            escalation_required=True,
            escalation_reason=intent,
        )

    return PolicyDecision(
        allowed=True,
        reason="No escalation required",
        policy_id="default_allow",
    )


class PolicyEngine:
    """Engine for evaluating policy decisions."""

    def __init__(self, policy: SystemBoundaryPolicy):
        self.policy = policy

    def classify_path(self, path: Path) -> EditSurfaceCategory:
        """Classify a path into an edit surface category."""
        return classify_path(path, _policy=self.policy)

    def can_edit_path(
        self, path: Path, actor: str = "ouroboros", action: str = "write"
    ) -> PolicyDecision:
        """Check if a path can be edited."""
        return can_edit_path(path, actor, action, _policy=self.policy)

    def should_prefer_workspace_patch(
        self, context: dict[str, Any] | None = None
    ) -> PolicyDecision:
        """Check if workspace patch should be preferred."""
        return should_prefer_workspace_patch(context, _policy=self.policy)

    def can_trigger_self_improvement(self, context: dict[str, Any]) -> PolicyDecision:
        """Check if self-improvement can be triggered."""
        return can_trigger_self_improvement(context, _policy=self.policy)

    def requires_human_escalation(self, context: dict[str, Any]) -> PolicyDecision:
        """Check if human escalation is required."""
        return requires_human_escalation(context, _policy=self.policy)

    def can_sandbox_self_edit(
        self,
        surface: str,
        capability_gap_detected: bool = False,
        changed_files_count: int = 0,
    ) -> PolicyDecision:
        """Check if a temporary sandbox self-edit is allowed."""
        return self.policy.sandbox_self_edit.can_sandbox_edit(
            surface=surface,
            capability_gap_detected=capability_gap_detected,
            changed_files_count=changed_files_count,
        )
