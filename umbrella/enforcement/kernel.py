"""Phase-aware capability enforcement for agent-facing workspace tools.

The kernel is intentionally policy-shaped rather than prompt-shaped.  It does
not try to understand every project domain; it enforces the control-plane
boundary that must hold for all domains:

* agent tools cannot mutate supervisor memory/log/control/ledger paths;
* verifier/evaluator/policy files are supervisor-owned unless explicitly
  approved by a future workflow;
* shell-like tools are treated as read-only and audited by filesystem diff;
* phase/tool/path decisions return typed issue codes for regression tests.
"""

from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import re
from typing import Any, Iterable


_WRITE_TOOLS = {
    "apply_workspace_patch",
    "replace_workspace_file",
    "delete_workspace_file",
    "sandbox_self_edit",
    "claude_code_edit",
}
_SHELL_TOOLS = {"run_workspace_command", "terminal_session", "run_python_code", "shell"}
_READ_ONLY_VERIFICATION_TOOLS = {
    "run_subtask_proof",
    "run_workspace_verify",
    "run_unit_tests",
    "run_real_e2e",
}
_KNOWN_PHASES = {
    "preflight",
    "research",
    "research_review",
    "plan",
    "plan_review",
    "execute",
    "subtask_review",
    "final_review",
    "verify",
    "self_improve",
}
_AGENT_WRITE_PHASES = {"execute", "verify", "final_review", "self_improve"}
_SHELL_PHASES = _KNOWN_PHASES
_SUPERVISOR_PREFIXES = (
    ".memory/",
    ".umbrella/",
    ".umbrella_scratch/",
    "logs/",
    "control/",
    "phase_signals/",
)
_EVALUATOR_PREFIXES = (
    ".evaluator/",
    "evaluator/",
    "hidden_evaluator/",
    ".hidden_evaluator/",
)
_VERIFIER_POLICY_FILES = {
    "verify.sh",
    "verification.toml",
    "workspace.toml",
}
_VERIFIER_POLICY_PREFIXES = (
    "policies/",
    ".policies/",
    "verification/",
    ".verification/",
)
_TEST_PATH_RE = re.compile(r"(^|/)(tests?/|test_[^/]+\.py$)")
_CACHE_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".hypothesis",
    "node_modules",
    ".next",
    "dist",
    "build",
}
_SOURCE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs"}
_CONFIG_FILES = {"pyproject.toml", "package.json", "tsconfig.json", "vite.config.ts"}
_INTERNAL_OBSERVATION_PREFIXES = (
    ".memory/drive/logs/",
    ".memory/drive/state/",
    ".umbrella/",
)


@dataclass(frozen=True)
class EnforcementIssue:
    code: str
    message: str
    path: str = ""
    severity: str = "error"


@dataclass(frozen=True)
class FileSnapshotEntry:
    size: int
    mtime_ns: int
    digest: str
    content: bytes | None = None


@dataclass(frozen=True)
class FilesystemChange:
    path: str
    kind: str


@dataclass(frozen=True)
class FilesystemSnapshot:
    root: Path
    entries: dict[str, FileSnapshotEntry]


def phase_from_context(ctx: Any) -> str:
    """Return the current phase id from a PhaseRunner/Ouroboros context."""

    for attr in ("phase", "phase_id", "current_phase"):
        value = str(getattr(ctx, attr, "") or "").strip().lower()
        if value in _KNOWN_PHASES:
            return value
    overlays = getattr(ctx, "context_overlays", None)
    if isinstance(overlays, dict):
        for key in ("phase_node", "phase_manifest"):
            value = overlays.get(key)
            if not isinstance(value, dict):
                continue
            for field in ("id", "manifest_id"):
                phase_id = str(value.get(field) or "").strip().lower()
                if phase_id in _KNOWN_PHASES:
                    return phase_id
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if ":" in task_id:
        for part in reversed(task_id.split(":")):
            phase_id = part.strip().lower()
            if phase_id in _KNOWN_PHASES:
                return phase_id
    view = getattr(ctx, "loop_state_view", None)
    if isinstance(view, dict):
        for key in ("umbrella_phase_id", "phase_id", "current_phase_id", "phase_label"):
            phase_id = str(view.get(key) or "").strip().lower()
            if phase_id in _KNOWN_PHASES:
                return phase_id
    label = str(getattr(ctx, "phase_label", "") or "").strip().lower()
    if label in _KNOWN_PHASES:
        return label
    return ""


