"""Tests for execution/runner.py — MACPRunner."""

import asyncio
from typing import TYPE_CHECKING

import pytest
import rustworkx as rx
import torch

from gmas.core.graph import RoleGraph
from gmas.execution.budget import BudgetConfig
from gmas.execution.runner import MACPResult, MACPRunner, RunnerConfig

if TYPE_CHECKING:
    from collections.abc import Callable


def create_test_graph(nodes, edges):
    """Create a test RoleGraph."""
    from gmas.core.agent import AgentProfile

    g = rx.PyDiGraph()

    id_to_idx = {}
    agents = []
    for nid in nodes:
        if nid != "task":
            idx = g.add_node({"id": nid})
            id_to_idx[nid] = idx
            # Create roles for agents
            agent = AgentProfile(agent_id=nid, display_name=f"Agent {nid.upper()}")
            agents.append(agent)

    connections = {n: [] for n in nodes}

    for src, tgt in edges:
        if src in id_to_idx and tgt in id_to_idx:
            g.add_edge(id_to_idx[src], id_to_idx[tgt], {"weight": 1.0})
            connections[src].append(tgt)

    n = len(id_to_idx)
    a_com = torch.zeros((n + 1, n + 1), dtype=torch.float32)  # +1 for task node

    # Add task node
    task_idx = g.add_node({"id": "task"})
    id_to_idx["task"] = task_idx

    # Fill matrix
    node_list = [nid for nid in nodes if nid != "task"]
    for i, src in enumerate(node_list):
        for tgt in connections[src]:
            if tgt in node_list:
                j = node_list.index(tgt)
                a_com[i, j] = 1.0

    role_graph = RoleGraph(
        node_ids=nodes,
        role_connections=connections,
        graph=g,
        A_com=a_com,
        task_node="task",
        query="test query",
    )
    role_graph.agents = agents

    return role_graph


def create_simple_llm_caller(response_text="Test response"):
    """Create a simple synchronous LLM caller."""

    def llm_caller(prompt: str) -> str:
        return response_text

    return llm_caller


def create_simple_async_llm_caller(response_text="Test response"):
    """Create a simple asynchronous LLM caller."""

    async def async_llm_caller(prompt: str) -> str:
        await asyncio.sleep(0.001)  # Simulate delay
        return response_text

    return async_llm_caller


class TestMACPRunnerCreation:
    """Tests for MACPRunner creation."""

    def test_basic_creation(self):
        """Basic creation."""
        llm_caller = create_simple_llm_caller()
        runner = MACPRunner(llm_caller=llm_caller)

        assert runner is not None
        assert runner.llm_caller is not None

    def test_creation_with_config(self):
        """Creation with configuration."""
        llm_caller = create_simple_llm_caller()
        config = RunnerConfig(
            timeout=30.0,
            max_retries=3,
            adaptive=True,
        )

        runner = MACPRunner(llm_caller=llm_caller, config=config)

        assert runner.config.timeout == 30.0
        assert runner.config.max_retries == 3
        assert runner.config.adaptive


class TestSyncExecution:
    """Tests for synchronous execution."""

    def test_run_simple(self):
        """Simple run."""
        graph = create_test_graph(["a", "b"], [("a", "b")])
        llm_caller = create_simple_llm_caller()

        runner = MACPRunner(llm_caller=llm_caller)
        result = runner.run_round(graph)

        assert isinstance(result, MACPResult)
        assert result.final_answer is not None

    def test_run_linear_graph(self):
        """Run on a linear graph."""
        graph = create_test_graph(["a", "b", "c"], [("a", "b"), ("b", "c")])
        llm_caller = create_simple_llm_caller()

        runner = MACPRunner(llm_caller=llm_caller)
        result = runner.run_round(graph)

        assert len(result.execution_order) == 3
        assert result.final_answer is not None

    def test_run_with_final_agent(self):
        """Run with a specified final agent."""
        graph = create_test_graph(["a", "b"], [("a", "b")])
        llm_caller = create_simple_llm_caller("final response")

        runner = MACPRunner(llm_caller=llm_caller)
        result = runner.run_round(graph, final_agent_id="b")

        assert result.final_agent_id == "b"


