"""Tests for execution/runner.py — simple data classes, LLMCallerFactory, EarlyStopCondition."""

import torch

from gmas.core.agent import AgentLLMConfig
from gmas.execution.runner import (
    EarlyStopCondition,
    HiddenState,
    LLMCallerFactory,
    MACPResult,
    RunnerConfig,
    StepContext,
    TopologyAction,
)

# ═══════════════════════════════════════════════════════════════
#  HiddenState
# ═══════════════════════════════════════════════════════════════


class TestHiddenState:
    def test_defaults(self):
        hs = HiddenState()
        assert hs.tensor is None
        assert hs.embedding is None
        assert hs.metadata == {}

    def test_with_tensors(self):
        t = torch.zeros(3)
        e = torch.ones(4)
        hs = HiddenState(tensor=t, embedding=e)
        assert hs.tensor is not None
        assert hs.embedding is not None

    def test_with_metadata(self):
        hs = HiddenState(metadata={"key": "value"})
        assert hs.metadata["key"] == "value"


# ═══════════════════════════════════════════════════════════════
#  StepContext
# ═══════════════════════════════════════════════════════════════


class TestStepContext:
    def test_minimal_creation(self):
        ctx = StepContext(agent_id="agent_a")
        assert ctx.agent_id == "agent_a"
        assert ctx.response is None
        assert ctx.messages == {}
        assert ctx.remaining_agents == []
        assert ctx.query == ""
        assert ctx.total_tokens == 0

    def test_full_creation(self):
        ctx = StepContext(
            agent_id="agent_a",
            response="Hello",
            messages={"agent_a": "Hello"},
            execution_order=["agent_a"],
            remaining_agents=["agent_b"],
            query="test?",
            total_tokens=100,
            metadata={"x": 1},
        )
        assert ctx.agent_id == "agent_a"
        assert ctx.response == "Hello"
        assert ctx.total_tokens == 100
        assert ctx.metadata["x"] == 1


# ═══════════════════════════════════════════════════════════════
#  TopologyAction
# ═══════════════════════════════════════════════════════════════


class TestTopologyAction:
    def test_defaults(self):
        action = TopologyAction()
        assert action.early_stop is False
        assert action.early_stop_reason is None
        assert action.add_edges == []
        assert action.remove_edges == []
        assert action.skip_agents == []
        assert action.force_agents == []
        assert action.condition_skip_agents == []
        assert action.condition_unskip_agents == []
        assert action.insert_chains == []
        assert action.new_end_agent is None
        assert action.trigger_rebuild is False

    def test_early_stop(self):
        action = TopologyAction(early_stop=True, early_stop_reason="done")
        assert action.early_stop is True
        assert action.early_stop_reason == "done"

    def test_add_edges(self):
        action = TopologyAction(add_edges=[("a", "b", 1.0), ("b", "c", 0.5)])
        assert len(action.add_edges) == 2

    def test_skip_and_force(self):
        action = TopologyAction(skip_agents=["a"], force_agents=["b"])
        assert "a" in action.skip_agents
        assert "b" in action.force_agents


# ═══════════════════════════════════════════════════════════════
#  EarlyStopCondition
# ═══════════════════════════════════════════════════════════════


