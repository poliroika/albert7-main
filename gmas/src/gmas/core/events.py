"""
Event and hook system for monitoring.

Provides:
- Typed events for all operations
- Event subscription via handlers
- Event hooks for external monitoring
- Structured change logging
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel, Field

from gmas.config.logging import logger

__all__ = [
    # Budget events
    "BudgetEvent",
    "BudgetExceededEvent",
    "BudgetWarningEvent",
    "EdgeAddedEvent",
    "EdgeRemovedEvent",
    "EdgeUpdatedEvent",
    # Base event
    "Event",
    "EventBus",
    # Event handler
    "EventHandler",
    "EventPriority",
    # Event types
    "EventType",
    # Execution events
    "ExecutionEvent",
    "GlobalEventBus",
    # Graph events
    "GraphEvent",
    # Logging handler
    "LoggingEventHandler",
    # Memory events
    "MemoryEvent",
    "MemoryExpiredEvent",
    "MemoryReadEvent",
    "MemoryWriteEvent",
    "MetricsEventHandler",
    "NodeAddedEvent",
    "NodeRemovedEvent",
    "NodeReplacedEvent",
    "RunCompletedEvent",
    "RunStartedEvent",
    "StepCompletedEvent",
    "StepFailedEvent",
    "StepRetriedEvent",
    "StepStartedEvent",
    "global_event_bus",
]


class EventType(StrEnum):
    """Event types."""

    # Graph events
    NODE_ADDED = "node_added"
    NODE_REMOVED = "node_removed"
    NODE_REPLACED = "node_replaced"
    EDGE_ADDED = "edge_added"
    EDGE_REMOVED = "edge_removed"
    EDGE_UPDATED = "edge_updated"

    # Execution events
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    STEP_RETRIED = "step_retried"
    PLAN_CREATED = "plan_created"
    PLAN_TOPOLOGY_CHANGED = "plan_topology_changed"

    # Memory events
    MEMORY_WRITE = "memory_write"
    MEMORY_READ = "memory_read"
    MEMORY_EXPIRED = "memory_expired"
    MEMORY_COMPRESSED = "memory_compressed"
    MEMORY_PROMOTED = "memory_promoted"

    # Budget events
    BUDGET_WARNING = "budget_warning"
    BUDGET_EXCEEDED = "budget_exceeded"

    # Metrics events
    METRICS_UPDATED = "metrics_updated"


class EventPriority(int, Enum):
    """Event processing priority."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class Event(BaseModel):
    event_type: EventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str | None = None
    priority: EventPriority = EventPriority.NORMAL
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the event to a dict for logging/transport."""
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "priority": self.priority.value,
            "metadata": self.metadata,
        }


class GraphEvent(Event):
    graph_id: str | None = None


class NodeAddedEvent(GraphEvent):
    event_type: EventType = EventType.NODE_ADDED
    node_id: str = ""
    node_data: dict[str, Any] = Field(default_factory=dict)
    connected_to: list[str] = Field(default_factory=list)


class NodeRemovedEvent(GraphEvent):
    event_type: EventType = EventType.NODE_REMOVED
    node_id: str = ""
    migration_policy: str = "discard"
    state_archived: bool = False


class NodeReplacedEvent(GraphEvent):
    event_type: EventType = EventType.NODE_REPLACED
    old_node_id: str = ""
    new_node_id: str = ""
    state_migrated: bool = False


class EdgeAddedEvent(GraphEvent):
    event_type: EventType = EventType.EDGE_ADDED
    source_id: str = ""
    target_id: str = ""
    weight: float = 1.0
    edge_data: dict[str, Any] = Field(default_factory=dict)


class EdgeRemovedEvent(GraphEvent):
    event_type: EventType = EventType.EDGE_REMOVED
    source_id: str = ""
    target_id: str = ""


class EdgeUpdatedEvent(GraphEvent):
    event_type: EventType = EventType.EDGE_UPDATED
    source_id: str = ""
    target_id: str = ""
    old_weight: float = 0.0
    new_weight: float = 0.0
    changes: dict[str, Any] = Field(default_factory=dict)


class ExecutionEvent(Event):
    run_id: str | None = None
    graph_id: str | None = None


class RunStartedEvent(ExecutionEvent):
    event_type: EventType = EventType.RUN_STARTED
    query: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    num_agents: int = 0


class RunCompletedEvent(ExecutionEvent):
    event_type: EventType = EventType.RUN_COMPLETED
    success: bool = True
    answer: str = ""
    total_steps: int = 0
    total_tokens: int = 0
    duration_ms: float = 0.0
    errors: list[str] = Field(default_factory=list)


class StepStartedEvent(ExecutionEvent):
    event_type: EventType = EventType.STEP_STARTED
    agent_id: str = ""
    step_index: int = 0
    predecessors: list[str] = Field(default_factory=list)


class StepCompletedEvent(ExecutionEvent):
    event_type: EventType = EventType.STEP_COMPLETED
    agent_id: str = ""
    step_index: int = 0
    success: bool = True
    response_length: int = 0
    tokens_used: int = 0
    duration_ms: float = 0.0


class StepFailedEvent(ExecutionEvent):
    event_type: EventType = EventType.STEP_FAILED
    priority: EventPriority = EventPriority.HIGH
    agent_id: str = ""
    step_index: int = 0
    error_type: str = ""
    error_message: str = ""
    will_retry: bool = False


class StepRetriedEvent(ExecutionEvent):
    event_type: EventType = EventType.STEP_RETRIED
    agent_id: str = ""
    attempt: int = 0
    max_attempts: int = 0
    delay_ms: float = 0.0


class MemoryEvent(Event):
    agent_id: str | None = None


class MemoryWriteEvent(MemoryEvent):
    event_type: EventType = EventType.MEMORY_WRITE
    key: str = ""
    value_size: int = 0
    memory_level: str = "working"


class MemoryReadEvent(MemoryEvent):
    event_type: EventType = EventType.MEMORY_READ
    key: str = ""
    found: bool = False
    memory_level: str = "working"


class MemoryExpiredEvent(MemoryEvent):
    event_type: EventType = EventType.MEMORY_EXPIRED
    key: str = ""
    ttl_seconds: float = 0.0


class BudgetEvent(Event):
    run_id: str | None = None


class BudgetWarningEvent(BudgetEvent):
    event_type: EventType = EventType.BUDGET_WARNING
    budget_type: str = ""
    current_value: float = 0.0
    limit: float = 0.0
    ratio: float = 0.0


class BudgetExceededEvent(BudgetEvent):
    event_type: EventType = EventType.BUDGET_EXCEEDED
    priority: EventPriority = EventPriority.CRITICAL
    budget_type: str = ""
    current_value: float = 0.0
    limit: float = 0.0
    action_taken: str = ""


class EventHandler[T: Event](ABC):
    """
    Base event handler.

    Attributes:
        raise_error: If True — exceptions from handle() propagate outward.
                     If False (default) — errors are logged and the bus
                     continues with the next handlers.

    """

    raise_error: bool = False

    @abstractmethod
    def handle(self, event: T) -> None:
        """Handle the event."""

    def can_handle(self, event: Event) -> bool:
        """Return True if the handler is ready to accept the event."""
        return True


class EventBus:
    """Simple event bus with type-based subscribers and global listeners."""

    def __init__(self):
        self._handlers: dict[EventType | None, list[EventHandler]] = {}
        self._global_handlers: list[EventHandler] = []
        self._enabled: bool = True

    def subscribe(
        self,
        event_type: EventType | None,
        handler: EventHandler | Callable[[Event], None],
    ) -> None:
        """Subscribe to events of a specific type or all events (when None)."""
        event_handler: EventHandler
        if isinstance(handler, EventHandler):
            event_handler = handler
        elif callable(handler):
            event_handler = CallableHandler(handler)
        else:
            msg = "Handler must be EventHandler or callable"
            raise TypeError(msg)

        if event_type is None:
            self._global_handlers.append(event_handler)
        else:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(event_handler)

    def unsubscribe(
        self,
        event_type: EventType | None,
        handler: EventHandler,
    ) -> None:
        """Unsubscribe from events of the specified type or from global events."""
        if event_type is None:
            if handler in self._global_handlers:
                self._global_handlers.remove(handler)
        elif event_type in self._handlers and handler in self._handlers[event_type]:
            self._handlers[event_type].remove(handler)

    def publish(self, event: Event) -> None:
        """Dispatch the event to all matching subscribers."""
        if not self._enabled:
            return

        all_handlers = []
        all_handlers.extend(self._global_handlers)
        if event.event_type in self._handlers:
            all_handlers.extend(self._handlers[event.event_type])
        for handler in all_handlers:
            try:
                if handler.can_handle(event):
                    handler.handle(event)
            except Exception as e:
                if handler.raise_error:
                    raise
                logger.debug(
                    "Event handler '{}' failed (set raise_error=True to propagate): {}",
                    type(handler).__name__,
                    e,
                )

    def enable(self) -> None:
        """Enable event processing."""
        self._enabled = True

    def disable(self) -> None:
        """Disable event processing."""
        self._enabled = False

    def clear(self) -> None:
        """Clear all registered handlers."""
        self._handlers.clear()
        self._global_handlers.clear()


class CallableHandler(EventHandler):
    """Wrapper that allows subscribing plain functions as event handlers."""

    def __init__(self, fn: Callable[[Event], None]):
        self._fn = fn

    def handle(self, event: Event) -> None:
        self._fn(event)


_global_bus: EventBus | None = None


def global_event_bus() -> EventBus:
    """Return the global event bus instance (create on first access)."""
    global _global_bus  # noqa: PLW0603
    if _global_bus is None:
        _global_bus = EventBus()
    return _global_bus


GlobalEventBus = global_event_bus


class LoggingEventHandler(EventHandler):
    """Handler that formats events and sends them to the log."""

    def __init__(
        self,
        log_level: str = "INFO",
        include_metadata: bool = True,
        format_func: Callable[[Event], str] | None = None,
    ):
        self.log_level = log_level
        self.include_metadata = include_metadata
        self.format_func = format_func or self._default_format
        self._logger = logger

    def handle(self, event: Event) -> None:
        """Write the event to the log according to priority and formatter."""
        message = self.format_func(event)

        level = self.log_level
        if event.priority == EventPriority.CRITICAL:
            level = "ERROR"
        elif event.priority == EventPriority.HIGH:
            level = "WARNING"

        if hasattr(self._logger, "log"):
            self._logger.log(level, message)
        else:
            getattr(self._logger, str(level).lower(), self._logger.info)(message)

    def _default_format(self, event: Event) -> str:
        """Build a default string for different event types."""
        parts = [
            f"[{event.event_type.value}]",
            f"source={event.source}" if event.source else "",
        ]

        if isinstance(event, NodeAddedEvent):
            parts.append(f"node={event.node_id}")
        elif isinstance(event, NodeRemovedEvent):
            parts.append(f"node={event.node_id} policy={event.migration_policy}")
        elif isinstance(event, EdgeAddedEvent):
            parts.append(f"edge={event.source_id}->{event.target_id} weight={event.weight}")
        elif isinstance(event, StepCompletedEvent):
            parts.append(f"agent={event.agent_id} success={event.success} tokens={event.tokens_used}")
        elif isinstance(event, StepFailedEvent):
            parts.append(f"agent={event.agent_id} error={event.error_message}")
        elif isinstance(event, BudgetWarningEvent):
            parts.append(f"{event.budget_type}: {event.current_value}/{event.limit} ({event.ratio:.1%})")
        elif isinstance(event, RunCompletedEvent):
            parts.append(f"success={event.success} steps={event.total_steps} tokens={event.total_tokens}")

        if self.include_metadata and event.metadata:
            parts.append(f"metadata={event.metadata}")

        return " ".join(filter(None, parts))


class MetricsEventHandler(EventHandler):
    """Metrics aggregator based on incoming events."""

    def __init__(self):
        self._event_counts: dict[str, int] = {}
        self._total_tokens: int = 0
        self._total_duration_ms: float = 0.0
        self._errors: list[dict[str, Any]] = []
        self._step_durations: list[float] = []
        self._budget_warnings: int = 0
        self._runs_completed: int = 0
        self._runs_failed: int = 0

    def handle(self, event: Event) -> None:
        """Accumulate event statistics (tokens, errors, durations)."""
        event_type = event.event_type.value
        self._event_counts[event_type] = self._event_counts.get(event_type, 0) + 1

        if isinstance(event, StepCompletedEvent):
            self._total_tokens += event.tokens_used
            self._step_durations.append(event.duration_ms)
            self._total_duration_ms += event.duration_ms

        elif isinstance(event, StepFailedEvent):
            self._errors.append(
                {
                    "agent_id": event.agent_id,
                    "error_type": event.error_type,
                    "error_message": event.error_message,
                    "timestamp": event.timestamp.isoformat(),
                }
            )

        elif isinstance(event, BudgetWarningEvent):
            self._budget_warnings += 1

        elif isinstance(event, RunCompletedEvent):
            if event.success:
                self._runs_completed += 1
            else:
                self._runs_failed += 1

    def get_metrics(self) -> dict[str, Any]:
        """Return aggregated metrics from collected events."""
        return {
            "event_counts": dict(self._event_counts),
            "total_tokens": self._total_tokens,
            "total_duration_ms": self._total_duration_ms,
            "avg_step_duration_ms": (
                sum(self._step_durations) / len(self._step_durations) if self._step_durations else 0.0
            ),
            "errors_count": len(self._errors),
            "errors": self._errors[-10:],
            "budget_warnings": self._budget_warnings,
            "runs_completed": self._runs_completed,
            "runs_failed": self._runs_failed,
        }

    def reset(self) -> None:
        """Reset accumulated metrics."""
        self._event_counts.clear()
        self._total_tokens = 0
        self._total_duration_ms = 0.0
        self._errors.clear()
        self._step_durations.clear()
        self._budget_warnings = 0
        self._runs_completed = 0
        self._runs_failed = 0


def emit_event(event: Event) -> None:
    """Publish an event through the global bus."""
    global_event_bus().publish(event)


def on_event(event_type: EventType | None = None):
    """Decorator for subscribing a function to events of the given type."""

    def decorator(fn: Callable[[Event], None]):
        global_event_bus().subscribe(event_type, fn)
        return fn

    return decorator
