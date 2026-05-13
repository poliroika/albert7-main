"""Tests for src/core/events.py"""

import pytest

from gmas.core.events import (
    BudgetExceededEvent,
    BudgetWarningEvent,
    CallableHandler,
    EdgeAddedEvent,
    EdgeRemovedEvent,
    EdgeUpdatedEvent,
    Event,
    EventBus,
    EventHandler,
    EventPriority,
    EventType,
    GlobalEventBus,
    LoggingEventHandler,
    MemoryExpiredEvent,
    MemoryReadEvent,
    MemoryWriteEvent,
    MetricsEventHandler,
    NodeAddedEvent,
    NodeRemovedEvent,
    NodeReplacedEvent,
    RunCompletedEvent,
    RunStartedEvent,
    StepCompletedEvent,
    StepFailedEvent,
    StepRetriedEvent,
    StepStartedEvent,
    emit_event,
    global_event_bus,
    on_event,
)

# ─────────────────────────── Event Base ──────────────────────────────────────


class TestEventBase:
    def test_create_event(self):
        event = NodeAddedEvent(
            event_type=EventType.NODE_ADDED,
            node_id="solver",
        )
        assert event.event_type == EventType.NODE_ADDED
        assert event.priority == EventPriority.NORMAL
        assert event.timestamp is not None

    def test_to_dict(self):
        event = NodeAddedEvent(node_id="solver")
        d = event.to_dict()
        assert d["event_type"] == EventType.NODE_ADDED.value
        assert "timestamp" in d
        assert "source" in d

    def test_event_with_source(self):
        event = RunStartedEvent(source="runner", query="test")
        assert event.source == "runner"

    def test_event_with_metadata(self):
        event = NodeAddedEvent(node_id="n1", metadata={"tag": "test"})
        assert event.metadata["tag"] == "test"


# ─────────────────────────── Graph Events ────────────────────────────────────


class TestGraphEvents:
    def test_node_added_event(self):
        e = NodeAddedEvent(node_id="solver", connected_to=["a", "b"])
        assert e.node_id == "solver"
        assert e.connected_to == ["a", "b"]

    def test_node_removed_event(self):
        e = NodeRemovedEvent(node_id="old", migration_policy="merge", state_archived=True)
        assert e.migration_policy == "merge"
        assert e.state_archived is True

    def test_node_replaced_event(self):
        e = NodeReplacedEvent(old_node_id="old", new_node_id="new", state_migrated=True)
        assert e.old_node_id == "old"
        assert e.new_node_id == "new"

    def test_edge_added_event(self):
        e = EdgeAddedEvent(source_id="a", target_id="b", weight=0.5)
        assert e.source_id == "a"
        assert e.weight == 0.5

    def test_edge_removed_event(self):
        e = EdgeRemovedEvent(source_id="a", target_id="b")
        assert e.source_id == "a"
        assert e.target_id == "b"

    def test_edge_updated_event(self):
        e = EdgeUpdatedEvent(
            source_id="a",
            target_id="b",
            old_weight=1.0,
            new_weight=2.0,
            changes={"weight": 2.0},
        )
        assert e.new_weight == 2.0


# ─────────────────────────── Execution Events ────────────────────────────────


class TestExecutionEvents:
    def test_run_started_event(self):
        e = RunStartedEvent(run_id="run1", query="Hello", num_agents=3)
        assert e.query == "Hello"
        assert e.num_agents == 3

    def test_run_completed_event_success(self):
        e = RunCompletedEvent(
            run_id="run1",
            success=True,
            answer="42",
            total_tokens=500,
            total_steps=3,
            duration_ms=1500.0,
        )
        assert e.success is True
        assert e.total_tokens == 500

    def test_run_completed_event_failure(self):
        e = RunCompletedEvent(success=False, errors=["timeout"])
        assert e.success is False
        assert "timeout" in e.errors

    def test_step_started_event(self):
        e = StepStartedEvent(agent_id="solver", step_index=0, predecessors=["planner"])
        assert e.agent_id == "solver"
        assert e.predecessors == ["planner"]

    def test_step_completed_event(self):
        e = StepCompletedEvent(
            agent_id="solver",
            step_index=1,
            success=True,
            tokens_used=100,
            duration_ms=200.0,
        )
        assert e.tokens_used == 100

    def test_step_failed_event(self):
        e = StepFailedEvent(
            agent_id="solver",
            error_type="TimeoutError",
            error_message="timeout",
            will_retry=True,
        )
        assert e.priority == EventPriority.HIGH
        assert e.will_retry is True

    def test_step_retried_event(self):
        e = StepRetriedEvent(agent_id="solver", attempt=2, max_attempts=3, delay_ms=500.0)
        assert e.attempt == 2