class TestEarlyStopCondition:
    def _make_ctx(self, **kwargs) -> StepContext:
        defaults: dict = {"agent_id": "a", "execution_order": [], "messages": {}}
        defaults.update(kwargs)
        return StepContext.model_validate(defaults)

    def test_basic_condition_true(self):
        cond = EarlyStopCondition(condition=lambda _ctx: True)
        ctx = self._make_ctx()
        should_stop, reason = cond.should_stop(ctx)
        assert should_stop is True
        assert "met" in reason

    def test_basic_condition_false(self):
        cond = EarlyStopCondition(condition=lambda _ctx: False)
        ctx = self._make_ctx()
        should_stop, reason = cond.should_stop(ctx)
        assert should_stop is False
        assert reason == ""

    def test_min_agents_not_met(self):
        cond = EarlyStopCondition(
            condition=lambda _ctx: True,
            min_agents_executed=3,
        )
        ctx = self._make_ctx(execution_order=["a", "b"])
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_min_agents_met(self):
        cond = EarlyStopCondition(
            condition=lambda _ctx: True,
            min_agents_executed=2,
        )
        ctx = self._make_ctx(execution_order=["a", "b"])
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is True

    def test_after_agents_not_matching(self):
        cond = EarlyStopCondition(
            condition=lambda _ctx: True,
            after_agents=["b"],
        )
        ctx = self._make_ctx(agent_id="a")
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_after_agents_matching(self):
        cond = EarlyStopCondition(
            condition=lambda _ctx: True,
            after_agents=["a"],
        )
        ctx = self._make_ctx(agent_id="a")
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is True

    def test_exception_in_condition_returns_false(self):
        def bad_condition(ctx):
            msg = "bad"
            raise ValueError(msg)

        cond = EarlyStopCondition(condition=bad_condition)
        ctx = self._make_ctx()
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_on_keyword_found(self):
        cond = EarlyStopCondition.on_keyword("FINAL ANSWER")
        ctx = self._make_ctx(response="Here is my FINAL ANSWER: 42")
        should_stop, reason = cond.should_stop(ctx)
        assert should_stop is True
        assert "FINAL ANSWER" in reason

    def test_on_keyword_not_found(self):
        cond = EarlyStopCondition.on_keyword("DONE")
        ctx = self._make_ctx(response="Work in progress")
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_on_keyword_case_sensitive(self):
        cond = EarlyStopCondition.on_keyword("DONE", case_sensitive=True)
        ctx = self._make_ctx(response="done")
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_on_keyword_in_all_messages(self):
        cond = EarlyStopCondition.on_keyword("answer", in_last_response=False)
        ctx = self._make_ctx(
            response="nothing here",
            messages={"a": "The answer is 42"},
        )
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is True

    def test_on_keyword_no_response(self):
        cond = EarlyStopCondition.on_keyword("DONE")
        ctx = self._make_ctx(response=None)
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_on_token_limit_exceeded(self):
        cond = EarlyStopCondition.on_token_limit(500)
        ctx = self._make_ctx(total_tokens=600)
        should_stop, reason = cond.should_stop(ctx)
        assert should_stop is True
        assert "500" in reason

    def test_on_token_limit_not_exceeded(self):
        cond = EarlyStopCondition.on_token_limit(500)
        ctx = self._make_ctx(total_tokens=400)
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_on_token_limit_custom_reason(self):
        cond = EarlyStopCondition.on_token_limit(100, reason="Too many tokens")
        ctx = self._make_ctx(total_tokens=200)
        _should_stop, reason = cond.should_stop(ctx)
        assert reason == "Too many tokens"

    def test_on_agent_count_exceeded(self):
        cond = EarlyStopCondition.on_agent_count(3)
        ctx = self._make_ctx(execution_order=["a", "b", "c"])
        should_stop, reason = cond.should_stop(ctx)
        assert should_stop is True
        assert "3" in reason

    def test_on_agent_count_not_exceeded(self):
        cond = EarlyStopCondition.on_agent_count(5)
        ctx = self._make_ctx(execution_order=["a", "b"])
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_on_metadata_key_present(self):
        cond = EarlyStopCondition.on_metadata("finished")
        ctx = self._make_ctx(metadata={"finished": True})
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is True

    def test_on_metadata_key_not_present(self):
        cond = EarlyStopCondition.on_metadata("finished")
        ctx = self._make_ctx(metadata={})
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_on_metadata_value_match(self):
        cond = EarlyStopCondition.on_metadata("score", 0.9)
        ctx = self._make_ctx(metadata={"score": 0.9})
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is True

    def test_on_metadata_value_no_match(self):
        cond = EarlyStopCondition.on_metadata("score", 0.9)
        ctx = self._make_ctx(metadata={"score": 0.5})
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_on_metadata_custom_comparator(self):
        cond = EarlyStopCondition.on_metadata("quality", 0.8, comparator=lambda v, t: v > t)
        ctx = self._make_ctx(metadata={"quality": 0.9})
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is True

    def test_on_custom(self):
        cond = EarlyStopCondition.on_custom(lambda _ctx: True, reason="Custom done")
        ctx = self._make_ctx()
        should_stop, reason = cond.should_stop(ctx)
        assert should_stop is True
        assert reason == "Custom done"

    def test_on_custom_with_extra_kwargs(self):
        cond = EarlyStopCondition.on_custom(
            lambda _ctx: True,
            reason="done",
            after_agents=["x"],
        )
        assert cond.after_agents == ["x"]

    def test_combine_any_one_true(self):
        cond = EarlyStopCondition.combine_any(
            [
                EarlyStopCondition(lambda _ctx: False),
                EarlyStopCondition(lambda _ctx: True),
            ]
        )
        ctx = self._make_ctx()
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is True

    def test_combine_any_all_false(self):
        cond = EarlyStopCondition.combine_any(
            [
                EarlyStopCondition(lambda _ctx: False),
                EarlyStopCondition(lambda _ctx: False),
            ]
        )
        ctx = self._make_ctx()
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False

    def test_combine_all_all_true(self):
        cond = EarlyStopCondition.combine_all(
            [
                EarlyStopCondition(lambda _ctx: True),
                EarlyStopCondition(lambda _ctx: True),
            ]
        )
        ctx = self._make_ctx()
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is True

    def test_combine_all_one_false(self):
        cond = EarlyStopCondition.combine_all(
            [
                EarlyStopCondition(lambda _ctx: True),
                EarlyStopCondition(lambda _ctx: False),
            ]
        )
        ctx = self._make_ctx()
        should_stop, _ = cond.should_stop(ctx)
        assert should_stop is False


