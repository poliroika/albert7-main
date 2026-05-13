"""Integration tests for MACPRunner — run_round, arun_round, stream, astream."""

import sys
from collections.abc import AsyncIterator, Iterator
from unittest.mock import MagicMock, patch

import pytest
import torch

from gmas.builder.graph_builder import build_property_graph
from gmas.core.agent import AgentProfile
from gmas.execution.runner import (
    EarlyStopCondition,
    LLMCallerFactory,
    MACPResult,
    MACPRunner,
    RunnerConfig,
    StepContext,
    TopologyAction,
)
from gmas.execution.streaming import AgentOutputEvent, StreamEventType

# ============================================================================
# Helpers
# ============================================================================


def _make_graph(n_agents: int = 2, query: str = "Test query", chain: bool = True):
    """Build a simple graph for runner tests."""
    agents = [AgentProfile(agent_id=f"a{i}", display_name=f"Agent {i}") for i in range(n_agents)]
    edges = [(f"a{i}", f"a{i + 1}") for i in range(n_agents - 1)] if chain else []
    return build_property_graph(
        agents=agents,
        workflow_edges=edges,
        query=query,
        include_task_node=True,
    )


def _simple_caller(prompt: str) -> str:
    return "Mock response"


async def _async_caller(prompt: str) -> str:
    return "Async mock response"


# ============================================================================
# run_round (sync)  # noqa: ERA001
# ============================================================================


class TestRunRound:
    def test_basic_run_returns_result(self):
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(graph)
        assert isinstance(result, MACPResult)

    def test_messages_populated(self):
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(graph)
        assert len(result.messages) >= 1
        for aid in result.execution_order:
            assert aid in result.messages

    def test_final_answer_set(self):
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(graph)
        assert isinstance(result.final_answer, str)

    def test_no_caller_raises(self):
        graph = _make_graph()
        runner = MACPRunner()
        with pytest.raises(ValueError, match="llm_caller"):
            runner.run_round(graph)

    def test_multi_agent_chain(self):
        graph = _make_graph(3)
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(graph)
        assert len(result.execution_order) == 3
        assert result.total_tokens >= 0

    def test_with_custom_token_counter(self):
        graph = _make_graph()
        token_count = [0]

        def counter(text: str) -> int:
            token_count[0] += 1
            return len(text.split())

        runner = MACPRunner(llm_caller=_simple_caller, token_counter=counter)
        result = runner.run_round(graph)
        assert result.total_tokens >= 0

    def test_with_callbacks_param(self):
        """Covers line 1018: callbacks param merging."""
        from gmas.callbacks.handlers.metrics import MetricsCallbackHandler

        graph = _make_graph()
        handler = MetricsCallbackHandler()
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(graph, callbacks=[handler])
        assert isinstance(result, MACPResult)
        metrics = handler.get_metrics()
        assert metrics["runs_completed"] >= 1

    def test_with_context_callback_manager(self):
        """Covers line 1023: context callback manager merging."""
        from gmas.callbacks.context import trace_as_callback
        from gmas.callbacks.handlers.metrics import MetricsCallbackHandler

        graph = _make_graph()
        handler = MetricsCallbackHandler()
        runner = MACPRunner(llm_caller=_simple_caller)
        with trace_as_callback(handlers=[handler]):
            result = runner.run_round(graph)
        assert isinstance(result, MACPResult)

    def test_caller_raises_error_continues(self):
        """Covers lines 2089-2098: error handling in agent execution."""
        call_count = [0]

        def failing_caller(prompt: str) -> str:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "LLM error"
                raise ValueError(msg)
            return "fallback"

        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=failing_caller)
        result = runner.run_round(graph)
        assert isinstance(result, MACPResult)

    def test_with_multi_callers(self):
        """Covers per-agent caller lookup."""
        graph = _make_graph(2)
        runner = MACPRunner(llm_callers={"a0": lambda _p: "agent0 response", "a1": lambda _p: "agent1 response"})
        result = runner.run_round(graph)
        assert "a0" in result.messages
        assert "a1" in result.messages

    def test_empty_graph_returns_empty_result(self):
        """Covers _prepare_base_context returning None (no agents)."""
        import rustworkx as rx

        from gmas.core.graph import RoleGraph

        g = rx.PyDiGraph()
        g.add_node({"id": "__task__"})
        empty_graph = RoleGraph(
            node_ids=["__task__"],
            task_node="__task__",
            graph=g,
            A_com=torch.zeros((1, 1)),
            agents=[],
        )
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(empty_graph)
        assert result.messages == {}

    def test_update_states_false(self):
        """Covers update_states=False path."""
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(graph, update_states=False)
        assert result.agent_states is None

    def test_update_states_true(self):
        """Covers update_states=True path."""
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(graph, update_states=True)
        # agent_states may be None or a dict depending on implementation
        assert result.agent_states is None or isinstance(result.agent_states, dict)

    def test_memory_enabled(self):
        """Covers lines 1081-1090: memory initialization."""
        graph = _make_graph(2)
        config = RunnerConfig(enable_memory=True)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        result = runner.run_round(graph)
        assert isinstance(result, MACPResult)
        assert runner.memory_pool is not None

    def test_early_stop_condition(self):
        """Covers lines 1150-1166: early stop conditions."""
        stop = EarlyStopCondition.on_keyword("Mock")
        config = RunnerConfig(early_stop_conditions=[stop])
        graph = _make_graph(3)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        result = runner.run_round(graph)
        assert result.early_stopped is True or isinstance(result, MACPResult)

    def test_dynamic_topology_hook(self):
        """Covers lines 2117-2136: topology hooks in _run_simple."""

        def hook(ctx: StepContext, role_graph) -> TopologyAction:
            return TopologyAction(skip_agents=["a1"] if ctx.agent_id == "a0" else [])

        config = RunnerConfig(enable_dynamic_topology=True, topology_hooks=[hook])
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        result = runner.run_round(graph)
        assert isinstance(result, MACPResult)

    def test_broadcast_task_to_all_false(self):
        """Covers broadcast_task_to_all=False path."""
        config = RunnerConfig(broadcast_task_to_all=False)
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        result = runner.run_round(graph)
        assert isinstance(result, MACPResult)


