"""Tests for callback handlers: FileCallbackHandler, MetricsCallbackHandler, StdoutCallbackHandler"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from gmas.callbacks.handlers.file import FileCallbackHandler
from gmas.callbacks.handlers.metrics import MetricsCallbackHandler
from gmas.callbacks.handlers.stdout import StdoutCallbackHandler

# ─────────────────────────── FileCallbackHandler ─────────────────────────────


@pytest.fixture
def tmp_log_file(tmp_path):
    return tmp_path / "events.jsonl"


@pytest.fixture
def file_handler(tmp_log_file):
    handler = FileCallbackHandler(tmp_log_file)
    yield handler
    handler.close()


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    with path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if line:
                events.append(json.loads(line))
    return events


class TestFileCallbackHandlerInit:
    def test_creates_file(self, tmp_log_file):
        handler = FileCallbackHandler(tmp_log_file)
        handler.close()
        assert tmp_log_file.exists()

    def test_creates_parent_dirs(self, tmp_path):
        deep_path = tmp_path / "logs" / "sub" / "events.jsonl"
        handler = FileCallbackHandler(deep_path)
        handler.close()
        assert deep_path.exists()

    def test_append_mode(self, tmp_log_file):
        run_id = uuid4()
        h = FileCallbackHandler(tmp_log_file, append=True)
        h.on_run_start(run_id=run_id, query="q1")
        h.close()
        h2 = FileCallbackHandler(tmp_log_file, append=True)
        h2.on_run_start(run_id=run_id, query="q2")
        h2.close()
        events = _read_events(tmp_log_file)
        assert len(events) == 2

    def test_overwrite_mode(self, tmp_log_file):
        run_id = uuid4()
        h = FileCallbackHandler(tmp_log_file, append=False)
        h.on_run_start(run_id=run_id, query="q1")
        h.close()
        h2 = FileCallbackHandler(tmp_log_file, append=False)
        h2.on_run_start(run_id=run_id, query="q2")
        h2.close()
        events = _read_events(tmp_log_file)
        assert len(events) == 1


class TestFileCallbackHandlerRunLifecycle:
    def test_on_run_start(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_run_start(
            run_id=run_id,
            query="test query",
            num_agents=3,
            execution_order=["a", "b", "c"],
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == "run_start"
        assert e["query"] == "test query"
        assert e["num_agents"] == 3
        assert e["run_id"] == str(run_id)

    def test_on_run_end_success(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_run_end(
            run_id=run_id,
            output="result",
            success=True,
            total_tokens=500,
            total_time_ms=1234.5,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "run_end"
        assert e["success"] is True
        assert e["total_tokens"] == 500

    def test_on_run_end_failure(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_run_end(
            run_id=run_id,
            output="",
            success=False,
            error=ValueError("something failed"),
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "run_end"
        assert e["success"] is False
        assert "something failed" in e["error"]


class TestFileCallbackHandlerAgentLifecycle:
    def test_on_agent_start(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_agent_start(
            run_id=run_id,
            agent_id="solver",
            agent_name="Solver",
            step_index=0,
            prompt="Solve this",
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "agent_start"
        assert e["agent_id"] == "solver"

    def test_on_agent_end(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_agent_end(
            run_id=run_id,
            agent_id="solver",
            output="answer",
            tokens_used=100,
            duration_ms=250.0,
            is_final=True,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "agent_end"
        assert e["tokens_used"] == 100
        assert e["is_final"] is True

    def test_on_agent_error(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_agent_error(
            ValueError("oops"),
            run_id=run_id,
            agent_id="solver",
            will_retry=True,
            attempt=1,
            max_attempts=3,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "agent_error"
        assert e["will_retry"] is True
        assert "ValueError" in e["error_type"] or "oops" in e["error_message"]

    def test_on_retry(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_retry(
            run_id=run_id,
            agent_id="solver",
            attempt=2,
            max_attempts=3,
            delay_ms=500.0,
            error="Timeout",
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "retry"
        assert e["attempt"] == 2


class TestFileCallbackHandlerTokenStreaming:
    def test_on_llm_new_token_first(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_llm_new_token(
            "Hello",
            run_id=run_id,
            agent_id="solver",
            is_first=True,
            token_index=0,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        assert len(events) == 1
        assert events[0]["is_first"] is True

    def test_on_llm_new_token_middle_not_logged(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_llm_new_token(
            "word",
            run_id=run_id,
            agent_id="solver",
            is_first=False,
            is_last=False,
            token_index=5,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        assert len(events) == 0  # middle tokens not logged

    def test_on_llm_new_token_last(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_llm_new_token(
            "end",
            run_id=run_id,
            agent_id="solver",
            is_last=True,
            token_index=99,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        assert len(events) == 1
        assert events[0]["is_last"] is True


class TestFileCallbackHandlerPlanningAndOther:
    def test_on_plan_created(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_plan_created(
            run_id=run_id,
            num_steps=3,
            execution_order=["a", "b", "c"],
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "plan_created"
        assert e["num_steps"] == 3

    def test_on_topology_changed(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_topology_changed(
            run_id=run_id,
            reason="agent pruned",
            old_remaining=["a", "b"],
            new_remaining=["b"],
            change_count=1,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "topology_changed"
        assert e["change_count"] == 1

    def test_on_prune(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_prune(run_id=run_id, agent_id="slow_agent", reason="too slow")
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        assert events[0]["event_type"] == "prune"
        assert events[0]["reason"] == "too slow"

    def test_on_fallback(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_fallback(
            run_id=run_id,
            failed_agent_id="agent_a",
            fallback_agent_id="agent_b",
            reason="failure",
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "fallback"
        assert e["failed_agent_id"] == "agent_a"

    def test_on_parallel_start(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_parallel_start(
            run_id=run_id,
            agent_ids=["a", "b"],
            group_index=0,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        assert events[0]["event_type"] == "parallel_start"

    def test_on_parallel_end(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_parallel_end(
            run_id=run_id,
            agent_ids=["a", "b"],
            group_index=0,
            successful=["a", "b"],
            failed=[],
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        assert events[0]["event_type"] == "parallel_end"

    def test_on_memory_read(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_memory_read(
            run_id=run_id,
            agent_id="agent1",
            entries_count=5,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        assert events[0]["event_type"] == "memory_read"

    def test_on_memory_write(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_memory_write(
            run_id=run_id,
            agent_id="agent1",
            key="context",
            value_size=1024,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        assert events[0]["event_type"] == "memory_write"

    def test_on_budget_warning(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_budget_warning(
            run_id=run_id,
            budget_type="tokens",
            current=800.0,
            limit=1000.0,
            ratio=0.8,
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "budget_warning"
        assert e["budget_type"] == "tokens"

    def test_on_budget_exceeded(self, file_handler, tmp_log_file):
        run_id = uuid4()
        file_handler.on_budget_exceeded(
            run_id=run_id,
            budget_type="requests",
            current=10.0,
            limit=10.0,
            action_taken="stop",
        )
        file_handler._file.flush()
        events = _read_events(tmp_log_file)
        e = events[0]
        assert e["event_type"] == "budget_exceeded"
        assert e["action_taken"] == "stop"

    def test_close_idempotent(self, file_handler):
        file_handler.close()
        file_handler.close()  # should not raise

    def test_flush_every(self, tmp_log_file):
        """Test flush_every parameter (event count flush trigger)."""
        handler = FileCallbackHandler(tmp_log_file, flush_every=3)
        run_id = uuid4()
        for _ in range(3):
            handler.on_run_start(run_id=run_id, query="q")
        handler.close()
        events = _read_events(tmp_log_file)
        assert len(events) == 3


# ─────────────────────────── MetricsCallbackHandler ──────────────────────────


class TestMetricsCallbackHandler:
    def setup_method(self):
        self.handler = MetricsCallbackHandler()
        self.run_id = uuid4()

    def test_initial_state(self):
        metrics = self.handler.get_metrics()
        assert metrics["total_tokens"] == 0
        assert metrics["runs_completed"] == 0
        assert metrics["runs_failed"] == 0
        assert metrics["retries"] == 0

    def test_on_run_start_records_time(self):
        self.handler.on_run_start(run_id=self.run_id, query="test")
        assert self.handler._run_start_time is not None

    def test_on_run_end_success(self):
        self.handler.on_run_start(run_id=self.run_id, query="test")
        self.handler.on_run_end(
            run_id=self.run_id,
            output="result",
            success=True,
            total_tokens=500,
            total_time_ms=1000.0,
        )
        assert self.handler.runs_completed == 1
        assert self.handler.runs_failed == 0
        assert self.handler.total_tokens == 500

    def test_on_run_end_failure(self):
        self.handler.on_run_start(run_id=self.run_id, query="test")
        self.handler.on_run_end(
            run_id=self.run_id,
            output="",
            success=False,
        )
        assert self.handler.runs_failed == 1
        assert self.handler.runs_completed == 0

    def test_on_agent_end_accumulates(self):
        self.handler.on_agent_end(
            run_id=self.run_id,
            agent_id="solver",
            output="answer",
            tokens_used=150,
            duration_ms=200.0,
        )
        self.handler.on_agent_end(
            run_id=self.run_id,
            agent_id="solver",
            output="another",
            tokens_used=100,
            duration_ms=100.0,
        )
        metrics = self.handler.get_metrics()
        assert metrics["total_tokens"] == 250
        assert metrics["agent_tokens"]["solver"] == 250
        assert metrics["agent_calls"]["solver"] == 2

    def test_on_agent_error(self):
        self.handler.on_agent_error(
            ValueError("timeout"),
            run_id=self.run_id,
            agent_id="solver",
        )
        metrics = self.handler.get_metrics()
        assert metrics["errors_count"] == 1
        assert "ValueError" in metrics["errors"][-1]["error_type"]

    def test_on_retry(self):
        self.handler.on_retry(
            run_id=self.run_id,
            agent_id="solver",
            attempt=1,
            max_attempts=3,
        )
        metrics = self.handler.get_metrics()
        assert metrics["retries"] == 1

    def test_on_budget_warning(self):
        self.handler.on_budget_warning(
            run_id=self.run_id,
            budget_type="tokens",
            current=800.0,
            limit=1000.0,
        )
        metrics = self.handler.get_metrics()
        assert metrics["budget_warnings"] == 1

    def test_on_tool_end(self):
        self.handler.on_tool_end(
            run_id=self.run_id,
            tool_name="code_interpreter",
            action="execute",
            success=True,
            duration_ms=100.0,
        )
        metrics = self.handler.get_metrics()
        assert "code_interpreter.execute" in metrics["tool_calls"]
        assert metrics["tool_calls"]["code_interpreter.execute"] == 1

    def test_on_tool_end_no_action(self):
        self.handler.on_tool_end(
            run_id=self.run_id,
            tool_name="file_search",
            success=True,
            duration_ms=50.0,
        )
        metrics = self.handler.get_metrics()
        assert "file_search" in metrics["tool_calls"]

    def test_on_tool_error(self):
        self.handler.on_tool_error(
            run_id=self.run_id,
            tool_name="web_search",
            error_type="TimeoutError",
            error_message="Connection timeout",
        )
        metrics = self.handler.get_metrics()
        assert metrics["tool_errors"] == 1
        assert metrics["errors_count"] >= 1

    def test_reset(self):
        self.handler.on_agent_end(
            run_id=self.run_id,
            agent_id="solver",
            output="x",
            tokens_used=100,
        )
        self.handler.reset()
        metrics = self.handler.get_metrics()
        assert metrics["total_tokens"] == 0
        assert metrics["runs_completed"] == 0

    def test_avg_tokens_per_agent(self):
        self.handler.on_agent_end(
            run_id=self.run_id,
            agent_id="a1",
            output="x",
            tokens_used=100,
        )
        self.handler.on_agent_end(
            run_id=self.run_id,
            agent_id="a2",
            output="y",
            tokens_used=200,
        )
        metrics = self.handler.get_metrics()
        avg = metrics["avg_tokens_per_agent"]
        assert avg == 150.0

    def test_multiple_runs(self):
        for i in range(3):
            rid = uuid4()
            self.handler.on_run_start(run_id=rid, query=f"q{i}")
            self.handler.on_run_end(run_id=rid, output="ok", success=True)
        assert self.handler.runs_completed == 3

    def test_total_duration_accumulates(self):
        self.handler.on_agent_end(run_id=self.run_id, agent_id="a1", output="x", duration_ms=100.0)
        self.handler.on_agent_end(run_id=self.run_id, agent_id="a1", output="y", duration_ms=200.0)
        assert self.handler.total_duration_ms == 300.0


# ─────────────────────────── StdoutCallbackHandler ───────────────────────────


class TestStdoutCallbackHandler:
    """StdoutCallbackHandler methods should not raise exceptions."""

    def setup_method(self):
        self.handler = StdoutCallbackHandler()
        self.run_id = uuid4()

    def test_on_run_start(self):
        self.handler.on_run_start(
            run_id=self.run_id,
            query="test",
            num_agents=3,
            execution_order=["a", "b", "c"],
        )
        assert self.handler._indent == 1

    def test_on_run_end_success(self):
        self.handler._indent = 1
        self.handler.on_run_end(
            run_id=self.run_id,
            output="result",
            success=True,
            total_tokens=100,
            total_time_ms=500.0,
        )
        assert self.handler._indent == 0

    def test_on_run_end_failure(self):
        self.handler._indent = 1
        self.handler.on_run_end(
            run_id=self.run_id,
            output="",
            success=False,
            error=RuntimeError("failed"),
        )

    def test_on_agent_start(self):
        initial = self.handler._indent
        self.handler.on_agent_start(
            run_id=self.run_id,
            agent_id="solver",
            agent_name="Solver",
            step_index=0,
            prompt="hello",
        )
        assert self.handler._indent == initial + 1

    def test_on_agent_start_with_prompt(self):
        handler = StdoutCallbackHandler(show_prompts=True)
        handler.on_agent_start(
            run_id=self.run_id,
            agent_id="solver",
            agent_name="Solver",
            step_index=0,
            prompt="A very long prompt that should be shown",
        )

    def test_on_agent_end(self):
        self.handler._indent = 1
        self.handler.on_agent_end(
            run_id=self.run_id,
            agent_id="solver",
            output="result",
            tokens_used=50,
            duration_ms=100.0,
            is_final=True,
        )
        assert self.handler._indent == 0

    def test_on_agent_end_with_output(self):
        handler = StdoutCallbackHandler(show_outputs=True)
        handler._indent = 1
        handler.on_agent_end(
            run_id=self.run_id,
            agent_id="solver",
            output="The answer is 42",
            tokens_used=50,
        )

    def test_on_agent_error_no_retry(self):
        self.handler.on_agent_error(
            ValueError("test"),
            run_id=self.run_id,
            agent_id="solver",
        )

    def test_on_agent_error_with_retry(self):
        self.handler.on_agent_error(
            ValueError("test"),
            run_id=self.run_id,
            agent_id="solver",
            will_retry=True,
            attempt=1,
            max_attempts=3,
        )

    def test_on_retry(self):
        self.handler.on_retry(
            run_id=self.run_id,
            agent_id="solver",
            attempt=2,
            max_attempts=3,
            delay_ms=500.0,
        )

    def test_on_llm_new_token(self):
        self.handler.on_llm_new_token(
            "token",
            run_id=self.run_id,
            agent_id="solver",
            is_first=True,
        )
        self.handler.on_llm_new_token(
            "token",
            run_id=self.run_id,
            agent_id="solver",
            is_last=True,
        )

    def test_on_plan_created(self):
        self.handler.on_plan_created(
            run_id=self.run_id,
            num_steps=3,
            execution_order=["a", "b", "c"],
        )

    def test_on_topology_changed(self):
        self.handler.on_topology_changed(
            run_id=self.run_id,
            reason="pruned",
            old_remaining=["a", "b"],
            new_remaining=["b"],
            change_count=1,
        )

    def test_on_prune(self):
        self.handler.on_prune(
            run_id=self.run_id,
            agent_id="slow_agent",
            reason="too slow",
        )

    def test_on_fallback(self):
        self.handler.on_fallback(
            run_id=self.run_id,
            failed_agent_id="agent_a",
            fallback_agent_id="agent_b",
        )

    def test_on_parallel_start(self):
        self.handler.on_parallel_start(
            run_id=self.run_id,
            agent_ids=["a", "b"],
            group_index=0,
        )

    def test_on_parallel_end(self):
        self.handler._indent = 1
        self.handler.on_parallel_end(
            run_id=self.run_id,
            agent_ids=["a", "b"],
            group_index=0,
            successful=["a", "b"],
        )

    def test_on_budget_warning(self):
        self.handler.on_budget_warning(
            run_id=self.run_id,
            budget_type="tokens",
            current=800.0,
            limit=1000.0,
            ratio=0.8,
        )

    def test_on_budget_exceeded(self):
        self.handler.on_budget_exceeded(
            run_id=self.run_id,
            budget_type="tokens",
            current=1000.0,
            limit=1000.0,
            action_taken="stop",
        )

    def test_on_tool_start(self):
        self.handler.on_tool_start(
            run_id=self.run_id,
            tool_name="code_interpreter",
            action="execute",
            arguments={"code": "print(1)"},
        )

    def test_on_tool_end_success(self):
        self.handler._indent = 1
        self.handler.on_tool_end(
            run_id=self.run_id,
            tool_name="code_interpreter",
            action="execute",
            success=True,
            duration_ms=100.0,
            output_size=50,
        )

    def test_on_tool_end_failure(self):
        self.handler._indent = 1
        self.handler.on_tool_end(
            run_id=self.run_id,
            tool_name="code_interpreter",
            action="execute",
            success=False,
            duration_ms=100.0,
        )

    def test_on_tool_error(self):
        self.handler.on_tool_error(
            run_id=self.run_id,
            tool_name="web_search",
            action="search",
            error_type="TimeoutError",
            error_message="Connection timed out",
        )

    def test_truncate(self):
        handler = StdoutCallbackHandler(truncate_length=10)
        short = "hello"
        long_text = "x" * 100
        assert handler._truncate(short) == short
        truncated = handler._truncate(long_text)
        assert truncated.endswith("...")
        assert len(truncated) == 13  # 10 + "..."

    def test_indent_not_go_below_zero(self):
        self.handler._indent = 0
        self.handler.on_run_end(
            run_id=self.run_id,
            output="x",
            success=True,
        )
        assert self.handler._indent == 0

    def test_run_without_execution_order(self):
        self.handler.on_run_start(
            run_id=self.run_id,
            query="test",
            num_agents=0,
        )


# ─────────────────────────── FileCallbackHandler - _file is None ─────────────


class TestFileCallbackHandlerFileNone:
    def test_write_event_returns_early_when_file_is_none(self, tmp_path):
        """Line 42: _write_event returns early when _file is None."""
        tmp_log_file = tmp_path / "test_events.jsonl"
        handler = FileCallbackHandler(tmp_log_file)
        # Manually set _file to None to simulate closed state
        handler._file = None
        # Should not raise and should not write anything
        handler._write_event("test_event", {"key": "value"})
        handler.close()
        # File should be empty or not exist since we bypassed normal writes
        import json

        events = []
        if tmp_log_file.exists():
            with tmp_log_file.open() as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if line:
                        events.append(json.loads(line))
        assert len(events) == 0
