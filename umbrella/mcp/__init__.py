"""Umbrella-side MCP (Model Context Protocol) integration.

This package gives the agent a way to discover MCP servers, register
them in a workspace-shared file, and expose their tools to Ouroboros.

Subpackages
-----------
- :mod:`umbrella.mcp.registry` -- on-disk store at ``.umbrella/mcp/registry.json``.
- :mod:`umbrella.mcp.discovery` -- Ouroboros tools ``mcp_discover`` and ``mcp_install``.
- :mod:`umbrella.mcp.client` -- thin wrapper for stdio + HTTP MCP transports.
- :mod:`umbrella.mcp.tools_bridge` -- materialises enabled MCP tools into the Ouroboros registry.
"""

from umbrella.mcp.registry import McpRegistry, McpServerSpec

__all__ = ["McpRegistry", "McpServerSpec"]
