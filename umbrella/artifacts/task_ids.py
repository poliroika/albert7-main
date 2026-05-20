"""Task artifact id helpers shared by Umbrella-side storage code."""

import hashlib


def task_artifact_stem(task_id: str | None, *, max_len: int = 120) -> str:
    """Return a cross-platform filename stem for task-scoped artifacts."""

    raw = str(task_id or "").strip() or "task"
    safe: list[str] = []
    changed = False
    for ch in raw:
        if ord(ch) < 32 or ch in '<>:"/\\|?*':
            safe.append("_")
            changed = True
        else:
            safe.append(ch)
    stem = "".join(safe).strip(" .") or "task"
    if len(stem) > max_len:
        stem = stem[:max_len].rstrip(" ._") or "task"
        changed = True
    if changed or stem != raw:
        suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        room = max(1, max_len - len(suffix) - 1)
        stem = f"{stem[:room].rstrip(' ._')}_{suffix}"
    return stem
