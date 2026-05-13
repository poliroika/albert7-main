"""Tests for the tools module."""

from collections.abc import Generator
from typing import TYPE_CHECKING

import pytest

from gmas.tools import (
    DuckDuckGoProvider,
    FunctionTool,
    SearchProvider,
    ShellTool,
    ToolCall,
    ToolRegistry,
    ToolResult,
    WebSearchTool,
    create_tool_from_config,
)

if TYPE_CHECKING:
    from gmas.tools.web_search import WebSearchTool as WebSearchToolType
else:
    WebSearchToolType = WebSearchTool


def _has_selenium_and_browser() -> bool:
    """Check whether selenium is importable AND at least one browser binary exists."""
    try:
        from selenium import webdriver as _wd  # noqa: F401
    except ImportError:
        return False

    import shutil

    candidates = [
        "google-chrome",
        "chromium",
        "chromium-browser",
        "firefox",
        "msedge",
    ]
    import sys

    if sys.platform == "win32":
        import os
        from pathlib import Path

        for prog in (
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe"),
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        ):
            if Path(prog).is_file():
                return True
    return any(shutil.which(name) for name in candidates)


selenium_required = pytest.mark.skipif(
    not _has_selenium_and_browser(),
    reason="Selenium not installed or no browser binary (chrome/firefox/edge) found on this machine",
)


class TestToolCall:
    """Tests for ToolCall."""

    def test_parse_xml_format(self):
        """Parsing tool_call in XML format."""
        response = """
Some text before.
<tool_call>
{"name": "test_tool", "arguments": {"arg1": "value1"}}
</tool_call>
Some text after.
"""
        calls = ToolCall.parse_from_response(response)
        assert len(calls) == 1
        assert calls[0].name == "test_tool"
        assert calls[0].arguments == {"arg1": "value1"}

    def test_parse_code_block_format(self):
        """Parsing tool_call in code block format."""
        response = """
```tool_call
{"name": "another_tool", "arguments": {"x": 42}}
```
"""
        calls = ToolCall.parse_from_response(response)
        assert len(calls) == 1
        assert calls[0].name == "another_tool"
        assert calls[0].arguments == {"x": 42}

    def test_parse_multiple_calls(self):
        """Parsing multiple tool_calls."""
        response = """
<tool_call>
{"name": "tool1", "arguments": {}}
</tool_call>
<tool_call>
{"name": "tool2", "arguments": {"a": 1}}
</tool_call>
"""
        calls = ToolCall.parse_from_response(response)
        assert len(calls) == 2
        assert calls[0].name == "tool1"
        assert calls[1].name == "tool2"

    def test_parse_no_calls(self):
        """Response without tool_call."""
        response = "Just a regular response without any tools."
        calls = ToolCall.parse_from_response(response)
        assert len(calls) == 0

    def test_parse_invalid_json(self):
        """Invalid JSON is ignored."""
        response = """
<tool_call>
{invalid json here}
</tool_call>
"""
        calls = ToolCall.parse_from_response(response)
        assert len(calls) == 0

    def test_parse_code_block_invalid_json_skipped(self):
        """Lines 63-65: Invalid JSON in code block format is skipped silently."""
        response = """
```tool_call
{not valid json!
```
"""
        calls = ToolCall.parse_from_response(response)
        assert len(calls) == 0


class TestToolResult:
    """Tests for ToolResult."""

    def test_success_message(self):
        """Formatting a successful result."""
        result = ToolResult(tool_name="test", success=True, output="Hello")
        msg = result.to_message()
        assert '<tool_result name="test">' in msg
        assert "Hello" in msg
        assert "</tool_result>" in msg

    def test_error_message(self):
        """Formatting an error."""
        result = ToolResult(tool_name="test", success=False, error="Something went wrong")
        msg = result.to_message()
        assert '<tool_error name="test">' in msg
        assert "Something went wrong" in msg
        assert "</tool_error>" in msg


