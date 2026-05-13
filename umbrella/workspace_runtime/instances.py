"""
Task instance creation and management.

This module handles the creation of task-specific workspace instances
from seed workspaces, including TASK_MAIN.md initialization.
"""

import json
import logging
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from collections.abc import Iterable

log = logging.getLogger(__name__)
from uuid import uuid4

from umbrella.workspace_registry.models import (
    SeedWorkspaceProfile,
    TaskBrief,
)
from umbrella.workspace_registry.task_main import (
    initialize_task_main_for_instance,
    load_task_main,
)
from umbrella.workspace_runtime.models import (
    WorkspaceInstance,
    WorkspaceSnapshot,
)


# Default instance root directory
DEFAULT_INSTANCES_ROOT = "instances"
_INSTANCE_ONLY_DIRS = {"runs", "snapshots", "reports", "memory", "logs"}
_SEED_ONLY_FILES = {"seed_profile.toml", "instance_metadata.json"}
_PROTECTED_REPORT_NAMES = {"latest_report.md", "latest_article.md", "latest_idea.md"}


def create_task_instance(
    seed: SeedWorkspaceProfile,
    task: TaskBrief,
    *,
    instances_root: Path | None = None,
    task_id: str | None = None,
    copy_seed_files: bool = True,
    exclude_patterns: list[str] | None = None,
) -> WorkspaceInstance:
    """
    Create a task-specific workspace instance from a seed.

    This function:
    1. Creates a new instance directory
    2. Copies or branches the seed workspace
    3. Creates or copies TASK_MAIN.md
    4. Initializes instance metadata
    5. Records lineage

    Args:
        seed: The seed workspace profile to base the instance on
        task: The task brief for this instance
        instances_root: Root directory for instances (default: seed path / instances)
        task_id: Optional task ID for tracking
        copy_seed_files: Whether to copy seed files (default: True)
        exclude_patterns: Patterns to exclude when copying

    Returns:
        WorkspaceInstance ready for execution
    """
    # Generate instance ID
    instance_id = f"{seed.workspace_id}_instance_{uuid4().hex[:8]}"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    # Determine instance root - instances should be INSIDE the seed workspace
    if instances_root is None:
        instances_root = seed.path / DEFAULT_INSTANCES_ROOT

    # Create instance path
    instance_path = instances_root / f"{instance_id}_{timestamp}"

    # Default exclude patterns
    default_excludes = [
        "runs/*",
        "snapshots/*",
        "reports/*",
        "memory/*",
        "logs/*",
        "instances/*",
        "__pycache__/*",
        "*.pyc",
        ".git/*",
        ".env",
        "*.log",
        "instance_metadata.json",
        "seed_profile.toml",
    ]
    if exclude_patterns is None:
        exclude_patterns = default_excludes

    # Copy seed files if requested
    if copy_seed_files:
        _copy_workspace(seed.path, instance_path, exclude_patterns)
    else:
        instance_path.mkdir(parents=True, exist_ok=True)

    # Create instance-specific directories
    for subdir in ["runs", "snapshots", "reports", "memory", "logs"]:
        (instance_path / subdir).mkdir(parents=True, exist_ok=True)

    _rewrite_workspace_identity(instance_path, seed, instance_id, task)
    _remove_seed_only_files(instance_path)

    # Create or copy TASK_MAIN.md
    seed_doc = load_task_main(seed.ref.task_main_path)
    task_main_path = initialize_task_main_for_instance(
        instance_path,
        seed_document=seed_doc,
        task_brief=task,
        task_id=task_id,
    )

    # Create instance metadata
    instance_metadata = _create_instance_metadata(
        instance_id=instance_id,
        seed=seed,
        task=task,
        task_id=task_id,
        instance_path=instance_path,
    )
    _write_instance_metadata(instance_path, instance_metadata)

    # Create WorkspaceInstance
    instance = WorkspaceInstance(
        instance_id=instance_id,
        workspace_id=instance_id,
        seed_workspace_id=seed.workspace_id,
        path=instance_path,
        config=instance_metadata,
        created_from_seed=True,
    )

    return instance


def _remove_seed_only_files(instance_path: Path) -> None:
    """Remove files that should not be copied from a seed into a task instance."""
    for filename in _SEED_ONLY_FILES:
        path = instance_path / filename
        if path.exists():
            path.unlink()


def _replace_toml_string(content: str, key: str, value: str) -> str:
    replacement = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(content):
        return pattern.sub(replacement, content, count=1)
    return content.rstrip() + f"\n{replacement}\n"


