"""Tests for MCPClient and MCPTool."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("mcp")
from mcp.types import AudioContent, CallToolResult, EmbeddedResource, ImageContent, ResourceLink, TextContent

from gmas.tools.base import BaseTool
from gmas.tools.mcp_client import MCPClient, MCPTool


def make_list_result(*names: str):
    """Fake list_tools() result with the given tool names."""
    result = MagicMock()
    result.tools = []

    for name in names:
        t = MagicMock()
        t.name = name
        t.description = f"{name} description"
        t.inputSchema = {"type": "object"}
        t.outputSchema = None
        t.icons = None
        t.annotations = None
        t.meta = None
        t.execution = None
        result.tools.append(t)

    return result


def make_call_result(
    text: str = "ok",
    *,
    is_error: bool = False,
    extra_texts: list[str] | None = None,
    structured_content: dict | None = None,
) -> CallToolResult:
    content: list[TextContent | ImageContent | AudioContent | ResourceLink | EmbeddedResource] = [
        TextContent(type="text", text=text)
    ]
    content.extend(TextContent(type="text", text=t) for t in extra_texts or [])

    return CallToolResult(content=content, isError=is_error, structuredContent=structured_content)


def patched_serve(session):
    """Return a _serve coroutine that injects a fake session and waits for close()."""

    async def _serve(self):
        self._is_stopped = asyncio.Event()
        self._session = session
        self._is_ready.set()
        await self._is_stopped.wait()

    return _serve


class FakeSession:
    """Minimal fake MCP session with AsyncMock methods."""

    def __init__(self, *tool_names: str, call_text: str = "ok", call_is_error: bool = False):
        self.initialize = AsyncMock()
        self.list_tools = AsyncMock(return_value=make_list_result(*tool_names or ("echo",)))
        self.call_tool = AsyncMock(return_value=make_call_result(call_text, is_error=call_is_error))


class TestMCPTool:
    def _tool(self, call_result: CallToolResult) -> MCPTool:
        client = MagicMock()
        client.call_tool.return_value = call_result

        return MCPTool(
            tool_name="echo",
            tool_description="echoes input",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            client=client,
            output_schema={"type": "object"},
            meta={"tag": "test"},
        )

    def test_is_base_tool(self):
        assert isinstance(self._tool(make_call_result()), BaseTool)

    def test_properties(self):
        tool = self._tool(make_call_result())

        assert tool.name == "echo"
        assert tool.description == "echoes input"
        assert "text" in tool.parameters_schema["properties"]
        assert tool.output_schema == {"type": "object"}
        assert tool.meta == {"tag": "test"}
        assert tool.icons is None
        assert tool.annotations is None
        assert tool.execution is None

    def test_execute_success(self):
        result = self._tool(make_call_result("hello")).execute(text="hello")

        assert result.success is True
        assert result.output == "hello"
        assert result.error is None

    def test_execute_is_error(self):
        result = self._tool(make_call_result("bad input", is_error=True)).execute()

        assert result.success is False
        assert result.error == "bad input"
        assert result.output == ""

    def test_execute_exception(self):
        client = MagicMock()
        client.call_tool.side_effect = TimeoutError("timed out")
        tool = MCPTool("echo", "echoes", {}, client)
        result = tool.execute()

        assert result.success is False
        assert result.error is not None
        assert "timed out" in result.error

    def test_execute_joins_multiple_text_blocks(self):
        result = self._tool(make_call_result("line1", extra_texts=["line2", "line3"])).execute()

        assert result.success is True
        assert result.output == "line1\nline2\nline3"

    def test_execute_structured_output(self):
        data = {"temperature": 22.5, "unit": "celsius"}
        result = self._tool(make_call_result("22.5°C", structured_content=data)).execute()

        assert result.success is True
        assert result.output == "22.5°C"
        assert result.structured_output == data

    def test_execute_structured_output_none_by_default(self):
        result = self._tool(make_call_result("hello")).execute()

        assert result.structured_output is None

    def test_execute_passes_kwargs(self):
        client = MagicMock()
        client.call_tool.return_value = make_call_result()
        tool = MCPTool("echo", "", {}, client)
        tool.execute(foo="bar", n=42)
        client.call_tool.assert_called_once_with("echo", {"foo": "bar", "n": 42})


class TestMCPClient:
    def test_raises_when_not_connected(self):
        client = MCPClient("http://localhost:9999")

        with pytest.raises(RuntimeError, match="Not connected"):
            client.call_tool("echo", {})
        with pytest.raises(RuntimeError, match="Not connected"):
            client.tools()

    def test_close_before_connect_does_not_raise(self):
        MCPClient("http://localhost:9999").close()

    def test_context_manager_connects_and_closes(self):
        session = FakeSession()

        with patch.object(MCPClient, "_serve", patched_serve(session)), MCPClient("http://fake") as client:
            assert client._session is not None

        assert client._session is None

    def test_tools_returns_mcp_tool_objects(self):
        session = FakeSession("tool_a", "tool_b")

        with patch.object(MCPClient, "_serve", patched_serve(session)), MCPClient("http://fake") as client:
            tools = client.tools()

        assert len(tools) == 2
        assert all(isinstance(t, MCPTool) for t in tools)
        assert [t.name for t in tools] == ["tool_a", "tool_b"]

    def test_tools_are_cached(self):
        session = FakeSession("tool_a")

        with patch.object(MCPClient, "_serve", patched_serve(session)), MCPClient("http://fake") as client:
            t1 = client.tools()
            t2 = client.tools()

        assert t1 is t2

        session.list_tools.assert_awaited_once()

    def test_tools_refresh_bypasses_cache(self):
        session = FakeSession("tool_a")

        with patch.object(MCPClient, "_serve", patched_serve(session)), MCPClient("http://fake") as client:
            t1 = client.tools()
            t2 = client.tools(refresh=True)

        assert t1 is not t2
        assert session.list_tools.await_count == 2

    def test_call_tool_returns_call_tool_result(self):
        session = FakeSession(call_text="pong")

        with patch.object(MCPClient, "_serve", patched_serve(session)), MCPClient("http://fake") as client:
            result = client.call_tool("echo", {"text": "ping"})

        assert isinstance(result, CallToolResult)
        assert result.isError is False
        first = result.content[0]
        assert isinstance(first, TextContent)
        assert first.text == "pong"

    def test_call_tool_forwards_arguments(self):
        session = FakeSession()

        with patch.object(MCPClient, "_serve", patched_serve(session)), MCPClient("http://fake") as client:
            client.call_tool("echo", {"x": 1, "y": 2})

        session.call_tool.assert_awaited_once()
        _, kwargs = session.call_tool.call_args
        assert kwargs["arguments"] == {"x": 1, "y": 2}

    def test_call_tool_forwards_meta(self):
        session = FakeSession()

        with patch.object(MCPClient, "_serve", patched_serve(session)), MCPClient("http://fake") as client:
            client.call_tool("echo", {}, meta={"progressToken": "tok-1"})

        _, kwargs = session.call_tool.call_args
        assert kwargs["meta"] == {"progressToken": "tok-1"}

    def test_call_tool_forwards_progress_callback(self):
        session = FakeSession()
        calls = []

        def on_progress(progress, total, message):
            calls.append((progress, total, message))

        with patch.object(MCPClient, "_serve", patched_serve(session)), MCPClient("http://fake") as client:
            client.call_tool("echo", {}, progress_callback=on_progress)

        _, kwargs = session.call_tool.call_args
        assert kwargs["progress_callback"] is not None

    def test_call_tool_passes_read_timeout_to_session(self):
        session = FakeSession()

        with (
            patch.object(MCPClient, "_serve", patched_serve(session)),
            MCPClient("http://fake", read_timeout_seconds=42.0) as client,
        ):
            client.call_tool("echo", {})

        _, kwargs = session.call_tool.call_args
        assert kwargs["read_timeout_seconds"] == timedelta(seconds=42.0)

    def test_reconnect(self):
        session = FakeSession("tool_a")

        with patch.object(MCPClient, "_serve", patched_serve(session)):
            client = MCPClient("http://fake")
            client.connect()
            names_first = [t.name for t in client.tools()]
            client.close()
            client.connect()
            names_second = [t.name for t in client.tools()]
            client.close()

        assert names_first == names_second

    def test_connect_propagates_serve_error(self):
        async def failing_serve(self):
            self._is_stopped = asyncio.Event()
            self._error = ConnectionRefusedError("server down")
            self._is_ready.set()

        with patch.object(MCPClient, "_serve", failing_serve):
            client = MCPClient("http://fake")
            with pytest.raises(ConnectionRefusedError, match="server down"):
                client.connect()

    def test_connect_timeout(self):
        async def hanging_serve(self):
            self._is_stopped = asyncio.Event()
            await self._is_stopped.wait()

        with patch.object(MCPClient, "_serve", hanging_serve):
            client = MCPClient("http://fake", timeout=0.1)

            with pytest.raises(TimeoutError):
                client.connect()

            client.close()

    def test_read_timeout_defaults_to_timeout(self):
        client = MCPClient("http://localhost:9999", timeout=10.0)
        assert client.read_timeout_seconds == 10.0

    def test_read_timeout_override(self):
        client = MCPClient("http://localhost:9999", timeout=10.0, read_timeout_seconds=60.0)
        assert client.read_timeout_seconds == 60.0

    def test_call_tool_uses_read_timeout(self):
        session = FakeSession()

        with patch.object(MCPClient, "_serve", patched_serve(session)):
            client = MCPClient("http://fake", read_timeout_seconds=99.0)
            client.connect()

            with patch.object(client, "_run_coro", wraps=client._run_coro) as spy:
                client.call_tool("echo", {"text": "hi"})
                spy.assert_called_once()
                _, kwargs = spy.call_args
                assert kwargs["timeout"] == 99.0

            client.close()

    def test_two_simultaneous_clients(self):
        session_a = FakeSession("tool_a")
        session_b = FakeSession("tool_b")

        with (
            patch.object(MCPClient, "_serve", patched_serve(session_a)),
            MCPClient("http://fake-a") as client_a,
            patch.object(MCPClient, "_serve", patched_serve(session_b)),
            MCPClient("http://fake-b") as client_b,
        ):
            names_a = [t.name for t in client_a.tools()]
            names_b = [t.name for t in client_b.tools()]

        assert names_a == ["tool_a"]
        assert names_b == ["tool_b"]