class TestShellTool:
    """Tests for ShellTool."""

    def test_name_and_description(self):
        """Checking name and description."""
        tool = ShellTool()
        assert tool.name == "shell"
        assert "shell command" in tool.description.lower()

    def test_execute_echo(self):
        """Executing a simple echo command."""
        tool = ShellTool(timeout=5)
        result = tool.execute(command="echo Hello")
        assert result.success is True
        assert "Hello" in result.output

    def test_execute_no_command(self):
        """Error when no command provided."""
        tool = ShellTool()
        result = tool.execute()
        assert result.success is False
        assert result.error is not None
        assert "No command" in result.error

    def test_allowed_commands(self):
        """Command whitelist."""
        tool = ShellTool(allowed_commands=["echo"])

        # Allowed command
        result = tool.execute(command="echo test")
        assert result.success is True

        # Forbidden command
        result = tool.execute(command="rm -rf /")
        assert result.success is False
        assert result.error is not None
        assert "not allowed" in result.error

    def test_parameters_schema(self):
        """Parameters schema."""
        tool = ShellTool()
        schema = tool.parameters_schema
        assert schema["type"] == "object"
        assert "command" in schema["properties"]


class TestFunctionTool:
    """Tests for FunctionTool."""

    def test_register_decorator(self):
        """Registration via decorator."""
        tool = FunctionTool()

        @tool.register
        def my_func(x: int) -> int:
            """Double the input."""
            return x * 2

        assert "my_func" in tool.list_functions()

    def test_register_with_custom_name(self):
        """Registration with a custom name."""
        tool = FunctionTool()

        @tool.register(name="custom_name")
        def some_func():
            pass

        assert "custom_name" in tool.list_functions()
        assert "some_func" not in tool.list_functions()

    def test_execute_function(self):
        """Executing a registered function."""
        tool = FunctionTool()

        @tool.register
        def add(a: int, b: int) -> int:
            return a + b

        result = tool.execute(function="add", a=2, b=3)
        assert result.success is True
        assert result.output == "5"

    def test_execute_unknown_function(self):
        """Error when calling an unregistered function."""
        tool = FunctionTool()
        result = tool.execute(function="unknown")
        assert result.success is False
        assert result.error is not None
        assert "not found" in result.error

    def test_execute_no_function_name(self):
        """Error when no function name provided."""
        tool = FunctionTool()
        result = tool.execute()
        assert result.success is False
        assert result.error is not None
        assert "No function name" in result.error


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def test_register_and_get(self):
        """Registering and getting a tool."""
        registry = ToolRegistry()
        tool = ShellTool()
        registry.register(tool)

        assert registry.has("shell")
        assert registry.get("shell") is tool

    def test_list_tools(self):
        """Listing registered tools."""
        registry = ToolRegistry()
        registry.register(ShellTool())
        registry.register(FunctionTool())

        tools = registry.list_tools()
        assert "shell" in tools
        assert "function_calling" in tools

    def test_execute(self):
        """Executing a tool through the registry."""
        registry = ToolRegistry()
        registry.register(ShellTool(timeout=5))

        call = ToolCall(name="shell", arguments={"command": "echo test"})
        result = registry.execute(call)
        assert result.success is True
        assert "test" in result.output

    def test_execute_unknown_tool(self):
        """Error when calling an unregistered tool."""
        registry = ToolRegistry()
        call = ToolCall(name="unknown", arguments={})
        result = registry.execute(call)
        assert result.success is False
        assert result.error is not None
        assert "not found" in result.error

    def test_execute_all(self):
        """Executing multiple calls."""
        registry = ToolRegistry()
        registry.register(ShellTool(timeout=5))

        calls = [
            ToolCall(name="shell", arguments={"command": "echo first"}),
            ToolCall(name="shell", arguments={"command": "echo second"}),
        ]
        results = registry.execute_all(calls)
        assert len(results) == 2
        assert all(r.success for r in results)

    def test_function_decorator(self):
        """Registering a function via registry decorator."""
        registry = ToolRegistry()

        @registry.function
        def greet(name: str) -> str:
            """Say hello."""
            return f"Hello, {name}!"

        assert registry.has("greet")
        result = registry.execute(ToolCall(name="greet", arguments={"name": "World"}))
        assert result.success is True
        assert result.output == "Hello, World!"

    def test_format_tools_prompt(self):
        """Formatting tools prompt."""
        registry = ToolRegistry()
        registry.register(ShellTool())

        prompt = registry.format_tools_prompt(["shell"])
        assert "Available tools:" in prompt
        assert "shell" in prompt
        assert "<tool_call>" in prompt

    def test_get_tools_for_agent(self):
        """Getting tools for an agent."""
        registry = ToolRegistry()
        registry.register(ShellTool())
        registry.register(FunctionTool())

        # Agent with both tools
        tools = registry.get_tools_for_agent(["shell", "function_calling"])
        assert len(tools) == 2

        # Agent with shell only
        tools = registry.get_tools_for_agent(["shell"])
        assert len(tools) == 1
        assert tools[0].name == "shell"

        # Agent with nonexistent tool
        tools = registry.get_tools_for_agent(["unknown"])
        assert len(tools) == 0

    def test_to_schemas(self):
        """Serialization to JSON Schema."""
        registry = ToolRegistry()
        registry.register(ShellTool())

        schemas = registry.to_schemas(["shell"])
        assert len(schemas) == 1
        assert schemas[0]["name"] == "shell"
        assert "description" in schemas[0]
        assert "parameters" in schemas[0]