def normalise_workspace_path(path: str | Path) -> str:
    text = str(path or "").replace("\\", "/").strip().strip("\"'`")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(normalise_workspace_path(path).lower()))


def classify_sensitive_path(path: str) -> str:
    rel = normalise_workspace_path(path)
    lower = rel.lower()
    if not lower:
        return "empty"
    if lower.startswith("../") or "/../" in lower:
        return "path_traversal"
    if any(lower.startswith(prefix) for prefix in _SUPERVISOR_PREFIXES):
        return "supervisor_path"
    if any(lower.startswith(prefix) for prefix in _EVALUATOR_PREFIXES):
        return "hidden_evaluator"
    if lower in _VERIFIER_POLICY_FILES or any(
        lower.startswith(prefix) for prefix in _VERIFIER_POLICY_PREFIXES
    ):
        return "verifier_policy"
    if is_test_path(lower):
        return "tests"
    return ""


def _is_internal_observation_path(path: str) -> bool:
    lower = normalise_workspace_path(path).lower()
    return any(lower.startswith(prefix) for prefix in _INTERNAL_OBSERVATION_PREFIXES)


def check_tool_allowed(tool_name: str, phase: str) -> list[EnforcementIssue]:
    tool = str(tool_name or "").strip()
    phase = str(phase or "").strip()
    if not phase:
        return []
    if tool in _WRITE_TOOLS and phase not in _AGENT_WRITE_PHASES:
        return [
            EnforcementIssue(
                code="tool_not_allowed_in_phase",
                message=f"`{tool}` is not allowed to mutate the workspace during phase `{phase}`.",
            )
        ]
    if tool in _SHELL_TOOLS and phase not in _SHELL_PHASES:
        return [
            EnforcementIssue(
                code="tool_not_allowed_in_phase",
                message=f"`{tool}` is not allowed during phase `{phase}`.",
            )
        ]
    return []


def check_workspace_paths(
    tool_name: str,
    phase: str,
    paths: Iterable[str | Path],
    *,
    write_kind: str = "modify",
    allow_verifier_policy_edit: bool = False,
) -> list[EnforcementIssue]:
    """Validate planned workspace path mutations before a tool writes."""

    issues = list(check_tool_allowed(tool_name, phase))
    for raw in paths:
        rel = normalise_workspace_path(raw)
        category = classify_sensitive_path(rel)
        if category in {"empty", "path_traversal"}:
            issues.append(
                EnforcementIssue(
                    code=category,
                    path=rel,
                    message=f"Refusing `{tool_name}` path `{rel}`.",
                )
            )
        elif category in {"supervisor_path", "hidden_evaluator"}:
            issues.append(
                EnforcementIssue(
                    code=f"{category}_write_denied",
                    path=rel,
                    message=(
                        f"`{rel}` is supervisor/evaluator-owned. Agent-facing "
                        "workspace tools may read summaries but cannot mutate this path."
                    ),
                )
            )
        elif category == "verifier_policy" and not allow_verifier_policy_edit:
            issues.append(
                EnforcementIssue(
                    code="verifier_policy_write_requires_supervisor_approval",
                    path=rel,
                    message=(
                        f"`{rel}` controls verification or policy. Evaluator "
                        "configuration is supervisor-owned and cannot be changed "
                        "through normal agent workspace edits."
                    ),
                )
            )
        elif category == "tests" and str(write_kind).lower() == "delete":
            issues.append(
                EnforcementIssue(
                    code="test_deletion_requires_supervisor_approval",
                    path=rel,
                    message=(
                        f"`{rel}` is a test/probe file. Deleting tests is a "
                        "tamper-sensitive mutation and requires a plan mutation "
                        "or supervisor approval."
                    ),
                )
            )
    return issues


