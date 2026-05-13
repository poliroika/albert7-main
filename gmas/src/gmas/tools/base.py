"""
Base classes for agent tools.

Tools are used via Native Function Calling (OpenAI/Anthropic API).
If an agent has tools, it ALWAYS uses them on every call.
"""

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """Tool invocation request."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def parse_from_response(cls, response: str) -> list["ToolCall"]:
        r"""
        Parse tool calls from an LLM response.

        Supports two formats:
        1. XML-like tags: <tool_call>{"name": "...", "arguments": {...}}</tool_call>
        2. Markdown code blocks: ```tool_call\n{"name": "...", "arguments": {...}}\n```

        Args:
            response: Text response from the LLM

        Returns:
            List of ToolCall objects

        """
        calls: list[ToolCall] = []

        # Pattern for XML-like tags
        xml_pattern = r"<tool_call>\s*(\{.*?\})\s*</tool_call>"
        xml_matches = re.findall(xml_pattern, response, re.DOTALL)

        for match in xml_matches:
            try:
                data = json.loads(match)
                if isinstance(data, dict) and "name" in data:
                    calls.append(cls(name=data["name"], arguments=data.get("arguments", {})))
            except (json.JSONDecodeError, ValueError):
                # Skip invalid JSON
                pass

        # Pattern for markdown code blocks
        code_block_pattern = r"```tool_call\s*\n(\{.*?\})\s*\n```"
        code_matches = re.findall(code_block_pattern, response, re.DOTALL)

        for match in code_matches:
            try:
                data = json.loads(match)
                if isinstance(data, dict) and "name" in data:
                    calls.append(cls(name=data["name"], arguments=data.get("arguments", {})))
            except (json.JSONDecodeError, ValueError):
                # Skip invalid JSON
                pass

        return calls


class ToolResult(BaseModel):
    """Tool execution result."""

    tool_name: str
    success: bool = True
    output: str = ""
    structured_output: dict | None = None
    error: str | None = None

    def to_message(self) -> str:
        """Format the result for insertion into a prompt."""
        if self.success:
            return f'<tool_result name="{self.tool_name}">\n{self.output}\n</tool_result>'
        return f'<tool_error name="{self.tool_name}">\n{self.error}\n</tool_error>'


class BaseTool(ABC):
    """
    Abstract base class for tools.

    All tools must inherit from this class and implement
    the name, description, and execute methods.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Tool description for the LLM."""
        ...

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """JSON Schema of the tool parameters."""
        return {"type": "object", "properties": {}}

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool with the given arguments."""
        ...

    def to_openai_schema(self) -> dict[str, Any]:
        """Serialize the tool to the OpenAI function calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


class ToolRegistry:
    """
    Tool registry for agents.

    Example:
        from gmas.tools import get_registry, CodeInterpreterTool

        # Get the global registry
        registry = get_registry()
        registry.register(CodeInterpreterTool())

        # Or create your own
        my_registry = ToolRegistry()
        my_registry.register(ShellTool())

    """

    def __init__(self):
        """Initialize an empty registry."""
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> "ToolRegistry":
        """
        Register a tool.

        Args:
            tool: Tool instance.

        Returns:
            self for method chaining.

        """
        self._tools[tool.name] = tool
        return self

    def function(
        self,
        func: Callable | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Callable:
        """
        Decorator for registering a function as a tool.

        Example:
            @registry.function
            def my_tool(arg: str) -> str:
                \"\"\"Tool description.\"\"\"
                return arg.upper()

        """

        def decorator(f: Callable) -> Callable:
            tool_name = name or getattr(f, "__name__", "unnamed")
            tool_desc = description or getattr(f, "__doc__", None) or f"Function {tool_name}"

            from .function_calling import FunctionWrapper

            wrapper = FunctionWrapper(
                func=f,
                tool_name=tool_name,
                tool_description=tool_desc,
            )
            self._tools[tool_name] = wrapper
            return f

        if func is not None:
            return decorator(func)
        return decorator

    def get(self, name: str) -> BaseTool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check whether a tool is registered."""
        return name in self._tools

    def execute(self, call: ToolCall) -> ToolResult:
        """Execute a tool call."""
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                tool_name=call.name,
                success=False,
                error=f"Tool '{call.name}' not found",
            )

        try:
            return tool.execute(**call.arguments)
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            return ToolResult(
                tool_name=call.name,
                success=False,
                error=str(e),
            )

    def execute_all(self, calls: list[ToolCall]) -> list[ToolResult]:
        """Execute multiple tool calls."""
        return [self.execute(call) for call in calls]

    def list_tools(self) -> list[str]:
        """Get the list of registered tool names."""
        return list(self._tools.keys())

    def get_tools(self, tool_names: list[str] | None = None) -> list[BaseTool]:
        """
        Get tools by name.

        Args:
            tool_names: List of tool names (None = all).

        Returns:
            List of BaseTool objects.

        """
        if tool_names is None:
            return list(self._tools.values())
        return [self._tools[name] for name in tool_names if name in self._tools]

    def to_openai_schemas(self, tool_names: list[str] | None = None) -> list[dict[str, Any]]:
        """
        Get schemas in the OpenAI function calling API format.

        Args:
            tool_names: List of tool names (None = all).

        Returns:
            List of schemas in OpenAI tools API format.

        """
        tools = self.get_tools(tool_names)
        return [tool.to_openai_schema() for tool in tools]

    def get_tools_for_agent(self, tool_names: list[str]) -> list[BaseTool]:
        """
        Get tools for an agent by name.

        Args:
            tool_names: List of tool names.

        Returns:
            List of BaseTool objects (only those that exist).

        """
        return [self._tools[name] for name in tool_names if name in self._tools]

    def to_schemas(self, tool_names: list[str] | None = None) -> list[dict[str, Any]]:
        """
        Get simplified tool schemas.

        Args:
            tool_names: List of tool names (None = all).

        Returns:
            List of schemas in simplified format.

        """
        tools = self.get_tools(tool_names)
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters_schema,
            }
            for tool in tools
        ]

    def format_tools_prompt(self, tool_names: list[str] | None = None) -> str:
        """
        Build a text prompt describing available tools.

        Args:
            tool_names: List of tool names (None = all).

        Returns:
            String with tool descriptions for the prompt.

        """
        tools = self.get_tools(tool_names)
        if not tools:
            return "No tools available."

        lines = ["Available tools:"]
        for tool in tools:
            lines.append(f"\n- {tool.name}: {tool.description}")
            params = tool.parameters_schema.get("properties", {})
            if params:
                for param_name, param_info in params.items():
                    param_type = param_info.get("type", "any")
                    param_desc = param_info.get("description", "")
                    lines.append(f"    - {param_name} ({param_type}): {param_desc}")

        lines.append("\nTo use a tool, respond with:")
        lines.append("<tool_call>")
        lines.append('{"name": "tool_name", "arguments": {...}}')
        lines.append("</tool_call>")

        return "\n".join(lines)


