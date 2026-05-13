"""One-time migration from monolithic ``.umbrella/memory/*.jsonl`` to per-workspace dirs."""

import json
import logging
from pathlib import Path
from typing import Any

from umbrella.memory.paths import manager_memory_root, workspace_memory_root

log = logging.getLogger(__name__)

_JSONL_NAMES = ("lessons", "gaps", "signals")


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _lesson_workspace_id(data: dict[str, Any]) -> str | None:
    lt = str(data.get("lesson_type") or "").lower()
    if lt == "manager":
        return None
    if lt == "workspace":
        wid = str(data.get("workspace_id") or "").strip()
        return wid or None
    return None


def _signal_workspace_id(data: dict[str, Any]) -> str | None:
    raw = data.get("workspace_id")
    if raw is None:
        return None
    w = str(raw).strip()
    return w or None


def _gap_workspace_id(data: dict[str, Any]) -> str | None:
    meta = data.get("metadata")
    if isinstance(meta, dict):
        w = str(meta.get("workspace_id") or "").strip()
        if w:
            return w
    return None


def _migrate_file(
    repo_root: Path,
    name: str,
    *,
    pick_workspace_id: Any,
) -> dict[str, Any]:
    mgr = manager_memory_root(repo_root)
    src = mgr / f"{name}.jsonl"
    legacy = mgr / f"{name}.legacy.jsonl"
    stats: dict[str, Any] = {"name": name, "migrated_lines": 0, "skipped": False}

    # Idempotent: a prior run left the full backup in ``*.legacy.jsonl``.
    if legacy.exists():
        stats["skipped"] = True
        return stats
    if not src.exists():
        stats["skipped"] = True
        return stats

    lines = [ln for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        stats["skipped"] = True
        return stats

    manager_lines: list[str] = []
    per_ws: dict[str, list[str]] = {}

    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            manager_lines.append(line)
            continue
        if not isinstance(data, dict):
            manager_lines.append(line)
            continue
        wid = pick_workspace_id(data)
        if wid:
            per_ws.setdefault(wid, []).append(line)
        else:
            manager_lines.append(line)

    for wid, bucket in list(per_ws.items()):
        try:
            dest = workspace_memory_root(repo_root, wid) / f"{name}.jsonl"
        except ValueError:
            log.warning("Skipping invalid workspace_id in migration: %r", wid)
            manager_lines.extend(bucket)
            del per_ws[wid]
            continue
        for ln in bucket:
            _append_line(dest, ln)

    # Preserve the original file, then write the manager-only shard in its place.
    src.rename(legacy)
    out_path = mgr / f"{name}.jsonl"
    out_path.write_text(
        "\n".join(manager_lines) + ("\n" if manager_lines else ""), encoding="utf-8"
    )
    stats["migrated_lines"] = len(lines)
    stats["workspace_buckets"] = len(per_ws)
    return stats


def migrate_to_per_workspace(repo_root: Path) -> dict[str, Any]:
    """Split legacy shared JSONL files into per-workspace ``.memory`` dirs.

    Idempotent: if ``*.legacy.jsonl`` exists and the active ``*.jsonl`` was
    recreated, a second call is a no-op for that stream.
    """
    out: dict[str, Any] = {"repo_root": str(repo_root), "streams": []}

    def _pick_lessons(data: dict[str, Any]) -> str | None:
        return _lesson_workspace_id(data)

    def _pick_signals(data: dict[str, Any]) -> str | None:
        return _signal_workspace_id(data)

    def _pick_gaps(data: dict[str, Any]) -> str | None:
        return _gap_workspace_id(data)

    pickers = {
        "lessons": _pick_lessons,
        "gaps": _pick_gaps,
        "signals": _pick_signals,
    }

    for name in _JSONL_NAMES:
        try:
            st = _migrate_file(repo_root, name, pick_workspace_id=pickers[name])
            out["streams"].append(st)
            if st.get("migrated_lines"):
                log.info("migrate_to_per_workspace: %s — %s", name, st)
        except Exception:
            log.exception("migrate_to_per_workspace failed for %s", name)
            out["streams"].append({"name": name, "error": True})

    # Optional: migrate hierarchical palace.jsonl filename is handled in HierarchicalMemory reader.
    return out


__all__ = ["migrate_to_per_workspace"]
