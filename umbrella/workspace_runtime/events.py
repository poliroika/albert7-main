"""
Lifecycle events for the workspace runtime.

This module defines events that occur during workspace execution,
enabling monitoring and observability.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from umbrella.workspace_runtime.models import (
    WorkspaceLifecycleStage,
    WorkspaceRunStatus,
)


class WorkspaceEventType(str, Enum):
    """Types of workspace lifecycle events."""

    # Lifecycle events
    CREATED = "created"
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    RESUMED = "resumed"

    # Stage events
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_FAILED = "stage_failed"

    # Artifact events
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_COLLECTED = "artifact_collected"

    # Checkpoint events
    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_RESTORED = "checkpoint_restored"

    # Snapshot events
    SNAPSHOT_CREATED = "snapshot_created"
    SNAPSHOT_RESTORED = "snapshot_restored"

    # Patch events
    PATCH_PROPOSED = "patch_proposed"
    PATCH_APPLIED = "patch_applied"
    PATCH_ROLLED_BACK = "patch_rolled_back"

    # Error events
    ERROR_OCCURRED = "error_occurred"
    WARNING_OCCURRED = "warning_occurred"


@dataclass
class WorkspaceLifecycleEvent:
    """
    An event that occurs during workspace lifecycle.

    Events are the primary observability mechanism for the workspace runtime.
    """

    # Event identification
    event_type: WorkspaceEventType
    event_id: str = field(default_factory=lambda: str(uuid4())[:8])
    event_type: WorkspaceEventType

    # Context
    workspace_id: str = ""
    instance_id: str = ""
    run_id: str = ""
    task_id: str | None = None

    # Timing
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Event details
    stage: WorkspaceLifecycleStage | None = None
    status: WorkspaceRunStatus | None = None

    # Event data
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    # Related artifacts
    artifact_ids: list[str] = field(default_factory=list)

    # Parent event (for chained events)
    parent_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "workspace_id": self.workspace_id,
            "instance_id": self.instance_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "timestamp": self.timestamp.isoformat(),
            "stage": self.stage.value if self.stage else None,
            "status": self.status.value if self.status else None,
            "message": self.message,
            "details": self.details,
            "artifact_ids": self.artifact_ids,
            "parent_event_id": self.parent_event_id,
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())


class WorkspaceEventLog:
    """
    Log of workspace lifecycle events.

    Provides event persistence and querying capabilities.
    """

    def __init__(self, log_path: Path | None = None):
        """
        Initialize the event log.

        Args:
            log_path: Optional path to persist events (defaults to in-memory only)
        """
        self._events: list[WorkspaceLifecycleEvent] = []
        self._log_path = log_path

        if log_path and log_path.exists():
            self._load_events()

    def _load_events(self) -> None:
        """Load existing events from the log file."""
        if not self._log_path:
            return

        try:
            with open(self._log_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        event_data = json.loads(line)
                        event = self._deserialize_event(event_data)
                        if event:
                            self._events.append(event)
        except Exception:
            pass

    def _deserialize_event(
        self, data: dict[str, Any]
    ) -> WorkspaceLifecycleEvent | None:
        """Deserialize an event from JSON data."""
        try:
            return WorkspaceLifecycleEvent(
                event_id=data["event_id"],
                event_type=WorkspaceEventType(data["event_type"]),
                workspace_id=data.get("workspace_id", ""),
                instance_id=data.get("instance_id", ""),
                run_id=data.get("run_id", ""),
                task_id=data.get("task_id"),
                timestamp=datetime.fromisoformat(data["timestamp"]),
                stage=WorkspaceLifecycleStage(data["stage"])
                if data.get("stage")
                else None,
                status=WorkspaceRunStatus(data["status"])
                if data.get("status")
                else None,
                message=data.get("message", ""),
                details=data.get("details", {}),
                artifact_ids=data.get("artifact_ids", []),
                parent_event_id=data.get("parent_event_id"),
            )
        except Exception:
            return None

    def log(self, event: WorkspaceLifecycleEvent) -> None:
        """
        Log an event.

        Args:
            event: The event to log
        """
        self._events.append(event)

        if self._log_path:
            self._persist_event(event)

    def _persist_event(self, event: WorkspaceLifecycleEvent) -> None:
        """Persist an event to the log file."""
        if not self._log_path:
            return

        self._log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(event.to_json() + "\n")

    def get_events(
        self,
        workspace_id: str | None = None,
        instance_id: str | None = None,
        run_id: str | None = None,
        event_types: list[WorkspaceEventType] | None = None,
    ) -> list[WorkspaceLifecycleEvent]:
        """
        Get events matching the given filters.

        Args:
            workspace_id: Filter by workspace ID
            instance_id: Filter by instance ID
            run_id: Filter by run ID
            event_types: Filter by event types

        Returns:
            List of matching events
        """
        events = self._events

        if workspace_id:
            events = [e for e in events if e.workspace_id == workspace_id]
        if instance_id:
            events = [e for e in events if e.instance_id == instance_id]
        if run_id:
            events = [e for e in events if e.run_id == run_id]
        if event_types:
            type_set = {et.value for et in event_types}
            events = [e for e in events if e.event_type.value in type_set]

        return events

    def get_latest_events(self, count: int = 10) -> list[WorkspaceLifecycleEvent]:
        """
        Get the most recent events.

        Args:
            count: Maximum number of events to return

        Returns:
            List of most recent events
        """
        return self._events[-count:] if len(self._events) > count else self._events

    def get_event_by_id(self, event_id: str) -> WorkspaceLifecycleEvent | None:
        """Get an event by its ID."""
        for event in self._events:
            if event.event_id == event_id:
                return event
        return None


class WorkspaceEventEmitter:
    """
    Emits workspace lifecycle events.

    Provides a convenient interface for creating and logging events.
    """

    def __init__(self, event_log: WorkspaceEventLog):
        """
        Initialize the event emitter.

        Args:
            event_log: The event log to emit events to
        """
        self._log = event_log
        self._context: dict[str, str] = {}

    def set_context(
        self,
        workspace_id: str = "",
        instance_id: str = "",
        run_id: str = "",
        task_id: str | None = None,
    ) -> None:
        """Set the context for emitted events."""
        self._context = {
            "workspace_id": workspace_id,
            "instance_id": instance_id,
            "run_id": run_id,
            "task_id": task_id or "",
        }

    def emit(
        self,
        event_type: WorkspaceEventType,
        message: str = "",
        details: dict[str, Any] | None = None,
        stage: WorkspaceLifecycleStage | None = None,
        status: WorkspaceRunStatus | None = None,
        artifact_ids: list[str] | None = None,
        parent_event_id: str | None = None,
    ) -> WorkspaceLifecycleEvent:
        """
        Emit a lifecycle event.

        Args:
            event_type: Type of event
            message: Event message
            details: Optional event details
            stage: Optional lifecycle stage
            status: Optional run status
            artifact_ids: Optional related artifact IDs
            parent_event_id: Optional parent event ID

        Returns:
            The emitted event
        """
        event = WorkspaceLifecycleEvent(
            event_type=event_type,
            workspace_id=self._context.get("workspace_id", ""),
            instance_id=self._context.get("instance_id", ""),
            run_id=self._context.get("run_id", ""),
            task_id=self._context.get("task_id"),
            message=message,
            details=details or {},
            stage=stage,
            status=status,
            artifact_ids=artifact_ids or [],
            parent_event_id=parent_event_id,
        )

        self._log.log(event)
        return event

    def emit_created(
        self, message: str = "Workspace created"
    ) -> WorkspaceLifecycleEvent:
        """Emit a workspace created event."""
        return self.emit(
            WorkspaceEventType.CREATED,
            message=message,
        )

    def emit_prepared(
        self, message: str = "Workspace prepared"
    ) -> WorkspaceLifecycleEvent:
        """Emit a workspace prepared event."""
        return self.emit(
            WorkspaceEventType.PREPARED,
            message=message,
            stage=WorkspaceLifecycleStage.PREPARE,
        )

    def emit_running(
        self, message: str = "Workspace running"
    ) -> WorkspaceLifecycleEvent:
        """Emit a workspace running event."""
        return self.emit(
            WorkspaceEventType.RUNNING,
            message=message,
            stage=WorkspaceLifecycleStage.RUN,
            status=WorkspaceRunStatus.RUNNING,
        )

    def emit_completed(
        self, message: str = "Workspace completed"
    ) -> WorkspaceLifecycleEvent:
        """Emit a workspace completed event."""
        return self.emit(
            WorkspaceEventType.COMPLETED,
            message=message,
            status=WorkspaceRunStatus.COMPLETED,
        )

    def emit_failed(
        self, message: str, details: dict[str, Any] | None = None
    ) -> WorkspaceLifecycleEvent:
        """Emit a workspace failed event."""
        return self.emit(
            WorkspaceEventType.FAILED,
            message=message,
            details=details,
            status=WorkspaceRunStatus.FAILED,
        )

    def emit_stage_started(
        self, stage: WorkspaceLifecycleStage
    ) -> WorkspaceLifecycleEvent:
        """Emit a stage started event."""
        return self.emit(
            WorkspaceEventType.STAGE_STARTED,
            message=f"Stage {stage.value} started",
            stage=stage,
        )

    def emit_stage_completed(
        self, stage: WorkspaceLifecycleStage
    ) -> WorkspaceLifecycleEvent:
        """Emit a stage completed event."""
        return self.emit(
            WorkspaceEventType.STAGE_COMPLETED,
            message=f"Stage {stage.value} completed",
            stage=stage,
        )

    def emit_artifact_created(
        self,
        artifact_id: str,
        artifact_type: str,
        path: str,
    ) -> WorkspaceLifecycleEvent:
        """Emit an artifact created event."""
        return self.emit(
            WorkspaceEventType.ARTIFACT_CREATED,
            message=f"Artifact created: {artifact_id}",
            artifact_ids=[artifact_id],
            details={
                "artifact_type": artifact_type,
                "path": path,
            },
        )

    def emit_checkpoint_created(self, checkpoint_id: str) -> WorkspaceLifecycleEvent:
        """Emit a checkpoint created event."""
        return self.emit(
            WorkspaceEventType.CHECKPOINT_CREATED,
            message=f"Checkpoint created: {checkpoint_id}",
            artifact_ids=[checkpoint_id],
        )

    def emit_error(
        self, error_message: str, error_details: dict[str, Any] | None = None
    ) -> WorkspaceLifecycleEvent:
        """Emit an error event."""
        return self.emit(
            WorkspaceEventType.ERROR_OCCURRED,
            message=error_message,
            details=error_details,
            status=WorkspaceRunStatus.FAILED,
        )