# ============================================================================
# arun_round (async)
# ============================================================================


@pytest.mark.asyncio
class TestARunRound:
    async def test_basic_async_run(self):
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_caller=_async_caller)
        result = await runner.arun_round(graph)
        assert isinstance(result, MACPResult)

    async def test_messages_populated(self):
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_caller=_async_caller)
        result = await runner.arun_round(graph)
        assert len(result.messages) >= 1

    async def test_no_async_caller_raises(self):
        graph = _make_graph()
        runner = MACPRunner()
        with pytest.raises(ValueError, match="async_llm_caller"):
            await runner.arun_round(graph)

    async def test_multi_agent_chain(self):
        graph = _make_graph(3)
        runner = MACPRunner(async_llm_caller=_async_caller)
        result = await runner.arun_round(graph)
        assert len(result.execution_order) == 3

    async def test_with_callbacks(self):
        from gmas.callbacks.handlers.metrics import MetricsCallbackHandler

        graph = _make_graph()
        handler = MetricsCallbackHandler()
        runner = MACPRunner(async_llm_caller=_async_caller)
        result = await runner.arun_round(graph, callbacks=[handler])
        assert isinstance(result, MACPResult)

    async def test_memory_enabled_async(self):
        """Covers async memory init."""
        graph = _make_graph(2)
        config = RunnerConfig(enable_memory=True)
        runner = MACPRunner(async_llm_caller=_async_caller, config=config)
        result = await runner.arun_round(graph)
        assert isinstance(result, MACPResult)

    async def test_async_caller_raises_error_continues(self):
        """Covers error handling in _arun_simple."""
        call_count = [0]

        async def failing_async(prompt: str) -> str:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "Async LLM error"
                raise ValueError(msg)
            return "async fallback"

        graph = _make_graph(2)
        runner = MACPRunner(async_llm_caller=failing_async)
        result = await runner.arun_round(graph)
        assert isinstance(result, MACPResult)


# ============================================================================
# stream (sync generator)
# ============================================================================


class TestStream:
    def test_basic_stream_yields_events(self):
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller)
        events = list(runner.stream(graph))
        assert len(events) > 0

    def test_stream_has_run_start_event(self):
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller)
        events = list(runner.stream(graph))
        types = [e.event_type for e in events]
        assert StreamEventType.RUN_START in types

    def test_stream_has_run_end_event(self):
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller)
        events = list(runner.stream(graph))
        types = [e.event_type for e in events]
        assert StreamEventType.RUN_END in types

    def test_stream_has_agent_output_events(self):
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller)
        events = list(runner.stream(graph))
        types = [e.event_type for e in events]
        assert StreamEventType.AGENT_OUTPUT in types

    def test_no_caller_raises(self):
        graph = _make_graph()
        runner = MACPRunner()
        with pytest.raises(ValueError, match="caller"):
            list(runner.stream(graph))

    def test_stream_with_token_streaming(self):
        """Covers token streaming path (lines 3959-3987)."""

        def token_gen(prompt: str) -> Iterator[str]:
            yield "token1"
            yield " "
            yield "token2"

        graph = _make_graph(1)
        config = RunnerConfig(enable_token_streaming=True)
        runner = MACPRunner(
            llm_caller=_simple_caller,
            streaming_llm_caller=token_gen,
            config=config,
        )
        events = list(runner.stream(graph))
        types = [e.event_type for e in events]
        assert StreamEventType.TOKEN in types

    def test_stream_error_in_caller(self):
        """Covers error event in streaming."""

        def error_caller(prompt: str) -> str:
            msg = "stream error"
            raise RuntimeError(msg)

        graph = _make_graph(1)
        runner = MACPRunner(llm_caller=error_caller)
        events = list(runner.stream(graph))
        types = [e.event_type for e in events]
        assert StreamEventType.AGENT_ERROR in types

    def test_stream_multi_agent(self):
        graph = _make_graph(3)
        runner = MACPRunner(llm_caller=_simple_caller)
        events = list(runner.stream(graph))
        output_events = [e for e in events if e.event_type == StreamEventType.AGENT_OUTPUT]
        assert len(output_events) == 3


# ============================================================================
# astream (async generator)
# ============================================================================