# Global tool registry
_global_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    """
    Get the global tool registry.

    Creates the registry on the first call (singleton).

    Example:
        from gmas.tools import get_registry, ShellTool

        registry = get_registry()
        registry.register(ShellTool())

    """
    global _global_registry  # noqa: PLW0603
    if _global_registry is None:
        _global_registry = ToolRegistry()
    return _global_registry


def register_tool(tool: BaseTool) -> BaseTool:
    """
    Register a tool in the global registry.

    Example:
        from gmas.tools import register_tool, ShellTool

        register_tool(ShellTool())  # Now available globally

    """
    get_registry().register(tool)
    return tool


def tool(
    func: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Callable:
    """
    Decorator for registering a function as a tool in the global registry.

    Example:
        from gmas.tools import tool

        @tool
        def fibonacci(n: int) -> str:
            '''Calculate the n-th Fibonacci number.'''
            a, b = 0, 1
            for _ in range(n):
                a, b = b, a + b
            return str(a)

        # Now 'fibonacci' is available globally via get_registry()

    """
    return get_registry().function(func, name=name, description=description)


# ============================================================
# Tool factory — creating tools from a dict config
# ============================================================

# Factory registry: tool_name -> callable(config_dict) -> BaseTool
_tool_factories: dict[str, Callable[..., BaseTool]] = {}


def register_tool_factory(tool_name: str, factory: Callable[..., BaseTool]) -> None:
    """
    Register a factory for creating a tool by name from a config.

    Args:
        tool_name: Tool name (e.g. "web_search").
        factory: Function that accepts **kwargs and returns a BaseTool.

    Example:
        register_tool_factory("web_search", lambda **kw: WebSearchTool(**kw))

    """
    _tool_factories[tool_name] = factory


def create_tool_from_config(config: dict[str, Any]) -> BaseTool | None:
    """
    Create a tool from a dict config.

    The config must contain a "name" key (tool name).
    All other keys are passed as constructor parameters.

    Args:
        config: Settings dictionary, e.g.:
            {"name": "web_search", "deep_search": "playwright"}

    Returns:
        BaseTool or None if no factory is found.

    Example:
        tool = create_tool_from_config({
            "name": "web_search",
            "deep_search": "playwright",
            "browser_config": {"headless": True, "browser": "chromium"},
        })

    """
    name = config.get("name") or config.get("tool") or config.get("id")
    if not name:
        return None

    factory = _tool_factories.get(name)
    if factory is None:
        return None

    # Remove identification keys, keep only parameters
    params = {k: v for k, v in config.items() if k not in ("name", "tool", "id")}
    return factory(**params)
