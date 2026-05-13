"""
Direct workspace code update functionality.

This module provides functions to update workspace code files directly
in the seed workspace, bypassing the instance-only patch mechanism.
This is useful for self-improvement when changes should be persisted
across all future instances.
"""

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from umbrella.workspace_runtime.models import WorkspaceInstance

log = logging.getLogger(__name__)


@dataclass
class CodeUpdateResult:
    """Result of applying a direct code update to workspace."""

    applied: bool
    summary: str
    updated_files: list[str] = field(default_factory=list)
    backup_path: str | None = None
    error: str | None = None


_DEFAULT_BACKUP_KEEP_LAST = 10


def _prune_old_backups(backup_dir: Path, keep_last: int) -> None:
    """Keep the ``keep_last`` newest ``seed_backup_*`` directories, drop the rest."""
    if keep_last <= 0 or not backup_dir.exists() or not backup_dir.is_dir():
        return
    candidates = [
        p
        for p in backup_dir.iterdir()
        if p.is_dir() and p.name.startswith("seed_backup_")
    ]
    if len(candidates) <= keep_last:
        return
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in candidates[keep_last:]:
        try:
            shutil.rmtree(stale)
        except OSError as exc:
            log.warning("Failed to prune backup %s: %s", stale, exc)


def backup_seed_workspace(
    seed_path: Path,
    backup_dir: Path,
    *,
    keep_last: int = _DEFAULT_BACKUP_KEEP_LAST,
) -> Path:
    """Snapshot the seed workspace into ``backup_dir``, then prune older snapshots."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"seed_backup_{timestamp}"

    important_dirs = ["agents", "graph", "models", "prompts", "tools"]
    important_files = ["workspace.toml", "TASK_MAIN.md", "seed_profile.toml"]

    backup_path.mkdir(parents=True, exist_ok=True)

    for dir_name in important_dirs:
        src = seed_path / dir_name
        if src.exists() and src.is_dir():
            shutil.copytree(src, backup_path / dir_name)

    for file_name in important_files:
        src = seed_path / file_name
        if src.exists() and src.is_file():
            shutil.copy2(src, backup_path / file_name)

    log.info(f"Created seed workspace backup at {backup_path}")

    try:
        _prune_old_backups(backup_dir, keep_last=keep_last)
    except Exception:
        log.debug("Backup prune failed", exc_info=True)

    return backup_path


def update_seed_workspace_file(
    seed_path: Path,
    relative_file_path: str,
    new_content: str,
    create_backup: bool = True,
    backup_dir: Path | None = None,
) -> CodeUpdateResult:
    """Update a single file in the seed workspace.

    Args:
        seed_path: Path to the seed workspace
        relative_file_path: Relative path from seed root (e.g., "agents/test.py")
        new_content: New content for the file
        create_backup: Whether to create a backup before updating
        backup_dir: Directory for backups (defaults to seed_path/backups)

    Returns:
        CodeUpdateResult with update status
    """
    target_file = (seed_path / relative_file_path).resolve()

    # Security check: ensure target is within seed_path
    try:
        target_file.relative_to(seed_path.resolve())
    except ValueError:
        return CodeUpdateResult(
            applied=False,
            summary=f"Security error: path {relative_file_path} escapes seed workspace",
            error="path_escape",
        )

    # Create backup if requested
    backup_path = None
    if create_backup:
        backup_root = backup_dir or (seed_path / "backups")
        backup_path = backup_seed_workspace(seed_path, backup_root)

    # Ensure parent directory exists
    target_file.parent.mkdir(parents=True, exist_ok=True)

    # Write the new content
    try:
        target_file.write_text(new_content, encoding="utf-8")
        log.info(f"Updated seed workspace file: {relative_file_path}")
        return CodeUpdateResult(
            applied=True,
            summary=f"Updated {relative_file_path} in seed workspace",
            updated_files=[str(target_file)],
            backup_path=str(backup_path) if backup_path else None,
        )
    except Exception as e:
        log.error(f"Failed to update {relative_file_path}: {e}")
        return CodeUpdateResult(
            applied=False,
            summary=f"Failed to update {relative_file_path}: {e}",
            error=str(e),
            backup_path=str(backup_path) if backup_path else None,
        )


def update_seed_workspace_from_instance(
    instance: WorkspaceInstance,
    files_to_update: list[str],
    seed_path: Path | None = None,
    create_backup: bool = True,
) -> CodeUpdateResult:
    """Copy specified files from instance to seed workspace.

    This is useful for promoting working code from an instance back to the seed.

    Args:
        instance: The workspace instance to copy from
        files_to_update: List of relative file paths to copy
        seed_path: Path to seed workspace (auto-detected from instance if None)
        create_backup: Whether to create a backup before updating

    Returns:
        CodeUpdateResult with update status
    """
    # Detect seed path from instance
    if seed_path is None:
        # instance.path is like workspaces/agent_research/instances/instance_xxx
        # seed is workspaces/agent_research
        instance_root = instance.path.resolve()
        workspaces_root = (
            instance_root.parent.parent
        )  # Go up from instances to workspace root
        seed_path = workspaces_root

    updated_files = []
    backup_path = None

    # Create backup if requested
    if create_backup:
        backup_dir = seed_path / "backups"
        backup_path = backup_seed_workspace(seed_path, backup_dir)

    # Copy each file
    for relative_path in files_to_update:
        instance_file = instance.path / relative_path
        seed_file = seed_path / relative_path

        if not instance_file.exists():
            log.warning(f"Instance file does not exist: {relative_path}")
            continue

        # Security check
        try:
            seed_file.resolve().relative_to(seed_path.resolve())
        except ValueError:
            log.warning(f"Skipping {relative_path}: path escapes seed workspace")
            continue

        # Ensure parent directory exists
        seed_file.parent.mkdir(parents=True, exist_ok=True)

        # Copy the file
        try:
            shutil.copy2(instance_file, seed_file)
            updated_files.append(relative_path)
            log.info(f"Copied {relative_path} from instance to seed")
        except Exception as e:
            log.error(f"Failed to copy {relative_path}: {e}")

    return CodeUpdateResult(
        applied=len(updated_files) > 0,
        summary=f"Updated {len(updated_files)} file(s) in seed workspace",
        updated_files=updated_files,
        backup_path=str(backup_path) if backup_path else None,
    )


def apply_code_update_to_seed(
    instance: WorkspaceInstance,
    code_updates: dict[str, str],
    seed_path: Path | None = None,
    create_backup: bool = True,
    commit_message: str | None = None,
) -> CodeUpdateResult:
    """Apply direct code updates to seed workspace.

    This function allows Umbrella to update workspace code files directly,
    bypassing the instance-only patch mechanism. Changes are applied
    to the seed workspace and will be used by all future instances.

    Args:
        instance: The workspace instance (used to detect seed path)
        code_updates: Dict of {relative_file_path: new_content}
        seed_path: Path to seed workspace (auto-detected if None)
        create_backup: Whether to create a backup before updating
        commit_message: Optional git commit message (if git is available)

    Returns:
        CodeUpdateResult with update status
    """
    # Detect seed path
    if seed_path is None:
        instance_root = instance.path.resolve()
        workspaces_root = instance_root.parent.parent
        seed_path = workspaces_root

    updated_files = []
    errors = []

    # Create backup
    backup_path = None
    if create_backup:
        backup_dir = seed_path / "backups"
        backup_path = backup_seed_workspace(seed_path, backup_dir)

    # Apply each update
    for relative_path, new_content in code_updates.items():
        result = update_seed_workspace_file(
            seed_path=seed_path,
            relative_file_path=relative_path,
            new_content=new_content,
            create_backup=False,  # Already created backup
        )
        if result.applied:
            updated_files.extend(result.updated_files)
        else:
            errors.append(f"{relative_path}: {result.error}")

    # Optional: commit to git
    if commit_message and updated_files:
        try:
            import subprocess

            subprocess.run(
                ["git", "add"] + [str(seed_path / f) for f in updated_files],
                cwd=seed_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", commit_message],
                cwd=seed_path,
                check=True,
                capture_output=True,
            )
            log.info(f"Committed {len(updated_files)} files to git: {commit_message}")
        except Exception as e:
            log.warning(f"Failed to commit to git: {e}")

    return CodeUpdateResult(
        applied=len(updated_files) > 0,
        summary=f"Updated {len(updated_files)} file(s) in seed workspace"
        + (f" with {len(errors)} error(s)" if errors else ""),
        updated_files=updated_files,
        backup_path=str(backup_path) if backup_path else None,
        error="; ".join(errors) if errors else None,
    )
