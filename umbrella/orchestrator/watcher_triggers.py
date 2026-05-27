import json
import os
import pathlib
import re
import time
from dataclasses import dataclass
from typing import Any

from umbrella.env import watcher_budget_enforcement_enabled
from umbrella.orchestrator.watcher_semantic import (
    current_semantic_error_streak,
    rounds_without_progress,
    semantic_thresholds,
    summarize_recent_tool_rows,
)


@dataclass
class TriggerEvent:
    kind: str
    context: dict[str, Any]


class WatcherTriggers:
    """
    Deterministic heuristic checks — no LLM calls.
    Returns a TriggerEvent when something needs Watcher attention.
    """

    def __init__(
        self,
        drive_root: pathlib.Path,
        stall_sec: int | None = None,
        repeat_m: int | None = None,
    ) -> None:
        self._drive = drive_root
        self._stall_sec = stall_sec or int(os.environ.get("OUROBOROS_WATCHER_STALL_SEC", "180"))
        _inject_m, _restart_m, abort_m = semantic_thresholds()
        self._semantic_inject_m = _inject_m
        self._semantic_abort_m = abort_m
        self._repeat_m = repeat_m or abort_m
        self._last_event_count: int = 0
        self._last_event_time: float = time.time()
        self._error_signatures: list[str] = []

    def check(
        self,
        *,
        phase: str,
        phase_started_at: float,
        task_id: str = "",
    ) -> TriggerEvent | None:
        events_path = self._drive / "logs" / "events.jsonl"
        current_count = _count_lines(events_path)
        if current_count == self._last_event_count:
            if time.time() - self._last_event_time > self._stall_sec:
                return TriggerEvent("stall", {
                    "phase": phase,
                    "stall_sec": self._stall_sec,
                    "no_new_events_for": time.time() - self._last_event_time,
                })
        else:
            self._last_event_count = current_count
            self._last_event_time = time.time()

        tools_path = self._drive / "logs" / "tools.jsonl"
        if tools_path.exists():
            streak, signature, category = current_semantic_error_streak(
                tools_path, task_id=task_id
            )
            if streak >= self._semantic_inject_m and signature:
                return TriggerEvent(
                    "repeat_semantic_failure",
                    {
                        "phase": phase,
                        "signature": signature,
                        "category": category,
                        "streak": streak,
                        "count": streak,
                        "inject_m": self._semantic_inject_m,
                        "abort_m": self._semantic_abort_m,
                        "recent_tools": summarize_recent_tool_rows(tools_path),
                    },
                )
            recent_errors = _recent_error_signatures(tools_path, n=self._repeat_m + 1)
            if len(recent_errors) >= self._repeat_m:
                if len(set(recent_errors[-self._repeat_m:])) == 1:
                    return TriggerEvent("repeat_error", {
                        "phase": phase,
                        "signature": recent_errors[-1],
                        "count": self._repeat_m,
                    })
            if phase == "execute":
                structural = _recent_structural_layout_signatures(
                    tools_path, n=self._repeat_m + 1
                )
                if len(structural) >= self._repeat_m:
                    if len(set(structural[-self._repeat_m:])) == 1:
                        return TriggerEvent("repeat_structural_layout", {
                            "phase": phase,
                            "reason": "greenfield_python_src_layout_policy",
                            "file_path": structural[-1],
                            "count": self._repeat_m,
                        })
            idle_rounds = rounds_without_progress(tools_path, task_id=task_id, window=20)
            if idle_rounds >= 20:
                return TriggerEvent(
                    "no_progress_token_burn",
                    {
                        "phase": phase,
                        "rounds_without_progress": idle_rounds,
                        "category": "no_progress_token_burn",
                    },
                )

        budget_exceeded = self._check_budget(phase, phase_started_at)
        if budget_exceeded:
            return TriggerEvent("phase_overrun", {"phase": phase, **budget_exceeded})

        return None

    def check_worker_alive(self, worker_pid: int | None) -> TriggerEvent | None:
        if worker_pid is None:
            return None
        try:
            if os.name == "nt":
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, worker_pid)
                if not handle:
                    return TriggerEvent("worker_panic", {"pid": worker_pid})
            else:
                os.kill(worker_pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return TriggerEvent("worker_panic", {"pid": worker_pid})
        return None

    def _check_budget(self, phase: str, started_at: float) -> dict[str, Any] | None:
        if not watcher_budget_enforcement_enabled():
            return None
        elapsed = time.time() - started_at
        budget_path = self._drive / "state" / f"{phase}.budget.json"
        if budget_path.exists():
            try:
                budget = json.loads(budget_path.read_text())
                max_sec = budget.get("max_seconds")
                if max_sec and elapsed > max_sec:
                    return {"elapsed_sec": elapsed, "max_seconds": max_sec}
                max_calls = budget.get("max_tool_calls")
                if max_calls:
                    tools_path = self._drive / "logs" / "tools.jsonl"
                    tool_calls = _count_lines(tools_path)
                    if tool_calls > max_calls:
                        return {
                            "tool_calls": tool_calls,
                            "max_tool_calls": max_calls,
                        }
            except Exception:
                pass
        return None


def _count_lines(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _recent_error_signatures(tools_path: pathlib.Path, *, n: int) -> list[str]:
    lines: list[str] = []
    try:
        with tools_path.open(encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    sigs: list[str] = []
    for line in reversed(lines):
        try:
            ev = json.loads(line)
            if ev.get("error") or (ev.get("exit_code", 0) != 0):
                raw = ev.get("error") or ev.get("stderr") or ev.get("output") or ""
                sig = re.sub(r"[\d\s]{4,}", " ", str(raw))[:120]
                sigs.append(sig)
        except Exception:
            pass
        if len(sigs) >= n:
            break
    return list(reversed(sigs))


def _recent_structural_layout_signatures(tools_path: pathlib.Path, *, n: int) -> list[str]:
    lines: list[str] = []
    try:
        with tools_path.open(encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    sigs: list[str] = []
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        blob = json.dumps(ev, ensure_ascii=False)
        if "greenfield_python_src_layout_policy" not in blob:
            continue
        path = ""
        for key in ("output", "result"):
            raw = ev.get(key)
            if isinstance(raw, dict):
                path = str(raw.get("file_path") or raw.get("bad_declared_path") or "")
                if path:
                    break
            text = str(raw or "")
            match = re.search(r'"file_path"\s*:\s*"([^"]+)"', text)
            if match:
                path = match.group(1)
                break
        sigs.append(path or "greenfield_python_src_layout_policy")
        if len(sigs) >= n:
            break
    return list(reversed(sigs))
