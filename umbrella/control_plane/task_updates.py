"""
On-demand human task updates for long-running manager sessions.

This module provides a small file-backed inbox so a running Umbrella task can
receive extra instructions from another terminal without restarting the run.
"""

import json
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, Field


class RuntimeTaskUpdate(BaseModel):
    """One human-authored runtime instruction for a manager task."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str
    instruction: str
    source: str = "terminal"
    created_at: float = Field(default_factory=time.time)


def _updates_root(control_state_dir: Path) -> Path:
    return Path(control_state_dir) / "task_updates"


def _task_update_dir(control_state_dir: Path, task_id: str, bucket: str) -> Path:
    path = _updates_root(control_state_dir) / task_id / bucket
    path.mkdir(parents=True, exist_ok=True)
    return path


def queue_runtime_task_update(
    control_state_dir: Path,
    task_id: str,
    instruction: str,
    *,
    source: str = "terminal",
) -> RuntimeTaskUpdate:
    """Persist a runtime task update to the pending inbox."""
    normalized_task_id = str(task_id).strip()
    normalized_instruction = str(instruction).strip()
    if not normalized_task_id:
        raise ValueError("task_id must be a non-empty string")
    if not normalized_instruction:
        raise ValueError("instruction must be a non-empty string")

    update = RuntimeTaskUpdate(
        task_id=normalized_task_id,
        instruction=normalized_instruction,
        source=source.strip() or "terminal",
    )

    pending_dir = _task_update_dir(control_state_dir, normalized_task_id, "pending")
    final_path = pending_dir / f"{int(update.created_at * 1000):013d}_{update.id}.json"
    tmp_path = final_path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(update.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(final_path)
    return update


def consume_runtime_task_updates(
    control_state_dir: Path,
    task_id: str,
) -> list[RuntimeTaskUpdate]:
    """Load and mark all pending runtime updates for a task as applied."""
    pending_dir = _task_update_dir(control_state_dir, task_id, "pending")
    applied_dir = _task_update_dir(control_state_dir, task_id, "applied")
    updates: list[RuntimeTaskUpdate] = []

    for path in sorted(pending_dir.glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        update = RuntimeTaskUpdate.model_validate_json(raw)
        updates.append(update)
        path.replace(applied_dir / path.name)

    return updates