# ═══════════════════════════════════════════════════════════════
#  LLMCallerFactory
# ═══════════════════════════════════════════════════════════════


class TestLLMCallerFactory:
    def test_init_minimal(self):
        factory = LLMCallerFactory()
        assert factory.default_caller is None
        assert factory.default_async_caller is None
        assert factory.default_config is None

    def test_init_with_default_caller(self):
        def default_caller(prompt):
            return "response"

        factory = LLMCallerFactory(default_caller=default_caller)
        assert factory.default_caller is default_caller

    def test_config_key(self):
        factory = LLMCallerFactory()
        config = AgentLLMConfig(
            base_url="http://localhost",
            model_name="gpt-4",
            api_key="sk-test",
        )
        key = factory._config_key(config)
        assert "http://localhost" in key
        assert "gpt-4" in key
        assert "sk-test" in key

    def test_merge_config_no_default(self):
        factory = LLMCallerFactory()
        config = AgentLLMConfig(model_name="gpt-4")
        merged = factory._merge_config(config)
        assert merged is config  # No default, returned as-is

    def test_merge_config_with_default(self):
        default = AgentLLMConfig(
            model_name="gpt-3.5-turbo",
            base_url="http://api.openai.com",
            max_tokens=1000,
            temperature=0.5,
        )
        factory = LLMCallerFactory(default_config=default)
        config = AgentLLMConfig(model_name="gpt-4")
        merged = factory._merge_config(config)
        # model_name from config, base_url from default
        assert merged.model_name == "gpt-4"
        assert merged.base_url == "http://api.openai.com"
        assert merged.max_tokens == 1000

    def test_merge_config_override_defaults(self):
        default = AgentLLMConfig(
            model_name="gpt-3.5",
            base_url="http://default.url",
            temperature=0.5,
        )
        factory = LLMCallerFactory(default_config=default)
        config = AgentLLMConfig(
            model_name="gpt-4",
            base_url="http://custom.url",
            temperature=0.2,
        )
        merged = factory._merge_config(config)
        assert merged.model_name == "gpt-4"
        assert merged.base_url == "http://custom.url"
        assert merged.temperature == 0.2

    def test_get_caller_no_config_returns_default(self):
        def default_caller(prompt):
            return "default"

        factory = LLMCallerFactory(default_caller=default_caller)
        caller = factory.get_caller(None)
        assert caller is default_caller

    def test_get_caller_unconfigured_returns_default(self):
        def default_caller(prompt):
            return "default"

        factory = LLMCallerFactory(default_caller=default_caller)
        config = AgentLLMConfig()  # Not configured
        caller = factory.get_caller(config)
        assert caller is default_caller

    def test_get_caller_with_builder(self):
        def built_caller(prompt):
            return "built"

        def builder(config):
            return built_caller

        factory = LLMCallerFactory(caller_builder=builder)
        config = AgentLLMConfig(model_name="gpt-4", base_url="http://api.example.com")
        caller = factory.get_caller(config)
        assert caller is built_caller

    def test_get_caller_cached(self):
        call_count = [0]

        def builder(config):
            call_count[0] += 1
            return lambda _prompt: "built"

        factory = LLMCallerFactory(caller_builder=builder)
        config = AgentLLMConfig(model_name="gpt-4", base_url="http://api.example.com")

        caller1 = factory.get_caller(config)
        caller2 = factory.get_caller(config)

        assert call_count[0] == 1  # Builder called only once
        assert caller1 is caller2

    def test_get_caller_no_builder_returns_default(self):
        def default_caller(prompt):
            return "default"

        factory = LLMCallerFactory(default_caller=default_caller)
        config = AgentLLMConfig(model_name="gpt-4", base_url="http://api.example.com")
        caller = factory.get_caller(config)
        assert caller is default_caller

    def test_get_async_caller_no_config_returns_default(self):
        async def default_async_caller(prompt):
            return "default"

        factory = LLMCallerFactory(default_async_caller=default_async_caller)
        caller = factory.get_async_caller(None)
        assert caller is default_async_caller

    def test_get_async_caller_with_builder(self):
        async def built_caller(prompt):
            return "built"

        def async_builder(config):
            return built_caller

        factory = LLMCallerFactory(async_caller_builder=async_builder)
        config = AgentLLMConfig(model_name="gpt-4", base_url="http://api.example.com")
        caller = factory.get_async_caller(config)
        assert caller is built_caller

    def test_get_async_caller_cached(self):
        async def built_caller(prompt):
            return "built"

        call_count = [0]

        def async_builder(config):
            call_count[0] += 1
            return built_caller

        factory = LLMCallerFactory(async_caller_builder=async_builder)
        config = AgentLLMConfig(model_name="gpt-4", base_url="http://api.example.com")

        caller1 = factory.get_async_caller(config)
        caller2 = factory.get_async_caller(config)
        assert call_count[0] == 1
        assert caller1 is caller2

    def test_create_openai_factory_basic(self):
        factory = LLMCallerFactory.create_openai_factory(
            default_api_key="test-key",
            default_model="gpt-4",
        )
        assert factory.default_config is not None
        assert factory.default_config.model_name == "gpt-4"
        assert factory.caller_builder is not None
        assert factory.async_caller_builder is not None

    def test_create_openai_factory_env_key(self, monkeypatch):
        monkeypatch.setenv("MY_API_KEY", "env-key-value")
        factory = LLMCallerFactory.create_openai_factory(
            default_api_key="$MY_API_KEY",
        )
        assert factory.default_config is not None
        assert factory.default_config.api_key == "env-key-value"

    def test_create_openai_factory_no_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        factory = LLMCallerFactory.create_openai_factory()
        assert factory is not None


