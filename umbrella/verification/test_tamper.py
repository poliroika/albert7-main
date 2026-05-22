"""Compatibility facade for test/evaluator tamper checks."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from umbrella.verification.diff_policy import (
    DiffPolicyIssue,
    run_diff_policy_guard,
    scan_unified_diff,
    scan_workspace_files,
)


def scan_test_tampering(
    workspace_path: str | Path,
    *,
    changed_files: Iterable[str] = (),
) -> list[DiffPolicyIssue]:
    return scan_workspace_files(workspace_path, changed_files=changed_files)


__all__ = [
    "DiffPolicyIssue",
    "run_diff_policy_guard",
    "scan_test_tampering",
    "scan_unified_diff",
    "scan_workspace_files",
]
