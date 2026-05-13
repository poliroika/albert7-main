"""
Tests for the telemetry system.
"""

import tempfile
from pathlib import Path

from umbrella.telemetry.events import (
    EventType,
    WorkspaceSelectedEvent,
    RunStartedEvent,
    RunCompletedEvent,
    PatchProposedEvent,
    EvalCompletedEvent,
    HumanCheckpointRequestedEvent,
    SelfImprovementConsideredEvent,
    ErrorOccurredEvent,
    create_event,
)
from umbrella.telemetry.metrics import (
    RunMetrics,
    PatchMetrics,
    MetricsRegistry,
    get_metrics_registry,
)
from umbrella.telemetry.store import (
    TelemetryStore,
)
from umbrella.evals.models import (
    EvaluationRecord,
    TaskSuccessRating,
    OutputQualityRating,
    StabilityRating,
    ComparisonReport,
    PatchOutcome,
)


def test_workspace_selected_event():
    """Test creation of workspace selected event."""
    event = WorkspaceSelectedEvent(
        task_id="test_task",
        workspace_id="test_workspace",
        seed_workspace_id="agent_research",
        selection_reason="Good fit for research task",
        confidence=0.9,
    )

    assert event.event_type == EventType.WORKSPACE_SELECTED
    assert event.task_id == "test_task"
    assert event.workspace_id == "test_workspace"
    assert event.data["seed_workspace_id"] == "agent_research"
    assert event.data["confidence"] == 0.9


def test_run_started_event():
    """Test creation of run started event."""
    event = RunStartedEvent(
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run_123",
        instance_id="instance_456",
    )

    assert event.event_type == EventType.RUN_STARTED
    assert event.run_id == "run_123"
    assert event.instance_id == "instance_456"


def test_run_completed_event():
    """Test creation of run completed event."""
    event = RunCompletedEvent(
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run_123",
        status="completed",
        duration_seconds=120.0,
        total_tokens=50000,
        error_count=0,
    )

    assert event.event_type == EventType.RUN_COMPLETED
    assert event.data["status"] == "completed"
    assert event.data["duration_seconds"] == 120.0
    assert event.data["total_tokens"] == 50000


def test_patch_proposed_event():
    """Test creation of patch proposed event."""
    event = PatchProposedEvent(
        task_id="test_task",
        workspace_id="test_workspace",
        patch_description="Fix agent routing logic",
        target_files=["graph/config.json", "agents/router.py"],
        expected_outcome="Faster execution",
    )

    assert event.event_type == EventType.PATCH_PROPOSED
    assert "Fix agent routing logic" in event.data["patch_description"]
    assert len(event.data["target_files"]) == 2


def test_eval_completed_event():
    """Test creation of eval completed event."""
    event = EvalCompletedEvent(
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run_123",
        task_success="complete",
        output_quality="good",
        overall_score=0.85,
        total_cost_usd=2.5,
    )

    assert event.event_type == EventType.EVAL_COMPLETED
    assert event.data["overall_score"] == 0.85
    assert event.data["total_cost_usd"] == 2.5


def test_human_checkpoint_requested_event():
    """Test creation of human checkpoint requested event."""
    event = HumanCheckpointRequestedEvent(
        task_id="test_task",
        checkpoint_id="checkpoint_123",
        reason="Prompt stack modification requested",
        checkpoint_type="prompt_rewrite",
    )

    assert event.event_type == EventType.HUMAN_CHECKPOINT_REQUESTED
    assert event.level == "warning"
    assert "checkpoint_123" in event.data["checkpoint_id"]


def test_self_improvement_considered_event():
    """Test creation of self improvement considered event."""
    event = SelfImprovementConsideredEvent(
        task_id="test_task",
        capability_gap="Retrieval not finding relevant context",
        gap_evidence=["Multiple failed queries", "Low hit rate"],
        decision="proceed",
    )

    assert event.event_type == EventType.SELF_IMPROVEMENT_CONSIDERED
    assert "Retrieval" in event.data["capability_gap"]
    assert len(event.data["gap_evidence"]) == 2


def test_error_occurred_event():
    """Test creation of error occurred event."""
    event = ErrorOccurredEvent(
        task_id="test_task",
        error_type="ValueError",
        error_message="Invalid configuration value",
        context={"file": "config.json", "line": 42},
    )

    assert event.event_type == EventType.ERROR_OCCURRED
    assert event.level == "error"
    assert event.data["error_type"] == "ValueError"
    assert event.data["context"]["line"] == 42


def test_create_generic_event():
    """Test creation of a generic telemetry event."""
    event = create_event(
        EventType.SYSTEM_STARTUP,
        task_id="system",
        data={"version": "1.0.0"},
    )

    assert event.event_type == EventType.SYSTEM_STARTUP
    assert event.data["version"] == "1.0.0"


def test_run_metrics_aggregation():
    """Test that run metrics aggregate correctly."""
    metrics = RunMetrics()

    eval1 = EvaluationRecord(
        id="eval1",
        task_id="task1",
        workspace_id="ws1",
        run_id="run1",
        instance_path=Path("."),
        task_success=TaskSuccessRating.COMPLETE,
        output_quality=OutputQualityRating.GOOD,
        stability=StabilityRating.STABLE,
        total_tokens=10000,
        total_duration_seconds=60.0,
        overall_score=0.8,
    )

    eval2 = EvaluationRecord(
        id="eval2",
        task_id="task1",
        workspace_id="ws1",
        run_id="run2",
        instance_path=Path("."),
        task_success=TaskSuccessRating.PARTIAL,
        output_quality=OutputQualityRating.FAIR,
        stability=StabilityRating.UNKNOWN,
        total_tokens=15000,
        total_duration_seconds=90.0,
        overall_score=0.5,
    )

    metrics.add_eval(eval1)
    metrics.add_eval(eval2)

    assert metrics.total_runs == 2
    assert metrics.successful_runs == 1
    assert metrics.complete_tasks == 1
    assert metrics.partial_tasks == 1
    assert metrics.total_tokens == 25000
    assert len(metrics.scores) == 2
    assert metrics.average_score == 0.65


