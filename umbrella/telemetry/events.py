"""
Telemetry events for the manager system.

This module defines the event schema for tracking system behavior,
workspace runs, and management decisions.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    """Types of telemetry events."""

    # Workspace lifecycle
    WORKSPACE_SELECTED = "workspace_selected"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    INSTANCE_CREATED = "instance_created"
    INSTANCE_DESTROYED = "instance_destroyed"

    # Patch lifecycle
    PATCH_PROPOSED = "patch_proposed"
    PATCH_APPLIED = "patch_applied"
    PATCH_REVERTED = "patch_reverted"
    PATCH_SKIPPED = "patch_skipped"
    WORKSPACE_CODE_UPDATED = "workspace_code_updated"

    # Evaluation
    EVAL_COMPLETED = "eval_completed"
    COMPARISON_GENERATED = "comparison_generated"

    # Promotion
    PROMOTION_CANDIDATE_CREATED = "promotion_candidate_created"
    PROMOTION_DECISION = "promotion_decision"
    PROMOTION_APPLIED = "promotion_applied"
    PROMOTION_REJECTED = "promotion_rejected"

    # Human interaction
    HUMAN_CHECKPOINT_REQUESTED = "human_checkpoint_requested"
    HUMAN_CHECKPOINT_APPROVED = "human_checkpoint_approved"
    HUMAN_CHECKPOINT_REJECTED = "human_checkpoint_rejected"
    HUMAN_MESSAGE_SENT = "human_message_sent"

    # Self-improvement
    SELF_IMPROVEMENT_CONSIDERED = "self_improvement_considered"
    SELF_IMPROVEMENT_STARTED = "self_improvement_started"
    SELF_IMPROVEMENT_COMPLETED = "self_improvement_completed"
    SELF_IMPROVEMENT_ABORTED = "self_improvement_aborted"

    # Prompt changes
    PROMPT_PATCH_PROPOSED = "prompt_patch_proposed"
    PROMPT_PATCH_APPLIED = "prompt_patch_applied"
    PROMPT_PATCH_REJECTED = "prompt_patch_rejected"

    # Retrieval
    RETRIEVAL_QUERY = "retrieval_query"
    RETRIEVAL_HIT = "retrieval_hit"
    RETRIEVAL_MISS = "retrieval_miss"

    # Memory
    MEMORY_WRITE = "memory_write"
    MEMORY_READ = "memory_read"
    LESSON_LEARNED = "lesson_learned"

    # Error tracking
    ERROR_OCCURRED = "error_occurred"
    WARNING_ISSUED = "warning_issued"

    # System
    SYSTEM_STARTUP = "system_startup"
    SYSTEM_SHUTDOWN = "system_shutdown"
    CONFIG_LOADED = "config_loaded"


@dataclass
class TelemetryEvent:
    """A single telemetry event."""

    event_type: EventType
    timestamp: float = field(default_factory=time.time)

    # Identification
    task_id: str = ""
    workspace_id: str = ""
    run_id: str = ""
    instance_id: str = ""

    # Event-specific data
    data: dict[str, Any] = field(default_factory=dict)

    # Context
    source: str = "umbrella"  # Source component
    level: str = "info"  # log level

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for serialization."""
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "datetime": datetime.fromtimestamp(
                self.timestamp, timezone.utc
            ).isoformat(),
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "data": self.data,
            "source": self.source,
            "level": self.level,
        }


class WorkspaceSelectedEvent(TelemetryEvent):
    """Event emitted when a workspace is selected for a task."""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_id: str,
        seed_workspace_id: str | None = None,
        selection_reason: str = "",
        confidence: float = 0.0,
    ):
        super().__init__(
            event_type=EventType.WORKSPACE_SELECTED,
            task_id=task_id,
            workspace_id=workspace_id,
            data={
                "seed_workspace_id": seed_workspace_id,
                "selection_reason": selection_reason,
                "confidence": confidence,
            },
        )


class RunStartedEvent(TelemetryEvent):
    """Event emitted when a workspace run starts."""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_id: str,
        run_id: str,
        instance_id: str = "",
    ):
        super().__init__(
            event_type=EventType.RUN_STARTED,
            task_id=task_id,
            workspace_id=workspace_id,
            run_id=run_id,
            instance_id=instance_id,
        )


class RunCompletedEvent(TelemetryEvent):
    """Event emitted when a workspace run completes."""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_id: str,
        run_id: str,
        instance_id: str = "",
        status: str = "",
        duration_seconds: float = 0.0,
        total_tokens: int = 0,
        error_count: int = 0,
    ):
        super().__init__(
            event_type=EventType.RUN_COMPLETED,
            task_id=task_id,
            workspace_id=workspace_id,
            run_id=run_id,
            instance_id=instance_id,
            data={
                "status": status,
                "duration_seconds": duration_seconds,
                "total_tokens": total_tokens,
                "error_count": error_count,
            },
        )


class PatchProposedEvent(TelemetryEvent):
    """Event emitted when a patch is proposed."""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_id: str,
        patch_description: str,
        target_files: list[str] | None = None,
        expected_outcome: str = "",
    ):
        super().__init__(
            event_type=EventType.PATCH_PROPOSED,
            task_id=task_id,
            workspace_id=workspace_id,
            data={
                "patch_description": patch_description,
                "target_files": target_files or [],
                "expected_outcome": expected_outcome,
            },
        )


