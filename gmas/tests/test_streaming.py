"""
Comprehensive tests for src/execution/streaming.py.
Covers all StreamEvent types, StreamBuffer, format_event, print/aprint helpers,
stream_to_string / astream_to_string utilities.
"""

from datetime import datetime
from uuid import uuid4

from gmas.execution.streaming import (
    AgentErrorEvent,
    AgentOutputEvent,
    AgentStartEvent,
    BudgetExceededEvent,
    BudgetWarningEvent,
    FallbackEvent,
    MemoryReadEvent,
    MemoryWriteEvent,
    ParallelEndEvent,
    ParallelStartEvent,
    PruneEvent,
    RunEndEvent,
    RunStartEvent,
    StreamBuffer,
    StreamEvent,
    StreamEventType,
    TokenEvent,
    TopologyChangedEvent,
    aprint_stream,
    astream_to_string,
    format_event,
    print_stream,
    stream_to_string,
)


def rid():
    return str(uuid4())


# ─────────────────────────── StreamEvent base ─────────────────────────────────


class TestStreamEventBase:
    def test_base_to_dict(self):
        e = StreamEvent(event_type="test_event", run_id=rid())
        d = e.to_dict()
        assert d["event_type"] == "test_event"
        assert "timestamp" in d

    def test_timestamp_defaults_to_now(self):
        e = StreamEvent(event_type="x")
        assert isinstance(e.timestamp, datetime)

    def test_metadata_default_empty(self):
        e = StreamEvent(event_type="x")
        assert e.metadata == {}


# ─────────────────────────── Event constructors ───────────────────────────────


class TestRunStartEvent:
    def test_basic(self):
        e = RunStartEvent(
            run_id=rid(),
            query="What is 2+2?",
            num_agents=3,
            execution_order=["a", "b", "c"],
        )
        assert e.event_type == "run_start"
        assert e.query == "What is 2+2?"
        assert e.num_agents == 3

    def test_defaults(self):
        e = RunStartEvent(run_id=rid(), query="test")
        assert e.num_agents == 0
        assert e.execution_order == []

    def test_no_run_id(self):
        e = RunStartEvent(query="test")
        assert e.run_id is None


class TestRunEndEvent:
    def test_basic(self):
        e = RunEndEvent(
            run_id=rid(),
            final_answer="Final answer",
            success=True,
            total_time=1.5,
            total_tokens=100,
        )
        assert e.event_type == "run_end"
        assert e.success is True
        assert e.total_tokens == 100

    def test_failure(self):
        e = RunEndEvent(run_id=rid(), final_answer="", success=False)
        assert e.success is False

    def test_with_errors(self):
        e = RunEndEvent(run_id=rid(), errors=["agent failed"], success=False)
        assert len(e.errors) == 1


class TestAgentStartEvent:
    def test_basic(self):
        e = AgentStartEvent(
            run_id=rid(),
            agent_id="solver",
            agent_name="Math Solver",
            step_index=1,
        )
        assert e.event_type == "agent_start"
        assert e.agent_id == "solver"
        assert e.step_index == 1

    def test_defaults(self):
        e = AgentStartEvent()
        assert e.agent_id == ""
        assert e.step_index == 0
        assert e.predecessors == []


class TestAgentOutputEvent:
    def test_basic(self):
        e = AgentOutputEvent(
            run_id=rid(),
            agent_id="solver",
            content="The answer is 42",
            tokens_used=20,
            duration_ms=150.0,
            is_final=True,
        )
        assert e.event_type == "agent_output"
        assert e.content == "The answer is 42"
        assert e.is_final is True

    def test_defaults(self):
        e = AgentOutputEvent(run_id=rid(), agent_id="a", content="output")
        assert e.tokens_used == 0
        assert e.is_final is False


class TestAgentErrorEvent:
    def test_basic(self):
        e = AgentErrorEvent(
            run_id=rid(),
            agent_id="faulty",
            error_message="Something went wrong",
            error_type="RuntimeError",
        )
        assert e.event_type == "agent_error"
        assert e.error_message == "Something went wrong"

    def test_will_retry_default(self):
        e = AgentErrorEvent(run_id=rid(), agent_id="a", error_message="err")
        assert e.will_retry is False


