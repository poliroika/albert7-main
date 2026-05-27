"""Validation helpers for the phase-manifest/tool-registry contract."""

import logging
import pathlib
import sys
from typing import Iterable

from umbrella.phases.base import PhaseManifest

log = logging.getLogger(__name__)


def _ensure_ouroboros_import_path(repo_root: pathlib.Path) -> None:
    """Make the nested Ouroboros package importable from bridge entrypoints."""
    outer = (repo_root / "ouroboros").resolve()
    inner = (outer / "ouroboros").resolve()
    if not inner.is_dir():
        return
    repo = repo_root.resolve()
    sys.path[:] = [
        path
        for path in sys.path
        if pathlib.Path(path or ".").resolve() not in {outer, repo}
    ]
    sys.path.insert(0, str(outer))
    for name in list(sys.modules):
        if name == "ouroboros" or name.startswith("ouroboros."):
            sys.modules.pop(name, None)


def _umbrella_declared_tool_names() -> set[str]:
    """Union Umbrella-owned tool specs even if a stale ``ouroboros`` import shadowed the registry."""
    names: set[str] = set()
    try:
        from umbrella.deep_agent_tools.ouroboros_entries import get_ouroboros_tool_entries

        names.update(entry.name for entry in get_ouroboros_tool_entries())
    except Exception:
        log.debug("Could not load ouroboros_entries tool names", exc_info=True)
    try:
        from umbrella.deep_agent_tools.phase_control_tools import get_tools

        names.update(entry.name for entry in get_tools())
    except Exception:
        log.debug("Could not load phase_control tool names", exc_info=True)
    return names


def registered_ouroboros_tool_names(repo_root: pathlib.Path) -> set[str]:
    """Return every registered Ouroboros tool name, including policy-disabled ones."""

    _ensure_ouroboros_import_path(repo_root)
    from ouroboros.tools.registry import ToolRegistry

    registry = ToolRegistry(
        repo_dir=repo_root,
        drive_root=repo_root / ".umbrella" / "tool-contract-drive",
        host_repo_root=repo_root,
    )
    entries = getattr(registry, "_entries", {})
    if isinstance(entries, dict):
        names = {str(name) for name in entries.keys()}
    else:
        names = set(registry.available_tools())
    return names | _umbrella_declared_tool_names()


def validate_phase_tool_contract(
    manifests: Iterable[PhaseManifest],
    *,
    repo_root: pathlib.Path,
) -> list[str]:
    registered = registered_ouroboros_tool_names(repo_root)
    errors: list[str] = []
    for manifest in manifests:
        declared = (
            set(manifest.allowed_tools)
            | set(manifest.forbidden_tools)
            | set(manifest.exit_criteria.required_calls)
        )
        missing = sorted(name for name in declared if name not in registered)
        if missing:
            errors.append(
                f"{manifest.id}: unknown tool(s) in phase contract: {', '.join(missing)}"
            )
    return errors