def _rewrite_workspace_identity(
    instance_path: Path,
    seed: SeedWorkspaceProfile,
    instance_id: str,
    task: TaskBrief,
) -> None:
    """Rewrite workspace.toml so on-disk identity matches the materialized instance."""
    workspace_toml_path = instance_path / "workspace.toml"
    if not workspace_toml_path.exists():
        return

    content = workspace_toml_path.read_text(encoding="utf-8")
    content = _replace_toml_string(content, "workspace_id", instance_id)
    content = _replace_toml_string(content, "name", f"{seed.ref.name} Task Instance")
    content = _replace_toml_string(
        content,
        "description",
        f"Task instance derived from {seed.workspace_id}: {(task.summary if hasattr(task, 'summary') else task.description)[:160]}",
    )
    workspace_toml_path.write_text(content, encoding="utf-8")


def _copy_workspace(
    source: Path,
    destination: Path,
    exclude_patterns: list[str],
) -> None:
    """
    Copy workspace files, respecting exclude patterns.
    """
    import fnmatch

    def should_exclude(path: Path) -> bool:
        rel_path = path.relative_to(source)
        path_str = str(rel_path).replace("\\", "/")
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(path_str, pattern):
                return True
            # Also check individual parts for directory patterns
            if pattern.endswith("/*"):
                dir_pattern = pattern[:-2]
                for part in rel_path.parts:
                    if fnmatch.fnmatch(part, dir_pattern):
                        return True
        return False

    destination.mkdir(parents=True, exist_ok=True)

    for item in source.iterdir():
        if should_exclude(item):
            continue

        dest_item = destination / item.name

        if item.is_file():
            shutil.copy2(item, dest_item)
        elif item.is_dir():
            if item.name in _INSTANCE_ONLY_DIRS:
                # Create empty directories for these
                dest_item.mkdir(exist_ok=True)
            else:
                shutil.copytree(
                    item,
                    dest_item,
                    ignore=shutil.ignore_patterns(
                        *[p.rstrip("/*") for p in exclude_patterns if p.endswith("/*")]
                    ),
                )


def _create_instance_metadata(
    instance_id: str,
    seed: SeedWorkspaceProfile,
    task: TaskBrief,
    task_id: str | None,
    instance_path: Path,
) -> dict[str, Any]:
    """
    Create metadata for a task instance.
    """
    now = datetime.now(timezone.utc)

    return {
        "instance_id": instance_id,
        "seed_workspace_id": seed.workspace_id,
        "task_id": task_id or task.task_id,
        "task_description": task.summary
        if hasattr(task, "summary")
        else task.description,
        "task_class": task.task_class
        if hasattr(task, "task_class")
        else str(task.task_class),
        "domains": task.domains if hasattr(task, "domains") else [],
        "created_at": now.isoformat(),
        "created_from_seed": True,
        "seed_path": str(seed.path),
        "instance_path": str(instance_path),
        "status": "created",
        "run_count": 0,
        "last_run_id": None,
        "promotion_eligible": False,
        "promotion_candidate": False,
        "lineage": {
            "seed_workspace_id": seed.workspace_id,
            "creation_timestamp": now.isoformat(),
            "creation_reason": f"Task: {(task.summary if hasattr(task, 'summary') else task.description)[:100]}",
        },
    }


def _write_instance_metadata(instance_path: Path, metadata: dict[str, Any]) -> None:
    """
    Write instance metadata to the workspace.
    """
    metadata_path = instance_path / "instance_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_instance_metadata(instance_path: Path) -> dict[str, Any] | None:
    """
    Load instance metadata from a workspace.
    """
    metadata_path = instance_path / "instance_metadata.json"
    if not metadata_path.exists():
        return None

    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def update_instance_metadata(
    instance_path: Path,
    updates: dict[str, Any],
) -> None:
    """
    Update instance metadata with new values.
    """
    metadata = load_instance_metadata(instance_path) or {}
    metadata.update(updates)
    metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_instance_metadata(instance_path, metadata)


def _looks_like_instance_dir(path: Path) -> bool:
    """Return True when a directory looks like a materialized task instance."""
    if not path.is_dir():
        return False
    if "instances" not in {part.lower() for part in path.parts}:
        return False
    if "_instance_" not in path.name:
        return False
    return (path / "instance_metadata.json").exists() or (
        path / "workspace.toml"
    ).exists()


def _remove_path(path: Path) -> bool:
    """Remove a file or directory.

    Returns True if successfully removed, False otherwise.
    Retries on Windows to handle temporary file locks.
    """
    import time

    max_retries = 3 if sys.platform == "win32" else 1
    retry_delay = 0.5

    for attempt in range(max_retries):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            return True
        except (PermissionError, OSError) as e:
            if attempt < max_retries - 1:
                log.debug(f"Retry {attempt + 1}/{max_retries} for {path}: {e}")
                time.sleep(retry_delay)
                # Force garbage collection to close file handles
                import gc

                gc.collect()
            else:
                log.warning(
                    f"Failed to remove {path} after {max_retries} attempts: {e}"
                )
                return False
    return False


