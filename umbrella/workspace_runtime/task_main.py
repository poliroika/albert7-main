"""
Runtime entry points for TASK_MAIN.md on a workspace root.

Delegates to ``umbrella.workspace_registry.task_main`` so managers and runners
depend on a small, stable surface.
"""

from typing import List, Optional

from umbrella.workspace_registry.models import TaskBrief, ValidationIssue, WorkspaceRef
from umbrella.workspace_registry.task_main import (
    TaskMainDocument,
    build_task_brief_from_task_main,
    load_task_main,
    validate_task_main_at_path,
)


def load_task_main_for_workspace(ref: WorkspaceRef) -> TaskMainDocument | None:
    """Load ``TASK_MAIN`` for a workspace reference (uses ``ref.task_main_file``)."""
    return load_task_main(ref.path / ref.task_main_file)


def build_task_brief_for_workspace(
    ref: WorkspaceRef,
    task_id: str | None = None,
) -> TaskBrief | None:
    """Build a ``TaskBrief`` from the workspace's canonical task file, if present."""
    doc = load_task_main_for_workspace(ref)
    if doc is None:
        return None
    return build_task_brief_from_task_main(
        doc,
        task_id=task_id,
        preferred_workspace_id=ref.workspace_id,
    )


def validate_task_main_for_workspace(ref: WorkspaceRef) -> list[ValidationIssue]:
    """Validate the task-main contract for this workspace root."""
    return validate_task_main_at_path(ref.path / ref.task_main_file)
