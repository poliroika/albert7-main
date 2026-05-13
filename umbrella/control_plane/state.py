"""
State machine for manager task processing.

Defines valid state transitions and manages manager state.
"""

import logging
from typing import Any

from pydantic import BaseModel

from umbrella.control_plane.models import (
    ManagerState,
    ManagerPhase,
)

log = logging.getLogger(__name__)


# =============================================================================
# Valid Transitions
# =============================================================================

# Define valid state transitions
_VALID_TRANSITIONS: dict[ManagerPhase, set[ManagerPhase]] = {
    # Initial phase can go to workspace selection or directly to completion
    ManagerPhase.TASK_RECEIVED: {
        ManagerPhase.WORKSPACE_SELECTED,
        ManagerPhase.TASK_COMPLETE,
        ManagerPhase.TASK_BLOCKED,
    },
    # Workspace selection flows
    ManagerPhase.WORKSPACE_SELECTED: {
        ManagerPhase.INSTANCE_PREPARED,
        ManagerPhase.TASK_FAILED,
        ManagerPhase.TASK_BLOCKED,
    },
    ManagerPhase.INSTANCE_PREPARED: {
        ManagerPhase.KNOWLEDGE_RETRIEVED,
        ManagerPhase.TASK_FAILED,
        ManagerPhase.TASK_BLOCKED,
    },
    # Knowledge retrieval flows
    ManagerPhase.KNOWLEDGE_RETRIEVED: {
        ManagerPhase.WORKSPACE_RUNNING,
        ManagerPhase.TASK_FAILED,
        ManagerPhase.TASK_BLOCKED,
    },
    # Execution flows
    ManagerPhase.WORKSPACE_RUNNING: {
        ManagerPhase.RUN_COMPLETE,
        ManagerPhase.TASK_FAILED,
        ManagerPhase.TASK_BLOCKED,
    },
    # Post-run analysis
    ManagerPhase.RUN_COMPLETE: {
        ManagerPhase.INSPECTION_COMPLETE,
        ManagerPhase.TASK_BLOCKED,
    },
    ManagerPhase.INSPECTION_COMPLETE: {
        ManagerPhase.DECISION_MADE,
        ManagerPhase.LESSON_RECORDED,
        ManagerPhase.TASK_COMPLETE,
        ManagerPhase.TASK_FAILED,
        ManagerPhase.TASK_BLOCKED,
    },
    # Decision flows
    ManagerPhase.DECISION_MADE: {
        ManagerPhase.PATCH_PROPOSED,
        ManagerPhase.SELF_IMPROVEMENT_PENDING,
        ManagerPhase.ESCALATED,
        ManagerPhase.KNOWLEDGE_RETRIEVED,  # Re-run after patch/lesson
        ManagerPhase.RUN_COMPLETE,  # Re-run
        ManagerPhase.LESSON_RECORDED,
        ManagerPhase.TASK_COMPLETE,
        ManagerPhase.TASK_FAILED,
    },
    # Workspace patching
    ManagerPhase.PATCH_PROPOSED: {
        ManagerPhase.PATCH_APPLIED,
        ManagerPhase.TASK_BLOCKED,
    },
    ManagerPhase.PATCH_APPLIED: {
        ManagerPhase.KNOWLEDGE_RETRIEVED,  # Re-run
        ManagerPhase.LESSON_RECORDED,
        ManagerPhase.PROMOTION_CONSIDERATION,
        ManagerPhase.TASK_COMPLETE,
        ManagerPhase.TASK_FAILED,
    },
    # Lesson recording
    ManagerPhase.LESSON_RECORDED: {
        ManagerPhase.KNOWLEDGE_RETRIEVED,
        ManagerPhase.PROMOTION_CONSIDERATION,
        ManagerPhase.TASK_COMPLETE,
        ManagerPhase.DECISION_MADE,  # Continue iterating
        ManagerPhase.TASK_FAILED,
    },
    # Promotion consideration
    ManagerPhase.PROMOTION_CONSIDERATION: {
        ManagerPhase.TASK_COMPLETE,
        ManagerPhase.ESCALATED,  # Human review required
        ManagerPhase.KNOWLEDGE_RETRIEVED,  # Continue with more runs
        ManagerPhase.TASK_FAILED,
    },
    # Self-improvement path
    ManagerPhase.SELF_IMPROVEMENT_PENDING: {
        ManagerPhase.SELF_IMPROVEMENT_APPROVED,
        ManagerPhase.ESCALATED,  # Needs approval
        ManagerPhase.TASK_BLOCKED,
    },
    ManagerPhase.SELF_IMPROVEMENT_APPROVED: {
        ManagerPhase.SELF_IMPROVEMENT_COMPLETE,
        ManagerPhase.TASK_FAILED,
    },
    ManagerPhase.SELF_IMPROVEMENT_COMPLETE: {
        ManagerPhase.TASK_RECEIVED,  # Restart task
        ManagerPhase.KNOWLEDGE_RETRIEVED,  # Resume with new knowledge
    },
    # Escalation
    ManagerPhase.ESCALATED: {
        ManagerPhase.ESCALATION_RESOLVED,
        ManagerPhase.TASK_BLOCKED,
    },
    ManagerPhase.ESCALATION_RESOLVED: {
        ManagerPhase.DECISION_MADE,  # Continue with resolution
        ManagerPhase.TASK_COMPLETE,
        ManagerPhase.TASK_FAILED,
    },
    # Terminal states
    ManagerPhase.TASK_COMPLETE: set(),
    ManagerPhase.TASK_FAILED: set(),
    ManagerPhase.TASK_BLOCKED: set(),
}