class TestToolRegistryMissingCoverage:
    """Tests for missing lines in tools/base.py."""

    def test_parse_from_response_invalid_json_code_block(self):
        """ToolCall.parse_from_response with invalid JSON in code block (lines 63-65)."""
        text = "```tool_call\nnot valid json\n```"
        calls = ToolCall.parse_from_response(text)
        assert calls == []  # Invalid JSON is skipped (lines 63-65)

    def test_parameters_schema_default(self):
        """BaseTool.parameters_schema returns default when not overridden."""
        # Create a minimal concrete tool that doesn't override parameters_schema
        from gmas.tools.base import BaseTool

        class MinimalTool(BaseTool):
            @property
            def name(self) -> str:
                return "minimal"

            @property
            def description(self) -> str:
                return "A minimal tool"

            def execute(self, **kwargs):
                from gmas.tools.base import ToolResult

                return ToolResult(tool_name="minimal", success=True, output="ok")

        tool = MinimalTool()
        schema = tool.parameters_schema  # line 108
        assert schema == {"type": "object", "properties": {}}

    def test_function_decorator_with_name(self):
        """ToolRegistry.function called with name returns decorator (line 196)."""
        registry = ToolRegistry()

        @registry.function(name="named_fn")  # func=None, returns decorator
        def my_fn(x: str) -> str:
            return x

        assert registry.has("named_fn")

    def test_execute_with_exception(self):
        """ToolRegistry.execute when tool raises ValueError (lines 218-219)."""
        from gmas.tools.base import BaseTool

        class ErrorTool(BaseTool):
            @property
            def name(self) -> str:
                return "error_tool"

            @property
            def description(self) -> str:
                return "Tool that raises"

            def execute(self, **kwargs):
                msg = "Intentional error"
                raise ValueError(msg)

        registry = ToolRegistry()
        registry.register(ErrorTool())
        result = registry.execute(ToolCall(name="error_tool", arguments={}))
        assert result.success is False
        assert result.error is not None
        assert "Intentional error" in result.error

    def test_get_tools_all(self):
        """ToolRegistry.get_tools with None returns all tools (line 245)."""
        registry = ToolRegistry()
        registry.register(ShellTool())
        tools = registry.get_tools(None)  # line 245
        assert len(tools) >= 1
        assert any(t.name == "shell" for t in tools)

    def test_to_openai_schemas(self):
        """ToolRegistry.to_openai_schemas (lines 259-260)."""
        registry = ToolRegistry()
        registry.register(ShellTool())
        schemas = registry.to_openai_schemas(["shell"])  # lines 259-260
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "shell"

    def test_format_tools_prompt_no_tools(self):
        """ToolRegistry.format_tools_prompt with no tools (line 309)."""
        registry = ToolRegistry()
        prompt = registry.format_tools_prompt()
        assert prompt == "No tools available."  # line 309

    def test_register_tool_global(self):
        """register_tool registers in global registry (lines 362-363)."""
        from gmas.tools.base import get_registry, register_tool

        tool = ShellTool()
        result = register_tool(tool)  # lines 362-363
        assert result is tool
        assert get_registry().has("shell")

    def test_tool_global_decorator(self):
        """@tool decorator registers function in global registry (line 389)."""
        from gmas.tools.base import get_registry
        from gmas.tools.base import tool as tool_decorator

        @tool_decorator  # line 389
        def decorated_fn(x: str) -> str:
            """Decorated function."""
            return x

        assert get_registry().has("decorated_fn")


