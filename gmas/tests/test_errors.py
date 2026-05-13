from datetime import datetime, timedelta

import pytest

from gmas.execution.errors import (
    AgentNotFoundError,
    BudgetExceededError,
    ErrorAction,
    ErrorPolicy,
    ExecutionError,
    ExecutionMetrics,
    RetryExhaustedError,
    StepExecutionResult,
    TimeoutError,  # noqa: A004
    ValidationError,
)


class TestExecutionError:
    """Constructor and serialization of the base error class."""

    def test_full_init_and_to_dict(self):
        """All constructor parameters and to_dict with cause."""
        cause = ValueError("oops")
        err = ExecutionError("fail", agent_id="a1", step_index=2, cause=cause, recoverable=False)
        assert str(err) == "fail"
        assert err.agent_id == "a1"
        assert err.step_index == 2
        assert err.cause is cause
        assert err.recoverable is False
        assert isinstance(err.timestamp, datetime)

        d = err.to_dict()
        assert d["type"] == "ExecutionError"
        assert d["message"] == "fail"
        assert d["agent_id"] == "a1"
        assert d["step_index"] == 2
        assert d["recoverable"] is False
        assert d["cause"] == "oops"
        assert d["timestamp"]

    def test_defaults_and_no_cause(self):
        """Default values and to_dict without cause."""
        err = ExecutionError("msg")
        assert err.agent_id is None
        assert err.step_index is None
        assert err.cause is None
        assert err.recoverable is True
        assert err.to_dict()["cause"] is None


class TestErrorSubclasses:
    """ExecutionError subclasses — constructors and inheritance."""

    def test_timeout_error(self):
        """TimeoutError: timeout seconds, recoverable=True."""
        err = TimeoutError("a1", 30.0, step_index=1)
        assert isinstance(err, ExecutionError)
        assert err.timeout_seconds == 30.0
        assert err.agent_id == "a1"
        assert err.step_index == 1
        assert err.recoverable is True
        assert "timed out" in str(err)

    def test_retry_exhausted_error(self):
        """RetryExhaustedError: exhausted retry attempts, recoverable=False."""
        cause = RuntimeError("boom")
        err = RetryExhaustedError("a2", 3, last_error=cause, step_index=5)
        assert isinstance(err, ExecutionError)
        assert err.attempts == 3
        assert err.last_error is cause
        assert err.cause is cause
        assert err.recoverable is False
        assert "3 attempts" in str(err)

    def test_budget_exceeded_error(self):
        """BudgetExceededError: budget limit exceeded."""
        err = BudgetExceededError("token", 1000.0, 1500.0, agent_id="a3")
        assert isinstance(err, ExecutionError)
        assert err.budget_type == "token"
        assert err.limit == 1000.0
        assert err.used == 1500.0
        assert err.recoverable is False
        assert "1500.0/1000.0" in str(err)

    def test_agent_not_found_error(self):
        """AgentNotFoundError: agent not found."""
        err = AgentNotFoundError("missing")
        assert isinstance(err, ExecutionError)
        assert err.agent_id == "missing"
        assert err.recoverable is False

    def test_validation_error(self):
        """ValidationError: invalid field and value."""
        err = ValidationError("bad input", field="name", value=42)
        assert isinstance(err, ExecutionError)
        assert err.field == "name"
        assert err.value == 42
        assert err.recoverable is False


class TestErrorPolicy:
    """Error routing via get_action."""

    def test_default_actions(self):
        """Default policy action for each error type."""
        policy = ErrorPolicy()
        assert policy.get_action(TimeoutError("a", 1.0)) == ErrorAction.RETRY
        assert policy.get_action(RetryExhaustedError("a", 1)) == ErrorAction.PRUNE
        assert policy.get_action(BudgetExceededError("t", 1, 2)) == ErrorAction.ABORT
        assert policy.get_action(AgentNotFoundError("a")) == ErrorAction.SKIP
        assert policy.get_action(ValidationError("v")) == ErrorAction.ABORT
        assert policy.get_action(ExecutionError("unknown")) == ErrorAction.SKIP

    def test_custom_policy(self):
        """Overridden timeout action."""
        policy = ErrorPolicy(on_timeout=ErrorAction.ABORT)
        assert policy.get_action(TimeoutError("a", 1.0)) == ErrorAction.ABORT

    def test_model_fields(self):
        """Default values for max_skipped_agents and abort_on_critical_path."""
        policy = ErrorPolicy()
        assert policy.max_skipped_agents == 5
        assert policy.abort_on_critical_path is True