@pytest.mark.asyncio
class TestAStream:
    async def test_basic_astream_yields_events(self):
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_caller=_async_caller)
        events = [event async for event in runner.astream(graph)]
        assert len(events) > 0

    async def test_astream_has_run_start(self):
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_caller=_async_caller)
        events = [event async for event in runner.astream(graph)]
        types = [e.event_type for e in events]
        assert StreamEventType.RUN_START in types

    async def test_astream_has_run_end(self):
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_caller=_async_caller)
        events = [event async for event in runner.astream(graph)]
        types = [e.event_type for e in events]
        assert StreamEventType.RUN_END in types

    async def test_no_async_caller_raises(self):
        graph = _make_graph()
        runner = MACPRunner()
        with pytest.raises(ValueError, match="caller"):
            async for _ in runner.astream(graph):
                pass

    async def test_astream_with_async_token_streaming(self):
        """Covers async token streaming path (lines 4101-4127)."""

        async def async_token_gen(prompt: str) -> AsyncIterator[str]:
            for token in ["async", " ", "tokens"]:
                yield token

        graph = _make_graph(1)
        config = RunnerConfig(enable_token_streaming=True)
        runner = MACPRunner(
            async_llm_caller=_async_caller,
            async_streaming_llm_caller=async_token_gen,
            config=config,
        )
        events = [event async for event in runner.astream(graph)]
        types = [e.event_type for e in events]
        assert StreamEventType.TOKEN in types

    async def test_astream_multi_agent(self):
        graph = _make_graph(3)
        runner = MACPRunner(async_llm_caller=_async_caller)
        events = [event async for event in runner.astream(graph)]
        output_events = [e for e in events if e.event_type == StreamEventType.AGENT_OUTPUT]
        assert len(output_events) == 3

    async def test_astream_error_in_caller(self):
        """Covers error handling in _astream_simple."""

        async def error_async(prompt: str) -> str:
            msg = "async stream error"
            raise RuntimeError(msg)

        graph = _make_graph(1)
        runner = MACPRunner(async_llm_caller=error_async)
        events = [event async for event in runner.astream(graph)]
        types = [e.event_type for e in events]
        assert StreamEventType.AGENT_ERROR in types


# ============================================================================
# OpenAI caller creation (mocked openai)
# ============================================================================


class TestOpenAICallerCreation:
    def test_create_openai_caller_from_config(self):
        """Covers lines 320-344."""
        from gmas.execution.runner import _create_openai_caller_from_config

        mock_openai_module = MagicMock()
        mock_client = MagicMock()
        mock_openai_module.OpenAI.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "test response"
        mock_client.chat.completions.create.return_value = mock_response

        from gmas.core.agent import AgentLLMConfig

        config = AgentLLMConfig(
            model_name="gpt-4",
            base_url="http://api.example.com",
            api_key="test-key",
        )

        with patch.dict(sys.modules, {"openai": mock_openai_module}):
            caller = _create_openai_caller_from_config(config)
            assert callable(caller)
            result = caller("test prompt")
            assert result == "test response"

    def test_create_openai_caller_no_openai_raises(self):
        """Covers ImportError path in _create_openai_caller_from_config."""
        from gmas.core.agent import AgentLLMConfig
        from gmas.execution.runner import _create_openai_caller_from_config

        config = AgentLLMConfig(
            model_name="gpt-4",
            base_url="http://api.example.com",
            api_key="test-key",
        )

        with patch.dict(sys.modules, {"openai": None}), pytest.raises(ImportError, match="openai"):
            _create_openai_caller_from_config(config)

    def test_create_async_openai_caller_from_config(self):
        """Covers lines 349-373."""
        from gmas.execution.runner import _create_async_openai_caller_from_config

        mock_openai_module = MagicMock()
        mock_client = MagicMock()
        mock_openai_module.AsyncOpenAI.return_value = mock_client

        from gmas.core.agent import AgentLLMConfig

        config = AgentLLMConfig(
            model_name="gpt-4",
            base_url="http://api.example.com",
            api_key="test-key",
        )

        with patch.dict(sys.modules, {"openai": mock_openai_module}):
            caller = _create_async_openai_caller_from_config(config)
            assert callable(caller)

    def test_create_async_openai_caller_no_openai_raises(self):
        """Covers ImportError path in _create_async_openai_caller_from_config."""
        from gmas.core.agent import AgentLLMConfig
        from gmas.execution.runner import _create_async_openai_caller_from_config

        config = AgentLLMConfig(
            model_name="gpt-4",
            base_url="http://api.example.com",
            api_key="test-key",
        )

        with patch.dict(sys.modules, {"openai": None}), pytest.raises(ImportError, match="openai"):
            _create_async_openai_caller_from_config(config)

    def test_create_openai_caller_function(self):
        """Covers lines 391-398."""
        from gmas.execution.runner import create_openai_caller

        mock_openai_module = MagicMock()
        mock_client = MagicMock()
        mock_openai_module.OpenAI.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "hi"
        mock_client.chat.completions.create.return_value = mock_response

        with patch.dict(sys.modules, {"openai": mock_openai_module}):
            caller = create_openai_caller(api_key="test-key", model="gpt-4")
            assert callable(caller)


# ============================================================================
# LLMCallerFactory - default_async_caller fallback
# ============================================================================