class TestWebSearchTool:
    """Tests for WebSearchTool."""

    def test_name_and_description(self):
        """Checking name and description."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool()
        assert tool.name == "web_search"
        assert "search" in tool.description.lower()
        assert "web" in tool.description.lower()

    def test_parameters_schema(self):
        """Parameters schema."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool()
        schema = tool.parameters_schema
        assert schema["type"] == "object"
        assert "query" in schema["properties"]
        assert "url" in schema["properties"]
        assert "fetch_content" in schema["properties"]

    def test_execute_no_query(self):
        """Error when no query provided."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool()
        result = tool.execute()
        assert result.success is False
        assert result.error is not None
        assert "No" in result.error
        assert "provided" in result.error

    def test_execute_empty_query(self):
        """Error when empty query provided."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool()
        result = tool.execute(query="")
        assert result.success is False
        assert result.error is not None
        assert "No" in result.error
        assert "provided" in result.error

    def test_execute_with_mock_provider(self):
        """Execution with a mock provider."""

        class MockProvider(SearchProvider):
            def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
                return [
                    {
                        "title": "Test Result 1",
                        "url": "https://example.com/1",
                        "snippet": f"This is a result for: {query}",
                    },
                    {
                        "title": "Test Result 2",
                        "url": "https://example.com/2",
                        "snippet": "Another test result snippet",
                    },
                ]

        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool(provider=MockProvider())
        result = tool.execute(query="test query")

        assert result.success is True
        assert "Test Result 1" in result.output
        assert "Test Result 2" in result.output
        assert "https://example.com/1" in result.output
        assert "test query" in result.output

    def test_execute_with_empty_results(self):
        """Handling empty results."""

        class EmptyProvider(SearchProvider):
            def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
                return []

        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool(provider=EmptyProvider())
        result = tool.execute(query="no results query")

        assert result.success is True
        assert "No results found" in result.output

    def test_max_results_limit(self):
        """Limiting the number of results."""

        class ManyResultsProvider(SearchProvider):
            def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
                # Return as many results as requested
                return [
                    {
                        "title": f"Result {i}",
                        "url": f"https://example.com/{i}",
                        "snippet": f"Snippet {i}",
                    }
                    for i in range(max_results)
                ]

        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool(provider=ManyResultsProvider(), max_results=3)
        result = tool.execute(query="test")

        assert result.success is True
        assert "Found 3 result(s)" in result.output

    def test_fetch_content_with_mock(self):
        """Test fetch_content with mock content."""

        class ContentProvider(SearchProvider):
            def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
                return [
                    {
                        "title": "Page with Content",
                        "url": "https://example.com/page",
                        "snippet": "Short snippet",
                        "content": "This is the full page content that was fetched from the URL.",
                    }
                ]

        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool(provider=ContentProvider(), fetch_content=True)
        result = tool.execute(query="test")

        assert result.success is True
        assert "Page with Content" in result.output
        assert "full page content" in result.output

    def test_url_parameter(self):
        """Test url parameter for reading a specific page."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool()
        # Non-existent URL, but we check that the parameter is handled
        result = tool.execute(url="https://nonexistent.invalid/page")
        # Should be a network error, not a validation error
        assert result.success is False
        assert result.error is not None
        assert "Failed to fetch" in result.error or "error" in result.error.lower()

    def test_to_openai_schema(self):
        """Serialization to OpenAI format."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = WebSearchTool()
        schema = tool.to_openai_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "web_search"
        assert "description" in schema["function"]
        assert "parameters" in schema["function"]

    def test_registry_integration(self):
        """Integration with ToolRegistry."""

        class MockProvider(SearchProvider):
            def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
                return [{"title": "Mock", "url": "https://mock.com", "snippet": "Mock result"}]

        assert WebSearchTool is not None, "WebSearchTool is not available"
        registry = ToolRegistry()
        registry.register(WebSearchTool(provider=MockProvider()))

        assert registry.has("web_search")

        call = ToolCall(name="web_search", arguments={"query": "test"})
        result = registry.execute(call)

        assert result.success is True
        assert "Mock" in result.output


