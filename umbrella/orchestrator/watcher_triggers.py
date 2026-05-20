import json
import os
import pathlib
import re
import time
from dataclasses import dataclass
from typing import Any


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
        self._repeat_m = repeat_m or int(os.environ.get("OUROBOROS_WATCHER_REPEAT_M", "3"))
        self._last_event_count: int = 0
        self._last_event_time: float = time.time()
        self._error_signatures: list[str] = []

    def check(self, *, phase: str, phase_started_at: float) -> TriggerEvent | None:
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
            recent_errors = _recent_error_signatures(tools_path, n=self._repeat_m + 1)
            if len(recent_errors) >= self._repeat_m:
                if len(set(recent_errors[-self._repeat_m:])) == 1:
                    return TriggerEvent("repeat_error", {
                        "phase": phase,
                        "signature": recent_errors[-1],
                        "count": self._repeat_m,
                    })

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
        elapsed = time.time() - started_at
        budget_path = self._drive / "state" / f"{phase}.budget.json"
        if budget_path.exists():
            try:
                budget = json.loads(budget_path.read_text())
                max_sec = budget.get("max_seconds")
                if max_sec and elapsed > max_sec:
                    return {"elapsed_sec": elapsed, "max_seconds": max_sec}
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
