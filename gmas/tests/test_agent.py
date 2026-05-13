from unittest.mock import patch

import torch

from gmas.core.agent import (
    AgentLLMConfig,
    AgentProfile,
    TaskNode,
    extract_agent_profiles,
)


class TestAgentLLMConfig:
    """LLM agent configuration."""

    def test_resolve_api_key_from_env(self):
        """resolve_api_key reads key from environment variable."""
        cfg = AgentLLMConfig(api_key="$MY_KEY")
        with patch.dict("os.environ", {"MY_KEY": "secret123"}):
            assert cfg.resolve_api_key() == "secret123"

    def test_resolve_api_key_direct(self):
        """resolve_api_key returns direct key value."""
        cfg = AgentLLMConfig(api_key="sk-abc")
        assert cfg.resolve_api_key() == "sk-abc"

    def test_resolve_api_key_none(self):
        """resolve_api_key returns None when no key is set."""
        cfg = AgentLLMConfig()
        assert cfg.resolve_api_key() is None

    def test_is_configured(self):
        """is_configured: True when model_name or base_url is set."""
        assert AgentLLMConfig().is_configured() is False
        assert AgentLLMConfig(model_name="gpt-4").is_configured() is True
        assert AgentLLMConfig(base_url="http://localhost").is_configured() is True

    def test_to_generation_params_full(self):
        """to_generation_params with all parameters set."""
        cfg = AgentLLMConfig(
            max_tokens=100,
            temperature=0.7,
            top_p=0.9,
            stop_sequences=["END"],
            extra_params={"seed": 42},
        )
        params = cfg.to_generation_params()
        assert params["max_tokens"] == 100
        assert params["temperature"] == 0.7
        assert params["top_p"] == 0.9
        assert params["stop"] == ["END"]
        assert params["seed"] == 42

    def test_to_generation_params_empty(self):
        """to_generation_params returns empty dict when no params are set."""
        assert AgentLLMConfig().to_generation_params() == {}


