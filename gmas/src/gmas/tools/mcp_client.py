"""
Remote MCP (Model Context Protocol) server support.

Allows agents to use tools from remote MCP servers via Streamable HTTP.
"""

import asyncio
import contextlib
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Self

try:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import CallToolResult, Icon, TextContent, ToolAnnotations, ToolExecution
except ImportError as e:
    msg = "MCPClient requires the 'mcp' package. Install it with: pip install 'frontier-ai-gmas[mcp]'"
    raise ImportError(msg) from e

from .base import BaseTool, ToolResult


class MCPTool(BaseTool):
    """A tool proxied from a remote MCP server."""

    def __init__(
        self,
        tool_name: str,
        tool_description: str,
        input_schema: dict[str, Any],
        client: "MCPClient",
        *,
        output_schema: dict[str, Any] | None = None,
        icons: list[Icon] | None = None,
        annotations: ToolAnnotations | None = None,
        meta: dict[str, Any] | None = None,
        execution: ToolExecution | None = None,
    ):
        """
        Args:
            tool_name: Tool name as reported by the MCP server.
            tool_description: Human-readable description of the tool.
            input_schema: JSON Schema dict describing the tool's parameters.
            client: The MCPClient that owns this tool and executes calls.
            output_schema: Optional JSON Schema describing the tool's structured output.
            icons: Optional list of icons for display in user interfaces.
            annotations: Optional hints about the tool's behavior (read-only, destructive, etc.).
            meta: Arbitrary metadata attached to the tool by the server.
            execution: Execution properties, e.g. whether the tool supports long-running tasks.

        """
        self._name = tool_name
        self._description = tool_description
        self._input_schema = input_schema
        self._client = client
        self.output_schema = output_schema
        self.icons = icons
        self.annotations = annotations
        self.meta = meta
        self.execution = execution

    @property
    def name(self) -> str:
        """Tool name as reported by the MCP server."""
        return self._name

    @property
    def description(self) -> str:
        """Human-readable description of the tool."""
        return self._description

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """JSON Schema describing the tool's input parameters."""
        return self._input_schema

    def execute(self, **kwargs: Any) -> ToolResult:
        """
        Call the tool on the remote MCP server via the BaseTool interface.

        Extracts text from all TextContent items and joins them. Returns a
        failed ToolResult if the server sets ``isError`` or if the call raises.
        For structured output use ``MCPClient.call_tool()`` directly.

        """
        try:
            result = self._client.call_tool(self._name, kwargs)

            if result.isError:
                error_text = "\n".join(item.text for item in result.content if isinstance(item, TextContent))

                return ToolResult(
                    tool_name=self._name,
                    success=False,
                    error=error_text,
                    structured_output=result.structuredContent,
                )

            text = "\n".join(item.text for item in result.content if isinstance(item, TextContent))

            return ToolResult(
                tool_name=self._name,
                success=True,
                output=text,
                structured_output=result.structuredContent,
            )

        except Exception as e:  # noqa: BLE001
            return ToolResult(tool_name=self._name, success=False, error=str(e))


