"""Materialise enabled Umbrella MCP registry servers as agent tool entries."""

import logging
import os
import re
from pathlib import Path
from typing import Any, List

try:
    from ouroboros.tools.registry import ToolEntry
except Exception:  # pragma: no cover - keeps Umbrella importable without Ouroboros
    ToolEntry = Any  # type: ignore[assignment]

log = logging.getLogger(__name__)


def _resolve_repo_root() -> Path:
    cwd = Path.cwd()
    for path in [cwd, *cwd.parents]:
        if (path / ".umbrella").is_dir():
            return path
    return cwd


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "tool"


def _build_handler(client_factory, tool_name: str):
    state: dict[str, Any] = {"client": None}

    def _handler(_ctx, **kwargs):
        try:
            client = state["client"]
            if client is None:
                client = client_factory()
                state["client"] = client
            return client.call_tool(tool_name, kwargs)
        except Exception as exc:
            return f"⚠️ MCP_TOOL_ERROR ({tool_name}): {exc}"

    return _handler


def build_mcp_tool_entries() -> list[ToolEntry]:
    if str(os.environ.get("OUROBOROS_MCP_DISABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return []
    try:
        from umbrella.mcp.client import open_client
        from umbrella.mcp.registry import McpRegistry
    except Exception:
        return []
    try:
        specs = McpRegistry(_resolve_repo_root()).list_servers()
    except Exception:
        log.warning("MCP registry load failed", exc_info=True)
        return []

    entries: list[ToolEntry] = []
    for spec in [s for s in specs if s.status == "enabled"]:
        prefix = f"mcp_{_safe_name(spec.name)}"
        try:

            def _factory(_spec=spec):
                client = open_client(_spec)
                if hasattr(client, "start"):
                    client.start()
                return client

            client = _factory()
            tool_descriptors = client.list_tools()
        except Exception as exc:
            log.warning("failed to query MCP server %s: %s", spec.name, exc)
            continue

        for desc in tool_descriptors:
            tool_name = f"{prefix}__{_safe_name(desc.name)}"
            entries.append(
                ToolEntry(
                    name=tool_name,
                    schema={
                        "name": tool_name,
                        "description": (
                            f"[MCP {spec.name}] {desc.description or 'remote MCP tool'} "
                            "(provided by an enabled MCP server; manage via the MCP Registry UI)."
                        ),
                        "parameters": desc.input_schema
                        or {"type": "object", "properties": {}},
                    },
                    handler=_build_handler(_factory, desc.name),
                    timeout_sec=120,
                )
            )
    return entries