class TestAgentProfile:
    """Agent profile — methods and serialization."""

    def test_defaults(self):
        """Default values of agent profile fields."""
        p = AgentProfile(agent_id="a1", display_name="Agent 1")
        assert p.persona == ""
        assert p.description == ""
        assert p.llm_backbone is None
        assert p.llm_config is None
        assert p.tools == []
        assert p.embedding is None
        assert p.state == []
        assert p.hidden_state is None
        assert p.input_schema is None
        assert p.output_schema is None

    def test_role_property(self):
        """Role property is an alias for agent_id."""
        p = AgentProfile(agent_id="writer", display_name="Writer")
        assert p.role == "writer"

    def test_has_tools(self):
        """has_tools returns True only when tools list is non-empty."""
        assert AgentProfile(agent_id="a", display_name="A").has_tools() is False
        assert AgentProfile(agent_id="a", display_name="A", tools=["search"]).has_tools() is True

    def test_get_tool_names_strings(self):
        """get_tool_names returns string tool names as-is."""
        p = AgentProfile(agent_id="a", display_name="A", tools=["search", "calc"])
        assert p.get_tool_names() == ["search", "calc"]

    def test_get_model_name_from_config(self):
        """get_model_name reads model name from llm_config."""
        cfg = AgentLLMConfig(model_name="gpt-4")
        p = AgentProfile(agent_id="a", display_name="A", llm_config=cfg)
        assert p.get_model_name() == "gpt-4"

    def test_get_model_name_from_backbone(self):
        """get_model_name falls back to llm_backbone."""
        p = AgentProfile(agent_id="a", display_name="A", llm_backbone="claude-3")
        assert p.get_model_name() == "claude-3"

    def test_get_llm_config_existing(self):
        """get_llm_config returns the existing config object."""
        cfg = AgentLLMConfig(model_name="gpt-4")
        p = AgentProfile(agent_id="a", display_name="A", llm_config=cfg)
        assert p.get_llm_config() is cfg

    def test_get_llm_config_default(self):
        """get_llm_config creates a default config from llm_backbone."""
        p = AgentProfile(agent_id="a", display_name="A", llm_backbone="claude-3")
        result = p.get_llm_config()
        assert isinstance(result, AgentLLMConfig)
        assert result.model_name == "claude-3"

    def test_has_custom_llm(self):
        """has_custom_llm returns True only when llm_config is set and configured."""
        assert AgentProfile(agent_id="a", display_name="A").has_custom_llm() is False
        cfg = AgentLLMConfig(model_name="gpt-4")
        assert AgentProfile(agent_id="a", display_name="A", llm_config=cfg).has_custom_llm() is True

    def test_with_llm_config(self):
        """with_llm_config returns an immutable copy with the new config."""
        p = AgentProfile(agent_id="a", display_name="A")
        cfg = AgentLLMConfig(model_name="gpt-4")
        p2 = p.with_llm_config(cfg)
        assert p2.llm_config is cfg
        assert p.llm_config is None

    def test_with_embedding(self):
        """with_embedding returns a copy with the given embedding tensor."""
        p = AgentProfile(agent_id="a", display_name="A")
        emb = torch.randn(16)
        p2 = p.with_embedding(emb)
        assert p2.embedding is emb
        assert p.embedding is None

    def test_state_methods(self):
        """with_state, append_state, and clear_state work immutably."""
        p = AgentProfile(agent_id="a", display_name="A")
        p2 = p.with_state([{"role": "user", "content": "hi"}])
        assert len(p2.state) == 1
        assert p.state == []

        p3 = p2.append_state({"role": "assistant", "content": "hello"})
        assert len(p3.state) == 2
        assert len(p2.state) == 1

        p4 = p3.clear_state()
        assert p4.state == []
        assert len(p3.state) == 2

    def test_to_text(self):
        """to_text produces a human-readable profile representation."""
        p = AgentProfile(
            agent_id="a",
            display_name="Writer",
            persona="a creative writer",
            description="Writes stories",
            tools=["search"],
            llm_backbone="gpt-4",
        )
        text = p.to_text()
        assert "Writer" in text
        assert "a creative writer" in text
        assert "Writes stories" in text
        assert "Tools: search" in text
        assert "LLM Backbone: gpt-4" in text

    def test_to_dict(self):
        """to_dict serializes the profile to a dictionary."""
        emb = torch.tensor([1.0, 2.0])
        cfg = AgentLLMConfig(model_name="gpt-4")
        p = AgentProfile(
            agent_id="a",
            display_name="A",
            persona="test",
            embedding=emb,
            llm_config=cfg,
        )
        d = p.to_dict()
        assert d["agent_id"] == "a"
        assert d["display_name"] == "A"
        assert d["persona"] == "test"
        assert d["embedding"] == [1.0, 2.0]
        assert "llm_config" in d
        assert d["llm_config"]["model_name"] == "gpt-4"


class TestTaskNode:
    """Virtual task node."""

    def test_defaults(self):
        """Default fields of TaskNode."""
        t = TaskNode(query="Solve X")
        assert t.agent_id == "__task__"
        assert t.type == "task"
        assert t.query == "Solve X"
        assert t.display_name == "Task"
        assert t.persona == ""
        assert t.embedding is None
        assert t.tools == []
        assert t.state == []

    def test_to_text(self):
        """to_text includes description and query."""
        t = TaskNode(query="Solve X", description="Important task")
        text = t.to_text()
        assert "Important task" in text
        assert "Task: Solve X" in text

    def test_to_text_empty_query(self):
        """to_text shows (unspecified) for blank query."""
        t = TaskNode(query="   ")
        assert "(unspecified)" in t.to_text()

    def test_with_embedding(self):
        """with_embedding returns a copy with the given embedding tensor."""
        t = TaskNode(query="Q")
        emb = torch.randn(8)
        t2 = t.with_embedding(emb)
        assert t2.embedding is emb
        assert t.embedding is None


