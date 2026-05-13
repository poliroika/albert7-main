"""
Comprehensive tests for src/builder/auto_builder.py.

Covers AutoBuilderConfig, AutoGraphBuilder, parsing/validation logic,
topology generation, agent generation, tool assignment, and graph assembly.
All tests use mock LLM callers — no real API calls.
"""

import json

import pytest
from pydantic import ValidationError

from gmas.builder.auto_builder import (
    FRAMEWORK_TOOLS,
    AgentSpec,
    AgentsResponse,
    AutoBuilderConfig,
    AutoGraphBuilder,
    TopologyResponse,
    _build_tools_section,
    _describe_agents,
    _resolve_tools,
    _strip_json,
)
from gmas.core.agent import AgentProfile

# ─────────────────────────── Helpers ──────────────────────────────────────────


def _mock_caller(response: str):
    """Return a sync structured caller that always returns the given string."""

    def caller(messages: list[dict[str, str]]) -> str:
        return response

    return caller


def _mock_multi_caller(*responses: str):
    """Return a sync caller that returns responses in order."""
    it = iter(responses)

    def caller(messages: list[dict[str, str]]) -> str:
        return next(it)

    return caller


async def _async_mock_caller_factory(response: str):
    """Return an async structured caller that always returns the given string."""

    async def caller(messages: list[dict[str, str]]) -> str:
        return response

    return caller


def _make_agents(*ids: str) -> list[AgentProfile]:
    return [AgentProfile(agent_id=aid, display_name=aid.title()) for aid in ids]


def _make_agents_with_tools() -> list[AgentProfile]:
    return [
        AgentProfile(
            agent_id="researcher",
            display_name="Researcher",
            persona="a researcher",
            description="Searches for information",
            tools=["web_search"],
        ),
        AgentProfile(
            agent_id="coder",
            display_name="Coder",
            persona="a coder",
            description="Writes code",
            tools=["code_interpreter"],
        ),
        AgentProfile(
            agent_id="reviewer",
            display_name="Reviewer",
            persona="a reviewer",
            description="Reviews results",
        ),
    ]


def _topology_json(edges, start=None, end=None, reasoning="test"):
    return json.dumps(
        {
            "edges": edges,
            "start_node": start,
            "end_node": end,
            "reasoning": reasoning,
        }
    )


def _agents_json(agents, reasoning="test"):
    return json.dumps(
        {
            "agents": agents,
            "reasoning": reasoning,
        }
    )


# ─────────────────────────── _strip_json ──────────────────────────────────────


