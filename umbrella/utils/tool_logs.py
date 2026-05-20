"""Helpers for interpreting Ouroboros tool log rows."""

import json
from typing import Any


def is_effective_write_tool_log_row(row: dict[str, Any]) -> bool:
    """Return True when a write-tool log row represents a persisted change."""

    if bool(row.get("is_error")):
        return False
    result = str(
        row.get("result") or row.get("result_preview") or row.get("output") or ""
    ).lstrip()
    if result.startswith("{"):
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            status = str(payload.get("status") or "").strip().lower()
            if status in {"blocked", "error", "failed", "missing", "not_found"}:
                return False
            if status in {"applied", "updated", "ok"}:
                return True
    if result.startswith("OK:"):
        return True
    prefix = result[:500]
    if "GIT_NO_CHANGES" in prefix:
        return False
    if "GIT_COMMIT_DISABLED_BY_POLICY" in prefix:
        return False
    if result.startswith(("ERROR:", "GIT_ERROR", "FILE_WRITE_ERROR", "WARNING:")):
        return False
    if any(marker in prefix for marker in ("TOOL_DENIED", "TOOL_ERROR", "PATH_ERROR")):
        return False
    return True