class TestExecutionMetrics:
    """Metrics accumulation, properties, and serialization."""

    def test_add_request_success(self):
        """Successful request — tokens and counters."""
        m = ExecutionMetrics()
        m.add_request(100, 50, success=True, latency_ms=200.0)
        assert m.prompt_tokens == 100
        assert m.completion_tokens == 50
        assert m.total_tokens == 150
        assert m.total_requests == 1
        assert m.successful_requests == 1
        assert m.failed_requests == 0
        assert m.retried_requests == 0

    def test_add_request_failed_and_retried(self):
        """Failed request with retry."""
        m = ExecutionMetrics()
        m.add_request(10, 5, success=False, latency_ms=50.0, retried=True)
        assert m.failed_requests == 1
        assert m.retried_requests == 1
        assert m.successful_requests == 0

    def test_success_rate(self):
        """success_rate: 0.0 when empty, 0.5 for one success out of two."""
        m = ExecutionMetrics()
        assert m.success_rate == 0.0
        m.add_request(1, 1, success=True, latency_ms=1.0)
        m.add_request(1, 1, success=False, latency_ms=1.0)
        assert m.success_rate == 0.5

    def test_duration_seconds(self):
        """duration_seconds derived from start_time and end_time."""
        now = datetime.now()
        m = ExecutionMetrics(start_time=now, end_time=now + timedelta(seconds=3))
        assert m.duration_seconds == pytest.approx(3.0)

    def test_duration_seconds_no_times(self):
        """duration_seconds is 0 when no timestamps are set."""
        assert ExecutionMetrics().duration_seconds == 0.0

    def test_to_dict_structure(self):
        """Dict structure: tokens, requests, agents, timing."""
        m = ExecutionMetrics(total_agents=10, executed_agents=8, skipped_agents=1, failed_agents=1)
        m.add_request(100, 50, success=True, latency_ms=200.0)
        d = m.to_dict()
        assert d["tokens"]["total"] == 150
        assert d["requests"]["success_rate"] == 1.0
        assert d["agents"]["total"] == 10
        assert d["timing"]["avg_latency_ms"] == 200.0


class TestStepExecutionResult:
    """Step execution result — properties and serialization."""

    def test_tokens_used(self):
        """tokens_used equals prompt + completion tokens."""
        r = StepExecutionResult(agent_id="a1", success=True, prompt_tokens=100, completion_tokens=50)
        assert r.tokens_used == 150

    def test_to_dict_success(self):
        """to_dict of a successful step with response."""
        r = StepExecutionResult(agent_id="a1", success=True, response="hello", latency_ms=10.0)
        d = r.to_dict()
        assert d["agent_id"] == "a1"
        assert d["success"] is True
        assert d["response_length"] == 5
        assert d["error"] is None
        assert d["status"]["skipped"] is False

    def test_to_dict_with_error(self):
        """to_dict with a nested error object."""
        err = ExecutionError("oops", agent_id="a1")
        r = StepExecutionResult(agent_id="a1", success=False, error=err)
        d = r.to_dict()
        assert d["error"]["type"] == "ExecutionError"
        assert d["response_length"] == 0

    def test_defaults(self):
        """Default values for all optional fields."""
        r = StepExecutionResult(agent_id="x", success=True)
        assert r.response is None
        assert r.error is None
        assert r.attempts == 1
        assert r.quality_score == 1.0
        assert r.skipped is False
        assert r.fallback_used is False