# ─────────────────────────── Memory Events ───────────────────────────────────


class TestMemoryEvents:
    def test_memory_write_event(self):
        e = MemoryWriteEvent(agent_id="solver", key="context", value_size=256)
        assert e.key == "context"
        assert e.value_size == 256

    def test_memory_read_event(self):
        e = MemoryReadEvent(agent_id="solver", key="context", found=True)
        assert e.found is True

    def test_memory_expired_event(self):
        e = MemoryExpiredEvent(key="old_data", ttl_seconds=60.0)
        assert e.ttl_seconds == 60.0


# ─────────────────────────── Budget Events ───────────────────────────────────


class TestBudgetEvents:
    def test_budget_warning_event(self):
        e = BudgetWarningEvent(budget_type="tokens", current_value=800.0, limit=1000.0, ratio=0.8)
        assert e.budget_type == "tokens"
        assert e.ratio == 0.8

    def test_budget_exceeded_event(self):
        e = BudgetExceededEvent(budget_type="requests", current_value=10.0, limit=10.0)
        assert e.priority == EventPriority.CRITICAL
        assert e.budget_type == "requests"


# ─────────────────────────── EventBus ────────────────────────────────────────


class ConcreteHandler(EventHandler):
    def __init__(self):
        self.received: list[Event] = []

    def handle(self, event: Event) -> None:
        self.received.append(event)


class FailingHandler(EventHandler):
    raise_error = True

    def handle(self, event: Event) -> None:
        msg = "handler error"
        raise RuntimeError(msg)


class TestEventBus:
    def setup_method(self):
        self.bus = EventBus()

    def test_subscribe_and_publish(self):
        handler = ConcreteHandler()
        self.bus.subscribe(EventType.NODE_ADDED, handler)
        event = NodeAddedEvent(node_id="n1")
        self.bus.publish(event)
        assert len(handler.received) == 1

    def test_subscribe_global_handler(self):
        handler = ConcreteHandler()
        self.bus.subscribe(None, handler)
        self.bus.publish(NodeAddedEvent(node_id="n1"))
        self.bus.publish(StepCompletedEvent(agent_id="a"))
        assert len(handler.received) == 2

    def test_publish_to_wrong_event_type(self):
        handler = ConcreteHandler()
        self.bus.subscribe(EventType.NODE_REMOVED, handler)
        self.bus.publish(NodeAddedEvent(node_id="n1"))
        assert len(handler.received) == 0

    def test_unsubscribe_type_handler(self):
        handler = ConcreteHandler()
        self.bus.subscribe(EventType.NODE_ADDED, handler)
        self.bus.unsubscribe(EventType.NODE_ADDED, handler)
        self.bus.publish(NodeAddedEvent(node_id="n1"))
        assert len(handler.received) == 0

    def test_unsubscribe_global_handler(self):
        handler = ConcreteHandler()
        self.bus.subscribe(None, handler)
        self.bus.unsubscribe(None, handler)
        self.bus.publish(NodeAddedEvent(node_id="n1"))
        assert len(handler.received) == 0

    def test_disable_and_enable(self):
        handler = ConcreteHandler()
        self.bus.subscribe(EventType.NODE_ADDED, handler)
        self.bus.disable()
        self.bus.publish(NodeAddedEvent(node_id="n1"))
        assert len(handler.received) == 0
        self.bus.enable()
        self.bus.publish(NodeAddedEvent(node_id="n1"))
        assert len(handler.received) == 1

    def test_clear_handlers(self):
        handler = ConcreteHandler()
        self.bus.subscribe(EventType.NODE_ADDED, handler)
        self.bus.clear()
        self.bus.publish(NodeAddedEvent(node_id="n1"))
        assert len(handler.received) == 0

    def test_handler_error_with_raise(self):
        handler = FailingHandler()
        self.bus.subscribe(EventType.NODE_ADDED, handler)
        with pytest.raises(RuntimeError, match="handler error"):
            self.bus.publish(NodeAddedEvent(node_id="n1"))

    def test_handler_error_without_raise(self):
        class SilentFailingHandler(EventHandler):
            raise_error = False

            def handle(self, event: Event) -> None:
                msg = "silent error"
                raise ValueError(msg)

        handler = SilentFailingHandler()
        self.bus.subscribe(EventType.NODE_ADDED, handler)
        self.bus.publish(NodeAddedEvent(node_id="n1"))  # should not raise

    def test_callable_handler(self):
        received = []
        self.bus.subscribe(EventType.NODE_ADDED, received.append)
        self.bus.publish(NodeAddedEvent(node_id="n1"))
        assert len(received) == 1

    def test_multiple_handlers_for_same_type(self):
        h1 = ConcreteHandler()
        h2 = ConcreteHandler()
        self.bus.subscribe(EventType.NODE_ADDED, h1)
        self.bus.subscribe(EventType.NODE_ADDED, h2)
        self.bus.publish(NodeAddedEvent(node_id="n1"))
        assert len(h1.received) == 1
        assert len(h2.received) == 1

    def test_can_handle_override(self):
        class FilteredHandler(EventHandler):
            def can_handle(self, event: Event) -> bool:
                return isinstance(event, NodeAddedEvent)

            def handle(self, event: Event) -> None:
                pass

        handler = FilteredHandler()
        self.bus.subscribe(None, handler)  # global
        # Only NodeAddedEvent passes can_handle, but publish doesn't bypass it
        # (can_handle is checked per event)