def test_patch_metrics_aggregation():
    """Test that patch metrics aggregate correctly."""
    metrics = PatchMetrics()

    comparison1 = ComparisonReport(
        id="comp1",
        task_id="task1",
        workspace_id="ws1",
        baseline_run_id="run1",
        comparison_run_id="run2",
        baseline_score=0.5,
        comparison_score=0.7,
        score_delta=0.2,
        baseline_task_success=TaskSuccessRating.PARTIAL,
        comparison_task_success=TaskSuccessRating.COMPLETE,
        baseline_output_quality=OutputQualityRating.FAIR,
        comparison_output_quality=OutputQualityRating.GOOD,
        baseline_cost_usd=1.0,
        comparison_cost_usd=1.2,
        cost_delta_usd=0.2,
        baseline_stability=StabilityRating.UNKNOWN,
        comparison_stability=StabilityRating.STABLE,
        overall_improvement=PatchOutcome.IMPROVED,
    )

    comparison2 = ComparisonReport(
        id="comp2",
        task_id="task1",
        workspace_id="ws1",
        baseline_run_id="run3",
        comparison_run_id="run4",
        baseline_score=0.6,
        comparison_score=0.4,
        score_delta=-0.2,
        baseline_task_success=TaskSuccessRating.PARTIAL,
        comparison_task_success=TaskSuccessRating.FAILED,
        baseline_output_quality=OutputQualityRating.FAIR,
        comparison_output_quality=OutputQualityRating.POOR,
        baseline_cost_usd=1.0,
        comparison_cost_usd=1.5,
        cost_delta_usd=0.5,
        baseline_stability=StabilityRating.STABLE,
        comparison_stability=StabilityRating.UNSTABLE,
        overall_improvement=PatchOutcome.REGRESSED,
    )

    metrics.add_comparison(comparison1)
    metrics.add_comparison(comparison2)

    assert metrics.total_patches == 2
    assert metrics.improved_patches == 1
    assert metrics.regressed_patches == 1
    assert metrics.patch_success_rate == 0.5


def test_metrics_registry():
    """Test the metrics registry."""
    registry = MetricsRegistry()

    # Record some metrics
    registry.increment_counter("test_counter", 5)
    assert registry.get_counter("test_counter") == 5

    registry.set_gauge("test_gauge", 42.0)
    assert registry.get_gauge("test_gauge") == 42.0

    registry.record_timing("test_timing", 1.5)
    registry.record_timing("test_timing", 2.0)
    registry.record_timing("test_timing", 2.5)

    stats = registry.get_timing_stats("test_timing")
    assert stats is not None
    assert stats["count"] == 3
    assert stats["mean"] == 2.0


def test_get_metrics_registry_singleton():
    """Test that get_metrics_registry returns a singleton."""
    registry1 = get_metrics_registry()
    registry2 = get_metrics_registry()

    # Should be the same instance
    assert registry1 is registry2


def test_telemetry_store_emit_and_flush():
    """Test emitting events to the telemetry store."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TelemetryStore(Path(tmpdir), max_events_per_file=10)

        # Emit some events
        for i in range(5):
            event = RunStartedEvent(
                task_id=f"task_{i}",
                workspace_id="test_workspace",
                run_id=f"run_{i}",
            )
            store.emit_event(event)

        # Flush events
        store.flush_events()

        # Check that event file was created
        event_files = list(store.events_dir.glob("events_*.jsonl"))
        assert len(event_files) >= 1


def test_telemetry_store_save_and_load_metrics():
    """Test saving and loading metrics snapshots."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TelemetryStore(Path(tmpdir))
        registry = MetricsRegistry()

        # Add some metrics
        registry.increment_counter("test", 10)
        registry.set_gauge("value", 3.14)

        # Save snapshot
        snapshot_path = store.save_metrics_snapshot(registry, "test_snapshot")

        # Load snapshot
        loaded = store.load_metrics_snapshot("test_snapshot")

        assert loaded is not None
        assert loaded["metrics"]["counters"]["test"] == 10
        assert loaded["metrics"]["gauges"]["value"] == 3.14


def test_emit_event_to_global_store():
    """Test emitting events to a telemetry store."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = TelemetryStore(Path(tmpdir))

        event = WorkspaceSelectedEvent(
            task_id="test_task",
            workspace_id="test_workspace",
        )

        # Emit to store directly
        store.emit_event(event)
        store.flush_events()

        event_files = list(store.events_dir.glob("events_*.jsonl"))
        assert len(event_files) >= 1


def test_telemetry_event_to_dict():
    """Test converting telemetry event to dictionary."""
    event = WorkspaceSelectedEvent(
        task_id="test_task",
        workspace_id="test_workspace",
        selection_reason="Test",
    )

    event_dict = event.to_dict()

    assert "event_type" in event_dict
    assert "timestamp" in event_dict
    assert "datetime" in event_dict
    assert event_dict["event_type"] == EventType.WORKSPACE_SELECTED.value
    assert event_dict["task_id"] == "test_task"
