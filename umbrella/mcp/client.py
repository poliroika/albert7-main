"""Thin MCP client wrapper.

This is intentionally minimal: the goal is to be testable and to make
``umbrella.mcp.tools_bridge`` work without a full MCP SDK.  When GMAS's
:class:`MCPClient` is available we use it for HTTP/SSE transports;
otherwise we fall back to a tiny stdio JSON-RPC implementation that
supports just enough of the protocol for ``initialize``,
``tools/list`` and ``tools/call``.
"""

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from collections.abc import Iterable

from umbrella.mcp.registry import McpServerSpec

log = logging.getLogger(__name__)


@dataclass
class McpToolDescriptor:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


class StdioMcpClient:
    """Tiny stdio MCP client (one JSON-RPC line per request/response)."""

    def __init__(self, spec: McpServerSpec, *, timeout: float = 30.0) -> None:
        if spec.transport != "stdio":
            raise ValueError("StdioMcpClient only supports stdio transport")
        self.spec = spec
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._next_id = 1

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        argv = [self.spec.command, *self.spec.args]
        env = {**self.spec.env}
        try:
            import os as _os

            env = {**_os.environ, **env}
        except Exception:
            pass
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
            )
        except OSError as exc:
            raise RuntimeError(
                f"failed to start MCP server {self.spec.name}: {exc}"
            ) from exc
        # Initialise.
        try:
            self._call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"roots": {}},
                    "clientInfo": {"name": "umbrella", "version": "1.0"},
                },
            )
        except Exception:
            log.warning("MCP initialize failed for %s", self.spec.name, exc_info=True)

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass

    def list_tools(self) -> list[McpToolDescriptor]:
        result = self._call("tools/list", {})
        if not isinstance(result, dict):
            return []
        items = result.get("tools") or []
        return [
            McpToolDescriptor(
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                input_schema=dict(
                    item.get("inputSchema") or item.get("input_schema") or {}
                ),
            )
            for item in items
            if isinstance(item, dict) and item.get("name")
        ]

    def call_tool(self, name: str, args: dict[str, Any]) -> str:
        result = self._call("tools/call", {"name": name, "arguments": args or {}})
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                texts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(str(block.get("text") or ""))
                if texts:
                    return "\n".join(texts)
            return json.dumps(result, ensure_ascii=False, default=str)
        return json.dumps(result, ensure_ascii=False, default=str)

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        if self._proc is None:
            self.start()
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("MCP process is not running")
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            assert self._proc.stdin is not None
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                response_line = self._proc.stdout.readline()
                if not response_line:
                    if self._proc.poll() is not None:
                        raise RuntimeError(
                            "MCP process exited while waiting for response"
                        )
                    continue
                try:
                    response = json.loads(response_line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    raise RuntimeError(f"MCP {method} failed: {response['error']}")
                return response.get("result")
            raise TimeoutError(f"MCP {method} timed out")


def open_client(spec: McpServerSpec) -> StdioMcpClient | Any:
    """Open an MCP client appropriate for the spec's transport.

    Today only stdio is implemented in-process.  HTTP/SSE specs return
    a placeholder client object that raises on use, so callers can
    decide what to do.
    """
    if spec.transport == "stdio":
        return StdioMcpClient(spec)
    try:
        from gmas.tools.mcp_client import MCPClient  # type: ignore[import-not-found]

        return MCPClient(spec.url, headers=spec.env or None)
    except Exception:
        log.warning("HTTP/SSE MCP transport not available for %s", spec.name)

        class _UnsupportedClient:
            def list_tools(self) -> list[McpToolDescriptor]:
                return []

            def call_tool(self, name: str, args: dict[str, Any]) -> str:
                return f"⚠️ MCP_TRANSPORT_UNAVAILABLE: {spec.transport}"

            def stop(self) -> None:  # pragma: no cover
                pass

        return _UnsupportedClient()


def list_enabled_clients(
    specs: Iterable[McpServerSpec],
) -> list[tuple[McpServerSpec, Any]]:
    """Open clients for every enabled spec and return (spec, client) pairs."""
    out: list[tuple[McpServerSpec, Any]] = []
    for spec in specs:
        if spec.status != "enabled":
            continue
        try:
            client = open_client(spec)
            if hasattr(client, "start"):
                client.start()
            out.append((spec, client))
        except Exception:
            log.warning("failed to open MCP client for %s", spec.name, exc_info=True)
    return out