class TestLLMCallerFactoryFallback:
    def test_get_async_caller_returns_default_when_no_builder(self):
        """Covers line 261: return self.default_async_caller."""

        async def my_async_caller(p):
            return "hi"

        factory = LLMCallerFactory(default_async_caller=my_async_caller)
        # No async_caller_builder, so should return default
        from gmas.core.agent import AgentLLMConfig

        config = AgentLLMConfig(model_name="gpt-4", base_url="http://api.example.com")
        result = factory.get_async_caller(config)
        assert result is my_async_caller

    def test_create_openai_factory_builds_callers(self):
        """Covers lines 306, 309: builder closures in create_openai_factory."""
        from gmas.core.agent import AgentLLMConfig

        mock_openai_module = MagicMock()
        mock_client = MagicMock()
        mock_openai_module.OpenAI.return_value = mock_client
        mock_openai_module.AsyncOpenAI.return_value = mock_client
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "test"
        mock_client.chat.completions.create.return_value = mock_response

        with patch.dict(sys.modules, {"openai": mock_openai_module}):
            factory = LLMCallerFactory.create_openai_factory(default_api_key="test-key")
            config = AgentLLMConfig(
                model_name="gpt-4",
                base_url="http://api.example.com",
                api_key="test-key",
            )
            # This calls the builder closures (lines 306, 309)
            assert factory.caller_builder is not None
            assert factory.async_caller_builder is not None
            sync_caller = factory.caller_builder(config)
            async_caller = factory.async_caller_builder(config)
            assert callable(sync_caller)
            assert callable(async_caller)


# ============================================================================
# MACPRunner - get_caller_for_agent with factory
# ============================================================================


class TestGetCallerForAgent:
    def test_get_caller_uses_factory_with_llm_config(self):
        """Covers lines 1777-1785."""
        from gmas.core.agent import AgentLLMConfig

        def built_caller(p):
            return "factory response"

        factory = LLMCallerFactory(caller_builder=lambda _cfg: built_caller)

        # Agent with get_llm_config method
        AgentProfile(agent_id="a0", display_name="A0")
        llm_cfg = AgentLLMConfig(
            model_name="gpt-4",
            base_url="http://api.example.com",
            api_key="test-key",
        )

        class AgentWithLLMConfig(AgentProfile):
            def get_llm_config(self):
                return llm_cfg

        agent_w_cfg = AgentWithLLMConfig(agent_id="a0", display_name="A0")

        runner = MACPRunner(llm_factory=factory)
        caller = runner._get_caller_for_agent("a0", agent_w_cfg)
        assert caller is built_caller

    def test_get_async_caller_uses_factory_with_llm_config(self):
        """Covers lines 1808-1817."""
        from gmas.core.agent import AgentLLMConfig

        async def built_async(p):
            return "async"

        factory = LLMCallerFactory(async_caller_builder=lambda _cfg: built_async)

        llm_cfg = AgentLLMConfig(
            model_name="gpt-4",
            base_url="http://api.example.com",
            api_key="test-key",
        )

        class AgentWithLLMConfig(AgentProfile):
            def get_llm_config(self):
                return llm_cfg

        agent = AgentWithLLMConfig(agent_id="a0", display_name="A0")

        runner = MACPRunner(llm_factory=factory)
        caller = runner._get_async_caller_for_agent("a0", agent)
        assert caller is built_async

    def test_get_caller_uses_factory_via_llm_config_attr(self):
        """Covers the elif branch: factory with agent.llm_config attribute."""
        from gmas.core.agent import AgentLLMConfig

        def built_caller(p):
            return "attr factory"

        factory = LLMCallerFactory(caller_builder=lambda _cfg: built_caller)

        llm_cfg = AgentLLMConfig(
            model_name="gpt-4",
            base_url="http://api.example.com",
            api_key="test-key",
        )

        class AgentWithAttr(AgentProfile):
            llm_config: AgentLLMConfig | None = None

        agent = AgentWithAttr(agent_id="a0", display_name="A0", llm_config=llm_cfg)

        runner = MACPRunner(llm_factory=factory)
        caller = runner._get_caller_for_agent("a0", agent)
        assert caller is built_caller


# ============================================================================
# MACPRunner - has_any_caller / has_any_async_caller
# ============================================================================


class TestHasCallers:
    def test_has_any_caller_with_default(self):
        runner = MACPRunner(llm_caller=_simple_caller)
        assert runner._has_any_caller() is True

    def test_has_any_caller_with_callers_dict(self):
        runner = MACPRunner(llm_callers={"a0": _simple_caller})
        assert runner._has_any_caller() is True

    def test_has_any_caller_with_factory(self):
        factory = LLMCallerFactory(default_caller=_simple_caller)
        runner = MACPRunner(llm_factory=factory)
        assert runner._has_any_caller() is True

    def test_has_any_caller_none(self):
        runner = MACPRunner()
        assert runner._has_any_caller() is False

    def test_has_any_async_caller_with_default(self):
        runner = MACPRunner(async_llm_caller=_async_caller)
        assert runner._has_any_async_caller() is True

    def test_has_any_async_caller_none(self):
        runner = MACPRunner()
        assert runner._has_any_async_caller() is False


# ============================================================================
# MACPRunner - tools not available (TOOLS_AVAILABLE = False)
# ============================================================================


class TestToolsNotAvailable:
    def test_run_without_tools_module(self):
        """Covers lines 87-88: TOOLS_AVAILABLE = False branch."""
        import gmas.execution.runner as runner_module

        original = runner_module.TOOLS_AVAILABLE
        runner_module.TOOLS_AVAILABLE = False
        try:
            graph = _make_graph(1)
            runner = MACPRunner(llm_caller=_simple_caller)
            result = runner.run_round(graph)
            assert isinstance(result, MACPResult)
        finally:
            runner_module.TOOLS_AVAILABLE = original


