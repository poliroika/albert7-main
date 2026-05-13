"""Integration tests verifying that all newly-wired callbacks fire correctly."""

from uuid import UUID

import pytest

from gmas.builder.graph_builder import build_property_graph
from gmas.callbacks.base import BaseCallbackHandler
from gmas.core.agent import AgentProfile
from gmas.execution.budget import BudgetConfig
from gmas.execution.runner import MACPRunner, RunnerConfig

# ────────────────────────── Recording handler ──────────────────────────────────


class RecordingHandler(BaseCallbackHandler):
    """Records every callback invocation for assertion."""

    def __init__(self):
        self.raise_error = False
        self.calls: list[tuple[str, dict]] = []

    def _record(self, method: str, **kwargs):
        self.calls.append((method, kwargs))

    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def calls_for(self, method: str) -> list[dict]:
        return [kw for name, kw in self.calls if name == method]

    # ── Core lifecycle ──
    def on_run_start(self, *, run_id, query, **kw):
        self._record("on_run_start", run_id=run_id, query=query)

    def on_run_end(self, *, run_id, output, **kw):
        self._record("on_run_end", run_id=run_id, output=output)

    def on_agent_start(self, *, run_id, agent_id, **kw):
        self._record("on_agent_start", run_id=run_id, agent_id=agent_id)

    def on_agent_end(self, *, run_id, agent_id, output, **kw):
        self._record("on_agent_end", run_id=run_id, agent_id=agent_id, output=output)

    def on_agent_error(self, error, *, run_id, agent_id, **kw):
        self._record("on_agent_error", run_id=run_id, agent_id=agent_id)

    # ── Tool callbacks ──
    def on_tool_start(self, *, run_id, tool_name, **kw):
        self._record("on_tool_start", run_id=run_id, tool_name=tool_name)

    def on_tool_end(self, *, run_id, tool_name, **kw):
        self._record("on_tool_end", run_id=run_id, tool_name=tool_name)

    def on_tool_error(self, *, run_id, tool_name, **kw):
        self._record("on_tool_error", run_id=run_id, tool_name=tool_name)

    # ── Retry ──
    def on_retry(self, *, run_id, agent_id, attempt, **kw):
        self._record("on_retry", run_id=run_id, agent_id=agent_id, attempt=attempt)

    # ── Memory ──
    def on_memory_read(self, *, run_id, agent_id, **kw):
        self._record("on_memory_read", run_id=run_id, agent_id=agent_id)

    def on_memory_write(self, *, run_id, agent_id, key, **kw):
        self._record("on_memory_write", run_id=run_id, agent_id=agent_id, key=key)

    # ── Budget ──
    def on_budget_warning(self, *, run_id, budget_type, **kw):
        self._record("on_budget_warning", run_id=run_id, budget_type=budget_type)

    def on_budget_exceeded(self, *, run_id, budget_type, **kw):
        self._record("on_budget_exceeded", run_id=run_id, budget_type=budget_type)

    # ── Plan / topology ──
    def on_plan_created(self, *, run_id, num_steps, execution_order, **kw):
        self._record("on_plan_created", run_id=run_id, num_steps=num_steps, execution_order=execution_order)

    def on_topology_changed(self, *, run_id, reason, **kw):
        self._record("on_topology_changed", run_id=run_id, reason=reason)

    # ── Prune / fallback ──
    def on_prune(self, *, run_id, agent_id, reason, **kw):
        self._record("on_prune", run_id=run_id, agent_id=agent_id, reason=reason)

    def on_fallback(self, *, run_id, failed_agent_id, fallback_agent_id, **kw):
        self._record("on_fallback", run_id=run_id, failed_agent_id=failed_agent_id, fallback_agent_id=fallback_agent_id)

    # ── Parallel ──
    def on_parallel_start(self, *, run_id, agent_ids, **kw):
        self._record("on_parallel_start", run_id=run_id, agent_ids=agent_ids)

    def on_parallel_end(self, *, run_id, agent_ids, **kw):
        self._record("on_parallel_end", run_id=run_id, agent_ids=agent_ids)

    def on_llm_new_token(self, token, *, run_id, agent_id, **kw):
        pass


# ────────────────────────── Helpers ────────────────────────────────────────────


def _make_graph(n_agents: int = 2, query: str = "Test query", chain: bool = True):
    agents = [AgentProfile(agent_id=f"a{i}", display_name=f"Agent {i}") for i in range(n_agents)]
    edges = [(f"a{i}", f"a{i + 1}") for i in range(n_agents - 1)] if chain else []
    return build_property_graph(
        agents=agents,
        workflow_edges=edges,
        query=query,
        include_task_node=True,
    )


def _simple_caller(prompt) -> str:
    return "Mock response"


async def _async_caller(prompt) -> str:
    return "Async mock response"


# ════════════════════════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════════════════════════