def blocked_payload(
    issues: Iterable[EnforcementIssue],
    *,
    tool_name: str = "",
    phase: str = "",
    touched_files: Iterable[str] = (),
) -> dict[str, Any]:
    issue_list = [issue for issue in issues if issue.severity == "error"]
    return {
        "status": "blocked",
        "reason": "umbrella_enforcement_kernel",
        "tool": tool_name,
        "phase": phase or None,
        "issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "path": issue.path,
                "severity": issue.severity,
            }
            for issue in issue_list
        ],
        "touched_files": [normalise_workspace_path(p) for p in touched_files],
        "next_step": (
            "Change source/docs/tests through the declared workspace tools only. "
            "Supervisor memory, logs, policy, verification, and evaluator paths "
            "are not candidate workspace deliverables."
        ),
    }


def _is_snapshot_ignored(rel: str) -> bool:
    parts = set(Path(rel).parts)
    return bool(parts & _CACHE_PARTS)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_entry(path: Path, *, capture_content: bool) -> FileSnapshotEntry:
    if capture_content:
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        stat = path.stat()
        return FileSnapshotEntry(
            size=int(stat.st_size),
            mtime_ns=int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))),
            digest=digest,
            content=content,
        )
    stat = path.stat()
    return FileSnapshotEntry(
        size=int(stat.st_size),
        mtime_ns=int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))),
        digest=_digest(path),
    )


def snapshot_workspace(
    root: str | Path,
    *,
    capture_content: bool = False,
) -> FilesystemSnapshot:
    root_path = Path(root).resolve()
    entries: dict[str, FileSnapshotEntry] = {}
    if not root_path.exists():
        return FilesystemSnapshot(root=root_path, entries={})
    for path in root_path.rglob("*"):
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(root_path).as_posix()
            if _is_snapshot_ignored(rel):
                continue
            entries[rel] = _snapshot_entry(path, capture_content=capture_content)
        except (OSError, ValueError):
            continue
    return FilesystemSnapshot(root=root_path, entries=entries)


def diff_snapshots(
    before: FilesystemSnapshot, after: FilesystemSnapshot
) -> list[FilesystemChange]:
    changes: list[FilesystemChange] = []
    before_keys = set(before.entries)
    after_keys = set(after.entries)
    for path in sorted(after_keys - before_keys):
        changes.append(FilesystemChange(path=path, kind="created"))
    for path in sorted(before_keys - after_keys):
        changes.append(FilesystemChange(path=path, kind="deleted"))
    for path in sorted(before_keys & after_keys):
        before_entry = before.entries[path]
        after_entry = after.entries[path]
        if (
            before_entry.size != after_entry.size
            or before_entry.mtime_ns != after_entry.mtime_ns
            or before_entry.digest != after_entry.digest
        ):
            changes.append(FilesystemChange(path=path, kind="modified"))
    return changes


def _prune_empty_parents(path: Path, root: Path) -> None:
    current = path.parent
    root = root.resolve()
    while True:
        try:
            if current.resolve() == root:
                return
            current.rmdir()
        except OSError:
            return
        current = current.parent