class PatchAppliedEvent(TelemetryEvent):
    """Event emitted when a patch is applied."""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_id: str,
        patch_description: str,
        files_modified: list[str] | None = None,
    ):
        super().__init__(
            event_type=EventType.PATCH_APPLIED,
            task_id=task_id,
            workspace_id=workspace_id,
            data={
                "patch_description": patch_description,
                "files_modified": files_modified or [],
            },
        )


class WorkspaceCodeUpdatedEvent(TelemetryEvent):
    """Event emitted when code is updated directly in the seed workspace."""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_id: str,
        updated_files: list[str],
        backup_path: str = "",
        description: str = "",
    ):
        super().__init__(
            event_type=EventType.WORKSPACE_CODE_UPDATED,
            task_id=task_id,
            workspace_id=workspace_id,
            data={
                "updated_files": updated_files,
                "backup_path": backup_path,
                "description": description,
            },
        )


class EvalCompletedEvent(TelemetryEvent):
    """Event emitted when an evaluation is completed."""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_id: str,
        run_id: str,
        task_success: str,
        output_quality: str,
        overall_score: float,
        total_cost_usd: float,
    ):
        super().__init__(
            event_type=EventType.EVAL_COMPLETED,
            task_id=task_id,
            workspace_id=workspace_id,
            run_id=run_id,
            data={
                "task_success": task_success,
                "output_quality": output_quality,
                "overall_score": overall_score,
                "total_cost_usd": total_cost_usd,
            },
        )


class HumanCheckpointRequestedEvent(TelemetryEvent):
    """Event emitted when human input is requested."""

    def __init__(
        self,
        *,
        task_id: str,
        checkpoint_id: str,
        reason: str,
        checkpoint_type: str = "",
    ):
        super().__init__(
            event_type=EventType.HUMAN_CHECKPOINT_REQUESTED,
            task_id=task_id,
            data={
                "checkpoint_id": checkpoint_id,
                "reason": reason,
                "checkpoint_type": checkpoint_type,
            },
            level="warning",
        )


class SelfImprovementConsideredEvent(TelemetryEvent):
    """Event emitted when self-improvement is considered."""

    def __init__(
        self,
        *,
        task_id: str,
        capability_gap: str,
        gap_evidence: list[str] | None = None,
        decision: str = "",  # "proceed", "skip"
    ):
        super().__init__(
            event_type=EventType.SELF_IMPROVEMENT_CONSIDERED,
            task_id=task_id,
            data={
                "capability_gap": capability_gap,
                "gap_evidence": gap_evidence or [],
                "decision": decision,
            },
        )


class RetrievalQueryEvent(TelemetryEvent):
    """Event emitted when a retrieval query is made."""

    def __init__(
        self,
        *,
        task_id: str,
        query: str,
        index_type: str = "",
        results_count: int = 0,
        latency_ms: float = 0.0,
    ):
        super().__init__(
            event_type=EventType.RETRIEVAL_QUERY,
            task_id=task_id,
            data={
                "query": query,
                "index_type": index_type,
                "results_count": results_count,
                "latency_ms": latency_ms,
            },
        )


class ErrorOccurredEvent(TelemetryEvent):
    """Event emitted when an error occurs."""

    def __init__(
        self,
        *,
        task_id: str,
        error_type: str,
        error_message: str,
        context: dict[str, Any] | None = None,
    ):
        super().__init__(
            event_type=EventType.ERROR_OCCURRED,
            task_id=task_id,
            data={
                "error_type": error_type,
                "error_message": error_message,
                "context": context or {},
            },
            level="error",
        )


class PromotionCandidateCreatedEvent(TelemetryEvent):
    """Event emitted when a promotion candidate is created."""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_id: str,
        candidate_id: str,
        patch_description: str,
        improvement_magnitude: float,
        generalizability_score: float,
    ):
        super().__init__(
            event_type=EventType.PROMOTION_CANDIDATE_CREATED,
            task_id=task_id,
            workspace_id=workspace_id,
            data={
                "candidate_id": candidate_id,
                "patch_description": patch_description,
                "improvement_magnitude": improvement_magnitude,
                "generalizability_score": generalizability_score,
            },
        )


class PromotionDecisionEvent(TelemetryEvent):
    """Event emitted when a promotion decision is made."""

    def __init__(
        self,
        *,
        task_id: str,
        workspace_id: str,
        candidate_id: str,
        decision: str,  # promote, local_only, needs_review, not_eligible
        reasoning: str,
        human_review_required: bool = False,
    ):
        super().__init__(
            event_type=EventType.PROMOTION_DECISION,
            task_id=task_id,
            workspace_id=workspace_id,
            data={
                "candidate_id": candidate_id,
                "decision": decision,
                "reasoning": reasoning,
                "human_review_required": human_review_required,
            },
            level="warning" if human_review_required else "info",
        )


def create_event(
    event_type: EventType,
    **kwargs,
) -> TelemetryEvent:
    """Create a generic telemetry event.

    Args:
        event_type: The type of event to create
        **kwargs: Additional event properties

    Returns:
        TelemetryEvent instance
    """
    return TelemetryEvent(
        event_type=event_type,
        **kwargs,
    )