def _sorted_children(directory: Path) -> list[Path]:
    return sorted(
        [child for child in directory.iterdir()],
        key=lambda child: child.stat().st_mtime,
        reverse=True,
    )


def _drop_older(entries: Iterable[Path], keep: int) -> list[Path]:
    keep_count = max(int(keep), 0)
    collected = list(entries)
    return collected[keep_count:]


def prune_instance_storage(
    instance_path: Path,
    *,
    keep_run_dirs: int = 2,
    keep_snapshots: int = 1,
    keep_report_files: int = 4,
    keep_log_files: int = 2,
) -> list[str]:
    """
    Remove stale run/snapshot/report/log artifacts from an instance.

    Keeps the newest entries plus well-known `latest_*` report files so the
    current task can still be inspected.
    """
    if not _looks_like_instance_dir(instance_path):
        return []

    removed: list[str] = []

    def remove_entries(entries: Iterable[Path]) -> None:
        for entry in entries:
            if _remove_path(entry):
                removed.append(str(entry))

    runs_dir = instance_path / "runs"
    if runs_dir.is_dir():
        remove_entries(_drop_older(_sorted_children(runs_dir), keep_run_dirs))

    snapshots_dir = instance_path / "snapshots"
    if snapshots_dir.is_dir():
        remove_entries(_drop_older(_sorted_children(snapshots_dir), keep_snapshots))

    reports_dir = instance_path / "reports"
    if reports_dir.is_dir():
        report_entries = _sorted_children(reports_dir)
        protected = [
            entry
            for entry in report_entries
            if entry.is_file() and entry.name in _PROTECTED_REPORT_NAMES
        ]
        candidate_entries = [
            entry for entry in report_entries if entry not in protected
        ]
        remove_entries(_drop_older(candidate_entries, keep_report_files))

    logs_dir = instance_path / "logs"
    if logs_dir.is_dir():
        remove_entries(_drop_older(_sorted_children(logs_dir), keep_log_files))

    if removed:
        update_instance_metadata(
            instance_path,
            {
                "storage_pruned_at": datetime.now(timezone.utc).isoformat(),
                "storage_pruned_entries": len(removed),
            },
        )

    return removed


def cleanup_detached_instances(
    instances_root: Path,
    *,
    active_instance_paths: Iterable[Path] | None = None,
    keep_latest_detached: int = 1,
) -> list[str]:
    """
    Delete detached instances that are not referenced by active manager tasks.

    The newest detached instance is kept by default for inspection; everything
    older is removed.
    """
    if not instances_root.is_dir():
        return []

    active_paths = {
        path.resolve() for path in (active_instance_paths or []) if path is not None
    }

    instance_dirs = [
        path
        for path in instances_root.iterdir()
        if path.is_dir() and path.name != "archived" and _looks_like_instance_dir(path)
    ]

    detached = [
        instance_dir
        for instance_dir in sorted(
            instance_dirs, key=lambda item: item.stat().st_mtime, reverse=True
        )
        if instance_dir.resolve() not in active_paths
    ]

    removed: list[str] = []
    for stale in detached[max(int(keep_latest_detached), 0) :]:
        _remove_path(stale)
        removed.append(str(stale))

    return removed