class TestTokenEvent:
    def test_basic(self):
        e = TokenEvent(
            run_id=rid(),
            agent_id="writer",
            token="Hello",
            token_index=0,
            is_first=True,
        )
        assert e.event_type == "token"
        assert e.token == "Hello"
        assert e.is_first is True

    def test_is_last(self):
        e = TokenEvent(token=".", is_last=True)
        assert e.is_last is True


class TestPruneEvent:
    def test_basic(self):
        e = PruneEvent(run_id=rid(), agent_id="pruned_agent", reason="low trust score")
        assert e.event_type == "prune"
        assert e.reason == "low trust score"

    def test_defaults(self):
        e = PruneEvent()
        assert e.agent_id == ""
        assert e.reason == ""


class TestFallbackEvent:
    def test_basic(self):
        e = FallbackEvent(
            run_id=rid(),
            failed_agent_id="broken_agent",
            fallback_agent_id="backup_agent",
        )
        assert e.event_type == "fallback"
        assert e.failed_agent_id == "broken_agent"

    def test_defaults(self):
        e = FallbackEvent()
        assert e.failed_agent_id == ""


class TestParallelEvents:
    def test_parallel_start(self):
        e = ParallelStartEvent(run_id=rid(), agent_ids=["a", "b", "c"], group_index=0)
        assert e.event_type == "parallel_start"
        assert len(e.agent_ids) == 3

    def test_parallel_end(self):
        e = ParallelEndEvent(
            run_id=rid(),
            agent_ids=["a", "b"],
            group_index=0,
            successful=["a"],
            failed=["b"],
        )
        assert e.event_type == "parallel_end"
        assert "a" in e.successful
        assert "b" in e.failed

    def test_parallel_end_defaults(self):
        e = ParallelEndEvent()
        assert e.successful == []
        assert e.failed == []


class TestMemoryEvents:
    def test_memory_read(self):
        e = MemoryReadEvent(run_id=rid(), agent_id="agent1", entries_count=5)
        assert e.event_type == "memory_read"
        assert e.entries_count == 5

    def test_memory_write(self):
        e = MemoryWriteEvent(run_id=rid(), agent_id="agent1", key="answer", value_size=42)
        assert e.event_type == "memory_write"
        assert e.key == "answer"

    def test_memory_read_defaults(self):
        e = MemoryReadEvent()
        assert e.entries_count == 0


class TestBudgetEvents:
    def test_budget_warning(self):
        e = BudgetWarningEvent(
            run_id=rid(),
            budget_type="tokens",
            current=800.0,
            limit=1000.0,
            ratio=0.8,
        )
        assert e.event_type == "budget_warning"
        assert e.budget_type == "tokens"

    def test_budget_exceeded(self):
        e = BudgetExceededEvent(
            run_id=rid(),
            budget_type="requests",
            current=100.0,
            limit=100.0,
        )
        assert e.event_type == "budget_exceeded"
        assert e.budget_type == "requests"


class TestTopologyChangedEvent:
    def test_basic(self):
        e = TopologyChangedEvent(
            run_id=rid(),
            reason="agent pruned",
            old_remaining=["a", "b", "c"],
            new_remaining=["b", "c"],
        )
        assert e.event_type == "topology_changed"
        assert "a" in e.old_remaining

    def test_defaults(self):
        e = TopologyChangedEvent()
        assert e.reason == ""
        assert e.old_remaining == []


# ─────────────────────────── format_event ─────────────────────────────────────


