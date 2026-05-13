"""
Workspace validation logic.

Validates workspaces against the required contract:
- workspace.toml exists
- TASK_MAIN.md exists
- valid capability tags
- consistent metadata
"""

from typing import List

import tomllib

from umbrella.workspace_registry.models import (
    WorkspaceRef,
    SeedWorkspaceProfile,
    ValidationIssue,
    ValidationSeverity,
)


def validate_workspace(ref: WorkspaceRef) -> list[ValidationIssue]:
    """
    Validate a workspace and return all validation issues.

    Args:
        ref: Workspace reference to validate

    Returns:
        List of validation issues found
    """
    issues = []

    # Check TASK_MAIN.md exists
    issues.extend(validate_task_main(ref))

    # Check workspace.toml fields
    issues.extend(validate_workspace_toml(ref))

    # Check mutable paths
    issues.extend(validate_mutable_paths(ref))

    # Check tool allowlist if specified
    if ref.tools_allowlist_file:
        issues.extend(validate_tool_allowlist(ref))

    return issues


def validate_task_main(ref: WorkspaceRef) -> list[ValidationIssue]:
    """
    Validate that TASK_MAIN.md exists and has reasonable content.

    Args:
        ref: Workspace reference to validate

    Returns:
        List of validation issues
    """
    from umbrella.workspace_registry.task_main import (
        load_task_main,
        validate_task_main_sections,
    )

    issues: list[ValidationIssue] = []
    task_main_path = ref.path / ref.task_main_file

    doc = load_task_main(task_main_path)
    if doc:
        issues.extend(validate_task_main_sections(doc))
    else:
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.ERROR,
                message=f"Required file not found: {ref.task_main_file}",
                path=task_main_path,
                suggestion=f"Create {ref.task_main_file} with task description, objectives, and constraints.",
            )
        )

    return issues


def validate_workspace_toml(ref: WorkspaceRef) -> list[ValidationIssue]:
    """
    Validate workspace.toml configuration.

    Args:
        ref: Workspace reference to validate

    Returns:
        List of validation issues
    """
    issues = []
    config_path = ref.path / "workspace.toml"

    if not config_path.exists():
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.ERROR,
                message="workspace.toml not found",
                path=config_path,
            )
        )
        return issues

    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))

        # Check required fields
        if "workspace_id" not in config and "name" not in config:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    message="workspace.toml missing workspace_id or name field",
                    path=config_path,
                )
            )

        # Check metadata section
        metadata = config.get("metadata", {})
        if not metadata:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.INFO,
                    message="workspace.toml missing [metadata] section",
                    path=config_path,
                    suggestion="Add [metadata] section with engine, owner, and notes.",
                )
            )

    except tomllib.TOMLDecodeError as e:
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.ERROR,
                message=f"Invalid TOML in workspace.toml: {e}",
                path=config_path,
            )
        )

    return issues


def validate_mutable_paths(ref: WorkspaceRef) -> list[ValidationIssue]:
    """
    Validate that mutable_paths are valid directories.

    Args:
        ref: Workspace reference to validate

    Returns:
        List of validation issues
    """
    issues = []

    if not ref.mutable_paths:
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.INFO,
                message="No mutable_paths defined",
                suggestion="Consider defining which directories can be modified.",
            )
        )
        return issues

    for path_str in ref.mutable_paths:
        dir_path = ref.path / path_str
        if not dir_path.exists():
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    message=f"Mutable path does not exist: {path_str}",
                    path=dir_path,
                )
            )

    return issues


def validate_tool_allowlist(ref: WorkspaceRef) -> list[ValidationIssue]:
    """
    Validate tool allowlist configuration.

    Args:
        ref: Workspace reference to validate

    Returns:
        List of validation issues
    """
    issues = []

    if not ref.tools_allowlist_file:
        return issues

    allowlist_path = ref.path / ref.tools_allowlist_file

    if not allowlist_path.exists():
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.WARNING,
                message=f"Tool allowlist file not found: {ref.tools_allowlist_file}",
                path=allowlist_path,
            )
        )
        return issues

    try:
        config = tomllib.loads(allowlist_path.read_text(encoding="utf-8"))
        allowed = config.get("allowed", [])

        if not allowed:
            issues.append(
                ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    message="Tool allowlist is empty",
                    path=allowlist_path,
                    suggestion="Add allowed tools to the 'allowed' list.",
                )
            )

    except tomllib.TOMLDecodeError as e:
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.ERROR,
                message=f"Invalid TOML in tool allowlist: {e}",
                path=allowlist_path,
            )
        )

    return issues


def validate_seed_profile(profile: SeedWorkspaceProfile) -> list[ValidationIssue]:
    """
    Validate a seed workspace profile.

    Args:
        profile: Seed profile to validate

    Returns:
        List of validation issues
    """
    issues = []

    # Validate base workspace
    issues.extend(validate_workspace(profile.ref))

    # Check capabilities
    if not profile.capabilities:
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.INFO,
                message="Seed profile has no capabilities defined",
                suggestion="Add capabilities to help with workspace selection.",
            )
        )

    # Check task classes
    if not profile.primary_task_classes:
        issues.append(
            ValidationIssue(
                severity=ValidationSeverity.INFO,
                message="Seed profile has no primary_task_classes defined",
                suggestion="Define task classes this workspace handles well.",
            )
        )

    return issues
