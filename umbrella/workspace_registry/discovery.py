"""
Workspace discovery and loading logic.

Discovers workspaces from the filesystem and loads their configurations.
"""

import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from umbrella.workspace_registry.models import (
    WorkspaceRef,
    SeedWorkspaceProfile,
    TaskInstanceProfile,
    WorkspaceLineageRecord,
    WorkspaceType,
    WorkspaceMaturity,
    WorkspaceCapability,
    WorkspaceSelectionHint,
    RegistryManifest,
)

_IGNORED_DISCOVERY_DIRS = {
    "runs",
    "snapshots",
    "reports",
    "memory",
    "logs",
    "__pycache__",
    ".git",
    "archived",
}


def load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file and return its contents."""
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_registry_manifest(root: Path) -> RegistryManifest | None:
    """
    Load workspaces/registry.toml if present.

    Declared seeds are validated in build_registry against discovered workspaces
    and loadable seed_profile.toml files.
    """
    path = Path(root) / "workspaces" / "registry.toml"
    if not path.exists():
        return None
    try:
        data = load_toml(path)
        seeds_raw = data.get("seeds", [])
        instances_raw = data.get("instances", [])
        seeds = tuple(str(s) for s in seeds_raw)
        instances = tuple(str(i) for i in instances_raw)
        version = str(data.get("version", "0.0.0"))
        return RegistryManifest(version=version, seeds=seeds, instances=instances)
    except Exception:
        return None


def discover_workspaces(root: Path) -> list[WorkspaceRef]:
    """
    Discover all workspaces under a root directory.

    Looks for directories containing workspace.toml files.
    """
    root_path = Path(root)
    workspaces_path = root_path / "workspaces"

    if not workspaces_path.exists():
        return []

    discovered = []
    config_paths = sorted(
        workspaces_path.rglob("workspace.toml"),
        key=lambda path: (len(path.relative_to(workspaces_path).parts), str(path)),
    )
    for config_path in config_paths:
        relative_parts = config_path.relative_to(workspaces_path).parts[:-1]
        if any(part in _IGNORED_DISCOVERY_DIRS for part in relative_parts):
            continue
        ref = load_workspace_config(config_path)
        if ref:
            discovered.append(ref)

    return discovered


def load_workspace_config(config_path: Path) -> WorkspaceRef | None:
    """
    Load workspace configuration from a workspace.toml file.

    Args:
        config_path: Path to the workspace.toml file

    Returns:
        WorkspaceRef if successful, None otherwise
    """
    if not config_path.exists():
        return None

    try:
        config = load_toml(config_path)
        workspace_dir = config_path.parent

        # Extract required fields
        workspace_id = config.get("workspace_id", workspace_dir.name)
        name = config.get("name", workspace_id)
        description = config.get("description", "")

        task_main_file = config.get("task_main_file")
        if not task_main_file:
            task_main_file = "TASK_MAIN.md"

        # Create WorkspaceRef
        ref = WorkspaceRef(
            workspace_id=workspace_id,
            name=name,
            description=description,
            path=workspace_dir,
            task_main_file=task_main_file,
            graph_file=config.get("graph_file"),
            agents_dir=config.get("agents_dir"),
            prompts_dir=config.get("prompts_dir"),
            tools_allowlist_file=config.get("tools_allowlist_file"),
            models_file=config.get("models_file"),
            policies_file=config.get("policies_file"),
            evals_dir=config.get("evals_dir", "evals"),
            experiments_dir=config.get("experiments_dir", "experiments"),
            runs_dir=config.get("runs_dir", "runs"),
            snapshots_dir=config.get("snapshots_dir", "snapshots"),
            reports_dir=config.get("reports_dir", "reports"),
            mutable_paths=config.get("mutable_paths", []),
        )

        # Load metadata section
        metadata = config.get("metadata", {})
        ref.engine = metadata.get("engine", "gmas")
        ref.engine_mutable = metadata.get("engine_mutable", False)
        ref.owner = metadata.get("owner", "manual")
        ref.notes = metadata.get("notes", "")

        return ref

    except Exception:
        return None


def load_seed_profile(workspace_path: Path) -> SeedWorkspaceProfile | None:
    """
    Load seed profile from a seed_profile.toml file if it exists.

    Args:
        workspace_path: Path to the workspace directory

    Returns:
        SeedWorkspaceProfile if successful, None otherwise
    """
    profile_path = workspace_path / "seed_profile.toml"
    if not profile_path.exists():
        return None

    try:
        config = load_toml(profile_path)

        # First load the workspace config
        ref = load_workspace_config(workspace_path / "workspace.toml")
        if not ref:
            return None

        # Parse capabilities
        capabilities = []
        for cap_data in config.get("capabilities", []):
            capabilities.append(
                WorkspaceCapability(
                    name=cap_data.get("name", ""),
                    description=cap_data.get("description", ""),
                    weight=cap_data.get("weight", 1.0),
                )
            )

        # Parse selection hints
        hints_data = config.get("selection_hints", {})
        selection_hints = WorkspaceSelectionHint(
            task_classes=hints_data.get("task_classes", []),
            keywords=hints_data.get("keywords", []),
            preferred_for_domains=hints_data.get("preferred_for_domains", []),
            avoided_for_domains=hints_data.get("avoided_for_domains", []),
        )

        # Parse maturity
        maturity_str = config.get("maturity", "experimental")
        try:
            maturity = WorkspaceMaturity(maturity_str)
        except ValueError:
            maturity = WorkspaceMaturity.EXPERIMENTAL

        return SeedWorkspaceProfile(
            ref=ref,
            workspace_type=WorkspaceType.SEED,
            maturity=maturity,
            capabilities=capabilities,
            selection_hints=selection_hints,
            primary_task_classes=config.get("primary_task_classes", []),
            allowed_mutation_surfaces=config.get("allowed_mutation_surfaces", []),
            required_tools=config.get("required_tools", []),
            eval_hooks=config.get("eval_hooks", []),
            human_dependency_level=config.get("human_dependency_level", "medium"),
        )

    except Exception:
        return None


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    return datetime.now(timezone.utc)


def load_task_instance_profile(workspace_path: Path) -> TaskInstanceProfile | None:
    """
    Load a task instance profile from workspace.toml + instance_metadata.json.
    """
    metadata_path = workspace_path / "instance_metadata.json"
    if not metadata_path.exists():
        return None

    ref = load_workspace_config(workspace_path / "workspace.toml")
    if not ref:
        return None

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    lineage_data = metadata.get("lineage", {})
    lineage = WorkspaceLineageRecord(
        lineage_id=str(metadata.get("instance_id") or ref.workspace_id),
        seed_workspace_id=str(metadata.get("seed_workspace_id", "")),
        creation_timestamp=_parse_datetime(
            lineage_data.get("creation_timestamp") or metadata.get("created_at")
        ),
        creation_reason=str(lineage_data.get("creation_reason", "")),
        task_id=metadata.get("task_id"),
        task_brief_summary=str(
            lineage_data.get("task_brief_summary")
            or metadata.get("task_description", "")
        ),
        promotion_eligible=bool(metadata.get("promotion_eligible", False)),
        promotion_candidate=bool(metadata.get("promotion_candidate", False)),
    )

    return TaskInstanceProfile(
        ref=ref,
        workspace_type=WorkspaceType.INSTANCE,
        lineage=lineage,
        seed_profile=None,
        task_brief=str(metadata.get("task_description", "")),
        task_class=str(metadata.get("task_class", "")),
        status=str(metadata.get("status", "created")),
        run_count=int(metadata.get("run_count", 0)),
    )