class TestFormatEvent:
    def test_format_run_start(self):
        e = RunStartEvent(run_id=rid(), query="test", num_agents=2)
        text = format_event(e)
        assert isinstance(text, str)
        assert len(text) > 0

    def test_format_agent_output(self):
        e = AgentOutputEvent(run_id=rid(), agent_id="agent1", content="output text")
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_run_end(self):
        e = RunEndEvent(run_id=rid(), final_answer="final", success=True)
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_token_event(self):
        e = TokenEvent(run_id=rid(), agent_id="a", token="hello")
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_agent_error(self):
        e = AgentErrorEvent(run_id=rid(), agent_id="bad", error_message="oops")
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_prune(self):
        e = PruneEvent(run_id=rid(), agent_id="a", reason="trust too low")
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_fallback(self):
        e = FallbackEvent(run_id=rid(), failed_agent_id="a", fallback_agent_id="b")
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_budget_warning(self):
        e = BudgetWarningEvent(budget_type="tokens", current=80.0, limit=100.0)
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_budget_exceeded(self):
        e = BudgetExceededEvent(budget_type="time", current=60.0, limit=60.0)
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_agent_output_verbose(self):
        e = AgentOutputEvent(agent_id="a", content="x" * 200)
        text = format_event(e, verbose=True)
        assert isinstance(text, str)

    def test_format_topology_changed(self):
        e = TopologyChangedEvent(reason="pruned", old_remaining=["a"], new_remaining=[])
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_parallel_start(self):
        e = ParallelStartEvent(agent_ids=["a", "b"])
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_parallel_end(self):
        e = ParallelEndEvent(agent_ids=["a"], successful=["a"], failed=[])
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_memory_read(self):
        e = MemoryReadEvent(agent_id="a", entries_count=3)
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_memory_write(self):
        e = MemoryWriteEvent(agent_id="a", key="k", value_size=10)
        text = format_event(e)
        assert isinstance(text, str)

    def test_format_agent_start(self):
        e = AgentStartEvent(agent_id="a", step_index=0)
        text = format_event(e)
        assert isinstance(text, str)


# ─────────────────────────── StreamBuffer ─────────────────────────────────────


class TestStreamBuffer:
    def test_add_and_events(self):
        buf = StreamBuffer()
        e = RunStartEvent(run_id=rid(), query="test")
        buf.add(e)
        assert len(buf.events) == 1
        assert buf.events[0] is e

    def test_add_agent_output_updates_final_answer(self):
        buf = StreamBuffer()
        e = AgentOutputEvent(agent_id="a", content="The answer", is_final=True)
        buf.add(e)
        assert buf.final_answer == "The answer"
        assert buf.final_agent_id == "a"

    def test_add_run_end_updates_final_answer(self):
        buf = StreamBuffer()
        e = RunEndEvent(final_answer="Run answer", final_agent_id="agent1", success=True)
        buf.add(e)
        assert buf.final_answer == "Run answer"

    def test_add_token_events_accumulate(self):
        buf = StreamBuffer()
        buf.add(TokenEvent(agent_id="w", token="Hel", is_first=True))
        buf.add(TokenEvent(agent_id="w", token="lo"))
        buf.add(TokenEvent(agent_id="w", token="!", is_last=True))
        assert "Hello!" in buf.agent_outputs.get("w", "")

    def test_get_output_for(self):
        buf = StreamBuffer()
        buf.add(AgentOutputEvent(agent_id="writer", content="Written text"))
        assert buf.get_output_for("writer") == "Written text"
        assert buf.get_output_for("unknown") == ""

    def test_get_output_for_in_progress_tokens(self):
        """Line 391: get_output_for returns joined tokens when agent is in _current_tokens."""
        buf = StreamBuffer()
        # Add token events without a final AgentOutputEvent
        buf.add(TokenEvent(agent_id="streamer", token="tok1", is_first=True))
        buf.add(TokenEvent(agent_id="streamer", token="tok2"))
        # Agent not in _agent_outputs yet, but is in _current_tokens
        output = buf.get_output_for("streamer")
        assert "tok1" in output or "tok2" in output

    def test_agent_outputs(self):
        buf = StreamBuffer()
        buf.add(AgentOutputEvent(agent_id="a", content="output_a"))
        buf.add(AgentOutputEvent(agent_id="b", content="output_b"))
        assert "a" in buf.agent_outputs
        assert "b" in buf.agent_outputs

    def test_clear(self):
        buf = StreamBuffer()
        buf.add(AgentOutputEvent(agent_id="a", content="data", is_final=True))
        buf.clear()
        assert len(buf.events) == 0
        assert buf.final_answer == ""

    def test_buffer_init(self):
        buf = StreamBuffer()
        assert buf.final_answer == ""
        assert buf.events == []


