"""
Tools for agents.

If an agent has tools — they are ALWAYS used on every LLM call.
Tools are passed via the API (the `tools` parameter), not in the prompt text.

Supported tools:
- shell: Shell command execution
- code_interpreter: Python code execution in a sandbox
- file_search: File and content search
- vector_search: Semantic similarity search over a knowledge base
- web_search: Information search on the internet (DuckDuckGo, Serper, etc.)
- Any custom functions via the @tool decorator

Usage example:
    from gmas.tools import tool, get_registry, CodeInterpreterTool
    from gmas.core.agent import AgentProfile
    from gmas.execution import MACPRunner

    # 1. Register tools (globally or via the registry)
    @tool
    def fibonacci(n: int) -> str:
        '''Calculate n-th Fibonacci number.'''
        a, b = 0, 1
        for _ in range(n):
            a, b = b, a + b
        return str(a)

    # 2. Create an agent with tools
    agent = AgentProfile(
        agent_id="math",
        display_name="Math Agent",
        persona="a helpful math assistant",
        tools=["fibonacci", "code_interpreter"],  # <-- tools here!
    )

    # 3. Run via runner — tools are used automatically
    runner = MACPRunner(llm_caller=my_caller)
    result = runner.run_round(graph)
"""

import contextlib

from .base import (
    BaseTool,
    ToolCall,
    ToolRegistry,
    ToolResult,
    create_tool_from_config,
    get_registry,
    register_tool,
    register_tool_factory,
    tool,
)
from .code_interpreter import CodeInterpreterTool
from .computer_use import ComputerUseTool
from .file_search import FileSearchTool
from .function_calling import FunctionTool, FunctionWrapper
from .llm_integration import (
    LLMResponse,
    LLMToolCall,
    OpenAICaller,
    OpenAIToolsCaller,
    create_openai_caller,
    create_openai_tools_caller,
    parse_anthropic_response,
    parse_openai_response,
)
from .shell import ShellTool
from .vector_search import VectorIndexTool, VectorSearchTool
from .web_search import (
    DuckDuckGoProvider,
    SearchProvider,
    SeleniumFetcher,
    SerperProvider,
    TavilyProvider,
    URLFetcher,
    WebSearchTool,
)

# MCP server support (optional — requires 'mcp' package)
with contextlib.suppress(ImportError):
    from .mcp_client import MCPClient, MCPTool

__all__ = [
    "BaseTool",
    "CodeInterpreterTool",
    "ComputerUseTool",
    "FileSearchTool",
    "FunctionTool",
    "FunctionWrapper",
    "LLMResponse",
    "LLMToolCall",
    "MCPClient",
    "MCPTool",
    "OpenAICaller",
    "OpenAIToolsCaller",
    "ShellTool",
    "ToolCall",
    "ToolRegistry",
    "ToolResult",
    "URLFetcher",
    "VectorIndexTool",
    "VectorSearchTool",
    "WebSearchTool",
    "create_openai_caller",  # Recommended way to create a caller
    "create_openai_tools_caller",  # = create_openai_caller
    "create_tool_from_config",
    "get_registry",
    "parse_anthropic_response",
    "parse_openai_response",
    "register_tool",
    "register_tool_factory",
    "tool",
]

try:
    from . import web_search as _web_search
except (ImportError, SyntaxError):
    pass
else:
    BochaProvider = _web_search.BochaProvider
    BraveProvider = _web_search.BraveProvider
    BrowserFetcher = _web_search.BrowserFetcher
    DuckDuckGoProvider = _web_search.DuckDuckGoProvider
    ExaProvider = _web_search.ExaProvider
    GoogleProvider = _web_search.GoogleProvider
    IntentClassifier = _web_search.IntentClassifier
    PlaywrightFetcher = _web_search.PlaywrightFetcher
    PROVIDER_REGISTRY = _web_search.PROVIDER_REGISTRY
    SearchCache = _web_search.SearchCache
    SearchError = _web_search.SearchError
    SearchProvider = _web_search.SearchProvider
    SearchRouter = _web_search.SearchRouter
    SearXNGProvider = _web_search.SearXNGProvider
    SeleniumFetcher = _web_search.SeleniumFetcher
    SerperProvider = _web_search.SerperProvider
    TavilyProvider = _web_search.TavilyProvider
    URLFetcher = _web_search.URLFetcher
    WebSearchTool = _web_search.WebSearchTool
    deduplicate_results = _web_search.deduplicate_results
    get_provider_class = _web_search.get_provider_class
    normalize_url = _web_search.normalize_url
    register_provider = _web_search.register_provider
    __all__ += [
        "PROVIDER_REGISTRY",
        "BochaProvider",
        "BraveProvider",
        "BrowserFetcher",
        "DuckDuckGoProvider",
        "ExaProvider",
        "GoogleProvider",
        "IntentClassifier",
        "PlaywrightFetcher",
        "SearXNGProvider",
        "SearchCache",
        "SearchError",
        "SearchProvider",
        "SearchRouter",
        "SeleniumFetcher",
        "SerperProvider",
        "TavilyProvider",
        "URLFetcher",
        "WebSearchTool",
        "deduplicate_results",
        "get_provider_class",
        "normalize_url",
        "register_provider",
    ]