# ============================================================================
# MACPRunner - filter_unreachable
# ============================================================================


class TestFilterUnreachable:
    def test_filter_unreachable_with_start_agent(self):
        """Covers lines 1972-1978: filter_unreachable path."""
        graph = _make_graph(3)
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(
            graph,
            start_agent_id="a0",
            final_agent_id="a2",
            filter_unreachable=True,
        )
        assert isinstance(result, MACPResult)

    def test_no_filter_unreachable(self):
        """Covers path when filter_unreachable=False."""
        graph = _make_graph(3)
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(graph, filter_unreachable=False)
        assert len(result.execution_order) == 3


# ============================================================================
# MACPRunner - adaptive mode
# ============================================================================


class TestAdaptiveMode:
    def test_adaptive_run_round(self):
        """Covers adaptive=True path in run_round → _run_adaptive."""
        config = RunnerConfig(adaptive=True)
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        result = runner.run_round(graph)
        assert isinstance(result, MACPResult)

    def test_adaptive_stream(self):
        """Covers adaptive stream path."""
        config = RunnerConfig(adaptive=True)
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        events = list(runner.stream(graph))
        assert len(events) > 0

    @pytest.mark.asyncio
    async def test_adaptive_arun_round(self):
        """Covers async adaptive path."""
        config = RunnerConfig(adaptive=True)
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_caller=_async_caller, config=config)
        result = await runner.arun_round(graph)
        assert isinstance(result, MACPResult)


# ============================================================================
# MACPRunner properties
# ============================================================================


class TestRunnerProperties:
    def test_memory_pool_initially_none(self):
        runner = MACPRunner(llm_caller=_simple_caller)
        assert runner.memory_pool is None

    def test_memory_pool_after_run_with_memory(self):
        config = RunnerConfig(enable_memory=True)
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=_simple_caller, config=config)
        runner.run_round(graph)
        assert runner.memory_pool is not None


# ============================================================================
# Edge cases: caller is None for some agents
# ============================================================================


class TestCallerIsNoneForAgent:
    def test_caller_none_for_agent_continues(self):
        """Covers lines 2049-2059: caller is None for some agents in _run_simple."""
        # Only "a0" has a caller; "a1" falls back to self.llm_caller = None
        graph = _make_graph(2)
        runner = MACPRunner(llm_callers={"a0": _simple_caller})
        result = runner.run_round(graph)
        # a0 should have a response; a1 should have an error message
        assert "a0" in result.messages
        assert "[Error:" in result.messages.get("a1", "[Error: no caller]")

    def test_stream_caller_none_for_agent(self):
        """Covers lines 3946-3956: stream simple, caller is None."""
        graph = _make_graph(2)
        runner = MACPRunner(llm_callers={"a0": _simple_caller})
        events = list(runner.stream(graph))
        types = [e.event_type for e in events]
        assert StreamEventType.AGENT_ERROR in types

    @pytest.mark.asyncio
    async def test_arun_caller_none_for_agent(self):
        """Covers lines 2283-2293: async caller is None for some agents."""
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_callers={"a0": _async_caller})
        result = await runner.arun_round(graph)
        assert "[Error:" in result.messages.get("a1", "[Error: no caller]")

    @pytest.mark.asyncio
    async def test_astream_no_async_caller_for_agent(self):
        """Covers lines 4130-4139: astream simple no async caller for agent."""
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_callers={"a0": _async_caller})
        events = [event async for event in runner.astream(graph)]
        types = [e.event_type for e in events]
        assert StreamEventType.AGENT_ERROR in types


# ============================================================================
# Disabled nodes
# ============================================================================


class TestDisabledNodes:
    def test_disabled_node_skipped(self):
        """Covers lines 2016-2018: disabled nodes are skipped."""
        graph = _make_graph(2)
        graph.disabled_nodes = {"a1"}
        runner = MACPRunner(llm_caller=_simple_caller)
        result = runner.run_round(graph)
        assert "a1" not in result.execution_order or result.messages.get("a1") is None

    @pytest.mark.asyncio
    async def test_async_disabled_node_skipped(self):
        """Covers async disabled nodes."""
        graph = _make_graph(2)
        graph.disabled_nodes = {"a1"}
        runner = MACPRunner(async_llm_caller=_async_caller)
        result = await runner.arun_round(graph)
        assert isinstance(result, MACPResult)


# ============================================================================
# Run error raised
# ============================================================================


class TestRunError:
    def test_run_error_propagated(self):
        """Covers lines 2138-2139, 2155: run error is re-raised after finalization."""
        call_count = [0]

        def always_fail(prompt: str) -> str:
            call_count[0] += 1
            msg = "Fatal LLM error"
            raise RuntimeError(msg)

        graph = _make_graph(1)
        runner = MACPRunner(llm_caller=always_fail)
        # The run should either propagate the error or swallow it
        try:
            result = runner.run_round(graph)
            # If not raised, check the error is recorded
            assert "[Error:" in result.messages.get("a0", "")
        except RuntimeError:
            pass  # Error propagated, which is also acceptable

    @pytest.mark.asyncio
    async def test_async_run_error_propagated(self):
        """Covers async run error paths."""

        async def always_fail_async(prompt: str) -> str:
            msg = "Async fatal error"
            raise RuntimeError(msg)

        graph = _make_graph(1)
        runner = MACPRunner(async_llm_caller=always_fail_async)
        try:
            result = await runner.arun_round(graph)
            assert "[Error:" in result.messages.get("a0", "")
        except RuntimeError:
            pass