# ─────────────────────────── stream_to_string ─────────────────────────────────


class TestStreamToString:
    def test_collects_agent_output(self):
        def gen():
            yield AgentOutputEvent(run_id=rid(), agent_id="a", content="Hello World", is_final=True)
            yield RunEndEvent(run_id=rid(), final_answer="Hello World", success=True)

        result = stream_to_string(gen())
        assert isinstance(result, str)

    def test_with_token_events_updates_current_agent(self):
        """Line 511: _handle_stream_event updates current_agent_ref for TokenEvent."""

        def gen():
            yield RunStartEvent(run_id=rid(), query="test")
            yield TokenEvent(agent_id="a", token="Hello", is_first=True)
            yield TokenEvent(agent_id="a", token=" World", is_last=True)
            yield RunEndEvent(run_id=rid(), final_answer="Hello World", success=True)

        result = stream_to_string(gen())
        assert isinstance(result, str)

    def test_empty_stream_returns_string(self):
        def gen():
            yield RunEndEvent(run_id=rid(), final_answer="", success=True)

        result = stream_to_string(gen())
        assert isinstance(result, str)

    def test_multiple_agents(self):
        def gen():
            yield AgentOutputEvent(agent_id="a", content="Part 1 ")
            yield AgentOutputEvent(agent_id="b", content="Part 2")
            yield RunEndEvent(final_answer="done", success=True)

        result = stream_to_string(gen())
        assert isinstance(result, str)


class TestAStreamToString:
    async def test_collects_agent_output(self):
        async def agen():
            yield AgentOutputEvent(run_id=rid(), agent_id="a", content="Hello", is_final=True)
            yield RunEndEvent(run_id=rid(), final_answer="Hello", success=True)

        result = await astream_to_string(agen())
        assert isinstance(result, str)

    async def test_empty_async_stream(self):
        async def agen():
            yield RunEndEvent(run_id=rid(), final_answer="", success=True)

        result = await astream_to_string(agen())
        assert isinstance(result, str)


# ─────────────────────────── print_stream / aprint_stream ─────────────────────


class TestPrintStream:
    def test_print_stream_runs(self, capsys):
        def gen():
            yield RunStartEvent(run_id=rid(), query="test", num_agents=1)
            yield AgentOutputEvent(run_id=rid(), agent_id="a", content="output")
            yield RunEndEvent(run_id=rid(), final_answer="output", success=True)

        print_stream(gen())
        # Should not raise

    async def test_aprint_stream_runs(self, capsys):
        async def agen():
            yield RunStartEvent(run_id=rid(), query="test")
            yield RunEndEvent(run_id=rid(), final_answer="done", success=True)

        await aprint_stream(agen())
        # Should not raise


# ─────────────────────────── StreamEventType enum ─────────────────────────────


class TestStreamEventType:
    def test_all_values_are_strings(self):
        for et in StreamEventType:
            assert isinstance(et.value, str)

    def test_run_start_value(self):
        assert StreamEventType.RUN_START.value == "run_start"

    def test_agent_output_value(self):
        assert StreamEventType.AGENT_OUTPUT.value == "agent_output"

    def test_token_value(self):
        assert StreamEventType.TOKEN.value == "token"

    def test_all_expected_types_present(self):
        types = {et.value for et in StreamEventType}
        assert "run_start" in types
        assert "run_end" in types
        assert "agent_start" in types
        assert "agent_output" in types
        assert "agent_error" in types
        assert "token" in types
        assert "prune" in types
        assert "fallback" in types