def snapshot_instance(
    instance: WorkspaceInstance,
    label: str,
    description: str = "",
    include_artifacts: bool = True,
) -> WorkspaceSnapshot:
    """
    Create a snapshot of a workspace instance.

    Snapshots enable checkpointing and rollback.

    Args:
        instance: The workspace instance to snapshot
        label: Human-readable label for the snapshot
        description: Optional description
        include_artifacts: Whether to include run artifacts

    Returns:
        WorkspaceSnapshot reference
    """
    snapshot_id = uuid4().hex[:8]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    snapshot_name = f"{timestamp}_{label.replace(' ', '_')[:40]}_{snapshot_id}"

    snapshot_path = instance.path / "snapshots" / snapshot_name
    snapshot_path.mkdir(parents=True, exist_ok=True)

    # Copy key workspace files
    key_files = [
        "TASK_MAIN.md",
        "workspace.toml",
        "instance_metadata.json",
    ]

    for filename in key_files:
        source = instance.path / filename
        if source.exists():
            shutil.copy2(source, snapshot_path / filename)

    # Copy graph directory if it exists
    graph_dir = instance.path / "graph"
    if graph_dir.exists():
        shutil.copytree(graph_dir, snapshot_path / "graph")

    # Copy agents directory if it exists
    agents_dir = instance.path / "agents"
    if agents_dir.exists():
        shutil.copytree(agents_dir, snapshot_path / "agents")

    # Copy prompts directory if it exists
    prompts_dir = instance.path / "prompts"
    if prompts_dir.exists():
        shutil.copytree(prompts_dir, snapshot_path / "prompts")

    # Include artifacts if requested
    if include_artifacts:
        for artifact_dir in ["reports", "runs"]:
            source_dir = instance.path / artifact_dir
            if source_dir.exists():
                # Copy only the most recent items
                items = sorted(
                    source_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
                )[:5]
                dest_dir = snapshot_path / artifact_dir
                dest_dir.mkdir(exist_ok=True)
                for item in items:
                    if item.is_file():
                        shutil.copy2(item, dest_dir / item.name)
                    elif item.is_dir():
                        shutil.copytree(item, dest_dir / item.name)

    # Create snapshot metadata
    snapshot_metadata = {
        "snapshot_id": snapshot_id,
        "instance_id": instance.instance_id,
        "workspace_id": instance.workspace_id,
        "label": label,
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "includes_graph": (snapshot_path / "graph").exists(),
        "includes_prompts": (snapshot_path / "prompts").exists(),
        "includes_artifacts": include_artifacts,
    }

    (snapshot_path / "snapshot_metadata.json").write_text(
        json.dumps(snapshot_metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return WorkspaceSnapshot(
        snapshot_id=snapshot_id,
        instance_id=instance.instance_id,
        workspace_id=instance.workspace_id,
        label=label,
        description=description,
        snapshot_path=snapshot_path,
        source_path=instance.path,
        includes_graph=(snapshot_path / "graph").exists(),
        includes_memory=False,
        includes_prompts=(snapshot_path / "prompts").exists(),
        includes_artifacts=include_artifacts,
    )


def archive_instance(instance: WorkspaceInstance) -> Path:
    """
    Archive a workspace instance.

    Moves the instance to an archive directory.

    Args:
        instance: The workspace instance to archive

    Returns:
        Path to the archived instance
    """
    archive_root = instance.path.parent / "archived"
    archive_root.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive_name = f"{instance.instance_id}_archived_{timestamp}"
    archive_path = archive_root / archive_name

    shutil.move(str(instance.path), str(archive_path))

    # Update metadata
    metadata = load_instance_metadata(archive_path) or {}
    metadata["status"] = "archived"
    metadata["archived_at"] = datetime.now(timezone.utc).isoformat()
    metadata["original_path"] = str(instance.path)
    _write_instance_metadata(archive_path, metadata)

    return archive_path


def list_instance_snapshots(instance: WorkspaceInstance) -> list[WorkspaceSnapshot]:
    """
    List all snapshots for a workspace instance.
    """
    snapshots_dir = instance.path / "snapshots"
    if not snapshots_dir.exists():
        return []

    snapshots = []
    for snapshot_dir in sorted(
        snapshots_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        if snapshot_dir.is_dir():
            metadata_path = snapshot_dir / "snapshot_metadata.json"
            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    snapshots.append(
                        WorkspaceSnapshot(
                            snapshot_id=metadata.get("snapshot_id", ""),
                            instance_id=metadata.get("instance_id", ""),
                            workspace_id=metadata.get("workspace_id", ""),
                            label=metadata.get("label", ""),
                            description=metadata.get("description", ""),
                            snapshot_path=snapshot_dir,
                            source_path=instance.path,
                            includes_graph=metadata.get("includes_graph", False),
                            includes_memory=False,
                            includes_prompts=metadata.get("includes_prompts", False),
                            includes_artifacts=metadata.get(
                                "includes_artifacts", False
                            ),
                        )
                    )
                except Exception:
                    continue

    return snapshots


def restore_snapshot(snapshot: WorkspaceSnapshot) -> WorkspaceInstance:
    """
    Restore a workspace instance from a snapshot.

    Args:
        snapshot: The snapshot to restore

    Returns:
        Restored WorkspaceInstance
    """
    # Create a new instance from the snapshot
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    restore_path = (
        snapshot.source_path.parent / f"{snapshot.instance_id}_restored_{timestamp}"
    )

    # Copy snapshot contents
    shutil.copytree(snapshot.snapshot_path, restore_path)

    # Update metadata
    metadata = load_instance_metadata(restore_path) or {}
    metadata["status"] = "restored"
    metadata["restored_from_snapshot"] = snapshot.snapshot_id
    metadata["restored_at"] = datetime.now(timezone.utc).isoformat()
    _write_instance_metadata(restore_path, metadata)

    return WorkspaceInstance(
        instance_id=snapshot.instance_id,
        workspace_id=snapshot.workspace_id,
        seed_workspace_id=metadata.get("seed_workspace_id", ""),
        path=restore_path,
        config=metadata,
        created_from_seed=False,
    )
