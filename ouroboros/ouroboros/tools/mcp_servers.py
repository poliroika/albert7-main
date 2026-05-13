"""Thin shim exposing Umbrella MCP server tool entries to Ouroboros."""

from typing import List

from ouroboros.tools.registry import ToolEntry

try:
    from umbrella.mcp.tool_entries import build_mcp_tool_entries
except Exception:  # pragma: no cover - Umbrella may be optional in standalone builds

    def build_mcp_tool_entries() -> list[ToolEntry]:  # type: ignore[no-redef]
        return []


def get_tools() -> list[ToolEntry]:
    return build_mcp_tool_entries()