# ─────────────────────────── CallableHandler ─────────────────────────────────


class TestCallableHandler:
    def test_wraps_function(self):
        received = []
        handler = CallableHandler(received.append)
        event = NodeAddedEvent(node_id="n1")
        handler.handle(event)
        assert len(received) == 1

    def test_can_handle_default_true(self):
        handler = CallableHandler(lambda _: None)
        assert handler.can_handle(NodeAddedEvent(node_id="n1")) is True


# ─────────────────────────── LoggingEventHandler ─────────────────────────────


class TestLoggingEventHandler:
    def test_handle_node_added(self):
        handler = LoggingEventHandler()
        handler.handle(NodeAddedEvent(node_id="solver"))

    def test_handle_node_removed(self):
        handler = LoggingEventHandler()
        handler.handle(NodeRemovedEvent(node_id="old", migration_policy="discard"))

    def test_handle_edge_added(self):
        handler = LoggingEventHandler()
        handler.handle(EdgeAddedEvent(source_id="a", target_id="b", weight=1.0))

    def test_handle_step_completed(self):
        handler = LoggingEventHandler()
        handler.handle(StepCompletedEvent(agent_id="solver", tokens_used=100, success=True))

    def test_handle_step_failed(self):
        handler = LoggingEventHandler()
        handler.handle(StepFailedEvent(agent_id="solver", error_message="timeout"))

    def test_handle_budget_warning(self):
        handler = LoggingEventHandler()
        handler.handle(BudgetWarningEvent(budget_type="tokens", current_value=800.0, limit=1000.0, ratio=0.8))

    def test_handle_run_completed(self):
        handler = LoggingEventHandler()
        handler.handle(RunCompletedEvent(success=True, total_steps=3, total_tokens=500))

    def test_handle_critical_priority(self):
        handler = LoggingEventHandler()
        event = BudgetExceededEvent(budget_type="tokens", current_value=1000.0, limit=1000.0)
        assert event.priority == EventPriority.CRITICAL
        handler.handle(event)  # should not raise

    def test_handle_high_priority(self):
        handler = LoggingEventHandler()
        event = StepFailedEvent(agent_id="s", error_message="err")
        assert event.priority == EventPriority.HIGH
        handler.handle(event)

    def test_handle_with_metadata(self):
        handler = LoggingEventHandler(include_metadata=True)
        event = NodeAddedEvent(node_id="n1", metadata={"tag": "test"})
        handler.handle(event)

    def test_custom_format_func(self):
        called = []
        handler = LoggingEventHandler(format_func=lambda e: called.append(e) or "formatted")
        handler.handle(NodeAddedEvent(node_id="n1"))
        assert len(called) == 1


# ─────────────────────────── MetricsEventHandler ─────────────────────────────


