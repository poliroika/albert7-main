"""Legacy success_test completion pre-checks used by Ouroboros regression tests."""

import json
import re
from pathlib import Path
from typing import Any

_PYTEST_PASS_RE = re.compile(r"(?i)\b\d+\s+passed\b")
_PYTEST_FAILURE_RE = re.compile(r"(?i)\b\d+\s+(?:failed|errors?|xfailed)\b")
_FUTURE_VERIFY_MARKERS_RE = re.compile(
    r"(?i)future\s+agent|multi_agent_gmas|skill_runtime|gmas\s+import"
)


def _subtask_success_test_text(subtask: dict[str, Any]) -> str:
    raw = subtask.get("success_test")
    if isinstance(raw, dict):
        for key in ("value", "command", "cmd", "pytest_id", "verification", "text"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _success_test_fragments(success_text: str) -> list[str]:
    text = str(success_text or "").strip()
    if not text:
        return []
    if text.lower().startswith("command:"):
        text = text.split(":", 1)[1].strip()
    text = re.sub(
        r"(?i)\s+exits?\s+with\s+(?:exit\s+)?code\s+\d+\s*$",
        "",
        text,
    ).strip()
    text = re.sub(r"(?i)\s+returns?\s+exit\s+code\s+\d+\s*$", "", text).strip()
    fragments = [text]
    pytest_match = re.search(
        r"(?i)(python\s+-m\s+pytest|pytest)\s+([^\s;]+(?:\s+[^\s;]+)*)",
        text,
    )
    if pytest_match:
        fragments.append(pytest_match.group(0).strip())
        fragments.append(pytest_match.group(2).strip())
    py_c = re.search(r"(?i)(?:^|\s)-c\s+(.+)$", text)
    if py_c:
        fragments.append(py_c.group(1).strip().strip("`'\""))
    return [
        re.sub(r"[^a-z0-9]+", "", fragment.lower())
        for fragment in fragments
        if len(re.sub(r"[^a-z0-9]+", "", fragment.lower())) >= 12
    ]


def _normalise_command_text(args: Any) -> str:
    if not isinstance(args, dict):
        return re.sub(r"[^a-z0-9]+", "", str(args or "").lower())
    raw = args.get("command") or args.get("argv") or args.get("cmd") or ""
    if isinstance(raw, (list, tuple)):
        raw = " ".join(str(part) for part in raw)
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


def _row_result_text(row: dict[str, Any]) -> str:
    for key in ("result_preview", "result", "output", "stderr", "error"):
        value = row.get(key)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _row_succeeded(row: dict[str, Any]) -> bool:
    if row.get("error"):
        return False
    text = _row_result_text(row)
    if not text:
        return bool(row.get("exit_code") == 0)
    if _PYTEST_FAILURE_RE.search(text):
        return False
    if _PYTEST_PASS_RE.search(text):
        return True
    return (
        '"exit_code": 0' in text
        or "'exit_code': 0" in text
        or '"passed": true' in text.lower()
        or '"status": "ok"' in text.lower()
    )


def _success_test_observed(*, success_text: str, rows: list[dict[str, Any]]) -> bool:
    text = str(success_text or "").strip()
    if not text:
        return False
    fragments = _success_test_fragments(text)
    lowered = text.lower()
    explicit_tools = {
        tool
        for tool in (
            "run_workspace_verify",
            "run_unit_tests",
            "harness_run",
            "shell",
        )
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(tool)}(?![A-Za-z0-9_])", lowered)
    }
    for row in rows:
        if not _row_succeeded(row):
            continue
        tool_name = str(row.get("tool") or "")
        if tool_name in explicit_tools and tool_name != "shell":
            return True
        if tool_name == "shell":
            command_text = _normalise_command_text(row.get("args"))
            if any(fragment and fragment in command_text for fragment in fragments):
                return True
    return False


def _subtask_scope_paths(subtask: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for key in (
        "files_to_create",
        "files_to_change",
        "files_affected",
        "contract_migration_files",
    ):
        raw = subtask.get(key)
        if isinstance(raw, str) and raw.strip():
            paths.add(raw.strip().replace("\\", "/").lstrip("/"))
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                norm = str(item or "").strip().replace("\\", "/").lstrip("/")
                if norm:
                    paths.add(norm)
    return paths


def _verify_failure_relevant_to_subtask(
    row: dict[str, Any], *, subtask: dict[str, Any]
) -> bool:
    text = _row_result_text(row)
    if not text.strip():
        return False
    try:
        payload = json.loads(text) if text.lstrip().startswith("{") else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict) and payload.get("passed") is True:
        return False
    summary = text
    if isinstance(payload, dict):
        summary = str(payload.get("summary") or payload.get("output") or text)
    if _FUTURE_VERIFY_MARKERS_RE.search(summary):
        scope_paths = _subtask_scope_paths(subtask)
        if not any(path in summary.replace("\\", "/") for path in scope_paths):
            return False
    scope_paths = _subtask_scope_paths(subtask)
    normalised_summary = summary.replace("\\", "/")
    for path in scope_paths:
        if path and path in normalised_summary:
            return True
    return False


def _load_task_tool_rows(ctx: Any) -> list[dict[str, Any]]:
    drive_root = getattr(ctx, "drive_root", None)
    if drive_root is None:
        return []
    path = Path(drive_root) / "logs" / "tools.jsonl"
    if not path.is_file():
        return []
    task_id = str(getattr(ctx, "task_id", "") or "")
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if task_id and str(row.get("task_id") or "") != task_id:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    return rows


def _phase_subtask_completion_issue(
    ctx: Any,
    *,
    current_phase: dict[str, Any] | None,
    subtask_id: str,
) -> str:
    """Legacy gate: success_test evidence + relevant verify failures before completion."""
    phase = current_phase if isinstance(current_phase, dict) else {}
    subtasks = phase.get("subtasks")
    if not isinstance(subtasks, list):
        return ""
    subtask = next(
        (
            item
            for item in subtasks
            if isinstance(item, dict) and str(item.get("id") or "") == str(subtask_id or "")
        ),
        None,
    )
    if subtask is None:
        return ""
    success_text = _subtask_success_test_text(subtask)
    if not success_text:
        return ""
    rows = _load_task_tool_rows(ctx)
    if not _success_test_observed(success_text=success_text, rows=rows):
        return (
            "ERROR: mark_subtask_complete rejected: no successful tool evidence matched "
            f"the declared success_test for subtask `{subtask_id}`."
        )
    for row in rows:
        if str(row.get("tool") or "") != "run_workspace_verify":
            continue
        if not _row_succeeded(row) and _verify_failure_relevant_to_subtask(row, subtask=subtask):
            return (
                "ERROR: mark_subtask_complete rejected: latest workspace verification "
                "failure is relevant to this subtask. Fix the failing required step "
                "or touched files before marking the subtask done."
            )
    return ""


__all__ = ["_phase_subtask_completion_issue"]
