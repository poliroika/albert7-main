"""Umbrella enforcement primitives.

This package contains supervisor-owned policy helpers that sit between agent
tools and mutable workspaces.  The first slice is deliberately small and
typed: tool entrypoints can ask one place whether a phase/tool/path mutation is
allowed, and can record immutable-ish supervisor ledger events without letting
the candidate workspace become the source of truth.
"""

from umbrella.enforcement.kernel import (
    EnforcementIssue,
    FilesystemChange,
    blocked_payload,
    check_post_tool_diff,
    check_verification_step_diff,
    check_workspace_paths,
    diff_snapshots,
    phase_from_context,
    restore_snapshot_changes,
    snapshot_workspace,
)
from umbrella.enforcement.ledger import append_supervisor_ledger_event

__all__ = [
    "EnforcementIssue",
    "FilesystemChange",
    "append_supervisor_ledger_event",
    "blocked_payload",
    "check_post_tool_diff",
    "check_verification_step_diff",
    "check_workspace_paths",
    "diff_snapshots",
    "phase_from_context",
    "restore_snapshot_changes",
    "snapshot_workspace",
]