class MCPClient:
    """
    Client for a remote MCP server over Streamable HTTP.

    Maintains a persistent MCP session in a background thread so the
    sync gMAS framework can call async MCP SDK methods without blocking.

    Args:
        url: MCP server URL.
        headers: Optional HTTP headers, e.g. ``{"Authorization": "Bearer <token>"}``.
        timeout: Seconds to wait for connection and tool calls.
        read_timeout_seconds: Separate timeout for tool calls. Defaults to ``timeout``.

    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        read_timeout_seconds: float | None = None,
    ):
        """
        Args:
            url: MCP server URL.
            headers: Optional HTTP headers (e.g. for authentication).
            timeout: Connection and call timeout in seconds.
            read_timeout_seconds: Separate timeout for tool calls. Defaults to ``timeout``.

        """
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout
        self.read_timeout_seconds = read_timeout_seconds if read_timeout_seconds is not None else timeout
        self._session: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._is_ready = threading.Event()
        self._is_stopped: asyncio.Event | None = None
        self._error: BaseException | None = None
        self._tools_cache: list[MCPTool] | None = None

    def connect(self) -> None:
        """Connect to the MCP server (blocks until the session is ready or timeout)."""
        if self._session is not None:
            return

        # The gMAS framework is sync, but MCP SDK is fully async.
        # The thread only exists because of the sync/async boundary.
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        if not self._is_ready.wait(timeout=self.timeout):
            self.close()
            msg = f"Timed out connecting to MCP server: {self.url}"
            raise TimeoutError(msg)

        if self._error:
            raise self._error

    def tools(self, *, refresh: bool = False) -> list[MCPTool]:
        """
        Return the tools advertised by the MCP server.

        Results are cached after the first call; subsequent calls return
        the same list without a network round-trip. Pass ``refresh=True``
        to re-fetch from the server (e.g. when the server may have added tools).
        """
        if self._tools_cache is not None and not refresh:
            return self._tools_cache

        self._ensure_connected()

        result = self._run_coro(self._session.list_tools())

        self._tools_cache = [
            MCPTool(
                tool_name=t.name,
                tool_description=t.description or "",
                input_schema=t.inputSchema if hasattr(t, "inputSchema") else {},
                client=self,
                output_schema=t.outputSchema,
                icons=t.icons,
                annotations=t.annotations,
                meta=t.meta,
                execution=t.execution,
            )
            for t in result.tools
        ]

        return self._tools_cache

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        progress_callback: Callable | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """
        Call a tool on the MCP server and return the raw ``CallToolResult``.

        The result contains:
        - ``content`` — list of content blocks (TextContent, ImageContent, …)
        - ``structuredContent`` — optional ``dict`` matching the tool's ``outputSchema``
        - ``isError`` — ``True`` if the server reported a tool-level error

        Args:
            name: Tool name.
            arguments: Tool arguments.
            progress_callback: Called with ``(progress, total, message)`` during
                long-running calls. The SDK auto-injects a ``progressToken``.
            meta: Request metadata (e.g. ``{"progressToken": "custom-token"}``).

        Raises on transport errors or timeout.
        Uses ``read_timeout_seconds`` instead of the connection ``timeout``.

        """
        self._ensure_connected()

        async_cb = None

        if progress_callback is not None:

            async def async_cb(progress: float, total: float | None, message: str | None) -> None:
                progress_callback(progress, total, message)

        return self._run_coro(
            self._session.call_tool(
                name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=self.read_timeout_seconds),
                progress_callback=async_cb,
                meta=meta,
            ),
            timeout=self.read_timeout_seconds,
        )

    def close(self) -> None:
        """
        Close the MCP session and shut down the background thread.

        Safe to call multiple times. After closing, ``connect()`` must be
        called again before making further requests.
        """
        if self._is_stopped is not None and self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._is_stopped.set)

        if self._thread is not None:
            self._thread.join(timeout=5)

        self._session = None
        self._tools_cache = None
        self._is_ready.clear()
        self._error = None

    def _ensure_connected(self) -> None:
        """Raise RuntimeError if the session has not been established yet."""
        if self._session is None:
            msg = "Not connected. Call connect() first or use as context manager."
            raise RuntimeError(msg)

    def _run_coro(self, coro: Any, *, timeout: float | None = None) -> Any:
        """Dispatch an async coroutine to the background event loop and wait for its result."""
        if self._loop is None:
            msg = "Event loop not initialized. Call connect() first."
            raise RuntimeError(msg)
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        return future.result(timeout=timeout if timeout is not None else self.timeout)

    def _run_loop(self) -> None:
        """Entry point for the background thread — owns the event loop for its lifetime."""
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._serve())
        self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        self._loop.close()

    async def _serve(self) -> None:
        """Open the Streamable HTTP transport and hand off to ``_run_session``."""
        is_stopped = asyncio.Event()
        self._is_stopped = is_stopped

        try:
            http_client = httpx.AsyncClient(headers=self.headers) if self.headers else None

            async with streamable_http_client(self.url, http_client=http_client) as (read, write, _):
                await self._run_session(read, write, is_stopped)

        except Exception as e:  # noqa: BLE001
            self._error = e
            self._is_ready.set()

    async def _run_session(self, read_stream: Any, write_stream: Any, is_stopped: asyncio.Event) -> None:
        """
        Initialize the MCP ClientSession and park until ``close()`` is called.

        The ``await is_stopped.wait()`` keeps this coroutine (and therefore
        both ``async with`` context managers above it) alive for the entire lifetime
        of the client, preserving the transport connection and session state.
        """
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            self._session = session
            self._is_ready.set()
            await is_stopped.wait()

    def __enter__(self) -> Self:
        """Connect on entry; enables use as a context manager."""
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        """Close on exit; enables use as a context manager."""
        self.close()

    def __del__(self) -> None:
        """Best-effort cleanup when the object is garbage-collected."""
        with contextlib.suppress(Exception):
            self.close()
