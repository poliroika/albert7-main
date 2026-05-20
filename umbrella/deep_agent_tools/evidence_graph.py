"""Typed evidence graph helpers shared by Umbrella phase gates.

This module is intentionally small: it models which proof files are available
to a phase-plan leaf before that leaf's ``success_test`` is allowed to run.
The goal is to keep execution contracts structural instead of relying on
phase-specific wording checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Iterator

from umbrella.deep_agent_tools.phase_contract_common import (
    _PLAN_FILE_FIELD_KEYS,
    _PLAN_SUCCESS_TEST_KEYS,
)


_PYTEST_COMMAND_SEGMENT_RE = re.compile(
    r"(?i)\b(?:python\s+-m\s+pytest|pytest)\b(?P<args>.*)$"
)
_EVIDENCE_FILE_FIELD_KEYS = set(_PLAN_FILE_FIELD_KEYS) | {
    "contract_migration_files",
    "success_test_contract_migration_files",
}


@dataclass(frozen=True)
class EvidenceIssue:
    code: str
    message: str
    subtask_id: str = ""
    targets: tuple[str, ...] = ()


def split_shell_command_segments(value: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    text = str(value or "")
    while index < len(text):
        ch = text[index]
        if escaped:
            current.append(ch)
            escaped = False
            index += 1
            continue
        if ch == "\\":
            current.append(ch)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            current.append(ch)
            index += 1
            continue
        if text.startswith("&&", index) or text.startswith("||", index):
            segments.append("".join(current))
            current = []
            index += 2
            continue
        if ch == ";":
            segments.append("".join(current))
            current = []
            index += 1
            continue
        current.append(ch)
        index += 1
    segments.append("".join(current))
    return segments


def pytest_file_targets_from_success_test(value: str) -> list[str]:
    targets: list[str] = []
    for segment in split_shell_command_segments(str(value or "")):
        match = _PYTEST_COMMAND_SEGMENT_RE.search(segment)
        if not match:
            continue
        for token in re.split(r"\s+", match.group("args").strip()):
            cleaned = token.strip().strip("`'\"()[]{}.,;")
            if not cleaned or cleaned.startswith("-"):
                continue
            cleaned = cleaned.split("::", 1)[0].replace("\\", "/").lstrip("./")
            if cleaned.startswith("tests/") and cleaned.endswith(".py"):
                targets.append(cleaned)
    return list(dict.fromkeys(targets))


def _normalise_path(value: str, *, workspace_id: str = "") -> str:
    path = str(value or "").strip().strip("`'\"").replace("\\", "/").lstrip("./")
    workspace = str(workspace_id or "").strip().strip("/\\")
    if workspace:
        for prefix in (f"workspaces/{workspace}/", f"{workspace}/"):
            if path.startswith(prefix):
                return path[len(prefix) :]
    return path


def _iter_file_field_path_strings(
    value: Any, *, in_file_field: bool = False, workspace_id: str = ""
) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_in_file_field = in_file_field or str(key).strip().lower() in _EVIDENCE_FILE_FIELD_KEYS
            yield from _iter_file_field_path_strings(
                child,
                in_file_field=child_in_file_field,
                workspace_id=workspace_id,
            )
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_file_field_path_strings(
                item,
                in_file_field=in_file_field,
                workspace_id=workspace_id,
            )
    elif in_file_field and isinstance(value, str):
        path = _normalise_path(value, workspace_id=workspace_id)
        if path:
            yield path


def _subtask_label(item: dict[str, Any], idx: int) -> str:
    return str(
        item.get("id")
        or item.get("subtask_id")
        or item.get("title")
        or item.get("name")
        or f"subtask #{idx}"
    )


def _success_test_text(item: dict[str, Any]) -> str:
    raw = None
    for key in (
        "success_test",
        "success_check",
        "success_checks",
        "acceptance_command",
        "verification_command",
        "verification_commands",
        "verification",
        "test_strategy",
        "test",
    ):
        if key not in _PLAN_SUCCESS_TEST_KEYS:
            continue
        raw = item.get(key)
        if raw:
            break
    if isinstance(raw, dict):
        parts: list[str] = []
        for key in ("value", "command", "cmd", "text", "pytest_id", "verification"):
            value = raw.get(key)
            if value:
                parts.append(str(value))
        return " ".join(parts).strip()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return "; ".join(str(value).strip() for value in raw if str(value).strip())
    return str(raw or "").strip()


class PhasePlanEvidenceGraph:
    def __init__(
        self,
        *,
        subtasks: Iterable[dict[str, Any]],
        plan: dict[str, Any],
        workspace_root: Path | None = None,
        workspace_id: str = "",
    ) -> None:
        self.subtasks = [item for item in subtasks if isinstance(item, dict)]
        self.plan = plan
        self.workspace_root = workspace_root
        self.workspace_id = workspace_id
        self.declared_paths = set(
            _iter_file_field_path_strings(plan, workspace_id=workspace_id)
        )
        self.declared_paths_by_index = [
            set(_iter_file_field_path_strings(item, workspace_id=workspace_id))
            for item in self.subtasks
        ]

    def _workspace_file_exists(self, rel_path: str) -> bool:
        return bool(self.workspace_root is not None and (self.workspace_root / rel_path).is_file())

    def pytest_target_availability_issues(self) -> list[EvidenceIssue]:
        issues: list[EvidenceIssue] = []
        available_from_prior_leaves: set[str] = set()
        for idx, subtask in enumerate(self.subtasks, start=1):
            label = _subtask_label(subtask, idx)
            declared_now = self.declared_paths_by_index[idx - 1]
            available_now = available_from_prior_leaves | declared_now
            if not declared_now:
                available_from_prior_leaves.update(declared_now)
                continue
            missing: list[str] = []
            declared_later: list[str] = []
            for target in pytest_file_targets_from_success_test(_success_test_text(subtask)):
                if target in available_now or self._workspace_file_exists(target):
                    continue
                if target in self.declared_paths:
                    declared_later.append(target)
                else:
                    missing.append(target)
            if missing:
                issues.append(
                    EvidenceIssue(
                        code="missing_pytest_proof_target",
                        subtask_id=label,
                        targets=tuple(missing),
                        message=(
                            f"subtask `{label}` success_test targets pytest file(s) "
                            f"{missing[:4]}, but those proof file(s) are not "
                            "declared in the same or an earlier plan leaf and do "
                            "not exist in the workspace. Add the checked-in test "
                            "file to this leaf's files_to_create/files_to_change/"
                            "files_affected, or change the success_test to a proof "
                            "owned by the leaf."
                        ),
                    )
                )
            if declared_later:
                issues.append(
                    EvidenceIssue(
                        code="future_pytest_proof_target",
                        subtask_id=label,
                        targets=tuple(declared_later),
                        message=(
                            f"subtask `{label}` success_test targets pytest file(s) "
                            f"{declared_later[:4]} that are declared only by a "
                            "later plan leaf. A leaf must run proof targets that "
                            "already exist or that it owns itself; move the test "
                            "file into this leaf's file contract or move the "
                            "success_test to the leaf that creates the proof."
                        ),
                    )
                )
            available_from_prior_leaves.update(declared_now)
        return issues


def phase_plan_pytest_target_availability_messages(
    *,
    subtasks: Iterable[dict[str, Any]],
    plan: dict[str, Any],
    workspace_root: Path | None = None,
    workspace_id: str = "",
) -> list[str]:
    graph = PhasePlanEvidenceGraph(
        subtasks=subtasks,
        plan=plan,
        workspace_root=workspace_root,
        workspace_id=workspace_id,
    )
    return [issue.message for issue in graph.pytest_target_availability_issues()]


__all__ = [
    "EvidenceIssue",
    "PhasePlanEvidenceGraph",
    "phase_plan_pytest_target_availability_messages",
    "pytest_file_targets_from_success_test",
    "split_shell_command_segments",
]
