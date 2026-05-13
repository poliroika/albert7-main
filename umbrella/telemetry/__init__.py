"""
Telemetry system for the manager.

This package provides:
- Event tracking and emission
- Metrics aggregation and tracking
- Persistent storage of telemetry data
- Query and export capabilities
"""

from umbrella.telemetry.events import (
    EventType,
    TelemetryEvent,
    WorkspaceSelectedEvent,
    RunStartedEvent,
    RunCompletedEvent,
    PatchProposedEvent,
    PatchAppliedEvent,
    WorkspaceCodeUpdatedEvent,
    EvalCompletedEvent,
    HumanCheckpointRequestedEvent,
    SelfImprovementConsideredEvent,
    RetrievalQueryEvent,
    ErrorOccurredEvent,
    PromotionCandidateCreatedEvent,
    PromotionDecisionEvent,
    create_event,
)
from umbrella.telemetry.metrics import (
    RunMetrics,
    PatchMetrics,
    TelemetrySummary,
    MetricsRegistry,
    get_metrics_registry,
)
from umbrella.telemetry.store import (
    TelemetryStore,
    get_telemetry_store,
    emit_event,
)

__all__ = [
    # Event types
    "EventType",
    "TelemetryEvent",
    "WorkspaceSelectedEvent",
    "RunStartedEvent",
    "RunCompletedEvent",
    "PatchProposedEvent",
    "PatchAppliedEvent",
    "WorkspaceCodeUpdatedEvent",
    "EvalCompletedEvent",
    "HumanCheckpointRequestedEvent",
    "SelfImprovementConsideredEvent",
    "RetrievalQueryEvent",
    "ErrorOccurredEvent",
    "PromotionCandidateCreatedEvent",
    "PromotionDecisionEvent",
    "create_event",
    # Metrics
    "RunMetrics",
    "PatchMetrics",
    "TelemetrySummary",
    "MetricsRegistry",
    "get_metrics_registry",
    # Store
    "TelemetryStore",
    "get_telemetry_store",
    "emit_event",
]
