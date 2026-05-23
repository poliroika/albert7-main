"""Factory helpers for durable memory backends."""

import os
from pathlib import Path
from typing import Any

from umbrella.memory.backends.canonical import CanonicalMemoryBackend
from umbrella.memory.backends.dual_write import DualWriteDurableBackend
from umbrella.memory.backends.hindsight import HindsightBackend
from umbrella.memory.kernel.telemetry import record_memory_event


def create_durable_backend(
    repo_root: Path,
    *,
    workspace_id: str = "",
    mode: str | None = None,
) -> Any:
    selected = (mode or os.getenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "canonical")).strip().lower()
    canonical = CanonicalMemoryBackend(repo_root=repo_root, workspace_id=workspace_id)
    if selected == "canonical":
        return canonical
    if selected == "hindsight":
        return HindsightBackend.from_env(repo_root=repo_root, workspace_id=workspace_id)
    if selected == "dual":
        return DualWriteDurableBackend(
            primary=canonical,
            secondary=HindsightBackend.from_env(
                repo_root=repo_root,
                workspace_id=workspace_id,
            ),
            secondary_best_effort=True,
        )
    raise ValueError(f"unknown durable memory backend mode: {selected}")


def hindsight_mirror_enabled() -> bool:
    mode = os.getenv("UMBRELLA_MEMORY_DURABLE_BACKEND", "canonical").strip().lower()
    enabled = os.getenv("UMBRELLA_HINDSIGHT_ENABLED", "0").strip().lower()
    return mode in {"dual", "hindsight"} and enabled in {"1", "true", "yes", "on"}


def retain_hindsight_lesson_best_effort(
    *,
    repo_root: Path,
    workspace_id: str,
    lesson: Any,
    op: str = "retain_lesson",
) -> dict[str, Any]:
    if not hindsight_mirror_enabled():
        return {"ok": False, "skipped": True, "reason": "disabled"}
    backend = HindsightBackend.from_env(repo_root=repo_root, workspace_id=workspace_id)
    try:
        return backend.retain_lesson(lesson)
    except Exception as exc:
        record_memory_event(
            repo_root,
            event_type="hindsight_backend_warnings",
            workspace_id=workspace_id,
            backend="hindsight",
            status="failed",
            error=str(exc),
            data={"op": op},
        )
        if os.getenv("UMBRELLA_HINDSIGHT_FAIL_CLOSED", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise
        return {"ok": False, "error": str(exc), "best_effort": True}


def retain_hindsight_event_best_effort(
    *,
    repo_root: Path,
    workspace_id: str,
    event: Any,
    op: str = "retain_event",
) -> dict[str, Any]:
    if not hindsight_mirror_enabled():
        return {"ok": False, "skipped": True, "reason": "disabled"}
    backend = HindsightBackend.from_env(repo_root=repo_root, workspace_id=workspace_id)
    try:
        return backend.retain_event(event)
    except Exception as exc:
        record_memory_event(
            repo_root,
            event_type="hindsight_backend_warnings",
            workspace_id=workspace_id,
            backend="hindsight",
            status="failed",
            error=str(exc),
            data={"op": op},
        )
        if os.getenv("UMBRELLA_HINDSIGHT_FAIL_CLOSED", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise
        return {"ok": False, "error": str(exc), "best_effort": True}