class TestMemoryCallbacks:
    """on_memory_read / on_memory_write fire when memory is enabled."""

    def test_memory_callbacks_fire_on_sync_run(self):
        handler = RecordingHandler()
        graph = _make_graph(2)
        config = RunnerConfig(enable_memory=True, callbacks=[handler])
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        result = runner.run_round(graph)

        assert result.execution_order
        writes = handler.calls_for("on_memory_write")
        assert len(writes) >= 1
        assert writes[0]["key"] == "assistant"

    @pytest.mark.asyncio
    async def test_memory_callbacks_fire_on_async_run(self):
        handler = RecordingHandler()
        graph = _make_graph(2)
        config = RunnerConfig(enable_memory=True, callbacks=[handler])
        runner = MACPRunner(async_llm_caller=_async_caller, config=config)
        result = await runner.arun_round(graph)

        assert result.execution_order
        writes = handler.calls_for("on_memory_write")
        assert len(writes) >= 1


class TestBudgetRecordUsage:
    """Budget record_usage is called after agent steps."""

    def test_budget_tracker_records_usage_on_sync(self):
        handler = RecordingHandler()
        budget_cfg = BudgetConfig(total_token_limit=1_000_000)
        config = RunnerConfig(callbacks=[handler], budget_config=budget_cfg)
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        result = runner.run_round(graph)

        assert result.execution_order
        assert runner._budget_tracker is not None
        assert runner._budget_tracker.global_tokens.used >= 0


class TestPruneCallbacksSimple:
    """on_prune fires for disabled nodes in simple run."""

    def test_disabled_node_triggers_prune_callback(self):
        handler = RecordingHandler()
        config = RunnerConfig(callbacks=[handler])
        graph = _make_graph(3)
        graph.disabled_nodes = {"a1"}
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        runner.run_round(graph)

        prune_calls = handler.calls_for("on_prune")
        assert len(prune_calls) >= 1
        pruned_ids = [c["agent_id"] for c in prune_calls]
        assert "a1" in pruned_ids
        assert prune_calls[0]["reason"] == "disabled_node"


class TestPlanCreatedCallback:
    """on_plan_created fires in adaptive mode."""

    def test_plan_created_fires_on_adaptive_sync(self):
        handler = RecordingHandler()
        config = RunnerConfig(adaptive=True, callbacks=[handler])
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        runner.run_round(graph)

        plan_calls = handler.calls_for("on_plan_created")
        assert len(plan_calls) == 1
        assert plan_calls[0]["num_steps"] >= 1
        assert isinstance(plan_calls[0]["execution_order"], list)

    @pytest.mark.asyncio
    async def test_plan_created_fires_on_adaptive_async(self):
        handler = RecordingHandler()
        config = RunnerConfig(adaptive=True, callbacks=[handler])
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_caller=_async_caller, config=config)
        await runner.arun_round(graph)

        plan_calls = handler.calls_for("on_plan_created")
        assert len(plan_calls) == 1


class TestPruneCallbacksAdaptive:
    """on_prune fires for disabled nodes in adaptive mode."""

    def test_disabled_node_triggers_prune_in_adaptive(self):
        handler = RecordingHandler()
        config = RunnerConfig(adaptive=True, callbacks=[handler])
        graph = _make_graph(3)
        graph.disabled_nodes = {"a1"}
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        runner.run_round(graph)

        prune_calls = handler.calls_for("on_prune")
        pruned_ids = [c["agent_id"] for c in prune_calls]
        assert "a1" in pruned_ids


class TestRetryCallback:
    """on_retry fires when agent execution fails and retries (_execute_step is used in adaptive mode)."""

    def test_retry_callback_fires_in_adaptive(self):
        call_count = 0

        def _failing_caller(prompt):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                msg = "Simulated failure"
                raise RuntimeError(msg)
            return "Recovery response"

        handler = RecordingHandler()
        config = RunnerConfig(
            adaptive=True,
            max_retries=2,
            retry_delay=0.01,
            callbacks=[handler],
        )
        graph = _make_graph(1)
        runner = MACPRunner(llm_caller=_failing_caller, config=config)
        runner.run_round(graph)

        retry_calls = handler.calls_for("on_retry")
        assert len(retry_calls) >= 1
        assert retry_calls[0]["attempt"] == 1


class TestCallbackRunIdConsistency:
    """All callbacks within a single run share the same run_id."""

    def test_run_id_consistent_across_callbacks(self):
        handler = RecordingHandler()
        config = RunnerConfig(enable_memory=True, callbacks=[handler])
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        runner.run_round(graph)

        run_ids = set()
        for _name, kw in handler.calls:
            if "run_id" in kw:
                run_ids.add(kw["run_id"])

        assert len(run_ids) == 1, f"Expected 1 unique run_id, got {len(run_ids)}"
        run_id = run_ids.pop()
        assert isinstance(run_id, UUID)


class TestCurrentRunIdStored:
    """_current_run_id is set on the runner during execution."""

    def test_current_run_id_set(self):
        handler = RecordingHandler()
        config = RunnerConfig(callbacks=[handler])
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        runner.run_round(graph)

        assert runner._current_run_id is not None
        assert isinstance(runner._current_run_id, UUID)