def restore_snapshot_changes(
    before: FilesystemSnapshot,
    changes: Iterable[FilesystemChange],
) -> dict[str, Any]:
    """Best-effort rollback for blocked opaque tool side effects."""

    restored: list[str] = []
    errors: list[dict[str, str]] = []
    root = before.root.resolve()
    for change in changes:
        rel = normalise_workspace_path(change.path)
        if not rel:
            continue
        try:
            target = (root / rel).resolve()
            if not (target == root or str(target).startswith(str(root) + os.sep)):
                errors.append({"path": rel, "error": "outside_workspace"})
                continue
            if change.kind == "created":
                if target.is_file() or target.is_symlink():
                    target.unlink()
                    restored.append(rel)
                    _prune_empty_parents(target, root)
                elif target.exists():
                    errors.append({"path": rel, "error": "created_path_not_file"})
                else:
                    restored.append(rel)
                continue

            entry = before.entries.get(rel)
            if entry is None or entry.content is None:
                errors.append({"path": rel, "error": "missing_snapshot_content"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(entry.content)
            restored.append(rel)
        except OSError as exc:
            errors.append({"path": rel, "error": str(exc)})
    return {"restored": restored, "errors": errors}


def check_post_tool_diff(
    tool_name: str,
    phase: str,
    changes: Iterable[FilesystemChange],
    *,
    allow_cache_writes: bool = True,
) -> list[EnforcementIssue]:
    """Validate actual filesystem mutations after an opaque tool call."""

    issues = list(check_tool_allowed(tool_name, phase))
    for change in changes:
        rel = normalise_workspace_path(change.path)
        if allow_cache_writes and _is_snapshot_ignored(rel):
            continue
        if _is_internal_observation_path(rel):
            continue
        category = classify_sensitive_path(rel)
        if category in {"supervisor_path", "hidden_evaluator"}:
            issues.append(
                EnforcementIssue(
                    code=f"post_tool_{category}_mutation",
                    path=rel,
                    message=(
                        f"`{tool_name}` mutated `{rel}` ({change.kind}), but "
                        "supervisor/evaluator paths are not writable by agent tools."
                    ),
                )
            )
        elif category == "verifier_policy":
            issues.append(
                EnforcementIssue(
                    code="post_tool_verifier_policy_mutation",
                    path=rel,
                    message=(
                        f"`{tool_name}` mutated verifier/policy path `{rel}` "
                        f"({change.kind}). Verification contracts are immutable "
                        "from the candidate workspace."
                    ),
                )
            )
        elif category == "tests" and change.kind == "deleted":
            issues.append(
                EnforcementIssue(
                    code="post_tool_test_deleted",
                    path=rel,
                    message=f"`{tool_name}` deleted test/probe file `{rel}`.",
                )
            )
        elif tool_name in (_SHELL_TOOLS | _READ_ONLY_VERIFICATION_TOOLS) and change.kind in {"created", "modified", "deleted"}:
            issues.append(
                EnforcementIssue(
                    code="shell_tool_workspace_mutation",
                    path=rel,
                    message=(
                        f"`{tool_name}` changed `{rel}` ({change.kind}). Shell "
                        "tools are verification/read-only surfaces; use "
                        "`apply_workspace_patch` or `delete_workspace_file` for "
                        "sanctioned candidate edits."
                    ),
                )
            )
    return issues


def check_verification_step_diff(
    changes: Iterable[FilesystemChange],
) -> list[EnforcementIssue]:
    """Reject verifier steps that mutate candidate code, tests, or policy."""

    issues: list[EnforcementIssue] = []
    for change in changes:
        rel = normalise_workspace_path(change.path)
        if _is_snapshot_ignored(rel):
            continue
        category = classify_sensitive_path(rel)
        suffix = Path(rel).suffix.lower()
        name = Path(rel).name.lower()
        if category in {"supervisor_path", "hidden_evaluator"}:
            issues.append(
                EnforcementIssue(
                    code=f"verification_{category}_mutation",
                    path=rel,
                    message=(
                        f"Verification step mutated supervisor/evaluator path "
                        f"`{rel}` ({change.kind})."
                    ),
                )
            )
        elif category == "verifier_policy" or name in _CONFIG_FILES:
            issues.append(
                EnforcementIssue(
                    code="verification_policy_mutation",
                    path=rel,
                    message=(
                        f"Verification step mutated verifier/config path `{rel}` "
                        f"({change.kind})."
                    ),
                )
            )
        elif category == "tests":
            issues.append(
                EnforcementIssue(
                    code="verification_test_mutation",
                    path=rel,
                    message=f"Verification step mutated test/probe `{rel}` ({change.kind}).",
                )
            )
        elif suffix in _SOURCE_EXTS:
            issues.append(
                EnforcementIssue(
                    code="verification_source_mutation",
                    path=rel,
                    message=f"Verification step mutated source file `{rel}` ({change.kind}).",
                )
            )
    return issues


def has_dangerous_claude_permission_mode(env: dict[str, str] | None = None) -> bool:
    effective = dict(os.environ)
    if env:
        effective.update(env)
    mode = effective.get("OUROBOROS_CLAUDE_CODE_PERMISSION_MODE", "")
    return mode.strip().lower() in {"bypasspermissions", "dangerously-skip-permissions"}


__all__ = [
    "EnforcementIssue",
    "FilesystemChange",
    "FilesystemSnapshot",
    "blocked_payload",
    "check_post_tool_diff",
    "check_tool_allowed",
    "check_verification_step_diff",
    "check_workspace_paths",
    "classify_sensitive_path",
    "diff_snapshots",
    "has_dangerous_claude_permission_mode",
    "is_test_path",
    "normalise_workspace_path",
    "phase_from_context",
    "snapshot_workspace",
]
