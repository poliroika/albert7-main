"""Small file-backed status store for Umbrella launcher / Ouroboros runs."""

import json
import os
import time
from pathlib import Path
from typing import Any


def status_dir(repo_root: Path) -> Path:
    return repo_root / ".umbrella" / "launcher"


def status_path(repo_root: Path) -> Path:
    return status_dir(repo_root) / "status.json"


def write_status(repo_root: Path, **fields: Any) -> dict[str, Any]:
    payload = read_status(repo_root)
    if fields.get("active") is True or fields.get("status") == "running":
        for stale_field in ("result", "task_id", "error"):
            if stale_field not in fields:
                payload.pop(stale_field, None)
    payload.update(fields)
    payload["updated_at"] = time.time()
    status_dir(repo_root).mkdir(parents=True, exist_ok=True)
    path = status_path(repo_root)
    # Use per-write temp files to avoid cross-process tmp collisions on Windows.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(5):
        try:
            tmp.replace(path)
            break
        except PermissionError:
            if attempt == 4:
                # Last-resort fallback when AV/indexer briefly locks destination.
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                if tmp.exists():
                    tmp.unlink()
                break
            time.sleep(0.05 * (attempt + 1))
    return payload


def read_status(repo_root: Path) -> dict[str, Any]:
    path = status_path(repo_root)
    if not path.exists():
        return {"active": False, "status": "idle"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return (
            payload
            if isinstance(payload, dict)
            else {"active": False, "status": "invalid"}
        )
    except Exception as exc:
        return {"active": False, "status": "error", "error": str(exc)}