class TestDuckDuckGoProvider:
    """Tests for DuckDuckGoProvider."""

    def test_initialization(self):
        """Provider initialization."""
        provider = DuckDuckGoProvider(timeout=5)
        assert provider._timeout == 5

    def test_search_returns_list(self):
        """The search method returns a list."""
        provider = DuckDuckGoProvider(timeout=5)
        # No real request in unit tests —
        # just check that the method exists and returns the expected type
        results = provider.search("python", max_results=3)
        assert isinstance(results, list)


# ======================================================================
# Tests for dict config and tool factory
# ======================================================================


class TestToolConfig:
    """Tests for creating tools from dict config."""

    def test_create_web_search_from_config_basic(self):
        """Creating WebSearchTool from minimal config."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = create_tool_from_config({"name": "web_search"})
        assert tool is not None
        assert tool.name == "web_search"
        assert isinstance(tool, WebSearchToolType)

    def test_create_web_search_with_deep_search(self):
        """Creating WebSearchTool with deep_search='selenium' from config."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = create_tool_from_config(
            {
                "name": "web_search",
                "deep_search": "selenium",
                "browser_config": {"headless": True},
            }
        )
        assert tool is not None
        assert isinstance(tool, WebSearchToolType)
        if isinstance(tool, WebSearchToolType):
            assert tool._browser_fetcher is not None
            assert tool._deep_search == "selenium"

    def test_create_web_search_with_params(self):
        """Creating WebSearchTool with parameters from config."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        tool = create_tool_from_config(
            {
                "name": "web_search",
                "max_results": 10,
                "fetch_content": True,
                "timeout": 30,
            }
        )
        assert isinstance(tool, WebSearchToolType)
        if isinstance(tool, WebSearchToolType):
            assert tool._max_results == 10
            assert tool._fetch_content is True
            assert tool._timeout == 30

    def test_create_unknown_tool_returns_none(self):
        """Unknown tool returns None."""
        tool = create_tool_from_config({"name": "unknown_tool_xyz"})
        assert tool is None

    def test_create_tool_empty_config(self):
        """Empty config returns None."""
        tool = create_tool_from_config({})
        assert tool is None

    def test_agent_profile_with_dict_tool(self):
        """AgentProfile with dict-config tool."""
        from gmas.core.agent import AgentProfile

        agent = AgentProfile(
            agent_id="browser",
            display_name="Browser Agent",
            tools=[{"name": "web_search", "deep_search": "selenium"}],
        )

        assert agent.get_tool_names() == ["web_search"]
        tool_objects = agent.get_tool_objects()
        assert len(tool_objects) == 1
        assert isinstance(tool_objects[0], WebSearchToolType)
        assert tool_objects[0].name == "web_search"
        if isinstance(tool_objects[0], WebSearchToolType):
            assert tool_objects[0]._browser_fetcher is not None

    def test_agent_profile_mixed_tools(self):
        """AgentProfile with mixed tool formats."""
        from gmas.core.agent import AgentProfile
        from gmas.tools import get_registry

        # Register shell in global registry
        registry = get_registry()
        registry.register(ShellTool(timeout=5))

        agent = AgentProfile(
            agent_id="mixed",
            display_name="Mixed Agent",
            tools=[
                "shell",
                {"name": "web_search", "max_results": 3},
            ],
        )

        names = agent.get_tool_names()
        assert "shell" in names
        assert "web_search" in names

        objects = agent.get_tool_objects()
        assert len(objects) == 2

    def test_schema_includes_action_when_deep_search(self):
        """Schema from dict-config with deep_search includes action."""
        tool = create_tool_from_config(
            {
                "name": "web_search",
                "deep_search": "selenium",
            }
        )
        assert tool is not None
        props = tool.parameters_schema["properties"]
        assert "action" in props
        assert "selector" in props


# ======================================================================
# Real Selenium tests (no mocks, real browser)
# ======================================================================


@selenium_required
class TestWebSearchSeleniumReal:
    """
    Real tests for WebSearchTool with Selenium.

    Use a real headless browser to interact with web pages.
    Skipped if Selenium is not installed.
    Uses browser="auto" — Selenium Manager handles browser/driver discovery.
    """

    # Base URL used for browser-level tests.  Overridable via env var so the
    # suite works on networks where certain domains are DNS-blocked.
    import os as _os

    _TEST_URL: str = _os.environ.get("SELENIUM_TEST_URL", "https://httpbin.org")
    _TEST_TITLE: str = _os.environ.get("SELENIUM_TEST_TITLE", "httpbin.org")
    # A page with simple, guaranteed-visible links for click tests.
    _TEST_CLICK_URL: str = _os.environ.get("SELENIUM_TEST_CLICK_URL", "https://httpbin.org/links/10/0")

    @pytest.fixture
    def tool(self) -> Generator[WebSearchTool]:
        """WebSearchTool with real Selenium (auto-detected browser)."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        t = WebSearchTool(
            deep_search="selenium",
            browser_config={
                "headless": True,
                "browser": "auto",
                "extra_wait": 1.0,
                "page_load_timeout": 30,
            },
        )
        yield t
        t.close()

    @pytest.fixture
    def tool_from_config(self) -> Generator[WebSearchTool]:
        """WebSearchTool from dict config."""
        assert WebSearchTool is not None, "WebSearchTool is not available"
        t = create_tool_from_config(
            {
                "name": "web_search",
                "deep_search": "selenium",
                "browser_config": {"headless": True, "browser": "auto"},
            }
        )
        assert isinstance(t, WebSearchToolType)
        assert t is not None
        yield t
        t.close()

    # ------------------------------------------------------------------
    # action="fetch" — real page load
    # ------------------------------------------------------------------

    def test_fetch_real_page(self, tool: WebSearchTool):
        """Loading a real page via Selenium."""
        assert tool is not None
        result = tool.execute(action="fetch", url=self._TEST_URL)

        assert result.success is True
        assert self._TEST_TITLE in result.output

    def test_fetch_with_wait_for_selector(self, tool: WebSearchTool):
        """Loading with waiting for CSS selector."""
        assert tool is not None
        result = tool.execute(
            action="fetch",
            url=self._TEST_URL,
            wait_for_selector="body",
        )

        assert result.success is True
        assert self._TEST_TITLE in result.output

    def test_fetch_auto_detect_by_url(self, tool: WebSearchTool):
        """Auto-detecting action=fetch by url."""
        assert tool is not None
        result = tool.execute(url=self._TEST_URL)

        assert result.success is True
        assert self._TEST_TITLE in result.output

    # ------------------------------------------------------------------
    # action="click" — real click
    # ------------------------------------------------------------------

    def test_click_real_element(self, tool: WebSearchTool):
        """Clicking a real link on the page."""
        assert tool is not None
        # Use a page with guaranteed visible links
        tool.execute(action="fetch", url=self._TEST_CLICK_URL)

        result = tool.execute(action="click", selector="a")

        assert result.success is True
        assert "Clicked element" in result.output

    def test_click_nonexistent_element(self, tool: WebSearchTool):
        """Clicking a nonexistent element — error."""
        assert tool is not None
        tool.execute(action="fetch", url=self._TEST_URL)

        result = tool.execute(action="click", selector="#nonexistent-element-xyz")

        assert result.success is False

    # ------------------------------------------------------------------
    # action="execute_js" — real JavaScript execution
    # ------------------------------------------------------------------

    def test_execute_js_real(self, tool: WebSearchTool):
        """Executing real JavaScript."""
        assert tool is not None
        tool.execute(action="fetch", url=self._TEST_URL)

        result = tool.execute(
            action="execute_js",
            js_code="return document.title",
        )

        assert result.success is True
        assert self._TEST_TITLE in result.output

    def test_execute_js_return_computed_value(self, tool: WebSearchTool):
        """JS returns a computed value."""
        assert tool is not None
        tool.execute(action="fetch", url=self._TEST_URL)

        result = tool.execute(
            action="execute_js",
            js_code="return 2 + 2",
        )

        assert result.success is True
        assert "4" in result.output

    def test_execute_js_dom_manipulation(self, tool: WebSearchTool):
        """JS manipulates the DOM and returns a result."""
        assert tool is not None
        tool.execute(action="fetch", url=self._TEST_URL)

        result = tool.execute(
            action="execute_js",
            js_code="return document.querySelectorAll('a').length",
        )

        assert result.success is True
        # Page has at least 1 link
        assert result.output  # not empty

    # ------------------------------------------------------------------
    # action="extract_links" — real link extraction
    # ------------------------------------------------------------------

    def test_extract_links_real(self, tool: WebSearchTool):
        """Extracting real links from the page."""
        assert tool is not None
        tool.execute(action="fetch", url=self._TEST_URL)

        result = tool.execute(action="extract_links")

        assert result.success is True
        assert "link(s)" in result.output

    def test_extract_links_with_url(self, tool: WebSearchTool):
        """Extracting links with a specified URL (fetch + extract)."""
        assert tool is not None
        result = tool.execute(
            action="extract_links",
            url=self._TEST_URL,
        )

        assert result.success is True
        assert "link(s)" in result.output

    # ------------------------------------------------------------------
    # action="get_content" — getting current page content
    # ------------------------------------------------------------------

    def test_get_content_after_fetch(self, tool: WebSearchTool):
        """Getting content after loading the page."""
        assert tool is not None
        tool.execute(action="fetch", url=self._TEST_URL)

        result = tool.execute(action="get_content")

        assert result.success is True
        assert self._TEST_TITLE in result.output

    # ------------------------------------------------------------------
    # action="fill" — real form filling
    # ------------------------------------------------------------------

    def test_fill_real_input(self, tool: WebSearchTool):
        """Filling a real input field (on httpbin.org/forms/post)."""
        assert tool is not None
        tool.execute(
            action="fetch",
            url="https://httpbin.org/forms/post",
            wait_for_selector="input",
        )

        result = tool.execute(
            action="fill",
            selector="input[name='custname']",
            value="Test User",
        )

        assert result.success is True
        assert "Filled" in result.output
        assert "Test User" in result.output

    # ------------------------------------------------------------------
    # action="search" — real search
    # ------------------------------------------------------------------

    def test_search_real(self, tool: WebSearchTool):
        """Real search via DuckDuckGo."""
        assert tool is not None
        result = tool.execute(action="search", query="Python programming")

        assert result.success is True
        # Should have results (or at least no error)
        assert result.output

    # ------------------------------------------------------------------
    # action="crawl" — real crawl
    # ------------------------------------------------------------------

    def test_crawl_real(self, tool: WebSearchTool):
        """Real site crawl."""
        assert tool is not None
        result = tool.execute(
            action="crawl",
            url=self._TEST_URL,
            max_depth=1,
            max_pages=2,
        )

        assert result.success is True
        assert "Crawled" in result.output or "page" in result.output.lower()

    # ------------------------------------------------------------------
    # Created from config and working for real
    # ------------------------------------------------------------------

    def test_config_created_tool_works(self, tool_from_config: WebSearchTool):
        """Tool created from dict-config works for real."""
        assert tool_from_config is not None
        result = tool_from_config.execute(
            action="fetch",
            url=self._TEST_URL,
        )

        assert result.success is True
        assert self._TEST_TITLE in result.output

    def test_config_created_tool_click(self, tool_from_config: WebSearchTool):
        """Tool from config — real click."""
        assert tool_from_config is not None
        tool_from_config.execute(action="fetch", url=self._TEST_CLICK_URL)
        result = tool_from_config.execute(action="click", selector="a")

        assert result.success is True

    def test_config_created_tool_js(self, tool_from_config: WebSearchTool):
        """Tool from config — real JS."""
        assert tool_from_config is not None
        tool_from_config.execute(action="fetch", url=self._TEST_URL)
        result = tool_from_config.execute(
            action="execute_js",
            js_code="return document.title",
        )

        assert result.success is True
        assert self._TEST_TITLE in result.output

    # ------------------------------------------------------------------
    # Registry integration with real Selenium
    # ------------------------------------------------------------------

    def test_registry_with_real_selenium(self, tool: WebSearchTool):
        """ToolRegistry + real Selenium."""
        assert tool is not None
        registry = ToolRegistry()
        registry.register(tool)

        call = ToolCall(
            name="web_search",
            arguments={"action": "fetch", "url": self._TEST_URL},
        )
        result = registry.execute(call)

        assert result.success is True
        assert self._TEST_TITLE in result.output

    def test_registry_click_real(self, tool: WebSearchTool):
        """ToolRegistry + real click."""
        assert tool is not None
        registry = ToolRegistry()
        registry.register(tool)

        # First fetch a page with visible links
        registry.execute(
            ToolCall(
                name="web_search",
                arguments={"action": "fetch", "url": self._TEST_CLICK_URL},
            )
        )

        # Then click
        result = registry.execute(
            ToolCall(
                name="web_search",
                arguments={"action": "click", "selector": "a"},
            )
        )

        assert result.success is True

    def test_registry_execute_js_real(self, tool: WebSearchTool):
        """ToolRegistry + real JS."""
        assert tool is not None
        registry = ToolRegistry()
        registry.register(tool)

        registry.execute(
            ToolCall(
                name="web_search",
                arguments={"action": "fetch", "url": self._TEST_URL},
            )
        )

        result = registry.execute(
            ToolCall(
                name="web_search",
                arguments={"action": "execute_js", "js_code": "return document.title"},
            )
        )

        assert result.success is True
        assert self._TEST_TITLE in result.output

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def test_fetch_invalid_url(self, tool: WebSearchTool):
        """Loading an invalid URL — error."""
        assert tool is not None
        result = tool.execute(action="fetch", url="https://this-domain-does-not-exist-xyz.invalid")

        assert result.success is False

    def test_execute_js_error(self, tool: WebSearchTool):
        """JS with an error."""
        assert tool is not None
        tool.execute(action="fetch", url=self._TEST_URL)

        result = tool.execute(
            action="execute_js",
            js_code="return nonExistentVariable.property",
        )

        assert result.success is False

    def test_no_action_no_args(self, tool: WebSearchTool):
        """Call without arguments — error."""
        assert tool is not None
        result = tool.execute()

        assert result.success is False