# =============================================================================
# State Machine
# =============================================================================


class ManagerStateMachine:
    """Manages state transitions for the manager."""

    def __init__(self, initial_state: ManagerState | None = None):
        self.state = initial_state or ManagerState(task_id="")

    def can_transition_to(self, new_phase: ManagerPhase) -> bool:
        """Check if a transition is valid."""
        if self.state.phase not in _VALID_TRANSITIONS:
            log.warning(f"Unknown current phase: {self.state.phase}")
            return False

        valid_targets = _VALID_TRANSITIONS[self.state.phase]
        return new_phase in valid_targets

    def transition_to(
        self, new_phase: ManagerPhase, reason: str = ""
    ) -> "StateTransition":
        """Transition to a new phase.

        Args:
            new_phase: The phase to transition to
            reason: Why this transition is happening

        Returns:
            StateTransition record

        Raises:
            ValueError: If transition is invalid
        """
        if not self.can_transition_to(new_phase):
            raise ValueError(
                f"Invalid transition from {self.state.phase} to {new_phase}. "
                f"Reason: {reason}"
            )

        old_phase = self.state.phase
        self.state.transition_to(new_phase)

        # Increment iteration count when entering execution phases
        if new_phase in {
            ManagerPhase.INSTANCE_PREPARED,
            ManagerPhase.WORKSPACE_RUNNING,
            ManagerPhase.PATCH_APPLIED,
        }:
            self.state.iteration_count += 1

        log.info(
            f"State transition: {old_phase} -> {new_phase} "
            f"(task_id={self.state.task_id}, reason={reason or 'N/A'})"
        )

        return StateTransition.create(
            task_id=self.state.task_id,
            from_phase=old_phase,
            to_phase=new_phase,
            reason=reason,
        )

    def force_phase(
        self, new_phase: ManagerPhase, reason: str = ""
    ) -> "StateTransition":
        """Force a phase change for explicit human/operator overrides."""
        old_phase = self.state.phase
        self.state.transition_to(new_phase)
        log.info(
            "State force-transition: %s -> %s (task_id=%s, reason=%s)",
            old_phase,
            new_phase,
            self.state.task_id,
            reason or "N/A",
        )
        return StateTransition.create(
            task_id=self.state.task_id,
            from_phase=old_phase,
            to_phase=new_phase,
            reason=reason,
        )

    def is_terminal(self) -> bool:
        """Check if current phase is terminal."""
        return self.state.phase in {
            ManagerPhase.TASK_COMPLETE,
            ManagerPhase.TASK_FAILED,
            ManagerPhase.TASK_BLOCKED,
        }

    def is_self_improvement_active(self) -> bool:
        """Check if currently in self-improvement path."""
        return self.state.phase in {
            ManagerPhase.SELF_IMPROVEMENT_PENDING,
            ManagerPhase.SELF_IMPROVEMENT_APPROVED,
            ManagerPhase.SELF_IMPROVEMENT_COMPLETE,
        }

    def is_escalated(self) -> bool:
        """Check if currently escalated to human."""
        return self.state.phase == ManagerPhase.ESCALATED

    def get_valid_next_phases(self) -> set[ManagerPhase]:
        """Get valid next phases from current state."""
        return _VALID_TRANSITIONS.get(self.state.phase, set())


class StateTransition(BaseModel):
    """Record of a state transition."""

    task_id: str
    from_phase: ManagerPhase
    to_phase: ManagerPhase
    reason: str
    timestamp: float
    decision_context: dict[str, Any] = {}

    @classmethod
    def create(
        cls,
        task_id: str,
        from_phase: ManagerPhase,
        to_phase: ManagerPhase,
        reason: str = "",
        **context: Any,
    ) -> "StateTransition":
        import time

        return cls(
            task_id=task_id,
            from_phase=from_phase,
            to_phase=to_phase,
            reason=reason,
            timestamp=time.time(),
            decision_context=context,
        )


def transition_to(phase: ManagerPhase, reason: str = "") -> None:
    """Decorator to wrap a function that causes a state transition.

    Usage:
        @transition_to(ManagerPhase.RUN_COMPLETE, "Workspace finished")
        def run_workspace(...):
            ...
    """

    def decorator(func):
        def wrapper(self, *args, **kwargs):
            result = func(self, *args, **kwargs)

            # Transition after function executes
            if hasattr(self, "state_machine"):
                self.state_machine.transition_to(phase, reason)

            return result

        return wrapper
