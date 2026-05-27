"""Parse workspace.toml charter seeds (capabilities, discovery channels)."""

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DISCOVERY_OUTCOMES = frozenset({"attempted", "no_results", "error", "skipped", "blocked"})


def _read_toml(path: Path) -> dict[str, Any]:
    import tomllib

    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, Exception) as exc:
        log.debug("workspace.toml read failed: %s", exc, exc_info=True)
        return {}


def load_workspace_charter(workspace_root: Path) -> dict[str, Any]:
    """Load charter from workspace.toml or .umbrella/workspace.toml."""

    root = workspace_root.resolve()
    for rel in ("workspace.toml", ".umbrella/workspace.toml"):
        path = root / rel
        if path.is_file():
            data = _read_toml(path)
            return _normalize_charter(data)
    return {"capabilities": [], "discovery_channels": [], "policies": {}}


def _normalize_charter(data: dict[str, Any]) -> dict[str, Any]:
    capabilities: list[dict[str, Any]] = []
    for item in data.get("capabilities") or ():
        if isinstance(item, dict):
            slug = str(item.get("slug") or "").strip().lower()
            if slug:
                capabilities.append(
                    {
                        "slug": slug,
                        "optional": bool(item.get("optional")),
                        "notes": str(item.get("notes") or ""),
                        "probe": item.get("probe") if isinstance(item.get("probe"), dict) else None,
                    }
                )
    discovery_channels: list[dict[str, Any]] = []
    discovery = data.get("discovery")
    if isinstance(discovery, dict):
        for item in discovery.get("required_channels") or ():
            if isinstance(item, dict):
                tool = str(item.get("tool") or "").strip()
                if tool:
                    discovery_channels.append(
                        {
                            "tool": tool,
                            "required": bool(item.get("required", True)),
                        }
                    )
    policies = data.get("policies") if isinstance(data.get("policies"), dict) else {}
    return {
        "capabilities": capabilities,
        "discovery_channels": discovery_channels,
        "policies": policies,
    }


def charter_capability_slugs(charter: dict[str, Any]) -> list[str]:
    return [
        str(item.get("slug") or "")
        for item in charter.get("capabilities") or []
        if str(item.get("slug") or "").strip()
    ]


def charter_required_discovery_tools(
    charter: dict[str, Any],
    *,
    allowed_tools: set[str] | frozenset[str],
) -> list[str]:
    required: list[str] = []
    for item in charter.get("discovery_channels") or []:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        if not tool or tool not in allowed_tools:
            continue
        if bool(item.get("required", True)):
            required.append(tool)
    return required


def normalize_discovery_channel(entry: Any) -> dict[str, str] | None:
    if not isinstance(entry, dict):
        return None
    tool = str(entry.get("tool") or "").strip()
    if not tool:
        return None
    outcome = str(entry.get("outcome") or "attempted").strip().lower()
    if outcome not in _DISCOVERY_OUTCOMES:
        outcome = "attempted"
    return {
        "tool": tool,
        "outcome": outcome,
        "notes": str(entry.get("notes") or "").strip(),
    }