# ═══════════════════════════════════════════════════════════════
#  MACPResult
# ═══════════════════════════════════════════════════════════════


class TestMACPResult:
    def test_basic_creation(self):
        result = MACPResult(
            messages={"a": "Hello"},
            final_answer="42",
            final_agent_id="a",
            execution_order=["a"],
        )
        assert result.final_answer == "42"
        assert result.final_agent_id == "a"
        assert result.messages == {"a": "Hello"}
        assert result.execution_order == ["a"]

    def test_defaults(self):
        result = MACPResult(
            messages={},
            final_answer="",
            final_agent_id="",
            execution_order=[],
        )
        assert result.agent_states is None
        assert result.step_results is None
        assert result.total_tokens == 0
        assert result.total_time == 0.0
        assert result.topology_changed_count == 0
        assert result.fallback_count == 0
        assert result.pruned_agents is None
        assert result.errors is None
        assert result.hidden_states is None
        assert result.metrics is None
        assert result.budget_summary is None
        assert result.early_stopped is False
        assert result.early_stop_reason is None
        assert result.topology_modifications == 0

    def test_named_tuple(self):
        result = MACPResult(
            messages={},
            final_answer="answer",
            final_agent_id="b",
            execution_order=["a", "b"],
            total_tokens=500,
            early_stopped=True,
            early_stop_reason="limit reached",
        )
        assert result.total_tokens == 500
        assert result.early_stopped is True
        assert result.early_stop_reason == "limit reached"


# ═══════════════════════════════════════════════════════════════
#  RunnerConfig
# ═══════════════════════════════════════════════════════════════


class TestRunnerConfig:
    def test_defaults(self):
        config = RunnerConfig()
        assert config.timeout == 60.0
        assert config.adaptive is False
        assert config.enable_parallel is True
        assert config.max_parallel_size == 5
        assert config.max_retries == 2
        assert config.retry_delay == 1.0
        assert config.update_states is True
        assert config.enable_hidden_channels is False
        assert config.enable_memory is False
        assert config.enable_token_streaming is False
        assert config.max_tool_iterations == 3

    def test_custom_config(self):
        config = RunnerConfig(
            timeout=30.0,
            adaptive=True,
            max_retries=5,
        )
        assert config.timeout == 30.0
        assert config.adaptive is True
        assert config.max_retries == 5

    def test_with_budget_config(self):
        from gmas.execution.budget import BudgetConfig

        budget = BudgetConfig(total_token_limit=1000)
        config = RunnerConfig(budget_config=budget)
        assert config.budget_config is not None
        assert config.budget_config.total_token_limit == 1000