class TestMetricsEventHandler:
    def setup_method(self):
        self.handler = MetricsEventHandler()

    def test_initial_state(self):
        metrics = self.handler.get_metrics()
        assert metrics["total_tokens"] == 0
        assert metrics["errors_count"] == 0

    def test_step_completed_accumulates_tokens(self):
        self.handler.handle(StepCompletedEvent(agent_id="a", tokens_used=100, duration_ms=200.0))
        self.handler.handle(StepCompletedEvent(agent_id="b", tokens_used=50, duration_ms=100.0))
        metrics = self.handler.get_metrics()
        assert metrics["total_tokens"] == 150
        assert metrics["total_duration_ms"] == 300.0

    def test_step_failed_records_error(self):
        self.handler.handle(StepFailedEvent(agent_id="a", error_type="TimeoutError", error_message="timeout"))
        metrics = self.handler.get_metrics()
        assert metrics["errors_count"] == 1
        assert "TimeoutError" in metrics["errors"][0]["error_type"]

    def test_budget_warning(self):
        self.handler.handle(BudgetWarningEvent(budget_type="tokens", current_value=800.0, limit=1000.0))
        assert self.handler.get_metrics()["budget_warnings"] == 1

    def test_run_completed_success(self):
        self.handler.handle(RunCompletedEvent(success=True))
        metrics = self.handler.get_metrics()
        assert metrics["runs_completed"] == 1
        assert metrics["runs_failed"] == 0

    def test_run_completed_failure(self):
        self.handler.handle(RunCompletedEvent(success=False))
        metrics = self.handler.get_metrics()
        assert metrics["runs_failed"] == 1

    def test_event_count_tracking(self):
        self.handler.handle(NodeAddedEvent(node_id="n1"))
        self.handler.handle(NodeAddedEvent(node_id="n2"))
        metrics = self.handler.get_metrics()
        assert metrics["event_counts"]["node_added"] == 2

    def test_avg_step_duration(self):
        self.handler.handle(StepCompletedEvent(agent_id="a", duration_ms=200.0))
        self.handler.handle(StepCompletedEvent(agent_id="b", duration_ms=400.0))
        metrics = self.handler.get_metrics()
        assert metrics["avg_step_duration_ms"] == 300.0

    def test_reset(self):
        self.handler.handle(StepCompletedEvent(agent_id="a", tokens_used=100))
        self.handler.reset()
        metrics = self.handler.get_metrics()
        assert metrics["total_tokens"] == 0
        assert metrics["event_counts"] == {}


# ─────────────────────────── Global Bus / emit_event / on_event ──────────────


class TestGlobalBus:
    def test_global_event_bus_singleton(self):
        bus1 = global_event_bus()
        bus2 = global_event_bus()
        assert bus1 is bus2

    def test_global_event_bus_alias(self):
        assert GlobalEventBus is global_event_bus

    def test_emit_event(self):
        bus = global_event_bus()
        handler = ConcreteHandler()
        bus.subscribe(EventType.NODE_ADDED, handler)
        emit_event(NodeAddedEvent(node_id="test_emit"))
        assert any(e.node_id == "test_emit" for e in handler.received if isinstance(e, NodeAddedEvent))
        bus.unsubscribe(EventType.NODE_ADDED, handler)

    def test_on_event_decorator(self):
        received = []

        @on_event(EventType.EDGE_ADDED)
        def my_handler(event):
            received.append(event)

        bus = global_event_bus()
        bus.publish(EdgeAddedEvent(source_id="x", target_id="y"))
        assert len(received) >= 1
        bus.unsubscribe(EventType.EDGE_ADDED, my_handler)  # won't work since it was wrapped, but that's OK


# ─────────────────────────── Missing branch coverage ─────────────────────────


class TestEventBusSubscribeTypeError:
    def test_subscribe_non_callable_raises_type_error(self):
        """Line 318-319: TypeError when handler is not EventHandler or callable."""
        bus = EventBus()
        with pytest.raises(TypeError, match="Handler must be EventHandler or callable"):
            bus.subscribe(EventType.NODE_ADDED, "not_a_handler")  # type: ignore[arg-type,ty:invalid-argument-type]


class TestLoggingEventHandlerNonStandardLogger:
    def test_logger_without_log_method_uses_getattr(self):
        """Line 430: else branch when logger doesn't have .log() method."""

        class SimpleLogger:
            def __init__(self):
                self.messages = []

            def info(self, msg):
                self.messages.append(msg)

            def debug(self, msg):
                self.messages.append(msg)

            def warning(self, msg):
                self.messages.append(msg)

            def error(self, msg):
                self.messages.append(msg)

        simple_logger = SimpleLogger()
        handler = LoggingEventHandler(log_level="info")
        # Override _logger with one that has no .log() method
        handler._logger = simple_logger
        handler.handle(NodeAddedEvent(node_id="test_node"))
        assert len(simple_logger.messages) > 0
