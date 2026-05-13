"""Materialise enabled MCP servers as Ouroboros tools.

When a run starts we look at ``.umbrella/mcp/registry.json`` and, for
every server with ``status == "enabled"``, we open a client and turn
each remote tool into a local :class:`ToolEntry` whose handler proxies
to the MCP server.  The proxy tools are namespaced as
``mcp_<server_name>__<tool_name>`` so they cannot collide with native
Ouroboros tools.
"""

import logging
import re
from pathlib import Path
from typing import Any

from umbrella.mcp.client import list_enabled_clients
from umbrella.mcp.registry import McpRegistry, McpServerSpec

log = logging.getLogger(__name__)

__all__ = ["register_mcp_tools", "shutdown_mcp_tools"]


_OPEN_CLIENTS: list[tuple[McpServerSpec, Any]] = []


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return cleaned or "tool"


def register_mcp_tools(registry: Any, repo_root: Path) -> dict[str, Any]:
    """Open enabled MCP clients and register their tools in the Ouroboros registry.

    ``registry`` here is :class:`ouroboros.tools.registry.ToolRegistry`.
    Returns a small report so callers can surface status.
    """
    from ouroboros.tools.registry import ToolEntry

    mcp_registry = McpRegistry(repo_root)
    specs = mcp_registry.list_servers()
    enabled = [s for s in specs if s.status == "enabled"]
    if not enabled:
        return {"enabled": 0, "tools_registered": 0}

    pairs = list_enabled_clients(enabled)
    tools_registered = 0
    failures: list[dict[str, Any]] = []
    for spec, client in pairs:
        try:
            tool_descriptors = client.list_tools()
        except Exception as exc:
            failures.append(
                {"server": spec.name, "phase": "list_tools", "error": str(exc)}
            )
            log.warning("MCP %s tools/list failed", spec.name, exc_info=True)
            try:
                if hasattr(client, "stop"):
                    client.stop()
            except Exception:
                pass
            continue
        prefix = f"mcp_{_safe_name(spec.name)}"
        for desc in tool_descriptors:
            tool_name = f"{prefix}__{_safe_name(desc.name)}"

            def _make_handler(_client=client, _tool_name=desc.name):
                def _handler(_ctx, **kwargs):
                    try:
                        return _client.call_tool(_tool_name, kwargs)
                    except Exception as exc:
                        return f"⚠️ MCP_TOOL_ERROR ({_tool_name}): {exc}"

                return _handler

            schema = desc.input_schema or {"type": "object", "properties": {}}
            entry = ToolEntry(
                name=tool_name,
                schema={
                    "name": tool_name,
                    "description": (
                        f"[MCP {spec.name}] {desc.description or 'remote MCP tool'} "
                        "(provided by an enabled MCP server; see Web UI > MCP Registry)."
                    ),
                    "parameters": schema,
                },
                handler=_make_handler(),
                timeout_sec=120,
            )
            registry.register(entry)
            tools_registered += 1
        _OPEN_CLIENTS.append((spec, client))
    return {
        "enabled": len(enabled),
        "tools_registered": tools_registered,
        "failures": failures,
    }


def shutdown_mcp_tools() -> None:
    """Best-effort stop of every MCP client started for this run."""
    while _OPEN_CLIENTS:
        spec, client = _OPEN_CLIENTS.pop()
        try:
            if hasattr(client, "stop"):
                client.stop()
        except Exception:
            log.warning("failed to stop MCP client %s", spec.name, exc_info=True)