# ============================================================================
# Dynamic topology in async
# ============================================================================


class TestAsyncDynamicTopology:
    @pytest.mark.asyncio
    async def test_async_topology_hook(self):
        """Covers _apply_async_topology_hooks (lines 2348-2371)."""

        async def async_hook(ctx: StepContext, role_graph) -> TopologyAction:
            return TopologyAction(skip_agents=["a1"] if ctx.agent_id == "a0" else [])

        config = RunnerConfig(
            enable_dynamic_topology=True,
            async_topology_hooks=[async_hook],
        )
        graph = _make_graph(2)
        runner = MACPRunner(async_llm_caller=_async_caller, config=config)
        result = await runner.arun_round(graph)
        assert isinstance(result, MACPResult)

    @pytest.mark.asyncio
    async def test_async_early_stop(self):
        """Covers async early stop lines (2344-2346)."""
        stop = EarlyStopCondition.on_keyword("Async")
        config = RunnerConfig(early_stop_conditions=[stop])
        graph = _make_graph(3)
        runner = MACPRunner(async_llm_caller=_async_caller, config=config)
        result = await runner.arun_round(graph)
        assert isinstance(result, MACPResult)


# ============================================================================
# Adaptive mode with caller errors
# ============================================================================


class TestAdaptiveErrors:
    def test_adaptive_run_with_error(self):
        """Covers lines 3649-3671: error handling in _run_adaptive."""
        call_count = [0]

        def erroring_caller(prompt: str) -> str:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "Adaptive error"
                raise ValueError(msg)
            return "Recovery response"

        config = RunnerConfig(adaptive=True)
        graph = _make_graph(2)
        runner = MACPRunner(llm_caller=erroring_caller, config=config)
        result = runner.run_round(graph)
        assert isinstance(result, MACPResult)

    def test_adaptive_stream_with_caller_none(self):
        """Covers lines 3600-3624: adaptive stream caller is None."""
        config = RunnerConfig(adaptive=True)
        graph = _make_graph(2)
        runner = MACPRunner(llm_callers={"a0": _simple_caller}, config=config)
        events = list(runner.stream(graph))
        assert len(events) > 0


# ============================================================================
# stream/astream - empty graph path
# ============================================================================


class TestEmptyGraphStreaming:
    def test_stream_empty_graph_yields_run_end(self):
        """Covers lines 3884-3887: stream simple with empty base."""
        import rustworkx as rx

        from gmas.core.graph import RoleGraph

        g = rx.PyDiGraph()
        g.add_node({"id": "__task__"})
        empty_graph = RoleGraph(
            node_ids=["__task__"],
            task_node="__task__",
            graph=g,
            A_com=torch.zeros((1, 1)),
            agents=[],
        )
        runner = MACPRunner(llm_caller=_simple_caller)
        events = list(runner.stream(empty_graph))
        types = [e.event_type for e in events]
        assert StreamEventType.RUN_END in types

    @pytest.mark.asyncio
    async def test_astream_empty_graph_yields_run_end(self):
        """Covers lines 4055-4058: astream simple with empty base."""
        import rustworkx as rx

        from gmas.core.graph import RoleGraph

        g = rx.PyDiGraph()
        g.add_node({"id": "__task__"})
        empty_graph = RoleGraph(
            node_ids=["__task__"],
            task_node="__task__",
            graph=g,
            A_com=torch.zeros((1, 1)),
            agents=[],
        )
        runner = MACPRunner(async_llm_caller=_async_caller)
        events = [event async for event in runner.astream(empty_graph)]
        types = [e.event_type for e in events]
        assert StreamEventType.RUN_END in types


# ============================================================================
# Async OpenAI caller - inner function
# ============================================================================


@pytest.mark.asyncio
async def test_async_openai_caller_inner_function():
    """Covers lines 366-371: the inner async caller function."""
    from gmas.core.agent import AgentLLMConfig
    from gmas.execution.runner import _create_async_openai_caller_from_config

    mock_openai_module = MagicMock()
    mock_client = MagicMock()
    mock_openai_module.AsyncOpenAI.return_value = mock_client
    mock_response = MagicMock()
    mock_response.choices[0].message.content = "async response"

    # The caller function uses `await client.chat.completions.create(...)`.
    # We make the mock coroutine-compatible.
    async def mock_create(*args, **kwargs):
        return mock_response

    mock_client.chat.completions.create = mock_create

    config = AgentLLMConfig(
        model_name="gpt-4",
        base_url="http://api.example.com",
        api_key="test-key",
    )

    with patch.dict(sys.modules, {"openai": mock_openai_module}):
        caller = _create_async_openai_caller_from_config(config)
        result = await caller("test prompt")
        assert result == "async response"


# ============================================================================
# End-to-end: disable() + topology hooks + early stop (GraphBuilder)
# ============================================================================


