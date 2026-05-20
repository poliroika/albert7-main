"""Validation helpers for the phase-manifest/tool-registry contract."""

import pathlib
import sys
from typing import Iterable

from umbrella.phases.base import PhaseManifest


def _ensure_ouroboros_import_path(repo_root: pathlib.Path) -> None:
    """Make the nested Ouroboros package importable from bridge entrypoints."""
    outer = (repo_root / "ouroboros").resolve()
    inner = (outer / "ouroboros").resolve()
    if not inner.is_dir():
        return
    sys.path[:] = [
        path
        for path in sys.path
        if pathlib.Path(path or ".").resolve() != outer
    ]
    sys.path.insert(0, str(outer))

    parent = sys.modules.get("ouroboros")
    parent_paths = (
        [
            str(pathlib.Path(str(path)).resolve())
            for path in getattr(parent, "__path__", []) or []
        ]
        if parent is not None
        else []
    )
    if parent is not None and str(inner) not in parent_paths:
        for name in list(sys.modules):
            if name == "ouroboros" or name.startswith("ouroboros."):
                sys.modules.pop(name, None)


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
        return {str(name) for name in entries.keys()}
    return set(registry.available_tools())


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