class TestExtractAgentProfiles:
    """Parsing agents from a dictionary."""

    def test_basic_extraction(self):
        """Basic parsing from agents_data dict."""
        data = {
            "agents": [
                {"agent": {"role": "writer", "name": "Writer", "persona": "writes"}},
                {"agent": {"role": "reviewer", "name": "Reviewer"}},
            ]
        }
        profiles = extract_agent_profiles(data)
        assert len(profiles) == 2
        assert profiles[0].agent_id == "writer"
        assert profiles[0].display_name == "Writer"
        assert profiles[1].agent_id == "reviewer"

    def test_duplicate_agents(self):
        """Duplicate roles — first occurrence is kept."""
        data = {
            "agents": [
                {"agent": {"role": "writer", "name": "First"}},
                {"agent": {"role": "writer", "name": "Second"}},
            ]
        }
        profiles = extract_agent_profiles(data)
        assert len(profiles) == 1
        assert profiles[0].display_name == "First"

    def test_invalid_entries(self):
        """Invalid entries are silently skipped."""
        data = {
            "agents": [
                "not a dict",
                {"agent": "not a dict either"},
                {"agent": {"no_role": True}},
                {"agent": {"role": "valid", "name": "OK"}},
            ]
        }
        profiles = extract_agent_profiles(data)
        assert len(profiles) == 1
        assert profiles[0].agent_id == "valid"

    def test_tools_extraction(self):
        """Tools are extracted from various formats and deduplicated."""
        data = {
            "agents": [
                {
                    "agent": {
                        "role": "a1",
                        "name": "A",
                        "tools": [
                            "search",
                            {"name": "calc"},
                            {"tool": "browser"},
                            42,
                            "search",
                        ],
                    }
                }
            ]
        }
        profiles = extract_agent_profiles(data)
        tools = profiles[0].tools
        assert "search" in tools
        assert "calc" in tools
        assert "browser" in tools
        assert len(tools) == 3

    def test_llm_backbone_extraction(self):
        """LLM backbone is extracted from various field formats."""
        data_str = {"agents": [{"agent": {"role": "a", "name": "A", "llm": "gpt-4"}}]}
        p1 = extract_agent_profiles(data_str)
        assert p1[0].llm_backbone == "gpt-4"

        data_dict = {"agents": [{"agent": {"role": "b", "name": "B", "model": {"name": "claude-3"}}}]}
        p2 = extract_agent_profiles(data_dict)
        assert p2[0].llm_backbone == "claude-3"


class TestAgentProfileMissingCoverage:
    """Tests for missing lines in core/agent.py."""

    def test_get_tool_names_with_base_tool_object(self):
        """get_tool_names when tools contains BaseTool objects (lines 143-144)."""
        from gmas.tools.shell import ShellTool

        shell = ShellTool()
        agent = AgentProfile(agent_id="test", display_name="Test", tools=[shell])
        names = agent.get_tool_names()
        assert "shell" in names

    def test_get_tool_objects_with_base_tool_object(self):
        """get_tool_objects when tools contains BaseTool objects (lines 182-183)."""
        from gmas.tools.shell import ShellTool

        shell = ShellTool()
        agent = AgentProfile(agent_id="test", display_name="Test", tools=[shell])
        objects = agent.get_tool_objects()
        assert shell in objects

    def test_with_hidden_state(self):
        """with_hidden_state returns updated copy (line 240)."""
        agent = AgentProfile(agent_id="test", display_name="Test")
        hidden = torch.zeros(10)
        new_agent = agent.with_hidden_state(hidden)
        assert new_agent.hidden_state is not None
        assert torch.equal(new_agent.hidden_state, hidden)

    def test_to_dict_with_schemas(self):
        """to_dict includes llm_config, input_schema, output_schema (lines 266, 268)."""
        agent = AgentProfile(
            agent_id="test",
            display_name="Test",
            llm_config=AgentLLMConfig(model_name="gpt-4"),
            input_schema={"type": "object"},
            output_schema={"type": "string"},
        )
        d = agent.to_dict()
        assert "llm_config" in d
        assert "input_schema" in d
        assert "output_schema" in d

    def test_extract_llm_backbone_dict_no_model_name_type(self):
        """_extract_llm_backbone when candidate is a dict with no model/name/type key (line 366)."""
        from gmas.core.agent import _extract_llm_backbone

        result = _extract_llm_backbone({"llm": {"unknown_key": "value"}})
        assert result is None
