"""
Human escalation - when the manager needs human input.

Handles:
- Blocking constraints that prevent autonomous action
- High-risk changes that require approval
- Cases where the manager cannot proceed
- Human checkpoint interface
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from umbrella.control_plane.models import (
    DecisionContext,
    EscalationRecord,
    EscalationReason,
    EscalationStatus,
    ActionType,
)
from umbrella.control_plane.models import generate_escalation_id

log = logging.getLogger(__name__)


# =============================================================================
# Escalation Checks
# =============================================================================


@dataclass
class BlockingConstraints:
    """Constraints that require human approval."""

    # Surface changes that need approval
    gm_modification_allowed: bool = False
    seed_promotion_allowed: bool = False
    task_main_modification_allowed: bool = False

    # Strategic changes that need approval
    major_architectural_shift_allowed: bool = False
    workspace_swap_allowed: bool = False

    # Policy-related -- self-improvement is allowed autonomously; the human
    # checkpoint mechanism already gates risky prompt rewrites separately.
    policy_change_allowed: bool = True
    prompt_rewrite_allowed: bool = True
    self_improve_allowed: bool = True

    # Safety
    requires_safety_review: bool = False

    def check_action_allowed(self, action: ActionType) -> tuple[bool, str | None]:
        """Check if an action is allowed under current constraints.

        Args:
            action: Action to check

        Returns:
            (is_allowed, reason_if_not_allowed)
        """
        # Self-improvement actions
        if action == ActionType.SELF_IMPROVE:
            if not self.self_improve_allowed:
                return False, "Self-improvement is not allowed"

        # Prompt stack modification
        elif action == ActionType.REWRITE_PROMPT_STACK:
            if not self.prompt_rewrite_allowed:
                return False, "Prompt stack modification is not allowed"
            return True, None

        # Workspace modifications
        elif action == ActionType.PATCH_WORKSPACE:
            pass

        # Framework changes
        elif action in (ActionType.MODIFY_POLICY,):
            if not self.policy_change_allowed:
                return False, "Policy modification is not allowed"

        return True, None


# =============================================================================
# Escalation Management
# =============================================================================


class HumanEscalation:
    """Manages escalation to human review."""

    def __init__(self, escalation_dir: Path):
        self.escalation_dir = escalation_dir
        self.escalation_dir.mkdir(parents=True, exist_ok=True)

    def check_blocking_constraints(
        self,
        context: DecisionContext,
        action: ActionType,
    ) -> tuple[bool, EscalationRecord | None]:
        """Check if an action is blocked by constraints.

        Args:
            context: Decision context
            action: Action being considered

        Returns:
            (is_blocked, escalation_record_if_blocked)
        """
        constraints = BlockingConstraints()

        is_allowed, block_reason = constraints.check_action_allowed(action)

        if is_allowed:
            return False, None

        # Create escalation record
        escalation = EscalationRecord(
            id=generate_escalation_id(),
            task_id=context.task_id,
            reason=EscalationReason.POLICY_VIOLATION,
            description=f"Action {action} requires approval: {block_reason}",
            status=EscalationStatus.PENDING,
            current_phase=context.manager_state.phase,
            workspace_id=context.workspace_id,
        )

        return True, escalation

    def escalate_to_human(
        self,
        context: DecisionContext,
        reason: EscalationReason,
        description: str,
        action: ActionType | None = None,
        details: dict[str, Any] | None = None,
    ) -> EscalationRecord:
        """Create an escalation to human.

        Args:
            context: Decision context
            reason: Why escalation is needed
            description: What needs human input
            action: Action that triggered escalation
            details: Additional context

        Returns:
            Escalation record
        """
        escalation = EscalationRecord(
            id=generate_escalation_id(),
            task_id=context.task_id,
            reason=reason,
            description=description,
            status=EscalationStatus.PENDING,
            current_phase=context.manager_state.phase,
            workspace_id=context.workspace_id,
            triggering_action=action,
        )

        # Save to file
        self._save_escalation(escalation)

        log.warning(
            f"Escalation created: {escalation.id} - {description} "
            f"(reason={reason}, task={context.task_id})"
        )

        return escalation

    def resolve_escalation(
        self,
        escalation_id: str,
        resolution: str,
        approved_by: Literal["human", "auto"] = "human",
    ) -> EscalationRecord:
        """Resolve an escalation with human input.

        Args:
            escalation_id: Escalation to resolve
            resolution: Human's response/instruction
            approved_by: Who approved this

        Returns:
            Updated escalation record
        """
        escalation = self.get_escalation(escalation_id)
        if escalation is None:
            raise ValueError(f"Escalation {escalation_id} not found")

        escalation.status = EscalationStatus.RESOLVED
        escalation.human_response = resolution
        escalation.resolved_at = time.time()

        # Save updated record
        self._save_escalation(escalation)

        log.info(f"Escalation {escalation_id} resolved: {resolution}")
        return escalation

    def get_escalation(self, escalation_id: str) -> EscalationRecord | None:
        """Get an escalation by ID.

        Args:
            escalation_id: Escalation ID

        Returns:
            Escalation record or None
        """
        path = self.escalation_dir / f"{escalation_id}.json"

        if not path.exists():
            return None

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        from umbrella.control_plane.models import EscalationRecord

        return EscalationRecord(**data)

    def get_pending_escalations(
        self, task_id: str | None = None
    ) -> list[EscalationRecord]:
        """Get all pending escalations.

        Args:
            task_id: Optional task filter

        Returns:
            List of pending escalations
        """
        pending = []

        for path in self.escalation_dir.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                    escalation = EscalationRecord(**data)

                if escalation.status == EscalationStatus.PENDING:
                    if task_id is None or escalation.task_id == task_id:
                        pending.append(escalation)
            except Exception as e:
                log.warning(f"Failed to load escalation from {path}: {e}")

        # Sort by creation time (oldest first)
        pending.sort(key=lambda e: e.created_at)

        return pending

    def _save_escalation(self, escalation: EscalationRecord) -> None:
        """Save escalation to file."""
        path = self.escalation_dir / f"{escalation.id}.json"

        with open(path, "w", encoding="utf-8") as f:
            json.dump(escalation.model_dump(), f, indent=2)


# =============================================================================
# Public API Functions
# =============================================================================


def check_blocking_constraints(
    context: DecisionContext,
    action: ActionType,
) -> tuple[bool, list[str]]:
    """Check if an action is blocked by constraints.

    Args:
        context: Decision context
        action: Action being considered

    Returns:
        (is_blocked, blocking_reasons)
    """
    constraints = BlockingConstraints()
    is_allowed, block_reason = constraints.check_action_allowed(action)

    if is_allowed:
        return False, []

    return True, [block_reason or "Action not allowed under current constraints"]


def escalate_to_human(
    escalation_dir: Path,
    context: DecisionContext,
    reason: EscalationReason,
    description: str,
    action: ActionType | None = None,
) -> EscalationRecord:
    """Create an escalation to human review.

    This is the main entry point for escalation.

    Args:
        escalation_dir: Directory for escalation records
        context: Decision context
        reason: Why escalation is needed
        description: What needs human input
        action: Action that triggered this

    Returns:
        Created escalation record
    """
    escalation_mgr = HumanEscalation(escalation_dir)

    return escalation_mgr.escalate_to_human(
        context=context,
        reason=reason,
        description=description,
        action=action,
    )


def get_pending_escalations(
    escalation_dir: Path,
    task_id: str | None = None,
) -> list[EscalationRecord]:
    """Get pending escalations that need human input.

    Args:
        escalation_dir: Directory for escalation records
        task_id: Optional task filter

    Returns:
        List of pending escalations
    """
    escalation_mgr = HumanEscalation(escalation_dir)
    return escalation_mgr.get_pending_escalations(task_id)