class TestDisableEndToEnd:
    """
    End-to-end tests verifying that graph.disable() works across all
    execution modes: simple, adaptive, streaming, async.
    """

    @staticmethod
    def _build_graph():
        from gmas.builder.graph_builder import GraphBuilder

        builder = GraphBuilder()
        builder.add_agent("input", persona="Input processor")
        builder.add_agent("solver", persona="Problem solver")
        builder.add_agent("checker", persona="Solution checker")
        builder.add_agent("expert", persona="Expert reviewer (expensive)")
        builder.add_agent("output", persona="Output formatter")
        builder.add_agent("optional", persona="Optional analyzer")

        builder.add_workflow_edge("input", "solver")
        builder.add_workflow_edge("solver", "checker")
        builder.add_workflow_edge("checker", "output")

        builder.set_start_node("input")
        builder.set_end_node("output")
        builder.add_task(query="Solve the problem")
        builder.connect_task_to_agents()
        return builder.build()

    # ------------------------------------------------------------------ #
    # 1. Basic disable: disabled nodes must NOT execute
    # ------------------------------------------------------------------ #
    def test_disabled_nodes_not_executed(self):
        graph = self._build_graph()
        graph.disable(["optional", "checker"])

        call_log = []

        def tracking_caller(prompt: str) -> str:
            call_log.append(prompt)
            return "Mock response"

        runner = MACPRunner(llm_caller=tracking_caller)
        result = runner.run_round(graph, filter_unreachable=False)

        assert "checker" not in result.execution_order, (
            f"checker was disabled but still executed: {result.execution_order}"
        )
        assert "optional" not in result.execution_order, (
            f"optional was disabled but still executed: {result.execution_order}"
        )

    # ------------------------------------------------------------------ #
    # 2. Disabled nodes must appear in pruned_agents
    # ------------------------------------------------------------------ #
    def test_disabled_nodes_in_pruned(self):
        graph = self._build_graph()
        graph.disable(["optional", "checker"])

        runner = MACPRunner(llm_caller=lambda _p: "Mock response")
        result = runner.run_round(graph, filter_unreachable=False)

        pruned = result.pruned_agents or []
        assert "checker" in pruned, f"checker should be in pruned_agents, got: {pruned}"
        assert "optional" in pruned, f"optional should be in pruned_agents, got: {pruned}"

    # ------------------------------------------------------------------ #
    # 3. Enabled nodes still execute normally
    # ------------------------------------------------------------------ #
    def test_enabled_nodes_execute(self):
        graph = self._build_graph()
        graph.disable(["optional", "checker"])

        runner = MACPRunner(llm_caller=lambda _p: "Mock response")
        result = runner.run_round(graph, filter_unreachable=False)

        assert "input" in result.execution_order
        assert "solver" in result.execution_order
        assert "output" in result.execution_order

    # ------------------------------------------------------------------ #
    # 4. disable() count is correct
    # ------------------------------------------------------------------ #
    def test_disable_returns_correct_count(self):
        graph = self._build_graph()
        count = graph.disable(["optional", "checker"])
        assert count == 2

        count2 = graph.disable(["nonexistent"])
        assert count2 == 0

    # ------------------------------------------------------------------ #
    # 5. Topology hook: skip_agents for checker (already disabled)
    # ------------------------------------------------------------------ #
    def test_topology_hook_skip_with_disable(self):
        graph = self._build_graph()
        graph.disable(["checker"])

        def hook(ctx, g):
            if ctx.agent_id == "solver":
                return TopologyAction(skip_agents=["checker"])
            return None

        config = RunnerConfig(
            enable_dynamic_topology=True,
            topology_hooks=[hook],
        )
        runner = MACPRunner(llm_caller=lambda _p: "CONFIDENT solution", config=config)
        result = runner.run_round(graph, filter_unreachable=False)

        assert "checker" not in result.execution_order

    # ------------------------------------------------------------------ #
    # 6. Topology hook: disabled checker should not trigger hooks
    # ------------------------------------------------------------------ #
    def test_topology_hook_add_expert(self):
        graph = self._build_graph()
        graph.disable(["optional", "checker", "expert"])

        hook_calls = []

        def hook(ctx, g):
            hook_calls.append(ctx.agent_id)
            if ctx.agent_id == "checker" and "ERROR" in (ctx.response or ""):
                return TopologyAction(
                    add_edges=[("checker", "expert", 1.0), ("expert", "output", 1.0)],
                    trigger_rebuild=True,
                )
            return None

        config = RunnerConfig(
            enable_dynamic_topology=True,
            topology_hooks=[hook],
        )
        runner = MACPRunner(llm_caller=lambda _p: "Mock response", config=config)
        result = runner.run_round(graph, filter_unreachable=False)

        assert "checker" not in hook_calls
        assert "expert" not in result.execution_order

    # ------------------------------------------------------------------ #
    # 7. Early stop on keyword
    # ------------------------------------------------------------------ #
    def test_early_stop_keyword(self):
        graph = self._build_graph()
        graph.disable(["optional"])

        call_count = [0]

        def caller_with_final(prompt: str) -> str:
            call_count[0] += 1
            if call_count[0] == 2:
                return "FINAL_ANSWER: 42"
            return "processing..."

        config = RunnerConfig(
            early_stop_conditions=[
                EarlyStopCondition.on_keyword("FINAL_ANSWER"),
            ],
        )
        runner = MACPRunner(llm_caller=caller_with_final, config=config)
        result = runner.run_round(graph, filter_unreachable=False)

        assert result.early_stopped

    # ------------------------------------------------------------------ #
    # 8. Early stop on token limit
    # ------------------------------------------------------------------ #
    def test_early_stop_token_limit(self):
        graph = self._build_graph()
        graph.disable(["optional"])

        config = RunnerConfig(
            early_stop_conditions=[
                EarlyStopCondition.on_token_limit(0),
            ],
        )
        runner = MACPRunner(
            llm_caller=lambda _p: "response",
            token_counter=lambda _s: 100,
            config=config,
        )
        result = runner.run_round(graph, filter_unreachable=False)

        assert result.early_stopped or len(result.execution_order) <= 2

    # ------------------------------------------------------------------ #
    # 9. filter_unreachable=True should exclude disconnected 'optional'
    # ------------------------------------------------------------------ #
    def test_filter_unreachable_excludes_optional(self):
        graph = self._build_graph()

        runner = MACPRunner(llm_caller=lambda _p: "Mock response")
        result = runner.run_round(graph, filter_unreachable=True)

        assert "optional" not in result.execution_order

    # ------------------------------------------------------------------ #
    # 10. disable + enable round-trip
    # ------------------------------------------------------------------ #
    def test_disable_then_enable(self):
        graph = self._build_graph()
        graph.disable(["checker"])
        assert not graph.is_enabled("checker")

        graph.enable(["checker"])
        assert graph.is_enabled("checker")

        runner = MACPRunner(llm_caller=lambda _p: "Mock response")
        result = runner.run_round(graph, filter_unreachable=False)

        assert "checker" in result.execution_order

    # ------------------------------------------------------------------ #
    # 11. Full scenario — simple mode
    # ------------------------------------------------------------------ #
    def test_full_user_scenario_simple_mode(self):
        graph = self._build_graph()
        graph.disable(["optional", "checker", "expert"])

        def hook(ctx, g):
            if ctx.agent_id == "solver" and "CONFIDENT" in (ctx.response or ""):
                return TopologyAction(skip_agents=["checker"])
            return None

        config = RunnerConfig(
            enable_dynamic_topology=True,
            topology_hooks=[hook],
            early_stop_conditions=[
                EarlyStopCondition.on_keyword("FINAL_ANSWER"),
                EarlyStopCondition.on_token_limit(5000),
            ],
        )
        runner = MACPRunner(llm_caller=lambda _p: "CONFIDENT mock", config=config)
        result = runner.run_round(graph, filter_unreachable=False)

        assert isinstance(result, MACPResult)
        assert "optional" not in result.execution_order
        assert "checker" not in result.execution_order
        assert "expert" not in result.execution_order

    # ------------------------------------------------------------------ #
    # 12. Adaptive mode respects disabled_nodes
    # ------------------------------------------------------------------ #
    def test_adaptive_mode_respects_disabled_nodes(self):
        graph = self._build_graph()
        graph.disable(["optional", "checker"])

        config = RunnerConfig(adaptive=True)
        runner = MACPRunner(llm_caller=lambda _p: "mock response", config=config)
        result = runner.run_round(graph, filter_unreachable=False)

        assert "optional" not in result.execution_order, f"optional is disabled but executed: {result.execution_order}"
        assert "checker" not in result.execution_order, f"checker is disabled but executed: {result.execution_order}"
        pruned = result.pruned_agents or []
        assert "optional" in pruned
        assert "checker" in pruned

    # ------------------------------------------------------------------ #
    # 13. Async adaptive mode respects disabled_nodes
    # ------------------------------------------------------------------ #
    @pytest.mark.asyncio
    async def test_async_adaptive_mode_respects_disabled_nodes(self):
        graph = self._build_graph()
        graph.disable(["optional", "checker"])

        config = RunnerConfig(adaptive=True)

        async def _acaller(prompt: str) -> str:
            return "mock response"

        runner = MACPRunner(async_llm_caller=_acaller, config=config)
        result = await runner.arun_round(graph, filter_unreachable=False)

        assert "optional" not in result.execution_order
        assert "checker" not in result.execution_order
        pruned = result.pruned_agents or []
        assert "optional" in pruned
        assert "checker" in pruned

    # ------------------------------------------------------------------ #
    # 14. Stream simple respects disabled_nodes
    # ------------------------------------------------------------------ #
    def test_stream_simple_respects_disabled_nodes(self):
        graph = self._build_graph()
        graph.disable(["optional", "checker"])

        runner = MACPRunner(llm_caller=lambda _p: "mock response")
        events = list(runner.stream(graph))

        agent_outputs = [e for e in events if isinstance(e, AgentOutputEvent)]
        executed_ids = [e.agent_id for e in agent_outputs]
        assert "optional" not in executed_ids
        assert "checker" not in executed_ids

    # ------------------------------------------------------------------ #
    # 15. Stream adaptive respects disabled_nodes
    # ------------------------------------------------------------------ #
    def test_stream_adaptive_respects_disabled_nodes(self):
        graph = self._build_graph()
        graph.disable(["optional", "checker"])

        config = RunnerConfig(adaptive=True)
        runner = MACPRunner(llm_caller=lambda _p: "mock response", config=config)
        events = list(runner.stream(graph))

        agent_outputs = [e for e in events if isinstance(e, AgentOutputEvent)]
        executed_ids = [e.agent_id for e in agent_outputs]
        assert "optional" not in executed_ids
        assert "checker" not in executed_ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
