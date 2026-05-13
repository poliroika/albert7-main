"""Auto-loaded thin shim exposing umbrella.mcp.discovery tools to Ouroboros.

Keeping the implementation in :mod:`umbrella.mcp.discovery` (not in
``ouroboros/tools``) lets it be reused by the Web Bridge and CLI
without a circular import.  This shim simply forwards ``get_tools()``.
"""

from typing import List

try:
    from umbrella.mcp.discovery import get_tools as _get_tools
except Exception:  # pragma: no cover - umbrella may be optional in some builds

    def _get_tools() -> list:  # type: ignore[no-redef]
        return []


def get_tools() -> list:
    return _get_tools()
