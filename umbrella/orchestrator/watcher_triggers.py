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
            semantic_errors = _recent_semantic_error_signatures(
                tools_path, n=self._repeat_m + 1
            )
            if len(semantic_errors) >= self._repeat_m:
                recent = semantic_errors[-self._repeat_m :]
                if len(set(recent)) == 1:
                    category = recent[-1].split(":", 1)[0]
                    return TriggerEvent("repeat_semantic_failure", {
                        "phase": phase,
                        "signature": recent[-1],
                        "category": category,
                        "count": self._repeat_m,
                    })
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


def _json_obj_from_preview(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _semantic_error_code(row: dict[str, Any]) -> str:
    tool = str(row.get("tool") or "")
    raw = row.get("result_preview") or row.get("result") or row.get("output") or ""
    payload = _json_obj_from_preview(raw)
    raw_text = str(raw or "").strip()
    blob = json.dumps({"row": row, "payload": payload}, ensure_ascii=False).lower()
    if tool == "run_subtask_proof" and payload.get("passed") is False:
        shell_result = payload.get("shell_result")
        shell_blob = (
            json.dumps(shell_result, ensure_ascii=False).lower()
            if isinstance(shell_result, dict)
            else ""
        )
        if "attributeerror" in shell_blob:
            return "proof_runtime_attribute_error"
        if "modulenotfounderror" in shell_blob or "importerror" in shell_blob:
            return "proof_runtime_import_error"
        if "typeerror" in shell_blob:
            return "proof_runtime_type_error"
        return "proof_not_passing"
    if "workspace_hash_mismatch" in blob or "diff_hash_mismatch" in blob:
        return "completion_hash_mismatch"
    if "completion_contract" in blob and (
        "required" in blob or "missing" in blob or "invalid" in blob
    ):
        return "completion_contract_invalid"
    if "fake_evidence_ref" in blob:
        return "fake_evidence_ref"
    if "subtask materialization missing" in blob or "subtask_materialization_missing" in blob:
        return "materialization_missing"
    if "verification_report.passed must be true" in blob:
        return "completion_with_failed_proof"
    if "gmas_context_before_first_write" in blob:
        return "context_gate_gmas_not_followed"
    if "subtask_context_read_required" in blob:
        return "context_gate_read_not_followed"
    if "patch_hunk_mismatch" in blob:
        return "patch_hunk_mismatch"
    if raw_text.startswith("ERROR:"):
        if tool in {"palace_add", "submit_research_summary"} and (
            "research_finding" in blob
            or "source_id" in blob
            or "evidence metadata" in blob
            or "finding" in blob
        ):
            return "research_memory_provenance_error"
        return "tool_result_error"
    return ""


def _recent_semantic_error_signatures(
    tools_path: pathlib.Path, *, n: int
) -> list[str]:
    try:
        lines = tools_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    sigs: list[str] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        code = _semantic_error_code(row)
        if not code:
            continue
        tool = str(row.get("tool") or "")
        payload = _json_obj_from_preview(
            row.get("result_preview") or row.get("result") or {}
        )
        subtask_id = ""
        args = row.get("args")
        if isinstance(args, dict):
            subtask_id = str(args.get("subtask_id") or "").strip()
        if not subtask_id and isinstance(payload, dict):
            subtask_id = str(payload.get("subtask_id") or "").strip()
        sigs.append(f"{code}:{tool}:{subtask_id}")
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
