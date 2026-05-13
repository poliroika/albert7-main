"""
Thread-safe live state for long-running manager / Ouroboros runs.

Used by ``umbrella.integration.runner`` to publish progress; snapshots can be
read by tests or future tooling (no separate HTTP server required).
"""

import copy
import threading
import time
from pathlib import Path
from typing import Any

from umbrella.control_plane.models import ActionType


class DashboardRunReporter:
    """Collects steps in the same shape as a manager TaskSession."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._task_id: str | None = None
        self._task_input: str = ""
        self._status: str = "idle"
        self._steps: list[dict[str, Any]] = []
        self._error: str | None = None
        self._started_at: float | None = None
        self._completed_at: float | None = None

    def start(self, task_id: str, task_input: str, task: Any) -> None:
        with self._lock:
            self._task_id = task_id
            self._task_input = task_input
            self._status = "active"
            self._steps = []
            self._error = None
            self._started_at = time.time()
            self._completed_at = None

    def step(
        self,
        *,
        services: Any,
        task: Any,
        action_result: Any,
        manager_result: Any,
        step_idx: int,
    ) -> None:
        action = getattr(action_result, "action", None)
        action_type = action.action_type if action is not None else None
        action_type_str = (
            action_type.value
            if action_type is not None and hasattr(action_type, "value")
            else (str(action_type) if action_type is not None else "none")
        )

        step_data: dict[str, Any] = {
            "step": step_idx,
            "phase": task.state.phase.value,
            "action": action_type_str,
            "outcome": action_result.outcome,
            "summary": action_result.summary,
            "timestamp": time.time(),
        }

        details = getattr(action_result, "details", None) or {}
        if details:
            step_data["details"] = {
                k: str(v) if isinstance(v, Path) else v for k, v in details.items()
            }

        suggested = getattr(action_result, "suggested_next_actions", None) or []
        if suggested:
            step_data["suggested_next"] = [str(a) for a in suggested]

        cp = services.control_plane
        if cp.last_retrieval_card:
            step_data["retrieval"] = {
                "pattern": cp.last_retrieval_card.recommended_pattern,
                "hits": len(cp.last_retrieval_card.hits),
                "key_files": [str(p) for p in cp.last_retrieval_card.key_files[:5]],
            }

        if cp.last_eval_record:
            step_data["eval"] = {
                "score": cp.last_eval_record.overall_score,
                "success": cp.last_eval_record.task_success.value,
                "quality": cp.last_eval_record.output_quality.value,
            }

        with self._lock:
            self._steps.append(step_data)
            if action_type is not None and action_type in (
                ActionType.COMPLETE_TASK,
                ActionType.FAIL_TASK,
            ):
                self._status = (
                    "complete" if action_result.outcome == "success" else "failed"
                )
                self._completed_at = time.time()
            elif action_type is not None and action_type == ActionType.WAIT_FOR_INPUT:
                self._status = "waiting"
                self._completed_at = time.time()

    def done(self, manager_result: Any, exc: BaseException | None = None) -> None:
        with self._lock:
            if exc is not None:
                self._status = "failed"
                self._error = str(exc)
                self._steps.append(
                    {
                        "step": len(self._steps),
                        "phase": "error",
                        "action": "run_manager_task",
                        "outcome": "failure",
                        "summary": str(exc),
                        "timestamp": time.time(),
                    }
                )
            elif self._status == "active":
                self._status = (
                    manager_result.status
                    if manager_result.status != "pending"
                    else "partial"
                )
            self._completed_at = self._completed_at or time.time()

    def snapshot(self, task: Any | None) -> dict[str, Any]:
        with self._lock:
            engine_state = {}
            if task is not None:
                engine_state = {
                    "phase": task.state.phase.value,
                    "iteration_count": task.state.iteration_count,
                    "workspace_id": task.state.current_workspace_id,
                    "instance_path": str(task.state.current_instance_path)
                    if task.state.current_instance_path
                    else None,
                    "retrieval_summary": task.state.retrieval_summary,
                    "retrieval_hit_count": task.state.retrieval_hit_count,
                    "last_patch_files": list(task.state.last_patch_files),
                    "last_patch_summary": task.state.last_patch_summary,
                    "runtime_update_count": task.state.runtime_update_count,
                    "latest_runtime_update": task.state.latest_runtime_update,
                    "task_class": task.brief.task_class.value,
                    "summary": task.brief.summary,
                }
            elapsed = (self._completed_at or time.time()) - (
                self._started_at or time.time()
            )
            active = self._status in ("active", "pending", "waiting")
            return {
                "active": active or len(self._steps) > 0,
                "task_id": self._task_id,
                "task_input": self._task_input,
                "status": self._status,
                "error": self._error,
                "started_at": self._started_at,
                "elapsed_seconds": round(elapsed, 2),
                "steps": copy.deepcopy(self._steps),
                "engine_state": engine_state,
                "full_manager_run": True,
            }


_global_reporter = DashboardRunReporter()
_active_task_ref: Any | None = None
_task_ref_lock = threading.Lock()


def attach_reporter() -> DashboardRunReporter:
    return _global_reporter


def set_active_task(task: Any | None) -> None:
    global _active_task_ref
    with _task_ref_lock:
        _active_task_ref = task


def get_reporter_snapshot() -> dict[str, Any]:
    with _task_ref_lock:
        task = _active_task_ref
    return _global_reporter.snapshot(task)