class TestStripJson:
    def test_plain_json(self):
        assert _strip_json('{"a": 1}') == '{"a": 1}'

    def test_markdown_fences(self):
        assert _strip_json('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_markdown_fences_no_lang(self):
        assert _strip_json('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_whitespace(self):
        assert _strip_json('  \n {"a": 1} \n ') == '{"a": 1}'


# ─────────────────────────── _describe_agents ─────────────────────────────────


class TestDescribeAgents:
    def test_basic_description(self):
        agents = _make_agents("a", "b")
        result = _describe_agents(agents)
        assert "a:" in result
        assert "b:" in result

    def test_includes_tools(self):
        agents = _make_agents_with_tools()
        result = _describe_agents(agents)
        assert "web_search" in result
        assert "code_interpreter" in result

    def test_no_tools_shows_reasoning_only(self):
        agents = [AgentProfile(agent_id="plain", display_name="Plain", persona="test")]
        result = _describe_agents(agents)
        assert "LLM reasoning only" in result


# ─────────────────────────── _resolve_tools ───────────────────────────────────


class TestResolveTools:
    def test_none_returns_all_framework_tools(self):
        result = _resolve_tools(None)
        assert set(result.keys()) == set(FRAMEWORK_TOOLS.keys())

    def test_list_resolves_to_dict(self):
        result = _resolve_tools(["web_search", "shell"])
        assert "web_search" in result
        assert "shell" in result
        assert "code_interpreter" not in result

    def test_dict_passed_through(self):
        custom = {"my_tool": "does stuff"}
        result = _resolve_tools(custom)
        assert result == custom

    def test_unknown_tool_in_list(self):
        result = _resolve_tools(["web_search", "unknown_tool"])
        assert result["web_search"] == FRAMEWORK_TOOLS["web_search"]
        assert result["unknown_tool"] == "Tool"


# ─────────────────────────── _build_tools_section ─────────────────────────────


class TestBuildToolsSection:
    def test_contains_tool_names(self):
        result = _build_tools_section({"web_search": "Search the web"})
        assert "web_search" in result
        assert "Search the web" in result


# ─────────────────────────── AutoBuilderConfig ────────────────────────────────


class TestAutoBuilderConfig:
    def test_defaults(self):
        cfg = AutoBuilderConfig()
        assert cfg.max_retries == 3
        assert cfg.max_agents == 10
        assert cfg.include_task_node is True
        assert cfg.default_llm_backbone is None
        assert cfg.available_tools is None

    def test_custom_values(self):
        cfg = AutoBuilderConfig(
            max_retries=5,
            max_agents=20,
            include_task_node=False,
            default_llm_backbone="gpt-4",
            default_temperature=0.5,
            available_tools=["web_search"],
        )
        assert cfg.max_retries == 5
        assert cfg.max_agents == 20
        assert cfg.default_llm_backbone == "gpt-4"

    def test_available_tools_as_dict(self):
        cfg = AutoBuilderConfig(available_tools={"my_tool": "desc"})
        assert cfg.available_tools == {"my_tool": "desc"}

    def test_max_retries_bounds(self):
        with pytest.raises(ValidationError):
            AutoBuilderConfig(max_retries=0)
        with pytest.raises(ValidationError):
            AutoBuilderConfig(max_retries=11)


# ─────────────────────────── Pydantic models ──────────────────────────────────


class TestPydanticModels:
    def test_agent_spec(self):
        spec = AgentSpec(agent_id="a", persona="test", tools=["web_search"])
        assert spec.agent_id == "a"
        assert spec.tools == ["web_search"]

    def test_agent_spec_defaults(self):
        spec = AgentSpec(agent_id="a")
        assert spec.persona == ""
        assert spec.tools == []

    def test_agents_response(self):
        resp = AgentsResponse(
            agents=[AgentSpec(agent_id="a"), AgentSpec(agent_id="b")],
            reasoning="test",
        )
        assert len(resp.agents) == 2

    def test_topology_response(self):
        resp = TopologyResponse(
            edges=[["a", "b"]],
            start_node="a",
            end_node="b",
        )
        assert resp.edges == [["a", "b"]]


# ─────────────────────── _parse_topology ──────────────────────────────────────


class TestParseTopology:
    def test_valid_chain(self):
        raw = _topology_json([["a", "b"], ["b", "c"]], "a", "c")
        result, error = AutoGraphBuilder._parse_topology(raw, {"a", "b", "c"})
        assert result is not None
        assert error is None
        assert result.start_node == "a"
        assert result.end_node == "c"

    def test_valid_diamond(self):
        raw = _topology_json(
            [["a", "b"], ["a", "c"], ["b", "d"], ["c", "d"]],
            "a",
            "d",
        )
        result, _error = AutoGraphBuilder._parse_topology(raw, {"a", "b", "c", "d"})
        assert result is not None
        assert len(result.edges) == 4

    def test_invalid_json(self):
        result, error = AutoGraphBuilder._parse_topology("not json", {"a"})
        assert result is None
        assert error is not None
        assert "Invalid JSON" in error

    def test_markdown_wrapped_json(self):
        raw = "```json\n" + _topology_json([["a", "b"]], "a", "b") + "\n```"
        result, _error = AutoGraphBuilder._parse_topology(raw, {"a", "b"})
        assert result is not None

    def test_unknown_agent_id(self):
        raw = _topology_json([["a", "unknown"]], "a", "unknown")
        result, error = AutoGraphBuilder._parse_topology(raw, {"a", "b"})
        assert result is None
        assert error is not None
        assert "Unknown agent IDs" in error

    def test_empty_edges(self):
        raw = _topology_json([], "a", "b")
        result, error = AutoGraphBuilder._parse_topology(raw, {"a", "b"})
        assert result is None
        assert error is not None
        assert "No edges" in error

    def test_cycle_detected(self):
        raw = _topology_json([["a", "b"], ["b", "c"], ["c", "a"]], "a", "c")
        result, error = AutoGraphBuilder._parse_topology(raw, {"a", "b", "c"})
        assert result is None
        assert error is not None
        assert "cycles" in error.lower()

    def test_isolated_node(self):
        raw = _topology_json([["a", "b"]], "a", "b")
        result, error = AutoGraphBuilder._parse_topology(raw, {"a", "b", "c"})
        assert result is None
        assert error is not None
        assert "Isolated" in error

    def test_edge_wrong_length(self):
        raw = json.dumps({"edges": [["a", "b", "c"]], "reasoning": ""})
        result, error = AutoGraphBuilder._parse_topology(raw, {"a", "b", "c"})
        assert result is None
        assert error is not None
        assert "2 elements" in error


# ─────────────────────── _parse_agents ────────────────────────────────────────


class TestParseAgents:
    def test_valid_agents(self):
        raw = _agents_json(
            [
                {"agent_id": "a", "persona": "test", "tools": []},
                {"agent_id": "b", "persona": "test", "tools": ["web_search"]},
            ]
        )
        result, _error = AutoGraphBuilder._parse_agents(raw)
        assert result is not None
        assert len(result.agents) == 2

    def test_invalid_json(self):
        result, error = AutoGraphBuilder._parse_agents("broken")
        assert result is None
        assert error is not None
        assert "Invalid JSON" in error

    def test_too_few_agents(self):
        raw = _agents_json([{"agent_id": "solo", "persona": "alone"}])
        result, error = AutoGraphBuilder._parse_agents(raw)
        assert result is None
        assert error is not None
        assert "At least 2" in error

    def test_duplicate_ids(self):
        raw = _agents_json(
            [
                {"agent_id": "a", "persona": "x"},
                {"agent_id": "a", "persona": "y"},
            ]
        )
        result, error = AutoGraphBuilder._parse_agents(raw)
        assert result is None
        assert error is not None
        assert "Duplicate" in error

    def test_tool_validation_passes(self):
        raw = _agents_json(
            [
                {"agent_id": "a", "tools": ["web_search"]},
                {"agent_id": "b", "tools": []},
            ]
        )
        result, _error = AutoGraphBuilder._parse_agents(raw, allowed_tools={"web_search", "shell"})
        assert result is not None

    def test_tool_validation_fails(self):
        raw = _agents_json(
            [
                {"agent_id": "a", "tools": ["bad_tool"]},
                {"agent_id": "b", "tools": []},
            ]
        )
        result, error = AutoGraphBuilder._parse_agents(raw, allowed_tools={"web_search"})
        assert result is None
        assert error is not None
        assert "Unknown tools" in error

    def test_no_tool_validation_when_none(self):
        raw = _agents_json(
            [
                {"agent_id": "a", "tools": ["anything"]},
                {"agent_id": "b", "tools": []},
            ]
        )
        result, _error = AutoGraphBuilder._parse_agents(raw, allowed_tools=None)
        assert result is not None


# ─────────────────── AutoGraphBuilder init ────────────────────────────────────


class TestAutoGraphBuilderInit:
    def test_requires_at_least_one_caller(self):
        with pytest.raises(ValueError, match="At least one"):
            AutoGraphBuilder()

    def test_sync_caller_only(self):
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"))
        assert auto._caller is not None
        assert auto._async_caller is None

    def test_default_config(self):
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"))
        assert isinstance(auto.config, AutoBuilderConfig)


# ─────────────── assemble_topology ────────────────────────────────────────────


class TestAssembleTopology:
    def test_chain_topology(self):
        response = _topology_json([["a", "b"], ["b", "c"]], "a", "c")
        auto = AutoGraphBuilder(llm_caller=_mock_caller(response))
        agents = _make_agents("a", "b", "c")

        graph = auto.assemble_topology(agents, query="Test task")

        assert "a" in graph.node_ids
        assert "b" in graph.node_ids
        assert "c" in graph.node_ids
        assert graph.start_node == "a"
        assert graph.end_node == "c"

    def test_diamond_topology(self):
        response = _topology_json(
            [["start", "left"], ["start", "right"], ["left", "end"], ["right", "end"]],
            "start",
            "end",
        )
        auto = AutoGraphBuilder(llm_caller=_mock_caller(response))
        agents = _make_agents("start", "left", "right", "end")

        graph = auto.assemble_topology(agents, query="Test diamond")

        ids = graph.node_ids
        a_com = graph.A_com
        si = ids.index("start")
        li = ids.index("left")
        ri = ids.index("right")
        ei = ids.index("end")
        assert a_com[si, li].item() > 0
        assert a_com[si, ri].item() > 0
        assert a_com[li, ei].item() > 0
        assert a_com[ri, ei].item() > 0

    def test_with_task_node(self):
        response = _topology_json([["a", "b"]], "a", "b")
        auto = AutoGraphBuilder(llm_caller=_mock_caller(response))
        agents = _make_agents("a", "b")

        graph = auto.assemble_topology(agents, query="Test query")

        assert "__task__" in graph.node_ids
        assert graph.query == "Test query"

    def test_without_task_node(self):
        response = _topology_json([["a", "b"]], "a", "b")
        config = AutoBuilderConfig(include_task_node=False)
        auto = AutoGraphBuilder(llm_caller=_mock_caller(response), config=config)
        agents = _make_agents("a", "b")

        graph = auto.assemble_topology(agents, query="Test query")

        assert "__task__" not in graph.node_ids

    def test_preserves_agent_tools(self):
        response = _topology_json(
            [["researcher", "coder"], ["coder", "reviewer"]],
            "researcher",
            "reviewer",
        )
        auto = AutoGraphBuilder(llm_caller=_mock_caller(response))
        agents = _make_agents_with_tools()

        graph = auto.assemble_topology(agents, query="Test")

        researcher = graph.get_agent_by_id("researcher")
        assert researcher is not None
        assert "web_search" in (researcher.tools or [])
        coder = graph.get_agent_by_id("coder")
        assert coder is not None
        assert "code_interpreter" in (coder.tools or [])

    def test_too_few_agents_raises(self):
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"))
        with pytest.raises(ValueError, match="At least 2"):
            auto.assemble_topology([_make_agents("solo")[0]], query="Test")

    def test_no_sync_caller_raises(self):
        async def async_caller(messages):
            return "{}"

        auto = AutoGraphBuilder(async_llm_caller=async_caller)
        with pytest.raises(ValueError, match="Sync llm_caller"):
            auto.assemble_topology(_make_agents("a", "b"), query="Test")

    def test_retry_on_bad_response(self):
        call_count = 0
        valid = _topology_json([["a", "b"]], "a", "b")

        def flaky_caller(messages):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "not json"
            return valid

        auto = AutoGraphBuilder(llm_caller=flaky_caller)
        graph = auto.assemble_topology(_make_agents("a", "b"), query="Test")
        assert graph is not None
        assert call_count == 2

    def test_all_retries_fail_raises(self):
        config = AutoBuilderConfig(max_retries=2)
        auto = AutoGraphBuilder(llm_caller=_mock_caller("bad"), config=config)
        with pytest.raises(ValueError, match="Failed to generate valid topology"):
            auto.assemble_topology(_make_agents("a", "b"), query="Test")

    def test_integrity_check(self):
        response = _topology_json([["a", "b"], ["b", "c"]], "a", "c")
        auto = AutoGraphBuilder(llm_caller=_mock_caller(response))
        graph = auto.assemble_topology(_make_agents("a", "b", "c"), query="Test")
        errors = graph.verify_integrity(raise_on_error=False)
        assert not errors


# ─────────────── assemble_full ────────────────────────────────────────────────


class TestAssembleFull:
    def _agents_resp(self, with_tools=True):
        agents = [
            {"agent_id": "planner", "persona": "a planner", "description": "Plans tasks", "tools": []},
            {
                "agent_id": "worker",
                "persona": "a worker",
                "description": "Does work",
                "tools": ["code_interpreter"] if with_tools else [],
            },
            {"agent_id": "checker", "persona": "a checker", "description": "Checks results", "tools": []},
        ]
        return _agents_json(agents)

    def _topology_resp(self):
        return _topology_json(
            [["planner", "worker"], ["worker", "checker"]],
            "planner",
            "checker",
        )

    def test_basic_full_assembly(self):
        caller = _mock_multi_caller(self._agents_resp(), self._topology_resp())
        auto = AutoGraphBuilder(llm_caller=caller)
        graph = auto.assemble_full(query="Test task")

        assert "planner" in graph.node_ids
        assert "worker" in graph.node_ids
        assert "checker" in graph.node_ids
        assert graph.start_node == "planner"
        assert graph.end_node == "checker"

    def test_tools_assigned(self):
        caller = _mock_multi_caller(self._agents_resp(with_tools=True), self._topology_resp())
        config = AutoBuilderConfig(available_tools=["code_interpreter", "web_search"])
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        graph = auto.assemble_full(query="Test task")

        worker = graph.get_agent_by_id("worker")
        assert worker is not None
        assert "code_interpreter" in (worker.tools or [])

    def test_llm_backbone_propagation(self):
        caller = _mock_multi_caller(self._agents_resp(), self._topology_resp())
        config = AutoBuilderConfig(default_llm_backbone="gpt-4o-mini")
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        graph = auto.assemble_full(query="Test task")

        for agent in graph.agents:
            if agent.agent_id != "__task__":
                assert agent.llm_backbone == "gpt-4o-mini"

    def test_temperature_propagation(self):
        caller = _mock_multi_caller(self._agents_resp(), self._topology_resp())
        config = AutoBuilderConfig(
            default_llm_backbone="gpt-4",
            default_temperature=0.3,
        )
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        graph = auto.assemble_full(query="Test task")

        for agent in graph.agents:
            if agent.agent_id != "__task__" and agent.llm_config:
                assert agent.llm_config.temperature == 0.3

    def test_diamond_full_assembly(self):
        agents_resp = _agents_json(
            [
                {"agent_id": "planner", "persona": "planner", "tools": []},
                {"agent_id": "researcher", "persona": "researcher", "tools": ["web_search"]},
                {"agent_id": "coder", "persona": "coder", "tools": ["code_interpreter"]},
                {"agent_id": "reporter", "persona": "reporter", "tools": []},
            ]
        )
        topology_resp = _topology_json(
            [["planner", "researcher"], ["planner", "coder"], ["researcher", "reporter"], ["coder", "reporter"]],
            "planner",
            "reporter",
        )
        caller = _mock_multi_caller(agents_resp, topology_resp)
        config = AutoBuilderConfig(available_tools=["web_search", "code_interpreter"])
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        graph = auto.assemble_full(query="Test diamond")

        ids = graph.node_ids
        a_com = graph.A_com
        pi = ids.index("planner")
        ri = ids.index("researcher")
        ci = ids.index("coder")
        assert a_com[pi, ri].item() > 0
        assert a_com[pi, ci].item() > 0

    def test_invalid_tool_causes_retry(self):
        bad_agents = _agents_json(
            [
                {"agent_id": "a", "persona": "a", "tools": ["nonexistent_tool"]},
                {"agent_id": "b", "persona": "b", "tools": []},
            ]
        )
        good_agents = _agents_json(
            [
                {"agent_id": "a", "persona": "a", "tools": ["web_search"]},
                {"agent_id": "b", "persona": "b", "tools": []},
            ]
        )
        topology = _topology_json([["a", "b"]], "a", "b")

        calls = []

        def caller(messages):
            calls.append(1)
            n = len(calls)
            if n == 1:
                return bad_agents
            if n == 2:
                return good_agents
            return topology

        config = AutoBuilderConfig(available_tools=["web_search"])
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        graph = auto.assemble_full(query="Test retry")
        assert graph is not None
        assert len(calls) == 3

    def test_no_sync_caller_raises(self):
        async def async_caller(messages):
            return "{}"

        auto = AutoGraphBuilder(async_llm_caller=async_caller)
        with pytest.raises(ValueError, match="Sync llm_caller"):
            auto.assemble_full(query="Test")

    def test_integrity_check(self):
        caller = _mock_multi_caller(self._agents_resp(), self._topology_resp())
        auto = AutoGraphBuilder(llm_caller=caller)
        graph = auto.assemble_full(query="Test")
        errors = graph.verify_integrity(raise_on_error=False)
        assert not errors


# ─────────────── Async tests ─────────────────────────────────────────────────


class TestAsyncAssembleTopology:
    @pytest.mark.asyncio
    async def test_async_topology(self):
        response = _topology_json([["a", "b"]], "a", "b")

        async def async_caller(messages):
            return response

        auto = AutoGraphBuilder(async_llm_caller=async_caller)
        agents = _make_agents("a", "b")
        graph = await auto.assemble_topology_async(agents, query="Async test")

        assert "a" in graph.node_ids
        assert graph.start_node == "a"

    @pytest.mark.asyncio
    async def test_async_no_caller_raises(self):
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"))
        with pytest.raises(ValueError, match="async_llm_caller"):
            await auto.assemble_topology_async(_make_agents("a", "b"), query="Test")


class TestAsyncAssembleFull:
    @pytest.mark.asyncio
    async def test_async_full(self):
        agents_resp = _agents_json(
            [
                {"agent_id": "a", "persona": "a", "tools": []},
                {"agent_id": "b", "persona": "b", "tools": []},
            ]
        )
        topology_resp = _topology_json([["a", "b"]], "a", "b")
        responses = iter([agents_resp, topology_resp])

        async def async_caller(messages):
            return next(responses)

        auto = AutoGraphBuilder(async_llm_caller=async_caller)
        graph = await auto.assemble_full_async(query="Async full test")

        assert "a" in graph.node_ids
        assert "b" in graph.node_ids


# ─────────────── _specs_to_profiles ───────────────────────────────────────────


class TestSpecsToProfiles:
    def test_basic_conversion(self):
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"))
        specs = [
            AgentSpec(agent_id="a", persona="test_a", description="Agent A"),
            AgentSpec(agent_id="b", persona="test_b", tools=["web_search"]),
        ]
        profiles = auto._specs_to_profiles(specs)
        assert len(profiles) == 2
        assert profiles[0].agent_id == "a"
        assert profiles[0].persona == "test_a"
        assert profiles[1].tools == ["web_search"]

    def test_display_name_generated(self):
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"))
        specs = [AgentSpec(agent_id="data_analyst")]
        profiles = auto._specs_to_profiles(specs)
        assert profiles[0].display_name == "Data Analyst"

    def test_llm_config_applied(self):
        config = AutoBuilderConfig(
            default_llm_backbone="gpt-4",
            default_temperature=0.5,
        )
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"), config=config)
        specs = [AgentSpec(agent_id="a")]
        profiles = auto._specs_to_profiles(specs)
        assert profiles[0].llm_backbone == "gpt-4"
        assert profiles[0].llm_config is not None
        assert profiles[0].llm_config.temperature == 0.5

    def test_no_llm_config_when_not_set(self):
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"))
        specs = [AgentSpec(agent_id="a")]
        profiles = auto._specs_to_profiles(specs)
        assert profiles[0].llm_config is None


# ─────────────── Custom Prompts ──────────────────────────────────────────────


def _capturing_caller(response: str):
    """Return a caller that records all messages and returns fixed response."""
    captured: list[list[dict[str, str]]] = []

    def caller(messages: list[dict[str, str]]) -> str:
        captured.append(list(messages))
        return response

    caller.captured = captured  # type: ignore[attr-defined,ty:unresolved-attribute]
    return caller


class TestCustomTopologyPrompt:
    """Verify custom topology prompts at config and per-call level."""

    def test_default_prompt_used_when_none(self):
        from gmas.builder.auto_builder import _TOPOLOGY_SYSTEM

        resp = _topology_json([["a", "b"]], "a", "b")
        caller = _capturing_caller(resp)
        auto = AutoGraphBuilder(llm_caller=caller)
        auto.assemble_topology(_make_agents("a", "b"), query="test")

        system_msg = caller.captured[0][0]["content"]
        assert system_msg == _TOPOLOGY_SYSTEM

    def test_config_topology_prompt(self):
        custom = "You are a custom topology designer. Return JSON."
        resp = _topology_json([["a", "b"]], "a", "b")
        caller = _capturing_caller(resp)
        config = AutoBuilderConfig(topology_prompt=custom)
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        auto.assemble_topology(_make_agents("a", "b"), query="test")

        system_msg = caller.captured[0][0]["content"]
        assert system_msg == custom

    def test_per_call_overrides_config(self):
        config_prompt = "Config-level prompt"
        call_prompt = "Per-call prompt"
        resp = _topology_json([["a", "b"]], "a", "b")
        caller = _capturing_caller(resp)
        config = AutoBuilderConfig(topology_prompt=config_prompt)
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        auto.assemble_topology(
            _make_agents("a", "b"),
            query="test",
            system_prompt=call_prompt,
        )

        system_msg = caller.captured[0][0]["content"]
        assert system_msg == call_prompt

    def test_per_call_with_no_config(self):
        call_prompt = "Just this prompt"
        resp = _topology_json([["a", "b"]], "a", "b")
        caller = _capturing_caller(resp)
        auto = AutoGraphBuilder(llm_caller=caller)
        auto.assemble_topology(
            _make_agents("a", "b"),
            query="test",
            system_prompt=call_prompt,
        )

        system_msg = caller.captured[0][0]["content"]
        assert system_msg == call_prompt


class TestCustomAgentsPrompt:
    """Verify custom agents prompts at config and per-call level."""

    def test_config_agents_prompt_with_placeholders(self):
        custom = "Design up to {max_agents} agents.\n{tools_section}\nReturn JSON."
        agents_resp = _agents_json(
            [
                {"agent_id": "a", "persona": "a", "tools": []},
                {"agent_id": "b", "persona": "b", "tools": []},
            ]
        )
        topology_resp = _topology_json([["a", "b"]], "a", "b")
        call_idx = iter([agents_resp, topology_resp])
        captured: list[list[dict[str, str]]] = []

        def caller(messages):
            captured.append(list(messages))
            return next(call_idx)

        config = AutoBuilderConfig(agents_prompt=custom, max_agents=4)
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        auto.assemble_full(query="test")

        agents_system_msg = captured[0][0]["content"]
        assert "Design up to 4 agents." in agents_system_msg
        assert "Return JSON." in agents_system_msg

    def test_config_agents_prompt_without_placeholders(self):
        custom = "You are an agent designer. Return valid JSON."
        agents_resp = _agents_json(
            [
                {"agent_id": "a", "persona": "a", "tools": []},
                {"agent_id": "b", "persona": "b", "tools": []},
            ]
        )
        topology_resp = _topology_json([["a", "b"]], "a", "b")
        call_idx = iter([agents_resp, topology_resp])

        captured: list[list[dict[str, str]]] = []

        def caller(messages):
            captured.append(list(messages))
            return next(call_idx)

        config = AutoBuilderConfig(agents_prompt=custom)
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        auto.assemble_full(query="test")

        assert captured[0][0]["content"] == custom

    def test_per_call_agents_prompt_overrides_config(self):
        config_prompt = "Config agents prompt"
        call_prompt = "Per-call agents prompt with {max_agents} agents.\n{tools_section}"
        agents_resp = _agents_json(
            [
                {"agent_id": "a", "persona": "a", "tools": []},
                {"agent_id": "b", "persona": "b", "tools": []},
            ]
        )
        topology_resp = _topology_json([["a", "b"]], "a", "b")
        call_idx = iter([agents_resp, topology_resp])

        captured: list[list[dict[str, str]]] = []

        def caller(messages):
            captured.append(list(messages))
            return next(call_idx)

        config = AutoBuilderConfig(agents_prompt=config_prompt, max_agents=7)
        auto = AutoGraphBuilder(llm_caller=caller, config=config)
        auto.assemble_full(query="test", agents_prompt=call_prompt)

        agents_system_msg = captured[0][0]["content"]
        assert "Per-call agents prompt with 7 agents." in agents_system_msg
        assert config_prompt not in agents_system_msg

    def test_full_assembly_separate_prompts(self):
        """Both topology_prompt and agents_prompt can be set independently."""
        agents_resp = _agents_json(
            [
                {"agent_id": "x", "persona": "x", "tools": []},
                {"agent_id": "y", "persona": "y", "tools": []},
            ]
        )
        topology_resp = _topology_json([["x", "y"]], "x", "y")
        call_idx = iter([agents_resp, topology_resp])

        captured: list[list[dict[str, str]]] = []

        def caller(messages):
            captured.append(list(messages))
            return next(call_idx)

        auto = AutoGraphBuilder(llm_caller=caller)
        graph = auto.assemble_full(
            query="test",
            agents_prompt="Custom agents instructions",
            topology_prompt="Custom topology instructions",
        )

        assert captured[0][0]["content"] == "Custom agents instructions"
        assert captured[1][0]["content"] == "Custom topology instructions"
        assert "x" in graph.node_ids
        assert "y" in graph.node_ids


class TestCustomPromptAsync:
    """Verify custom prompts work with async API."""

    @pytest.mark.asyncio
    async def test_async_topology_custom_prompt(self):
        call_prompt = "Async custom topology prompt"
        resp = _topology_json([["a", "b"]], "a", "b")

        captured: list[list[dict[str, str]]] = []

        async def async_caller(messages):
            captured.append(list(messages))
            return resp

        auto = AutoGraphBuilder(async_llm_caller=async_caller)
        await auto.assemble_topology_async(
            _make_agents("a", "b"),
            query="test",
            system_prompt=call_prompt,
        )

        assert captured[0][0]["content"] == call_prompt

    @pytest.mark.asyncio
    async def test_async_full_custom_prompts(self):
        agents_resp = _agents_json(
            [
                {"agent_id": "a", "persona": "a", "tools": []},
                {"agent_id": "b", "persona": "b", "tools": []},
            ]
        )
        topology_resp = _topology_json([["a", "b"]], "a", "b")
        responses = iter([agents_resp, topology_resp])

        captured: list[list[dict[str, str]]] = []

        async def async_caller(messages):
            captured.append(list(messages))
            return next(responses)

        auto = AutoGraphBuilder(async_llm_caller=async_caller)
        await auto.assemble_full_async(
            query="test",
            agents_prompt="Async agents prompt",
            topology_prompt="Async topology prompt",
        )

        assert captured[0][0]["content"] == "Async agents prompt"
        assert captured[1][0]["content"] == "Async topology prompt"

    @pytest.mark.asyncio
    async def test_async_config_prompt_used(self):
        custom = "Async config topology prompt"
        resp = _topology_json([["a", "b"]], "a", "b")

        captured: list[list[dict[str, str]]] = []

        async def async_caller(messages):
            captured.append(list(messages))
            return resp

        config = AutoBuilderConfig(topology_prompt=custom)
        auto = AutoGraphBuilder(async_llm_caller=async_caller, config=config)
        await auto.assemble_topology_async(
            _make_agents("a", "b"),
            query="test",
        )

        assert captured[0][0]["content"] == custom


class TestResolvePromptHelpers:
    """Direct tests for _resolve_topology_prompt and _resolve_agents_prompt."""

    def test_resolve_topology_default(self):
        from gmas.builder.auto_builder import _TOPOLOGY_SYSTEM

        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"))
        assert auto._resolve_topology_prompt(None) == _TOPOLOGY_SYSTEM

    def test_resolve_topology_config(self):
        config = AutoBuilderConfig(topology_prompt="config prompt")
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"), config=config)
        assert auto._resolve_topology_prompt(None) == "config prompt"

    def test_resolve_topology_override(self):
        config = AutoBuilderConfig(topology_prompt="config prompt")
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"), config=config)
        assert auto._resolve_topology_prompt("override") == "override"

    def test_resolve_agents_default(self):
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"))
        tools = _resolve_tools(None)
        result = auto._resolve_agents_prompt(None, tools)
        assert "agents" in result.lower() or "JSON" in result

    def test_resolve_agents_config_with_placeholders(self):
        config = AutoBuilderConfig(
            agents_prompt="Max {max_agents} agents.\n{tools_section}",
            max_agents=3,
        )
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"), config=config)
        tools = {"web_search": "Search the web"}
        result = auto._resolve_agents_prompt(None, tools)
        assert "Max 3 agents." in result
        assert "web_search" in result

    def test_resolve_agents_override_beats_config(self):
        config = AutoBuilderConfig(agents_prompt="Config prompt")
        auto = AutoGraphBuilder(llm_caller=_mock_caller("{}"), config=config)
        result = auto._resolve_agents_prompt("Override {max_agents}", {})
        assert result.startswith("Override")