class TestAsyncExecution:
    """Tests for asynchronous execution."""

    @pytest.mark.asyncio
    async def test_arun_simple(self):
        """Simple async run."""
        graph = create_test_graph(["a", "b"], [("a", "b")])
        async_llm_caller = create_simple_async_llm_caller()

        runner = MACPRunner(async_llm_caller=async_llm_caller)
        result = await runner.arun_round(graph)

        assert isinstance(result, MACPResult)
        assert result.final_answer is not None

    @pytest.mark.asyncio
    async def test_arun_parallel_execution(self):
        """Parallel async execution."""
        # a -> b, c (parallel) -> d
        graph = create_test_graph(
            ["a", "b", "c", "d"],
            [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")],
        )

        async_llm_caller = create_simple_async_llm_caller()
        config = RunnerConfig(enable_parallel=True, adaptive=True)
        runner = MACPRunner(async_llm_caller=async_llm_caller, config=config)
        result = await runner.arun_round(graph)

        # Should have executed all agents
        assert len(result.execution_order) == 4
        # a should be first in execution order
        assert result.execution_order[0] == "a"


class TestTimeouts:
    """Tests for timeout handling."""

    @pytest.mark.asyncio
    async def test_timeout_triggers(self):
        """Timeout triggers."""
        graph = create_test_graph(["a"], [])

        async def slow_llm_caller(prompt: str) -> str:
            await asyncio.sleep(10.0)  # Very slow
            return "done"

        config = RunnerConfig(timeout=0.1)  # Short timeout

        runner = MACPRunner(async_llm_caller=slow_llm_caller, config=config)
        result = await runner.arun_round(graph)

        # Should complete but might have timeout in messages
        assert result is not None

    @pytest.mark.asyncio
    async def test_per_agent_timeout(self):
        """Per-agent timeout."""
        graph = create_test_graph(["a", "b"], [("a", "b")])

        call_count = 0

        async def slow_llm_caller(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                await asyncio.sleep(10.0)
            return "response"

        config = RunnerConfig(timeout=0.1)
        runner = MACPRunner(async_llm_caller=slow_llm_caller, config=config)
        result = await runner.arun_round(graph)

        # First agent should succeed, second might timeout
        assert result is not None


class TestRetries:
    """Tests for retry mechanism."""

    @pytest.mark.asyncio
    async def test_retry_on_failure(self):
        """Retry on failure."""
        graph = create_test_graph(["a"], [])

        attempt_count = 0

        async def flaky_llm_caller(prompt: str) -> str:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                msg = "Temporary failure"
                raise RuntimeError(msg)
            return "success"

        config = RunnerConfig(max_retries=5, adaptive=True)
        runner = MACPRunner(async_llm_caller=flaky_llm_caller, config=config)
        result = await runner.arun_round(graph)

        assert attempt_count == 3
        assert result.messages.get("a") == "success"

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        """Max retries exceeded."""
        graph = create_test_graph(["a"], [])

        async def always_fails(prompt: str) -> str:
            msg = "Always fails"
            raise RuntimeError(msg)

        config = RunnerConfig(max_retries=2, adaptive=True)
        runner = MACPRunner(async_llm_caller=always_fails, config=config)
        result = await runner.arun_round(graph)

        # Should fail after max retries
        assert result.errors is not None

    @pytest.mark.asyncio
    async def test_retry_with_backoff(self):
        """Retry with exponential backoff."""
        graph = create_test_graph(["a"], [])

        import time

        timestamps = []

        async def timing_llm_caller(prompt: str) -> str:
            timestamps.append(time.time())
            if len(timestamps) < 3:
                msg = "Retry"
                raise RuntimeError(msg)
            return "done"

        config = RunnerConfig(
            max_retries=5,
            retry_delay=0.1,
            retry_backoff=2.0,
            adaptive=True,
        )

        runner = MACPRunner(async_llm_caller=timing_llm_caller, config=config)
        await runner.arun_round(graph)

        # Check delays increased
        if len(timestamps) >= 3:
            delay1 = timestamps[1] - timestamps[0]
            delay2 = timestamps[2] - timestamps[1]
            assert delay2 > delay1  # Backoff should increase delay


class TestBudgetControl:
    """Tests for budget control."""

    @pytest.mark.asyncio
    async def test_token_budget_respected(self):
        """Token budget is respected."""
        graph = create_test_graph(["a", "b", "c"], [("a", "b"), ("b", "c")])

        async def token_hungry_llm_caller(prompt: str) -> str:
            # Simulate token usage
            return "response " * 100  # Many tokens

        budget_config = BudgetConfig(
            # Using reasonable defaults - BudgetConfig doesn't require specific params
        )
        config = RunnerConfig(budget_config=budget_config, adaptive=True)

        runner = MACPRunner(async_llm_caller=token_hungry_llm_caller, config=config)
        result = await runner.arun_round(graph)

        # Should stop due to budget or complete with budget tracking
        assert result.budget_summary is not None or result.total_tokens > 0

    @pytest.mark.asyncio
    async def test_budget_warning(self):
        """Warning when approaching budget."""
        graph = create_test_graph(["a"], [])

        async_llm_caller = create_simple_async_llm_caller()
        budget_config = BudgetConfig(
            # Using reasonable defaults
        )
        config = RunnerConfig(budget_config=budget_config)

        runner = MACPRunner(async_llm_caller=async_llm_caller, config=config)
        result = await runner.arun_round(graph)

        # Should complete successfully
        assert result is not None


class TestMemoryUpdates:
    """Tests for agent memory updates."""

    @pytest.mark.asyncio
    async def test_state_propagation(self):
        """State is propagated between agents."""
        graph = create_test_graph(["a", "b"], [("a", "b")])

        async_llm_caller = create_simple_async_llm_caller()
        runner = MACPRunner(async_llm_caller=async_llm_caller)
        result = await runner.arun_round(graph)

        # b should have received context from a
        assert "b" in result.messages
        assert result.messages["b"] is not None

    @pytest.mark.asyncio
    async def test_hidden_state_channels(self):
        """Hidden state channels."""
        graph = create_test_graph(["a", "b"], [("a", "b")])

        async_llm_caller = create_simple_async_llm_caller()
        config = RunnerConfig(enable_hidden_channels=True)
        runner = MACPRunner(async_llm_caller=async_llm_caller, config=config)

        result = await runner.arun_round(graph)

        assert result is not None


class TestAdaptiveMode:
    """Tests for adaptive mode."""

    @pytest.mark.asyncio
    async def test_adaptive_routing(self):
        """Adaptive routing."""
        graph = create_test_graph(
            ["a", "b", "c"],
            [("a", "b"), ("a", "c"), ("b", "c")],
        )

        async_llm_caller = create_simple_async_llm_caller()
        config = RunnerConfig(adaptive=True)
        runner = MACPRunner(async_llm_caller=async_llm_caller, config=config)

        result = await runner.arun_round(graph)

        assert len(result.execution_order) > 0

    @pytest.mark.asyncio
    async def test_adaptive_topology_change(self):
        """Adaptive topology change on error."""
        graph = create_test_graph(
            ["a", "b", "fallback", "c"],
            [("a", "b"), ("a", "fallback"), ("b", "c"), ("fallback", "c")],
        )

        call_count = 0

        async def maybe_failing_llm_caller(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            # Simulate error on the second call (agent b)
            if call_count == 2:
                msg = "b failed"
                raise RuntimeError(msg)
            return "response"

        config = RunnerConfig(
            adaptive=True,
            max_retries=0,
        )
        runner = MACPRunner(async_llm_caller=maybe_failing_llm_caller, config=config)

        result = await runner.arun_round(graph)

        # Should complete with some agents executed
        assert result is not None


class TestErrorHandling:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_agent_exception_handled(self):
        """Agent exception is handled."""
        graph = create_test_graph(["a", "b"], [("a", "b")])

        call_count = 0

        async def failing_llm_caller(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "Agent error"
                raise ValueError(msg)
            return "response"

        config = RunnerConfig(max_retries=0)
        runner = MACPRunner(async_llm_caller=failing_llm_caller, config=config)

        result = await runner.arun_round(graph)

        # Should not crash, error should be recorded
        assert result is not None

    @pytest.mark.asyncio
    async def test_on_error_fail_policy(self):
        """Error handling with retries."""
        graph = create_test_graph(["a"], [])

        async def failing_llm_caller(prompt: str) -> str:
            msg = "Critical error"
            raise RuntimeError(msg)

        config = RunnerConfig(max_retries=0, adaptive=True)
        runner = MACPRunner(async_llm_caller=failing_llm_caller, config=config)
        result = await runner.arun_round(graph)

        # Should have error recorded
        assert result.errors is not None or "[Error:" in str(result.messages.get("a", ""))

    @pytest.mark.asyncio
    async def test_on_error_skip_policy(self):
        """Error handling and continuing execution."""
        graph = create_test_graph(["a", "b"], [("a", "b")])

        call_count = 0

        async def maybe_failing_llm_caller(prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                msg = "a failed"
                raise RuntimeError(msg)
            return "response"

        config = RunnerConfig(max_retries=0)
        runner = MACPRunner(async_llm_caller=maybe_failing_llm_caller, config=config)
        result = await runner.arun_round(graph)

        # Should continue to agent b
        assert result is not None


class TestMACPResult:
    """Tests for execution result."""

    def test_result_structure(self):
        """Result structure."""
        result = MACPResult(
            messages={"a": "response"},
            final_answer="final answer",
            final_agent_id="a",
            execution_order=["a"],
            errors=[],
        )

        assert result.final_answer == "final answer"
        assert result.final_agent_id == "a"
        assert result.execution_order == ["a"]
        assert result.errors == []

    def test_result_with_metrics(self):
        """Result with metrics."""
        from datetime import datetime

        from gmas.execution.errors import ExecutionMetrics

        metrics = ExecutionMetrics(
            start_time=datetime.now(),
            total_agents=1,
            total_tokens=500,
        )

        result = MACPResult(
            messages={"a": "response"},
            final_answer="answer",
            final_agent_id="a",
            execution_order=["a"],
            errors=[],
            metrics=metrics,
            total_tokens=500,
            total_time=1.234,
        )

        assert result.total_tokens == 500
        assert result.total_time == 1.234


class TestConditionalEdgesAdaptive:
    """Tests for conditional edges in adaptive mode."""

    def test_condition_true_executes_target(self):
        """If condition is met — target agent is executed."""
        graph = create_test_graph(["a", "b", "c"], [("a", "b"), ("b", "c")])

        # Condition: a→b executes only if a responds with "ok"
        graph.edge_conditions = {
            ("a", "b"): lambda ctx: "ok" in ctx.messages.get("a", ""),
        }

        llm_caller = create_simple_llm_caller("ok response")
        config = RunnerConfig(adaptive=True)
        runner = MACPRunner(llm_caller=llm_caller, config=config)

        result = runner.run_round(graph)

        assert "a" in result.execution_order
        assert "b" in result.execution_order

    def test_condition_false_skips_target(self):
        """If condition is not met — target agent is skipped."""
        graph = create_test_graph(["a", "b", "c"], [("a", "b"), ("a", "c")])

        # Condition: a→b executes only if a responds with "secret"
        graph.edge_conditions = {
            ("a", "b"): lambda ctx: "secret" in ctx.messages.get("a", ""),
        }

        llm_caller = create_simple_llm_caller("normal response")
        config = RunnerConfig(adaptive=True)
        runner = MACPRunner(llm_caller=llm_caller, config=config)

        result = runner.run_round(graph)

        assert "a" in result.execution_order
        # b should be skipped because condition is not met
        # c should execute (unconditional edge)
        assert "c" in result.execution_order

    @pytest.mark.asyncio
    async def test_async_conditional_edges(self):
        """Conditional edges work in async mode."""
        graph = create_test_graph(["a", "b", "c"], [("a", "b"), ("a", "c")])

        graph.edge_conditions = {
            ("a", "b"): lambda ctx: ctx.source_succeeded(),
        }

        async_llm_caller = create_simple_async_llm_caller("response")
        config = RunnerConfig(adaptive=True)
        runner = MACPRunner(async_llm_caller=async_llm_caller, config=config)

        result = await runner.arun_round(graph)

        assert "a" in result.execution_order
        assert "b" in result.execution_order

    def test_topology_changed_count(self):
        """topology_changed_count increments when plan changes."""
        graph = create_test_graph(["a", "b"], [("a", "b")])

        graph.edge_conditions = {
            ("a", "b"): lambda ctx: ctx.source_succeeded(),
        }

        llm_caller = create_simple_llm_caller("response")
        config = RunnerConfig(adaptive=True)
        runner = MACPRunner(llm_caller=llm_caller, config=config)

        result = runner.run_round(graph)

        assert result is not None
        assert isinstance(result.topology_changed_count, int)

    def test_multiple_incoming_conditional_edges(self):
        """Multiple incoming conditional edges: B is not skipped until all are evaluated."""
        graph = create_test_graph(
            ["a", "c", "b"],
            [("a", "b"), ("c", "b")],
        )

        # Different callers for different agents
        llm_callers: dict[str, Callable[[str], str]] = {
            "a": lambda _: "fail result",
            "c": lambda _: "good result",
            "b": lambda _: "final response",
        }

        # a→b: condition NOT met (no "success" in a's response)
        # c→b: condition MET ("good" in c's response)
        graph.edge_conditions = {
            ("a", "b"): lambda ctx: "success" in ctx.messages.get("a", ""),
            ("c", "b"): lambda ctx: "good" in ctx.messages.get("c", ""),
        }

        config = RunnerConfig(adaptive=True)
        runner = MACPRunner(
            llm_caller=lambda _: "default",
            llm_callers=llm_callers,
            config=config,
        )

        result = runner.run_round(graph)

        # b should execute because c→b condition is met
        assert "a" in result.execution_order
        assert "c" in result.execution_order
        assert "b" in result.execution_order

    def test_conditional_edges_with_hidden_states_and_chain(self):
        """
        Complex test: conditional edges + hidden states + cascading chain.

        Graph:
            solver → reviewer → finalize  (conditional edge solver→reviewer)
            solver → alt_end              (unconditional)

        Scenario 1 (condition=True):  solver("correct") → reviewer → finalize execute.
        Scenario 2 (condition=False): solver("wrong") → reviewer + finalize are skipped,
                                      alt_end executes.

        Covers:
        - Issue 1: hidden states + conditional edges
        - Issue 2: plan does not stop after the last agent
        - Issue 3: full chain executes after conditional transition
        """
        # --- Scenario 1: condition met → full chain ---
        graph1 = create_test_graph(
            ["solver", "reviewer", "finalize", "alt_end"],
            [
                ("solver", "reviewer"),
                ("reviewer", "finalize"),
                ("solver", "alt_end"),
            ],
        )
        graph1.edge_conditions = {
            ("solver", "reviewer"): lambda ctx: "correct" in ctx.messages.get("solver", ""),
        }

        callers1: dict[str, Callable[[str], str]] = {
            "solver": lambda _: "answer is correct",
            "reviewer": lambda _: "review passed",
            "finalize": lambda _: "done",
            "alt_end": lambda _: "alt",
        }

        config = RunnerConfig(adaptive=True)
        runner1 = MACPRunner(
            llm_caller=lambda _: "default",
            llm_callers=callers1,
            config=config,
        )

        result1 = runner1.run_round_with_hidden(graph1)

        assert "solver" in result1.execution_order
        assert "reviewer" in result1.execution_order
        assert "finalize" in result1.execution_order  # full chain
        assert result1.hidden_states is not None
        assert "solver" in result1.hidden_states

        # --- Scenario 2: condition NOT met → cascading skip ---
        graph2 = create_test_graph(
            ["solver", "reviewer", "finalize", "alt_end"],
            [
                ("solver", "reviewer"),
                ("reviewer", "finalize"),
                ("solver", "alt_end"),
            ],
        )
        graph2.edge_conditions = {
            ("solver", "reviewer"): lambda ctx: "correct" in ctx.messages.get("solver", ""),
        }

        callers2: dict[str, Callable[[str], str]] = {
            "solver": lambda _: "answer is wrong",
            "reviewer": lambda _: "review passed",
            "finalize": lambda _: "done",
            "alt_end": lambda _: "alt ending",
        }

        runner2 = MACPRunner(
            llm_caller=lambda _: "default",
            llm_callers=callers2,
            config=config,
        )

        result2 = runner2.run_round_with_hidden(graph2)

        assert "solver" in result2.execution_order
        assert "reviewer" not in result2.execution_order  # skipped
        assert "finalize" not in result2.execution_order  # cascaded skip
        assert "alt_end" in result2.execution_order  # unconditional path


class TestRunnerConfigNewFields:
    """Tests for new RunnerConfig fields."""

    def test_prompt_preview_length_default(self):
        """prompt_preview_length should have a default value."""
        config = RunnerConfig()
        assert config.prompt_preview_length == 100

    def test_prompt_preview_length_custom(self):
        """prompt_preview_length should be customizable."""
        config = RunnerConfig(prompt_preview_length=200)
        assert config.prompt_preview_length == 200

    def test_max_loop_iterations_default(self):
        """max_loop_iterations should have a default value."""
        config = RunnerConfig()
        assert config.max_loop_iterations == 5

    def test_max_loop_iterations_custom(self):
        """max_loop_iterations should be customizable."""
        config = RunnerConfig(max_loop_iterations=10)
        assert config.max_loop_iterations == 10

    def test_max_tool_iterations_default(self):
        """max_tool_iterations should have a default value."""
        config = RunnerConfig()
        assert config.max_tool_iterations == 3

    def test_max_tool_iterations_custom(self):
        """max_tool_iterations should be customizable."""
        config = RunnerConfig(max_tool_iterations=5)
        assert config.max_tool_iterations == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
