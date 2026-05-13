"""
Workspace registry for the umbrella integration layer.

This module provides a formal registry for workspaces, allowing
ouroboros to reason about workspaces as managed execution units
instead of treating them as arbitrary folders.
"""

from umbrella.workspace_registry.models import (
    WorkspaceRef,
    SeedWorkspaceProfile,
    TaskInstanceProfile,
    WorkspaceCapability,
    WorkspaceLineageRecord,
    WorkspaceSelectionHint,
    TaskBrief,
    WorkspaceMatch,
    ValidationIssue,
    WorkspaceMaturity,
    WorkspaceType,
    RegistryManifest,
)
from umbrella.workspace_registry.registry import WorkspaceRegistry, build_registry
from umbrella.workspace_registry.task_main import (
    TaskMainDocument,
    TaskMainSection,
    load_task_main,
    validate_task_main_at_path,
    validate_task_main_content,
    build_task_brief_from_task_main,
    render_task_main_template,
    initialize_task_main_for_instance,
)

__all__ = [
    "WorkspaceRef",
    "SeedWorkspaceProfile",
    "TaskInstanceProfile",
    "WorkspaceCapability",
    "WorkspaceLineageRecord",
    "WorkspaceSelectionHint",
    "TaskBrief",
    "WorkspaceMatch",
    "ValidationIssue",
    "WorkspaceMaturity",
    "WorkspaceType",
    "RegistryManifest",
    "WorkspaceRegistry",
    "build_registry",
    # Task main contract
    "TaskMainDocument",
    "TaskMainSection",
    "load_task_main",
    "validate_task_main_at_path",
    "validate_task_main_content",
    "build_task_brief_from_task_main",
    "render_task_main_template",
    "initialize_task_main_for_instance",
]
