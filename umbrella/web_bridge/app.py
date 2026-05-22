import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

from umbrella.web_bridge.chat_launcher import load_launcher_runs, save_launcher_runs
from umbrella.web_bridge.cleanup import (
    wipe_memory_node,
    wipe_run_artifacts,
    wipe_workspace_artifacts,
)
from umbrella.web_bridge.util import (
    DEFAULT_MODELS,
    DEFAULT_TOOLS,
    REPO_ROOT,
    iso_utc,
    load_store,
    now_ts,
    read_jsonl,
    read_toml,
    save_store,
    short_text,
    slug_workspace_name,
    store_path,
)
from umbrella.orchestration.task_input import resolve_task_text
from umbrella.utils.tool_logs import is_effective_write_tool_log_row

try:
    from umbrella.env import load_env
except Exception:  # pragma: no cover - optional in tiny test fixtures
    load_env = None  # type: ignore[assignment]


def _ensure_repo_python_paths(repo_root: Path) -> None:
    """Expose bundled editable packages when bridge is launched from source."""
    for rel, package in (("ouroboros", "ouroboros"), ("gmas/src", "gmas")):
        source_root = (repo_root / rel).resolve()
        if not (source_root / package).exists():
            continue
        value = str(source_root)
        if value not in sys.path:
            sys.path.insert(0, value)


def _filter_terminal_scrollback_for_run(text: str, run_id: str) -> str:
    """Return terminal scrollback blocks that belong to ``run_id``."""

    run = str(run_id or "").strip()
    if not run:
        return text
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in str(text or "").splitlines():
        if line.startswith("## ws="):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    matched: list[str] = []
    for block in blocks:
        header = block[0]
        if f"run={run}" in header or f"task={run}:" in header or f"task={run} " in header:
            matched.extend(block)
            matched.append("")
    return "\n".join(matched).strip()


class WebBridgeApp:
    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = (repo_root or REPO_ROOT).resolve()
        _ensure_repo_python_paths(self.repo_root)
        self.workspaces_root = self.repo_root / "workspaces"
        self._run_lock = threading.Lock()
        self._run_threads: dict[str, threading.Thread] = {}
        # Sister registry for cancel-aware worker handles. Keys mirror
        # ``_run_threads``; values carry references that ``cancel_run``
        # uses to escalate from cooperative-stop to forced abort
        # (orchestrator, force-stop event, started-at, kind).
        self._workers: dict[str, dict[str, Any]] = {}

    def _load_runtime_env(self) -> None:
        if load_env is None:
            return
        try:
            load_env(repo_root=self.repo_root)
        except Exception:
            pass

    def _current_model_id(self) -> str:
        self._load_runtime_env()
        env_path = self.repo_root / ".env"
        try:
            for line in env_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                if key in {"OUROBOROS_MODEL", "LLM_MODEL"}:
                    value = value.strip().strip('"').strip("'")
                    if value:
                        return value
        except OSError:
            pass
        return (
            os.environ.get("OUROBOROS_MODEL")
            or os.environ.get("LLM_MODEL")
            or DEFAULT_MODELS[0]["id"]
        ).strip()

    def _workspace_path(self, ws_id: str) -> Path | None:
        ws = self.get_workspace(ws_id)
        if not ws:
            return None
        return (self.repo_root / str(ws.get("path") or f"workspaces/{ws_id}")).resolve()

    def _web_runs(self) -> dict[str, dict[str, Any]]:
        rows = load_store("web_runs.json", {})
        return rows if isinstance(rows, dict) else {}

    def _save_web_runs(self, rows: dict[str, dict[str, Any]]) -> None:
        save_store("web_runs.json", rows)

    def _upsert_web_run(self, run_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._run_lock:
            rows = self._web_runs()
            current = rows.get(run_id, {})
            current.update(patch)
            rows[run_id] = current
            self._save_web_runs(rows)
            return current

    def _get_web_run(self, run_id: str) -> dict[str, Any] | None:
        run = self._web_runs().get(run_id)
        return run if isinstance(run, dict) else None

    @staticmethod
    def _parse_ts_value(ts: Any) -> float:
        try:
            if isinstance(ts, str) and ts:
                return time.mktime(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
            if isinstance(ts, (int, float)):
                return float(ts)
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _rewrite_jsonl(path: Path, keep_row: Any) -> int:
        if not path.exists():
            return 0
        kept: list[str] = []
        removed = 0
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if isinstance(row, dict) and not keep_row(row):
                removed += 1
                continue
            kept.append(
                json.dumps(row, ensure_ascii=False, default=str)
                if isinstance(row, dict)
                else line
            )
        if kept:
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                path.write_text("", encoding="utf-8")
        return removed

    @staticmethod
    def _coerce_int(
        value: Any, default: int | None = None, *, min_value: int | None = None
    ) -> int | None:
        try:
            if value is None or value == "":
                return default
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if min_value is not None:
            parsed = max(min_value, parsed)
        return parsed

    def _current_max_rounds(self) -> int:
        self._load_runtime_env()
        value = self._coerce_int(
            os.environ.get("OUROBOROS_MAX_ROUNDS"), 200, min_value=0
        )
        return 200 if value is None else value

    def _current_max_verify_retries(self) -> int:
        self._load_runtime_env()
        value = self._coerce_int(
            os.environ.get("OUROBOROS_WEB_MAX_VERIFY_RETRIES"), 20, min_value=0
        )
        return 20 if value is None else value

    @staticmethod
    def _attempt_task_id(run_id: str, attempt: int) -> str:
        return run_id if attempt <= 1 else f"{run_id}__a{attempt}"

    def _workspace_drive_root(self, ws_id: str | None) -> Path | None:
        """Resolve the Ouroboros drive root the worker actually writes into."""
        if ws_id:
            workspace_drive = self.workspaces_root / ws_id / ".memory" / "drive"
            if workspace_drive.exists():
                return workspace_drive
        try:
            from umbrella.integration.ouroboros_bridge import workspace_drive_root

            return workspace_drive_root(self.repo_root, ws_id or None)
        except Exception:
            if not ws_id:
                return self.repo_root / ".umbrella" / "ouroboros_drive"
            return self.workspaces_root / ws_id / ".memory" / "drive"

    def _stop_request_paths(self, ws_id: str | None) -> list[Path]:
        """Every state file the running Ouroboros loop checks for stop requests."""
        paths: list[Path] = [
            self.repo_root / ".umbrella" / "launcher" / "stop_requested.json",
            self.repo_root
            / ".umbrella"
            / "ouroboros_drive"
            / "state"
            / "stop_requested.json",
        ]
        drive = self._workspace_drive_root(ws_id)
        if drive is not None:
            paths.append(drive / "state" / "stop_requested.json")
        out: list[Path] = []
        seen: set[str] = set()
        for p in paths:
            try:
                key = str(p.resolve())
            except OSError:
                key = str(p)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    def _clear_stop_requests(self, ws_id: str | None) -> None:
        for path in self._stop_request_paths(ws_id):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _task_id_matches_run(
        task_id: Any, run_id: str, attempt_task_ids: set[str] | None = None
    ) -> bool:
        value = str(task_id or "")
        if not value:
            return False
        if (
            value == run_id
            or value.startswith(f"{run_id}:")
            or value.startswith(f"{run_id}__")
        ):
            return True
        for attempt_task_id in attempt_task_ids or set():
            attempt = str(attempt_task_id or "")
            if attempt and (
                value == attempt
                or value.startswith(f"{attempt}:")
                or value.startswith(f"{attempt}__")
            ):
                return True
        return False

    def _attempt_task_ids_for_run(
        self, run_id: str, run: dict[str, Any] | None = None
    ) -> set[str]:
        values: set[str] = {run_id}
        source = run if isinstance(run, dict) else self._get_web_run(run_id)
        if isinstance(source, dict):
            for key in ("attempt_task_ids", "task_ids"):
                raw = source.get(key)
                if isinstance(raw, list):
                    values.update(str(v) for v in raw if str(v or "").strip())
            full_result = (
                source.get("full_result")
                if isinstance(source.get("full_result"), dict)
                else {}
            )
            if full_result.get("task_id"):
                values.add(str(full_result["task_id"]))
            harness_sources: list[dict[str, Any]] = []
            harness_meta = (
                source.get("harness_meta")
                if isinstance(source.get("harness_meta"), dict)
                else {}
            )
            if harness_meta:
                harness_sources.append(harness_meta)
            harness_result = (
                full_result.get("harness_result")
                if isinstance(full_result.get("harness_result"), dict)
                else {}
            )
            if harness_result:
                harness_sources.append(harness_result)
            for harness_payload in harness_sources:
                for item in harness_payload.get("candidate_run_ids") or []:
                    if str(item or "").strip():
                        values.add(str(item))
                for stage in harness_payload.get("stages") or []:
                    if not isinstance(stage, dict):
                        continue
                    for candidate in stage.get("candidates") or []:
                        if not isinstance(candidate, dict):
                            continue
                        child_id = str(candidate.get("run_id") or "").strip()
                        if child_id:
                            values.add(child_id)
            attempt = self._coerce_int(source.get("attempt"), None, min_value=1)
            if attempt:
                for idx in range(1, attempt + 1):
                    values.add(self._attempt_task_id(run_id, idx))
        return values

    def _append_thread_run_messages(
        self, thread_id: str, run: dict[str, Any], label: str
    ) -> None:
        thread = self.get_thread(thread_id)
        if thread is None:
            return
        messages = load_store(f"messages_{thread_id}.json", [])
        ts = iso_utc(now_ts())
        user_msg = {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "thread_id": thread_id,
            "role": "user",
            "content": label,
            "created_at": ts,
        }
        assistant_msg = {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "thread_id": thread_id,
            "role": "assistant",
            "content": (
                f"Real Ouroboros run started for `{run['workspace_id']}`: `{run['id']}`. "
                "Watch progress in Runs / Logs."
            ),
            "run_id": run["id"],
            "created_at": ts,
        }
        messages.extend([user_msg, assistant_msg])
        save_store(f"messages_{thread_id}.json", messages)
        thread["updated_at"] = ts
        thread["message_count"] = len(messages)
        threads = load_store("threads.json", [])
        for i, item in enumerate(threads):
            if item.get("id") == thread_id:
                threads[i] = thread
                break
        save_store("threads.json", threads)

    def _append_thread_finalize_message(
        self, thread_id: str, run_id: str, result: dict[str, Any]
    ) -> None:
        thread = self.get_thread(thread_id)
        if thread is None:
            return
        ws_id = str(result.get("workspace_id") or thread.get("workspace_id") or "")
        attempt_task_ids = self._attempt_task_ids_for_run(
            run_id, self._get_web_run(run_id) or {}
        )
        status = self._normalize_run_status(str(result.get("status") or ""))
        title = (
            "Готово"
            if status == "completed"
            else ("Прерван" if status == "cancelled" else "Не получилось")
        )
        lines = [f"## Итог: {title}", ""]
        final_message = short_text(
            str(result.get("final_message") or result.get("error") or ""), 900
        )
        if final_message:
            lines.extend([final_message, ""])
        harness_result = (
            result.get("harness_result")
            if isinstance(result.get("harness_result"), dict)
            else {}
        )
        harness_stages = (
            harness_result.get("stages") if isinstance(harness_result, dict) else []
        )
        if isinstance(harness_stages, list) and harness_stages:
            lines.append("### Harness stages")
            for stage in harness_stages[:10]:
                if not isinstance(stage, dict):
                    continue
                title_text = short_text(
                    str(stage.get("title") or stage.get("stage_id") or "stage"), 100
                )
                winner = str(
                    stage.get("winner_id") or stage.get("winner_run_id") or "no winner"
                )
                pruned = len(stage.get("pruned_candidate_ids") or [])
                applied = "applied" if stage.get("winner_applied") else "selected"
                lines.append(f"- {title_text}: {winner} {applied}, pruned {pruned}")
            lines.append("")
        changes = [
            str(item)
            for item in (
                result.get("promoted_files") or result.get("changes_made") or []
            )
        ][:12]
        if changes:
            lines.append("### Что изменено")
            lines.extend(f"- `{item}`" for item in changes)
            lines.append("")
        verification = (
            result.get("verification_report")
            if isinstance(result.get("verification_report"), dict)
            else {}
        )
        verification_rows = (
            verification.get("results") if isinstance(verification, dict) else []
        )
        if verification_rows:
            lines.append("### Verification")
            for item in verification_rows[:10]:
                if not isinstance(item, dict):
                    continue
                marker = "passed" if item.get("status") == "passed" else "failed"
                summary = short_text(
                    str(item.get("summary") or item.get("error") or ""), 220
                )
                lines.append(f"- [{marker}] {item.get('name') or 'check'}: {summary}")
            lines.append("")
        tools = self._tools_used_for_run(ws_id, run_id) if ws_id else []
        if tools:
            lines.append("### Tools used")
            lines.append(", ".join(f"`{tool}`" for tool in tools[:20]))
            lines.append("")
        memory_root = self.workspaces_root / ws_id / ".memory" if ws_id else None
        if memory_root:
            ideas = [
                row
                for row in read_jsonl(memory_root / "ideas.jsonl", limit=800)
                if self._task_id_matches_run(
                    row.get("task_id"), run_id, attempt_task_ids
                )
            ]
            subtasks = [
                row for row in ideas if str(row.get("kind") or "") == "subtask_result"
            ]
            if subtasks:
                lines.append("### Subtasks")
                for row in subtasks[-12:]:
                    title_text = short_text(str(row.get("title") or "subtask"), 100)
                    summary = short_text(str(row.get("content") or ""), 260)
                    lines.append(f"- {title_text}: {summary}")
                lines.append("")
            lessons = [
                row
                for row in read_jsonl(memory_root / "lessons.jsonl", limit=500)
                if self._task_id_matches_run(
                    row.get("task_id"), run_id, attempt_task_ids
                )
            ]
            if lessons:
                lines.append("### Memory lessons")
                for row in lessons[-5:]:
                    lines.append(
                        f"- {short_text(str(row.get('conclusion') or row.get('change_summary') or row), 260)}"
                    )
                lines.append("")
            events = [
                row
                for row in read_jsonl(
                    memory_root / "drive" / "logs" / "events.jsonl", limit=1200
                )
                if self._task_id_matches_run(
                    row.get("task_id"), run_id, attempt_task_ids
                )
                and str(row.get("type") or "")
                in {"tool_preflight_error", "tool_forbidden", "task_metrics"}
            ]
            problem_events = [
                row for row in events if str(row.get("type") or "") != "task_metrics"
            ]
            if problem_events and status != "completed":
                lines.append("### Что пошло не так")
                for row in problem_events[-8:]:
                    lines.append(
                        f"- {row.get('type')} phase={row.get('phase') or '?'} "
                        f"tool={row.get('tool') or '?'}: {short_text(str(row.get('error') or row.get('message') or ''), 220)}"
                    )
                lines.append("")
        if status != "completed" and result.get("promotion_blocked_reason"):
            lines.append(
                f"Причина блокировки promotion: `{result.get('promotion_blocked_reason')}`"
            )
        content = "\n".join(lines).strip()
        ts = iso_utc(now_ts())
        messages = load_store(f"messages_{thread_id}.json", [])
        messages.append(
            {
                "id": f"msg_{uuid.uuid4().hex[:12]}",
                "thread_id": thread_id,
                "role": "assistant",
                "content": content,
                "run_id": run_id,
                "created_at": ts,
            }
        )
        save_store(f"messages_{thread_id}.json", messages)
        thread["updated_at"] = ts
        thread["message_count"] = len(messages)
        threads = load_store("threads.json", [])
        for i, item in enumerate(threads):
            if item.get("id") == thread_id:
                threads[i] = thread
                break
        save_store("threads.json", threads)

    @staticmethod
    def _normalize_run_status(status: str) -> str:
        raw = str(status or "").lower()
        if raw in {"verified", "complete", "completed", "ok", "success", "succeeded"}:
            return "completed"
        if raw in {
            "failed",
            "failed_verification",
            "failed_hygiene",
            "failed_self_review",
            "phase_impasse",
            "incomplete_subtasks",
            "incomplete_discovery",
            "verified_with_blocking_noise",
            "error",
            "incomplete",
        }:
            return "failed"
        if raw in {"cancelled", "stopped"}:
            return "cancelled"
        if raw in {"queued", "running"}:
            return raw
        return "queued"

    def _append_task_message(self, ws_id: str, message: str) -> None:
        workspace_path = self._workspace_path(ws_id)
        if workspace_path is None:
            raise ValueError(f"workspace not found: {ws_id}")
        task_path = workspace_path / "TASK_MAIN.md"
        existing = (
            task_path.read_text(encoding="utf-8", errors="replace")
            if task_path.exists()
            else ""
        )
        stamp = iso_utc(now_ts())
        block = f"\n\n## Web chat request ({stamp})\n\n{message.strip()}\n"
        task_path.write_text(existing.rstrip() + block, encoding="utf-8")

    def _active_run_for_workspace(self, ws_id: str) -> dict[str, Any] | None:
        for run_id, worker in list(self._run_threads.items()):
            if worker is None or not worker.is_alive():
                continue
            run = self._get_web_run(run_id) or self.get_run(run_id) or {}
            if not run or run.get("workspace_id") != ws_id:
                continue
            if run.get("status") in {"queued", "running"}:
                return run
            # A cancelled/detached Python worker can still drain its current
            # LLM/tool turn. Treat it as active until the thread exits so a new
            # run cannot race against late writes from the old one.
            return {
                **run,
                "status": "stopping",
                "detached_worker_alive": True,
                "result_preview": (
                    run.get("result_preview")
                    or "Previous run is still stopping; wait before starting another run."
                ),
            }
        for run in self._web_runs().values():
            if not isinstance(run, dict):
                continue
            if run.get("workspace_id") != ws_id:
                continue
            if run.get("status") not in {"queued", "running"}:
                continue
            run_id = str(run.get("id") or "")
            worker = self._run_threads.get(run_id)
            if worker is not None and worker.is_alive():
                return run
        return None

    def _repair_stale_web_run(self, run: dict[str, Any]) -> dict[str, Any]:
        if run.get("status") not in {"queued", "running"}:
            return run
        run_id = str(run.get("id") or "")
        if not run_id:
            return run
        worker = self._run_threads.get(run_id)
        if worker is not None and worker.is_alive():
            return run
        created_ts = self._parse_ts_value(run.get("created_at"))
        if created_ts and now_ts() - created_ts < 10:
            return run
        status = "cancelled" if run.get("stop_requested") else "failed"
        preview = (
            "Run was stopped; the worker is no longer attached to the web bridge."
            if status == "cancelled"
            else "Run worker is no longer active. Start a new run to continue."
        )
        return self._upsert_web_run(
            run_id,
            {
                "status": status,
                "result_preview": run.get("result_preview") or preview,
                "updated_at": iso_utc(now_ts()),
                "finished_at": run.get("finished_at") or iso_utc(now_ts()),
            },
        )

    def start_workspace_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start a phase-based run. Every run goes through PhaseRunner.

        ``harness_candidates`` (default 1) controls per-phase parallelism:
        N candidates run per phase, the watcher/heuristic picks the winner.
        """
        ws_id = str(payload.get("workspace_id") or "").strip()
        if not ws_id:
            raise ValueError("workspace_id is required")
        workspace_path = self._workspace_path(ws_id)
        if workspace_path is None:
            raise ValueError(f"workspace not found: {ws_id}")

        active = self._active_run_for_workspace(ws_id)
        if active is not None:
            return {**active, "already_running": True}

        thread_id = str(payload.get("thread_id") or "").strip()
        append_message = str(payload.get("append_message") or "").strip()
        if append_message:
            self._append_task_message(ws_id, append_message)

        explicit_task = str(payload.get("task") or payload.get("input") or "").strip()
        if explicit_task:
            task_text = explicit_task
            task_resolution = type("R", (), {
                "task_text": task_text,
                "task_source": "api_payload",
                "task_hash": "",
                "task_missing": False,
                "missing_status": None,
                "error": None,
            })()
        else:
            task_resolution = resolve_task_text(workspace_path)
            task_text = task_resolution.task_text

        candidates_per_phase = self._coerce_int(
            payload.get("harness_candidates"), 1, min_value=1
        ) or 1
        if candidates_per_phase > 8:
            candidates_per_phase = 8
        is_harness = candidates_per_phase > 1 or bool(payload.get("harness_mode"))
        if is_harness and candidates_per_phase < 2:
            candidates_per_phase = 3

        run_id = (
            f"harness_web_{uuid.uuid4().hex[:8]}"
            if is_harness
            else f"phase_web_{uuid.uuid4().hex[:8]}"
        )
        now_iso = iso_utc(now_ts())
        selected_model = str(payload.get("model") or "").strip()
        model = selected_model or self._current_model_id()
        max_rounds = self._coerce_int(
            payload.get("max_rounds"), self._current_max_rounds(), min_value=0
        )
        max_verify_retries = self._coerce_int(
            payload.get("max_verify_retries"),
            self._current_max_verify_retries(),
            min_value=0,
        )

        run = {
            "id": run_id,
            "workspace_id": ws_id,
            "status": "running",
            "mode": "harness" if is_harness else "phase_runner",
            "model": model,
            "total_cost": 0.0,
            "total_steps": 0,
            "total_duration_ms": 0,
            "tools_used": [],
            "max_rounds": max_rounds,
            "max_verify_retries": max_verify_retries,
            "thread_id": thread_id or None,
            "attempt": 1,
            "max_attempts": 1,
            "attempt_task_ids": [run_id],
            "task_text": short_text(task_text, 800),
            "task_source": getattr(task_resolution, "task_source", "task_main"),
            "task_hash": getattr(task_resolution, "task_hash", ""),
            "task_missing": getattr(task_resolution, "task_missing", False),
            "result_preview": (
                f"Phase run started ({candidates_per_phase} candidates per phase)."
                if is_harness
                else "Phase run started."
            ),
            "created_at": now_iso,
            "updated_at": now_iso,
            "source": "web_bridge",
            "candidates_per_phase": candidates_per_phase,
            "phase_events": [],
        }
        self._upsert_web_run(run_id, run)

        if getattr(task_resolution, "task_missing", False):
            missing_patch = {
                "status": getattr(task_resolution, "missing_status", None)
                or "missing_task_main",
                "running": False,
                "finished_at": iso_utc(now_ts()),
                "updated_at": iso_utc(now_ts()),
                "result_preview": getattr(task_resolution, "error", None),
                "error": getattr(task_resolution, "error", None),
            }
            self._upsert_web_run(run_id, missing_patch)
            if thread_id:
                self._append_thread_run_messages(
                    thread_id, {**run, **missing_patch}, "TASK_MAIN.md missing"
                )
            return {**run, **missing_patch}

        if thread_id:
            label = (
                f"Phase run (harness x {candidates_per_phase})"
                if is_harness
                else "Phase run"
            )
            self._append_thread_run_messages(thread_id, run, label)

        thread = threading.Thread(
            target=self._run_phase_runner_worker,
            args=(
                run_id,
                ws_id,
                task_text,
                model,
                candidates_per_phase,
                max_rounds,
                max_verify_retries,
            ),
            name=f"PhaseRunner-{run_id}",
            daemon=True,
        )
        with self._run_lock:
            self._run_threads[run_id] = thread
            self._workers[run_id] = {
                "thread": thread,
                "kind": "phase_runner",
                "started_at": now_ts(),
                "orchestrator": None,
            }
        thread.start()
        return run

    def _run_phase_runner_worker(
        self,
        run_id: str,
        ws_id: str,
        task_text: str,
        model: str,
        candidates_per_phase: int,
        max_rounds: int | None = None,
        max_verify_retries: int | None = None,
    ) -> None:
        """Background worker: drives PhaseRunner and mirrors envelopes into the web run record."""
        env_snapshot = self._snapshot_env()
        self._clear_stop_requests(ws_id)
        self._load_runtime_env()
        self._ensure_web_discovery_defaults()

        if model:
            os.environ["LLM_MODEL"] = model
            os.environ["OUROBOROS_MODEL"] = model
        if max_rounds is not None:
            os.environ["OUROBOROS_MAX_ROUNDS"] = str(max(0, int(max_rounds)))
        if max_verify_retries is not None:
            os.environ["OUROBOROS_WEB_MAX_VERIFY_RETRIES"] = str(
                max(0, int(max_verify_retries))
            )

        started_at = now_ts()
        phase_events: list[dict[str, Any]] = []
        tools_used: set[str] = set()
        final_status = "succeeded"
        final_error: str | None = None

        try:
            from umbrella.orchestrator.runner import PhaseRunner
            from umbrella.utils.result_envelope import ResultEnvelope
        except Exception as exc:
            log.exception("PhaseRunner import failed for run %s", run_id)
            final_run = self._upsert_web_run(
                run_id,
                {
                    "status": "failed",
                    "running": False,
                    "total_steps": 0,
                    "result_preview": str(exc),
                    "error": str(exc),
                    "finished_at": iso_utc(now_ts()),
                    "updated_at": iso_utc(now_ts()),
                },
            )
            thread_id = str(final_run.get("thread_id") or "").strip()
            if thread_id:
                try:
                    self._append_thread_finalize_message(thread_id, run_id, final_run)
                except Exception:
                    log.debug("Failed to append import-failure message", exc_info=True)
            with self._run_lock:
                self._workers.pop(run_id, None)
                self._run_threads.pop(run_id, None)
            self._restore_env(env_snapshot)
            return

        def on_env(env: ResultEnvelope) -> None:
            payload = env.to_dict()
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            data = payload.get("data") or {}
            errors = payload.get("errors") or []
            error_msg = ""
            if errors and isinstance(errors[0], dict):
                error_msg = str(errors[0].get("message") or "")
            evt = data.get("event") or payload.get("phase") or "envelope"
            phase_events.append(
                {
                    "ts": iso_utc(now_ts()),
                    "phase": meta.get("phase") or payload.get("phase"),
                    "event": evt,
                    "outcome": data.get("outcome"),
                    "took_ms": meta.get("took_ms") or payload.get("took_ms"),
                    "error": error_msg or payload.get("error"),
                }
            )
            self._upsert_web_run(
                run_id,
                {
                    "status": "running",
                    "total_steps": len(phase_events),
                    "tools_used": sorted(tools_used),
                    "phase_events": phase_events[-20:],
                    "updated_at": iso_utc(now_ts()),
                    "last_phase": payload.get("phase"),
                },
            )

        def stop_text(value: str | None) -> bool:
            lines = str(value or "").strip().splitlines()
            head = lines[0].strip().lower() if lines else ""
            return (
                head.startswith("stop requested by dashboard")
                or head.startswith("stop requested from the web ui")
                or head.startswith("stop requested by web ui")
                or head.startswith("stop_requested")
            )

        try:
            runner = PhaseRunner(
                repo_root=self.repo_root,
                workspace_id=ws_id,
                candidates_per_phase=candidates_per_phase,
                on_envelope=on_env,
            )
            envelopes = list(runner.run(task_text, run_id=run_id))
            for envelope in envelopes:
                payload = envelope.to_dict()
                if not payload.get("ok", False):
                    final_status = "failed"
                    errors = payload.get("errors") or []
                    if errors and isinstance(errors[0], dict):
                        final_error = (
                            str(errors[0].get("message") or "")
                            or "phase runner reported error"
                        )
                    else:
                        final_error = "phase runner reported error"
                    if self._stop_was_requested(run_id, ws_id) or stop_text(final_error):
                        final_status = "cancelled"
                    break
        except Exception as exc:
            log.exception("PhaseRunner worker crashed for run %s", run_id)
            final_status = "failed"
            final_error = str(exc)
        finally:
            duration_ms = int((now_ts() - started_at) * 1000)
            log_summary = self._run_log_summary(ws_id, run_id)
            for tool in log_summary.get("tools_used") or []:
                tools_used.add(str(tool))
            if self._stop_was_requested(run_id, ws_id) or stop_text(final_error):
                final_status = "cancelled"
                final_error = final_error or "Run cancelled from the web UI."
            patch: dict[str, Any] = {
                "status": final_status,
                "running": False,
                "total_steps": len(phase_events),
                "total_duration_ms": duration_ms,
                "tools_used": sorted(tools_used),
                "llm_rounds": log_summary.get("llm_rounds", 0),
                "prompt_tokens": log_summary.get("prompt_tokens", 0),
                "completion_tokens": log_summary.get("completion_tokens", 0),
                "models": log_summary.get("models", []),
                "forbidden_tool_attempts": log_summary.get("forbidden_tool_attempts", 0),
                "verification_status": log_summary.get("verification_status"),
                "phase_events": phase_events[-50:],
                "finished_at": iso_utc(now_ts()),
                "updated_at": iso_utc(now_ts()),
                "result_preview": (
                    final_error
                    if final_status in {"failed", "cancelled"}
                    else (
                        f"Phase run completed ({len(phase_events)} events, "
                        f"{log_summary.get('llm_rounds', 0)} LLM rounds)."
                    )
                ),
            }
            if final_error:
                patch["error"] = final_error
            final_run = self._upsert_web_run(run_id, patch)
            thread_id = str(final_run.get("thread_id") or "").strip()
            if thread_id:
                try:
                    self._append_thread_finalize_message(thread_id, run_id, final_run)
                except Exception:
                    log.debug("Failed to append final thread run message", exc_info=True)
            self._restore_env(env_snapshot)
            self._clear_stop_requests(ws_id)
            with self._run_lock:
                self._run_threads.pop(run_id, None)
                self._workers.pop(run_id, None)

    _ENV_KEYS_TO_RESTORE: tuple[str, ...] = (
        "LLM_MODEL",
        "OUROBOROS_MODEL",
        "OUROBOROS_MAX_ROUNDS",
        "OUROBOROS_PLANNER_PHASE_ROUNDS",
        "OUROBOROS_PLANNER_REMEDIATION_ROUNDS",
        "OUROBOROS_TOOL_PREFLIGHT_REPAIR_ROUNDS",
        "OUROBOROS_WEB_MAX_VERIFY_RETRIES",
        "OUROBOROS_DEEP_SEARCH_ENABLED",
        "OUROBOROS_DEEP_SEARCH_ENGINE",
        "OUROBOROS_DEEP_SEARCH_PROVIDER",
        "OUROBOROS_DEEP_SEARCH_BUDGET",
        "OUROBOROS_GITHUB_DISCOVERY_BUDGET",
        "OUROBOROS_HARNESS_MAX_PARALLEL",
        "OUROBOROS_HARNESS_TIMEOUT_HOURS",
    )
    def _snapshot_env(self) -> dict[str, str | None]:
        return {key: os.environ.get(key) for key in self._ENV_KEYS_TO_RESTORE}

    @staticmethod
    def _restore_env(snapshot: dict[str, str | None]) -> None:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _ensure_web_discovery_defaults(self) -> None:
        """Compatibility hook: GMAS DuckDuckGo is now the no-key default."""
        return

    def _stop_was_requested(self, run_id: str, ws_id: str | None) -> bool:
        run = self._get_web_run(run_id) or {}
        if run.get("stop_requested"):
            return True
        for path in self._stop_request_paths(ws_id):
            if path.exists():
                return True
        return False

    def list_workspaces(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.workspaces_root.exists():
            return items
        overrides = load_store("workspace_overrides.json", {})
        for entry in sorted(self.workspaces_root.iterdir()):
            if not entry.is_dir():
                continue
            toml = read_toml(entry / "workspace.toml")
            ws_section = toml.get("workspace") or {}
            ws_id = str(ws_section.get("id") or entry.name)
            override = overrides.get(ws_id, {})
            task_main = entry / "TASK_MAIN.md"
            description = ""
            if task_main.exists():
                try:
                    description = short_text(
                        task_main.read_text(encoding="utf-8", errors="replace"), 240
                    )
                except OSError:
                    pass
            try:
                st = entry.stat()
                created_at = iso_utc(st.st_ctime)
                updated_at = iso_utc(st.st_mtime)
            except OSError:
                created_at = updated_at = iso_utc(now_ts())
            items.append(
                {
                    "id": ws_id,
                    "name": override.get("name")
                    or ws_section.get("name")
                    or entry.name,
                    "description": override.get("description") or description,
                    "language": ws_section.get("language", "python"),
                    "path": str(entry.relative_to(self.repo_root)),
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
        items.sort(key=lambda w: w["updated_at"], reverse=True)
        return items

    def get_workspace(self, ws_id: str) -> dict[str, Any] | None:
        for ws in self.list_workspaces():
            if ws["id"] == ws_id:
                return ws
        return None

    def create_workspace(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        ws_id = slug_workspace_name(name)
        target = self.workspaces_root / ws_id
        target.mkdir(parents=True, exist_ok=True)
        toml_path = target / "workspace.toml"
        if not toml_path.exists():
            toml_path.write_text(
                f'[workspace]\nid = "{ws_id}"\nname = "{name}"\nlanguage = "python"\n\n'
                "[verification]\nskip_behavioral = true\n",
                encoding="utf-8",
            )
        task_path = target / "TASK_MAIN.md"
        if not task_path.exists():
            desc = (payload.get("description") or "").strip()
            task_path.write_text(
                f"# TASK: {name}\n\n## Goal\n{desc or 'Опишите задачу.'}\n",
                encoding="utf-8",
            )
        overrides = load_store("workspace_overrides.json", {})
        if payload.get("description"):
            overrides[ws_id] = {
                **overrides.get(ws_id, {}),
                "description": payload["description"],
            }
        if payload.get("name") and payload["name"] != name:
            overrides[ws_id] = {**overrides.get(ws_id, {}), "name": payload["name"]}
        save_store("workspace_overrides.json", overrides)
        ws = self.get_workspace(ws_id)
        assert ws is not None
        return ws

    def update_workspace(
        self, ws_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self.get_workspace(ws_id) is None:
            return None
        overrides = load_store("workspace_overrides.json", {})
        current = overrides.get(ws_id, {})
        for key in ("name", "description"):
            if key in patch and patch[key] is not None:
                current[key] = patch[key]
        overrides[ws_id] = current
        save_store("workspace_overrides.json", overrides)
        return self.get_workspace(ws_id)

    def delete_workspace(self, ws_id: str) -> dict[str, Any]:
        if not ws_id:
            return {
                "ok": False,
                "removed": False,
                "workspace_id": ws_id,
                "reason": "empty workspace_id",
            }

        active = self._active_run_for_workspace(ws_id)
        if active is not None:
            raise ValueError(
                f"workspace {ws_id} has an active run ({active.get('id')}); cancel it before deleting"
            )

        run_results: list[dict[str, Any]] = []
        for run_id, run in list(self._web_runs().items()):
            if isinstance(run, dict) and run.get("workspace_id") == ws_id:
                try:
                    run_results.append(self.delete_run(run_id, ws_id))
                except Exception as exc:
                    run_results.append(
                        {
                            "ok": False,
                            "removed": False,
                            "run_id": run_id,
                            "reason": str(exc),
                        }
                    )

        threads = load_store("threads.json", [])
        kept_threads: list[dict[str, Any]] = []
        thread_messages_removed = 0
        for thread in threads:
            if isinstance(thread, dict) and thread.get("workspace_id") == ws_id:
                msg_path = store_path(f"messages_{thread.get('id')}.json")
                if msg_path.exists():
                    try:
                        msg_path.unlink(missing_ok=True)
                        thread_messages_removed += 1
                    except OSError:
                        pass
            else:
                kept_threads.append(thread)
        if len(kept_threads) != len(threads):
            save_store("threads.json", kept_threads)

        overrides = load_store("workspace_overrides.json", {})
        overrides_removed = 0
        if ws_id in overrides:
            del overrides[ws_id]
            save_store("workspace_overrides.json", overrides)
            overrides_removed = 1

        settings_removed = 0
        settings_path = store_path(f"settings_{ws_id}.json")
        if settings_path.exists():
            try:
                settings_path.unlink(missing_ok=True)
                settings_removed = 1
            except OSError:
                pass

        report = wipe_workspace_artifacts(self.repo_root, ws_id)
        for path in self._stop_request_paths(ws_id):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

        workspace_dir = self.workspaces_root / ws_id
        ok = not workspace_dir.exists() and not report.errors
        report.counts.setdefault("threads_messages_removed", thread_messages_removed)
        report.counts.setdefault("workspace_overrides_removed", overrides_removed)
        report.counts.setdefault("settings_files_removed", settings_removed)
        return {
            "ok": ok,
            "removed": not workspace_dir.exists(),
            "workspace_id": ws_id,
            "report": report.to_dict(),
            "run_results": run_results,
        }

    def _events(self, ws_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        path = (
            self.workspaces_root / ws_id / ".memory" / "drive" / "logs" / "events.jsonl"
        )
        return read_jsonl(path, limit=limit)

    def _list_task_results(self, ws_id: str) -> list[dict[str, Any]]:
        results_dir = (
            self.workspaces_root / ws_id / ".memory" / "drive" / "task_results"
        )
        if not results_dir.exists():
            return []
        results: list[dict[str, Any]] = []
        for f in sorted(
            results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            if f.name.endswith(".verification.md"):
                continue
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(obj, dict):
                obj["_path"] = str(f.relative_to(self.repo_root))
                obj["_mtime"] = f.stat().st_mtime
                results.append(obj)
        return results

    def _tools_used_for_run(self, ws_id: str, run_id: str) -> list[str]:
        attempt_task_ids = self._attempt_task_ids_for_run(run_id)
        rows = read_jsonl(
            self.workspaces_root / ws_id / ".memory" / "drive" / "logs" / "tools.jsonl",
            limit=1200,
        )
        tools: list[str] = []
        for row in rows:
            if not self._task_id_matches_run(
                row.get("task_id"), run_id, attempt_task_ids
            ):
                continue
            tool = str(row.get("tool") or row.get("tool_name") or "").strip()
            if tool and tool not in tools:
                tools.append(tool)
        return tools

    def _run_log_summary(self, ws_id: str, run_id: str) -> dict[str, Any]:
        drive = self.workspaces_root / ws_id / ".memory" / "drive"
        attempt_task_ids = self._attempt_task_ids_for_run(run_id)
        round_rows = read_jsonl(drive / "logs" / "round_io.jsonl", limit=12000)
        event_rows = read_jsonl(drive / "logs" / "events.jsonl", limit=12000)
        tool_rows = read_jsonl(drive / "logs" / "tools.jsonl", limit=16000)

        prompt_tokens = 0
        completion_tokens = 0
        models: list[str] = []
        llm_rounds = 0
        for row in round_rows:
            if not self._task_id_matches_run(row.get("task_id"), run_id, attempt_task_ids):
                continue
            llm_rounds += 1
            usage = row.get("usage") if isinstance(row.get("usage"), dict) else row
            prompt_tokens += self._coerce_int(usage.get("prompt_tokens"), 0) or 0
            completion_tokens += self._coerce_int(usage.get("completion_tokens"), 0) or 0
            model = str(row.get("model") or "").strip()
            if model and model not in models:
                models.append(model)

        if llm_rounds == 0:
            for row in event_rows:
                if str(row.get("type") or "") != "llm_round":
                    continue
                if not self._task_id_matches_run(row.get("task_id"), run_id, attempt_task_ids):
                    continue
                llm_rounds += 1
                prompt_tokens += self._coerce_int(row.get("prompt_tokens"), 0) or 0
                completion_tokens += self._coerce_int(row.get("completion_tokens"), 0) or 0
                model = str(row.get("model") or "").strip()
                if model and model not in models:
                    models.append(model)

        tools: list[str] = []
        verification_status: str | None = None
        for row in tool_rows:
            if not self._task_id_matches_run(row.get("task_id"), run_id, attempt_task_ids):
                continue
            tool = str(row.get("tool") or row.get("tool_name") or "").strip()
            if tool and tool not in tools:
                tools.append(tool)
            if tool == "run_workspace_verify":
                preview = str(row.get("result_preview") or "")
                lower = preview.lower()
                if '"skipped": true' in lower or "skipped" in lower:
                    verification_status = "skipped"
                elif '"passed": true' in lower or "passed" in lower:
                    verification_status = "passed"
                elif preview:
                    verification_status = "failed"

        forbidden = 0
        for row in event_rows:
            if str(row.get("type") or "") != "tool_forbidden":
                continue
            if self._task_id_matches_run(row.get("task_id"), run_id, attempt_task_ids):
                forbidden += 1

        return {
            "llm_rounds": llm_rounds,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "models": models,
            "tools_used": tools,
            "forbidden_tool_attempts": forbidden,
            "verification_status": verification_status,
        }

    def _workspace_has_run_artifacts(self, ws_id: str | None, run_id: str) -> bool:
        if not ws_id:
            return False
        workspace_memory = self.workspaces_root / ws_id / ".memory"
        for directory in (
            workspace_memory / "drive" / "task_results",
            workspace_memory / "drive" / "task_plans",
        ):
            if not directory.exists():
                continue
            for pattern in (
                f"{run_id}.json",
                f"{run_id}.verification.md",
                f"{run_id}__a*.json",
                f"{run_id}__a*.verification.md",
                f"{run_id}__s*.json",
                f"{run_id}__s*.verification.md",
            ):
                if any(directory.glob(pattern)):
                    return True
        for path in (
            workspace_memory / "drive" / "logs" / "events.jsonl",
            workspace_memory / "drive" / "logs" / "round_io.jsonl",
            workspace_memory / "drive" / "logs" / "tools.jsonl",
            workspace_memory / "ideas.jsonl",
            workspace_memory / "lessons.jsonl",
        ):
            for row in read_jsonl(path, limit=5000):
                if self._task_id_matches_run(
                    row.get("task_id") or row.get("run_id"), run_id
                ):
                    return True
        return False

    @classmethod
    def _compact_run_for_list(cls, run: dict[str, Any]) -> dict[str, Any]:
        compact = dict(run)
        compact.pop("full_result", None)
        if isinstance(compact.get("result_preview"), str):
            compact["result_preview"] = short_text(compact["result_preview"], 600)
        if isinstance(compact.get("phase_events"), list):
            compact["phase_events"] = compact["phase_events"][-10:]
        return compact

    def _infer_workspace_id_for_run(self, run_id: str) -> str | None:
        web_run = self._get_web_run(run_id)
        if web_run and web_run.get("workspace_id"):
            return str(web_run["workspace_id"])

        threads = load_store("threads.json", [])
        for thread in threads if isinstance(threads, list) else []:
            if not isinstance(thread, dict):
                continue
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                continue
            messages = load_store(f"messages_{thread_id}.json", [])
            if not isinstance(messages, list):
                continue
            if any(isinstance(m, dict) and m.get("run_id") == run_id for m in messages):
                ws_id = str(thread.get("workspace_id") or "").strip()
                if ws_id:
                    return ws_id

        for ws in self.list_workspaces():
            ws_id = str(ws.get("id") or "")
            if ws_id and self._workspace_has_run_artifacts(ws_id, run_id):
                return ws_id
        return None

    @staticmethod
    def _remove_path(path: Path) -> bool:
        if not path.exists():
            return False
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def delete_run(self, run_id: str, ws_id: str | None = None) -> dict[str, Any]:
        scoped_ws_id = str(ws_id or "").strip() or None
        run = self.get_run(run_id, scoped_ws_id)
        if run and scoped_ws_id and str(run.get("workspace_id") or "") != scoped_ws_id:
            return {
                "ok": False,
                "removed": False,
                "run_id": run_id,
                "workspace_id": scoped_ws_id,
                "reason": "run belongs to a different workspace",
            }
        if not run:
            try:
                legacy_launcher_run = next(
                    (
                        item
                        for item in load_launcher_runs()
                        if isinstance(item, dict)
                        and str(item.get("id") or "") == run_id
                    ),
                    None,
                )
            except Exception:
                legacy_launcher_run = None
            if legacy_launcher_run:
                legacy_ws_id = str(
                    legacy_launcher_run.get("workspace_id") or ""
                ).strip()
                if scoped_ws_id and legacy_ws_id and legacy_ws_id != scoped_ws_id:
                    return {
                        "ok": False,
                        "removed": False,
                        "run_id": run_id,
                        "workspace_id": scoped_ws_id,
                        "reason": "run belongs to a different workspace",
                    }
                run = {
                    **legacy_launcher_run,
                    "workspace_id": legacy_ws_id or scoped_ws_id or "",
                    "status": legacy_launcher_run.get("status") or "completed",
                }
        if not run:
            inferred_ws_id = (
                scoped_ws_id
                if self._workspace_has_run_artifacts(scoped_ws_id, run_id)
                else None
            )
            inferred_ws_id = inferred_ws_id or (
                None if scoped_ws_id else self._infer_workspace_id_for_run(run_id)
            )
            if not inferred_ws_id:
                return {
                    "ok": False,
                    "removed": False,
                    "run_id": run_id,
                    "workspace_id": scoped_ws_id,
                    "reason": "run not found and no matching artifacts were found",
                }
            run = {
                "id": run_id,
                "workspace_id": inferred_ws_id,
                "status": "completed",
                "attempt_task_ids": [run_id],
            }
        if run.get("status") in {"queued", "running"}:
            self.cancel_run(run_id, wait_seconds=15.0)
            run = self.get_run(run_id) or run
        worker = self._run_threads.get(run_id)
        if worker is not None and worker.is_alive():
            worker.join(timeout=5.0)
        if worker is not None and worker.is_alive():
            self._upsert_web_run(
                run_id,
                {
                    "status": "cancelled",
                    "result_preview": (
                        "Cancelled and detached: agent worker did not stop within 20s, "
                        "artifacts were not removed."
                    ),
                    "updated_at": iso_utc(now_ts()),
                    "finished_at": iso_utc(now_ts()),
                },
            )
            return {
                "ok": False,
                "removed": False,
                "run_id": run_id,
                "reason": "worker still alive after cancel; refusing to wipe live artifacts",
            }
        ws_id = str(run.get("workspace_id") or "")
        full_result = (
            run.get("full_result") if isinstance(run.get("full_result"), dict) else {}
        )
        candidate_manifest = full_result.get("candidate_manifest_path") or run.get(
            "candidate_manifest_path"
        )
        candidate_id = str(
            full_result.get("candidate_id") or run.get("candidate_id") or ""
        ).strip()
        attempt_task_ids = self._attempt_task_ids_for_run(run_id, run)
        harness_meta = (
            run.get("harness_meta") if isinstance(run.get("harness_meta"), dict) else {}
        )
        candidate_run_ids = [
            str(item)
            for item in (harness_meta.get("candidate_run_ids") or [])
            if str(item or "").strip()
        ]

        details: dict[str, Any] = {
            "web_run": False,
            "launcher_runs": 0,
            "messages": 0,
            "candidate_index_filtered": 0,
        }

        rows = self._web_runs()
        if run_id in rows:
            del rows[run_id]
            self._save_web_runs(rows)
            details["web_run"] = True

        try:
            launcher_rows = load_launcher_runs()
        except Exception:
            launcher_rows = []
        launcher_log_paths_removed: list[str] = []
        if launcher_rows:
            kept_launcher_rows = []
            for item in launcher_rows:
                if not isinstance(item, dict) or str(item.get("id") or "") != run_id:
                    kept_launcher_rows.append(item)
                    continue
                details["launcher_runs"] += 1
                log_path = item.get("log_path")
                if log_path:
                    path = Path(str(log_path))
                    if not path.is_absolute():
                        path = self.repo_root / path
                    if self._remove_path(path):
                        launcher_log_paths_removed.append(str(path))
            if len(kept_launcher_rows) != len(launcher_rows):
                try:
                    save_launcher_runs(kept_launcher_rows)
                except Exception:
                    pass

        report = wipe_run_artifacts(
            self.repo_root,
            ws_id or None,
            run_id,
            attempt_task_ids,
            candidate_manifest_path=candidate_manifest,
            candidate_id=candidate_id or None,
            candidate_run_ids=candidate_run_ids,
        )
        for path_str in launcher_log_paths_removed:
            report.removed_paths.append(path_str)
            report.counts["launcher_log"] = report.counts.get("launcher_log", 0) + 1

        # Filter the global meta-harness candidate index (separate file
        # whose row layout we know about).
        candidate_index = (
            self.repo_root
            / ".umbrella"
            / "meta_harness"
            / "experiments"
            / "_default"
            / "candidates"
            / "index.json"
        )
        if candidate_index.exists():
            try:
                payload = json.loads(
                    candidate_index.read_text(encoding="utf-8", errors="replace")
                )
            except Exception:
                payload = None

            def candidate_matches(item: Any, key: str = "") -> bool:
                if key and candidate_id and key == candidate_id:
                    return True
                if not isinstance(item, dict):
                    return False
                item_candidate_id = str(
                    item.get("candidate_id") or item.get("id") or ""
                ).strip()
                if candidate_id and item_candidate_id == candidate_id:
                    return True
                return self._task_id_matches_run(
                    item.get("task_id"), run_id, attempt_task_ids
                )

            def remember_candidate_dir(item: Any) -> Path | None:
                if not isinstance(item, dict):
                    return None
                manifest = item.get("manifest_path") or item.get(
                    "candidate_manifest_path"
                )
                if manifest:
                    manifest_path = Path(str(manifest))
                    if not manifest_path.is_absolute():
                        manifest_path = self.repo_root / manifest_path
                    return manifest_path.parent
                item_candidate_id = str(
                    item.get("candidate_id") or item.get("id") or ""
                ).strip()
                if item_candidate_id:
                    return candidate_index.parent / item_candidate_id
                return None

            extra_dirs: list[Path] = []
            if isinstance(payload, list):
                kept = []
                for item in payload:
                    if candidate_matches(item):
                        target = remember_candidate_dir(item)
                        if target is not None:
                            extra_dirs.append(target)
                    else:
                        kept.append(item)
                if len(kept) != len(payload):
                    candidate_index.write_text(
                        json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    details["candidate_index_filtered"] += len(payload) - len(kept)
            elif isinstance(payload, dict):
                changed = False
                for key in list(payload.keys()):
                    item = payload.get(key)
                    if candidate_matches(item, key):
                        target = remember_candidate_dir(item)
                        if target is not None:
                            extra_dirs.append(target)
                        del payload[key]
                        changed = True
                if changed:
                    candidate_index.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    details["candidate_index_filtered"] += 1
            umbrella_root = (self.repo_root / ".umbrella").resolve()
            for candidate_dir in extra_dirs:
                try:
                    candidate_dir.resolve().relative_to(umbrella_root)
                except (OSError, ValueError):
                    continue
                if candidate_dir.exists() and self._remove_path(candidate_dir):
                    report.removed_paths.append(str(candidate_dir))
                    report.counts["harness_candidate_dir"] = (
                        report.counts.get("harness_candidate_dir", 0) + 1
                    )

        threads = load_store("threads.json", [])
        for thread in threads:
            thread_id = thread.get("id")
            if not thread_id:
                continue
            messages_path = store_path(f"messages_{thread_id}.json")
            messages = load_store(f"messages_{thread_id}.json", [])
            if not isinstance(messages, list):
                continue
            kept_messages = [
                m
                for m in messages
                if not (isinstance(m, dict) and m.get("run_id") == run_id)
            ]
            if len(kept_messages) != len(messages):
                details["messages"] += len(messages) - len(kept_messages)
                save_store(f"messages_{thread_id}.json", kept_messages)
                thread["message_count"] = len(kept_messages)
                thread["updated_at"] = iso_utc(now_ts())
            elif not messages_path.exists():
                continue
        save_store("threads.json", threads)

        # Empty the now-orphaned workspace .memory dir if no other runs
        # still reference it (mirrors prior behavior so the next run
        # starts from a clean slate).
        if ws_id:
            workspace_memory = self.workspaces_root / ws_id / ".memory"
            remaining_workspace_runs = [
                item
                for item in self._web_runs().values()
                if isinstance(item, dict) and item.get("workspace_id") == ws_id
            ]
            task_results_dir = workspace_memory / "drive" / "task_results"
            remaining_task_results = []
            if task_results_dir.exists():
                remaining_task_results = [
                    path
                    for path in task_results_dir.glob("*.json")
                    if not path.name.endswith(".verification.md")
                ]
            if (
                not remaining_workspace_runs
                and not remaining_task_results
                and workspace_memory.exists()
            ):
                # Release the cached PalaceBackend / ChromaDB client so the
                # SQLite WAL file and HNSW data_level0.bin are closed
                # before we remove the directory. Without this Windows
                # raises PermissionError and the workspace .memory/palace
                # tree leaks across runs.
                import gc as _gc
                import time as _time

                try:
                    from umbrella.memory.palace_backend import (
                        clear_palace_backend_cache,
                    )

                    clear_palace_backend_cache(workspace_memory / "palace")
                except Exception:
                    pass
                try:
                    from chromadb.api.shared_system_client import (
                        SharedSystemClient,
                    )

                    SharedSystemClient._identifier_to_system.clear()
                except Exception:
                    pass
                _gc.collect()
                # ChromaDB releases mmap'd HNSW files asynchronously on
                # Windows; give the OS a moment to drop the handles
                # before the first rmtree attempt.
                _time.sleep(0.5)

                last_exc: OSError | None = None
                for attempt in range(8):
                    try:
                        shutil.rmtree(workspace_memory)
                        last_exc = None
                        break
                    except OSError as exc:
                        last_exc = exc
                        _gc.collect()
                        _time.sleep(0.3 * (attempt + 1))
                if last_exc is None:
                    report.removed_paths.append(str(workspace_memory))
                    report.counts["workspace_memory_dir"] = 1
                else:
                    report.errors.append(
                        f"failed to remove {workspace_memory}: {last_exc}"
                    )

        return {
            "ok": True,
            "removed": True,
            "run_id": run_id,
            "workspace_id": ws_id,
            "details": details,
            "report": report.to_dict(),
        }

    def list_runs(
        self, ws_id: str | None, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        if not ws_id:
            return {"runs": [], "total": 0}
        all_results = self._list_task_results(ws_id)
        harness_child_ids: set[str] = set()
        web_runs = [
            self._repair_stale_web_run(run)
            for run in self._web_runs().values()
            if isinstance(run, dict)
        ]
        # Build a map of every existing parent web_run id -> its full
        # set of attempt/internal/harness child ids, so subordinate
        # task_results (remediation rounds, harness stage candidates,
        # internal retries) collapse into the parent row instead of
        # showing up as separate rows in the UI.
        parent_attempt_ids: dict[str, set[str]] = {}
        for web_run in web_runs:
            if (
                not isinstance(web_run, dict)
                or (web_run.get("workspace_id") or web_run.get("workspace")) != ws_id
            ):
                continue
            parent_id = str(web_run.get("id") or "")
            if not parent_id:
                continue
            attempt_ids = self._attempt_task_ids_for_run(parent_id, web_run)
            full_result = (
                web_run.get("full_result")
                if isinstance(web_run.get("full_result"), dict)
                else {}
            )
            for value in full_result.get("internal_task_ids") or []:
                if str(value or "").strip():
                    attempt_ids.add(str(value))
            parent_attempt_ids[parent_id] = attempt_ids
            harness_meta = (
                web_run.get("harness_meta")
                if isinstance(web_run.get("harness_meta"), dict)
                else {}
            )
            for child_id in harness_meta.get("candidate_run_ids") or []:
                if str(child_id or "").strip():
                    harness_child_ids.add(str(child_id))
            for stage in harness_meta.get("stages") or []:
                if not isinstance(stage, dict):
                    continue
                for candidate in stage.get("candidates") or []:
                    if (
                        isinstance(candidate, dict)
                        and str(candidate.get("run_id") or "").strip()
                    ):
                        harness_child_ids.add(str(candidate["run_id"]))
        runs_by_id = {
            run["id"]: run
            for run in (
                r
                for r in web_runs
                if isinstance(r, dict)
                and (r.get("workspace_id") or r.get("workspace")) == ws_id
            )
        }
        # Track tools/model surfaced from filtered child task_results so we
        # can fold them back into the parent web_run row.
        parent_extras: dict[str, dict[str, Any]] = {}
        for raw in all_results:
            run = self._task_result_to_run(raw, ws_id)
            if run["id"] in harness_child_ids:
                continue
            # If this task_result belongs to a parent web_run as a
            # remediation/internal retry, merge useful bits into the
            # parent and skip surfacing it as its own row.
            parent_match: str | None = None
            if run["id"] in runs_by_id:
                parent_match = run["id"]
            else:
                for parent_id, ids in parent_attempt_ids.items():
                    if self._task_id_matches_run(run["id"], parent_id, ids):
                        parent_match = parent_id
                        break
            if parent_match and parent_match != run["id"]:
                extras = parent_extras.setdefault(
                    parent_match, {"tools_used": [], "models": []}
                )
                for tool in run.get("tools_used") or []:
                    if tool and tool not in extras["tools_used"]:
                        extras["tools_used"].append(tool)
                model = run.get("model")
                if model and model not in extras["models"]:
                    extras["models"].append(model)
                continue
            existing = runs_by_id.get(run["id"])
            if existing and existing.get("source") == "web_bridge":
                merged = {**run, **existing}
                if not merged.get("tools_used"):
                    merged["tools_used"] = self._tools_used_for_run(ws_id, run["id"])
                runs_by_id[run["id"]] = merged
            elif existing is None or existing.get("status") != "running":
                if not run.get("tools_used"):
                    run["tools_used"] = self._tools_used_for_run(ws_id, run["id"])
                runs_by_id[run["id"]] = run
        # Apply folded extras (tools_used aggregation + model fallback).
        for parent_id, extras in parent_extras.items():
            row = runs_by_id.get(parent_id)
            if not isinstance(row, dict):
                continue
            existing_tools = list(row.get("tools_used") or [])
            for tool in extras.get("tools_used") or []:
                if tool and tool not in existing_tools:
                    existing_tools.append(tool)
            if existing_tools:
                row["tools_used"] = existing_tools
            if not row.get("model"):
                models = extras.get("models") or []
                if models:
                    row["model"] = models[0]
        # Last-resort fill: any row still missing model gets the harness
        # candidate model or the bridge's current default so the UI never
        # shows N/A for a real run.
        for row in runs_by_id.values():
            if isinstance(row, dict) and not row.get("model"):
                harness_meta = (
                    row.get("harness_meta")
                    if isinstance(row.get("harness_meta"), dict)
                    else {}
                )
                models = harness_meta.get("models") or []
                if isinstance(models, list) and models:
                    row["model"] = models[0]
        runs = sorted(
            runs_by_id.values(),
            key=lambda r: str(r.get("updated_at") or ""),
            reverse=True,
        )
        total = len(runs)
        runs = [
            self._compact_run_for_list(run) for run in runs[offset : offset + limit]
        ]
        return {"runs": runs, "total": total, "limit": limit, "offset": offset}

    def _task_result_to_run(self, task: dict[str, Any], ws_id: str) -> dict[str, Any]:
        raw_status = str(task.get("status") or "queued").lower()
        status_map = {
            "complete": "completed",
            "completed": "completed",
            "verified": "completed",
            "ok": "completed",
            "success": "completed",
            "failed": "failed",
            "failed_verification": "failed",
            "failed_hygiene": "failed",
            "failed_self_review": "failed",
            "phase_impasse": "failed",
            "incomplete_subtasks": "failed",
            "incomplete_discovery": "failed",
            "verified_with_blocking_noise": "failed",
            "incomplete": "failed",
            "error": "failed",
            "running": "running",
            "in_progress": "running",
            "cancelled": "cancelled",
            "stopped": "cancelled",
            "queued": "queued",
        }
        status = status_map.get(
            raw_status, raw_status if raw_status in status_map.values() else "queued"
        )
        # Stop-text override: when the launcher persisted ``status='completed'``
        # because the agent's ``_check_stop_requested`` returned its canonical
        # "Stop requested by dashboard: …" message, the row must surface as
        # ``cancelled`` in the UI — otherwise users see "Готово" for a run
        # they explicitly killed. The integration's loop already maps this to
        # ``cancelled`` on the live worker, but old task_result files written
        # before the integration fix still carry ``status='completed'``.
        if status != "cancelled":
            result_text = str(
                task.get("final_message")
                or task.get("result")
                or task.get("result_text")
                or task.get("result_preview")
                or ""
            ).strip()
            if result_text:
                head = result_text.splitlines()[0].strip().lower()
                if (
                    head.startswith("stop requested by dashboard")
                    or head.startswith("stop requested from the web ui")
                    or head.startswith("stop requested by web ui")
                    or head.startswith("stop_requested")
                ):
                    status = "cancelled"
        ts_raw = task.get("ts")
        try:
            if isinstance(ts_raw, str) and ts_raw:
                ts = time.mktime(time.strptime(ts_raw[:19], "%Y-%m-%dT%H:%M:%S"))
            elif isinstance(ts_raw, (int, float)):
                ts = float(ts_raw)
            else:
                ts = task.get("_mtime") or now_ts()
        except Exception:
            ts = task.get("_mtime") or now_ts()
        run_id = str(task.get("task_id") or task.get("id") or uuid.uuid4().hex)
        model = (
            task.get("model")
            or task.get("llm_model")
            or task.get("active_model")
            or task.get("model_used")
        )
        return {
            "id": run_id,
            "workspace_id": ws_id,
            "status": status,
            "model": model,
            "total_cost": float(task.get("cost_usd") or 0.0),
            "total_steps": int(
                task.get("llm_tool_invocations")
                or task.get("total_rounds")
                or task.get("rounds")
                or 0
            ),
            "total_duration_ms": int(
                task.get("duration_ms") or float(task.get("duration_sec") or 0) * 1000
            ),
            "tools_used": task.get("tools_used") or [],
            "task_text": task.get("task_text") or task.get("task_input"),
            "result_preview": short_text(
                str(
                    task.get("final_message")
                    or task.get("result")
                    or task.get("error")
                    or ""
                ),
                600,
            ),
            "created_at": iso_utc(ts),
            "updated_at": iso_utc(ts),
        }

    def get_run(self, run_id: str, ws_id: str | None = None) -> dict[str, Any] | None:
        web_run = self._get_web_run(run_id)
        if isinstance(web_run, dict):
            web_run = self._repair_stale_web_run(web_run)
        if web_run and web_run.get("status") in {"queued", "running"}:
            return web_run
        candidates: list[str]
        if ws_id:
            candidates = [ws_id]
        elif web_run and web_run.get("workspace_id"):
            candidates = [str(web_run["workspace_id"])]
        else:
            candidates = [w["id"] for w in self.list_workspaces()]
        for wid in candidates:
            for raw in self._list_task_results(wid):
                rid = str(raw.get("task_id") or raw.get("id") or "")
                if rid == run_id:
                    run = self._task_result_to_run(raw, wid)
                    run["full_result"] = raw
                    run["raw_path"] = raw.get("_path")
                    if not run.get("tools_used"):
                        run["tools_used"] = self._tools_used_for_run(wid, run_id)
                    if web_run:
                        merged = {**run, **web_run, "raw_path": run.get("raw_path")}
                        if not merged.get("tools_used"):
                            merged["tools_used"] = run.get("tools_used") or []
                        return merged
                    return run
        return web_run

    def get_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        run = self.get_run(run_id)
        if not run:
            return []
        ws_id = run["workspace_id"]
        attempt_task_ids = self._attempt_task_ids_for_run(run_id, run)
        configured_rounds = self._coerce_int(run.get("max_rounds"), 0, min_value=0) or 0
        attempt_count = max(1, len(attempt_task_ids))
        per_attempt_rounds = (
            200 if configured_rounds == 0 else max(120, configured_rounds + 40)
        )
        round_limit = max(2000, per_attempt_rounds * attempt_count)
        round_io = read_jsonl(
            self.workspaces_root
            / ws_id
            / ".memory"
            / "drive"
            / "logs"
            / "round_io.jsonl",
            limit=round_limit,
        )
        tool_io = read_jsonl(
            self.workspaces_root / ws_id / ".memory" / "drive" / "logs" / "tools.jsonl",
            limit=max(1600, round_limit * 2),
        )
        event_io = read_jsonl(
            self.workspaces_root
            / ws_id
            / ".memory"
            / "drive"
            / "logs"
            / "events.jsonl",
            limit=max(1600, round_limit * 2),
        )
        steps: list[dict[str, Any]] = []
        prev_ts_by_kind: dict[str, float] = {}
        for idx, ev in enumerate(event_io):
            if str(ev.get("kind") or "") != "harness":
                continue
            if not self._task_id_matches_run(
                ev.get("task_id"), run_id, attempt_task_ids
            ):
                continue
            ev_type = str(ev.get("type") or "harness_event")
            if not (
                ev_type.startswith("stage_")
                or ev_type in {"harness_started", "harness_finished"}
            ):
                continue
            ts_val = self._parse_ts_value(
                ev.get("ts") or ev.get("timestamp") or ev.get("created_at")
            )
            prev_ts = prev_ts_by_kind.get("harness")
            duration_ms = int((ts_val - prev_ts) * 1000) if prev_ts and ts_val else 0
            if ts_val:
                prev_ts_by_kind["harness"] = ts_val
            steps.append(
                {
                    "id": f"{run_id}-harness-{idx}",
                    "run_id": run_id,
                    "type": "thinking",
                    "name": ev_type,
                    "status": "failed"
                    if "failed" in str(ev.get("message") or "").lower()
                    else "completed",
                    "duration_ms": max(duration_ms, 0),
                    "cost": 0.0,
                    "data": short_text(
                        json.dumps(
                            {
                                "stage": {
                                    "index": ev.get("stage_index"),
                                    "id": ev.get("stage_id"),
                                    "title": ev.get("stage_title"),
                                    "kind": ev.get("stage_kind"),
                                },
                                "candidate_id": ev.get("candidate_id"),
                                "message": ev.get("message"),
                                "data": ev.get("data"),
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                        1800,
                    ),
                    "created_at": iso_utc(ts_val) if ts_val else iso_utc(now_ts()),
                }
            )
        diagnostic_event_types = {
            "tool_preflight_error",
            "tool_arg_error",
            "tool_forbidden",
            "tool_execution_error",
            "task_error",
            "subtask_rescue_continuation",
        }
        for idx, ev in enumerate(event_io):
            if not self._task_id_matches_run(
                ev.get("task_id"), run_id, attempt_task_ids
            ):
                continue
            ev_type = str(ev.get("type") or "")
            if ev_type not in diagnostic_event_types:
                continue
            ts_val = self._parse_ts_value(
                ev.get("ts") or ev.get("timestamp") or ev.get("created_at")
            )
            tool_name = str(ev.get("tool") or ev.get("tool_name") or ev_type)
            steps.append(
                {
                    "id": f"{run_id}-event-{idx}",
                    "run_id": run_id,
                    "type": "tool_call",
                    "name": ev_type,
                    "status": "failed"
                    if ("error" in ev_type or ev.get("error"))
                    else "completed",
                    "duration_ms": 0,
                    "cost": 0.0,
                    "data": short_text(
                        json.dumps(
                            {
                                "tool": tool_name,
                                "phase": ev.get("phase"),
                                "round": ev.get("round"),
                                "reason": ev.get("reason"),
                                "message": ev.get("message"),
                                "error": ev.get("error"),
                                "args": ev.get("args"),
                                "repair_attempt": ev.get("repair_attempt"),
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                        2200,
                    ),
                    "created_at": iso_utc(ts_val) if ts_val else iso_utc(now_ts()),
                }
            )
        for idx, ev in enumerate(round_io):
            if not self._task_id_matches_run(
                ev.get("task_id"), run_id, attempt_task_ids
            ):
                continue
            ev_type = str(ev.get("type") or ev.get("phase") or "round")
            ts = ev.get("ts") or ev.get("timestamp")
            ts_val = self._parse_ts_value(ts)
            prev_ts = prev_ts_by_kind.get("round")
            duration_ms = int((ts_val - prev_ts) * 1000) if prev_ts and ts_val else 0
            if ts_val:
                prev_ts_by_kind["round"] = ts_val
            output = ev.get("output") if isinstance(ev.get("output"), dict) else {}
            tool_calls = output.get("tool_calls") if isinstance(output, dict) else []
            preview = output.get("content_preview") if isinstance(output, dict) else ""
            name = str(ev.get("phase") or ev_type)
            steps.append(
                {
                    "id": f"{run_id}-{idx}",
                    "run_id": run_id,
                    "type": "thinking",
                    "name": f"{name} round {ev.get('round')}",
                    "status": "completed",
                    "duration_ms": max(duration_ms, 0),
                    "cost": 0.0,
                    "data": short_text(
                        json.dumps(
                            {
                                "phase": ev.get("phase"),
                                "model": ev.get("model"),
                                "round": ev.get("round"),
                                "assistant_preview": preview,
                                "tool_calls_requested": tool_calls,
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                        1800,
                    ),
                    "created_at": iso_utc(ts_val) if ts_val else iso_utc(now_ts()),
                }
            )
        for idx, ev in enumerate(tool_io):
            if not self._task_id_matches_run(
                ev.get("task_id"), run_id, attempt_task_ids
            ):
                continue
            ts_val = self._parse_ts_value(ev.get("ts") or ev.get("timestamp"))
            prev_ts = prev_ts_by_kind.get("tool")
            duration_ms = int((ts_val - prev_ts) * 1000) if prev_ts and ts_val else 0
            if ts_val:
                prev_ts_by_kind["tool"] = ts_val
            tool_name = str(ev.get("tool") or ev.get("tool_name") or "tool")
            steps.append(
                {
                    "id": f"{run_id}-tool-{idx}",
                    "run_id": run_id,
                    "type": "tool_call",
                    "name": tool_name,
                    "status": "completed",
                    "duration_ms": max(duration_ms, 0),
                    "cost": 0.0,
                    "data": short_text(
                        json.dumps(
                            {
                                "args": ev.get("args"),
                                "result_preview": ev.get("result_preview")
                                or ev.get("result"),
                            },
                            ensure_ascii=False,
                            default=str,
                        ),
                        2000,
                    ),
                    "created_at": iso_utc(ts_val) if ts_val else iso_utc(now_ts()),
                }
            )
        terminal_path = (
            self.workspaces_root
            / ws_id
            / ".memory"
            / "drive"
            / "memory"
            / "terminal_scrollback.md"
        )
        if terminal_path.exists():
            try:
                terminal_text = terminal_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                terminal_text = _filter_terminal_scrollback_for_run(
                    terminal_text, run_id
                )
                terminal_mtime = terminal_path.stat().st_mtime
            except OSError:
                terminal_text = ""
                terminal_mtime = now_ts()
            if terminal_text.strip():
                steps.append(
                    {
                        "id": f"{run_id}-terminal-scrollback",
                        "run_id": run_id,
                        "type": "tool_call",
                        "name": "terminal_scrollback",
                        "status": "completed",
                        "duration_ms": 0,
                        "cost": 0.0,
                        "data": short_text(terminal_text[-5000:], 5000),
                        "created_at": iso_utc(terminal_mtime),
                    }
                )
        non_terminal_steps = [
            s for s in steps if s.get("name") != "terminal_scrollback"
        ]
        if not non_terminal_steps and isinstance(run.get("full_result"), dict):
            result = run["full_result"]
            ts = run.get("updated_at") or run.get("created_at") or iso_utc(now_ts())
            telemetry = (
                result.get("quality_telemetry")
                if isinstance(result.get("quality_telemetry"), dict)
                else {}
            )
            phases = telemetry.get("phases") if isinstance(telemetry, dict) else []
            for idx, phase in enumerate(phases or []):
                steps.append(
                    {
                        "id": f"{run_id}-phase-{idx}",
                        "run_id": run_id,
                        "type": "thinking",
                        "name": str(phase),
                        "status": "completed",
                        "duration_ms": 0,
                        "cost": 0.0,
                        "data": short_text(
                            json.dumps(
                                {
                                    "phase": phase,
                                    "model": result.get("model") or run.get("model"),
                                    "llm_rounds": telemetry.get("llm_rounds"),
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                            1200,
                        ),
                        "created_at": ts,
                    }
                )
            verification = result.get("verification_report")
            if isinstance(verification, dict):
                for idx, item in enumerate(verification.get("results") or []):
                    if not isinstance(item, dict):
                        continue
                    status = "completed" if item.get("status") == "passed" else "failed"
                    steps.append(
                        {
                            "id": f"{run_id}-verify-{idx}",
                            "run_id": run_id,
                            "type": "tool_call",
                            "name": str(item.get("name") or "verification"),
                            "status": status,
                            "duration_ms": int(
                                float(item.get("duration_seconds") or 0) * 1000
                            ),
                            "cost": 0.0,
                            "data": short_text(
                                json.dumps(
                                    {
                                        "kind": item.get("kind"),
                                        "summary": item.get("summary"),
                                        "stdout_tail": item.get("stdout_tail"),
                                        "stderr_tail": item.get("stderr_tail"),
                                        "error": item.get("error"),
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                                2200,
                            ),
                            "created_at": ts,
                        }
                    )
            if not any(s.get("name") != "terminal_scrollback" for s in steps):
                steps.append(
                    {
                        "id": f"{run_id}-summary",
                        "run_id": run_id,
                        "type": "response",
                        "name": str(
                            result.get("status") or run.get("status") or "result"
                        ),
                        "status": "failed"
                        if run.get("status") == "failed"
                        else "completed",
                        "duration_ms": 0,
                        "cost": 0.0,
                        "data": short_text(
                            str(
                                result.get("final_message")
                                or result.get("error")
                                or run.get("result_preview")
                                or ""
                            ),
                            2200,
                        ),
                        "created_at": ts,
                    }
                )
        steps.sort(key=lambda item: str(item.get("created_at") or ""))
        return steps

    def get_run_timeline(self, run_id: str) -> dict[str, Any]:
        """Phase-level timeline for the Runs detail view.

        Buckets ``round_io.jsonl`` and ``tools.jsonl`` entries into
        named phases (``initial`` + ``remediation_<N>`` for each
        ``remediation_started`` event). Output shape::

            {
              "run_id": "...",
              "workspace_id": "...",
              "phases": [
                {
                  "name": "initial",
                  "label": "Initial pass",
                  "started_at": "...iso...",
                  "ended_at": "...iso...",
                  "duration_ms": 12345,
                  "rounds": 408,
                  "tool_calls": 612,
                  "write_tool_calls": 100,
                  "preflight_errors": 11,
                  "verification_status": "failed" | "passed" | "skipped",
                },
                ...
              ]
            }

        Empty buckets are still returned so the UI can render a
        coherent linear story.
        """
        run = self.get_run(run_id)
        if not run:
            return {"run_id": run_id, "workspace_id": "", "phases": []}
        ws_id = run["workspace_id"]
        drive_root = self.workspaces_root / ws_id / ".memory" / "drive"

        _plan_path = drive_root / "state" / "phase_plan.json"
        if not _plan_path.exists():
            for _candidate in self.workspaces_root.glob("*/.memory/drive/state/phase_plan.json"):
                try:
                    _d = json.loads(_candidate.read_text(encoding="utf-8"))
                    if _d.get("run_id") == run_id:
                        _plan_path = _candidate
                        break
                except Exception:
                    pass

        if _plan_path.exists():
            try:
                _plan = json.loads(_plan_path.read_text(encoding="utf-8"))
                _nodes = _plan.get("nodes") or []
                _label_map = {
                    "preflight": "Pre-flight",
                    "research": "Research",
                    "research_review": "Research Review",
                    "plan": "Plan",
                    "plan_review": "Plan Review",
                    "execute": "Execute",
                    "execute_review": "Execute Review",
                    "final": "Final",
                    "verify": "Verify",
                    "reflexion": "Reflexion",
                }
                _verify_map = {
                    "done": "passed",
                    "failed": "failed",
                    "skipped": "skipped",
                }
                _phases = []
                for _pn in _nodes:
                    _pid = str(_pn.get("id") or _pn.get("manifest_id") or "")
                    _status = str(_pn.get("status") or "pending")
                    _started = _pn.get("started_at")
                    _ended = _pn.get("ended_at")
                    _duration = None
                    if isinstance(_started, (int, float)) and isinstance(_ended, (int, float)):
                        _duration = max(0, int((_ended - _started) * 1000))
                    _phases.append({
                        "name": _pid,
                        "label": _label_map.get(_pid, _pid.replace("_", " ").title()),
                        "duration_ms": _duration,
                        "rounds": 0,
                        "tool_calls": 0,
                        "write_tool_calls": 0,
                        "preflight_errors": 0,
                        "verification_status": _verify_map.get(_status),
                        "status": _status,
                    })
                attempt_task_ids = self._attempt_task_ids_for_run(run_id, run)
                _round_io = read_jsonl(drive_root / "logs" / "round_io.jsonl", limit=8000)
                _tool_io = read_jsonl(drive_root / "logs" / "tools.jsonl", limit=12000)
                _event_io = read_jsonl(drive_root / "logs" / "events.jsonl", limit=8000)
                _write_tools = {
                    "update_workspace_seed",
                    "apply_workspace_patch",
                    "update_workspace_from_instance",
                    "commit_workspace_changes",
                    "delete_workspace_file",
                    "repo_write_commit",
                }

                def _idx_for_task(task_id: Any) -> int | None:
                    raw = str(task_id or "")
                    for idx, phase in enumerate(_phases):
                        if raw == f"{run_id}:{phase['name']}" or raw.startswith(
                            f"{run_id}:{phase['name']}:"
                        ):
                            return idx
                    return None

                def _idx_for_ts(ts_val: float | None) -> int | None:
                    if ts_val is None:
                        return None
                    for idx, node in enumerate(_nodes):
                        start = node.get("started_at")
                        end = node.get("ended_at")
                        if isinstance(start, (int, float)) and ts_val >= float(start):
                            if not isinstance(end, (int, float)) or ts_val <= float(end):
                                return idx
                    return None

                for _row in _round_io:
                    if not self._task_id_matches_run(_row.get("task_id"), run_id, attempt_task_ids):
                        continue
                    _idx = _idx_for_task(_row.get("task_id")) or _idx_for_ts(
                        self._parse_ts_value(_row.get("ts") or _row.get("timestamp"))
                    )
                    if _idx is not None and 0 <= _idx < len(_phases):
                        _phases[_idx]["rounds"] += 1
                for _row in _tool_io:
                    if not self._task_id_matches_run(_row.get("task_id"), run_id, attempt_task_ids):
                        continue
                    _idx = _idx_for_task(_row.get("task_id")) or _idx_for_ts(
                        self._parse_ts_value(_row.get("ts") or _row.get("timestamp"))
                    )
                    if _idx is not None and 0 <= _idx < len(_phases):
                        _phases[_idx]["tool_calls"] += 1
                        if (
                            str(_row.get("tool") or "") in _write_tools
                            and is_effective_write_tool_log_row(_row)
                        ):
                            _phases[_idx]["write_tool_calls"] += 1
                for _row in _event_io:
                    if str(_row.get("type") or "") != "tool_forbidden":
                        continue
                    if not self._task_id_matches_run(_row.get("task_id"), run_id, attempt_task_ids):
                        continue
                    _idx = _idx_for_task(_row.get("task_id")) or _idx_for_ts(
                        self._parse_ts_value(_row.get("ts") or _row.get("timestamp"))
                    )
                    if _idx is not None and 0 <= _idx < len(_phases):
                        _phases[_idx]["preflight_errors"] += 1
                return {"run_id": run_id, "workspace_id": ws_id, "phases": _phases, "source": "phase_plan"}
            except Exception:
                pass

        attempt_task_ids = self._attempt_task_ids_for_run(run_id, run)

        round_io = read_jsonl(drive_root / "logs" / "round_io.jsonl", limit=8000)
        tool_io = read_jsonl(drive_root / "logs" / "tools.jsonl", limit=12000)
        event_io = read_jsonl(drive_root / "logs" / "events.jsonl", limit=8000)

        write_tool_names = {
            "update_workspace_seed",
            "update_workspace_file",
            "update_workspace_from_instance",
            "commit_workspace_changes",
            "create_workspace_file",
            "delete_workspace_file",
        }

        boundaries: list[tuple[float, int]] = []
        for ev in event_io:
            if str(ev.get("type") or "") != "remediation_started":
                continue
            if not self._task_id_matches_run(
                ev.get("task_id"), run_id, attempt_task_ids
            ):
                continue
            ts_val = self._parse_ts_value(ev.get("ts") or ev.get("timestamp"))
            attempt = self._coerce_int(ev.get("attempt"), None, min_value=1)
            if ts_val and attempt is not None:
                boundaries.append((float(ts_val), int(attempt)))
        boundaries.sort()

        # Build phase definitions: initial covers (-inf, first_boundary],
        # remediation_N covers (boundary_N, boundary_{N+1}].
        phases: list[dict[str, Any]] = [
            {
                "name": "initial",
                "label": "Initial pass",
                "started_at_ts": None,
                "ended_at_ts": boundaries[0][0] if boundaries else None,
                "rounds": 0,
                "tool_calls": 0,
                "write_tool_calls": 0,
                "preflight_errors": 0,
            }
        ]
        for idx, (ts_val, attempt_n) in enumerate(boundaries):
            ended_at = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else None
            phases.append(
                {
                    "name": f"remediation_{attempt_n}",
                    "label": f"Remediation {attempt_n}",
                    "started_at_ts": ts_val,
                    "ended_at_ts": ended_at,
                    "rounds": 0,
                    "tool_calls": 0,
                    "write_tool_calls": 0,
                    "preflight_errors": 0,
                }
            )

        def _phase_index_for_ts(ts_val: float | None) -> int:
            """Return index of the phase that owns ``ts_val``."""
            if ts_val is None:
                return 0
            for idx in range(len(phases) - 1, -1, -1):
                start = phases[idx].get("started_at_ts")
                if start is None or ts_val >= float(start):
                    return idx
            return 0

        # Bucket round_io.
        for ev in round_io:
            if not self._task_id_matches_run(
                ev.get("task_id"), run_id, attempt_task_ids
            ):
                continue
            ts_val = self._parse_ts_value(ev.get("ts") or ev.get("timestamp"))
            phase_idx = _phase_index_for_ts(ts_val)
            phase = phases[phase_idx]
            phase["rounds"] += 1
            if ts_val:
                cur_start = phase.get("started_at_ts")
                if cur_start is None or ts_val < float(cur_start):
                    phase["started_at_ts"] = ts_val
                cur_end = phase.get("ended_at_ts")
                if cur_end is None or ts_val > float(cur_end):
                    phase["ended_at_ts"] = ts_val

        # Bucket tool_io.
        for ev in tool_io:
            if not self._task_id_matches_run(
                ev.get("task_id"), run_id, attempt_task_ids
            ):
                continue
            ts_val = self._parse_ts_value(ev.get("ts") or ev.get("timestamp"))
            phase_idx = _phase_index_for_ts(ts_val)
            phases[phase_idx]["tool_calls"] += 1
            if str(ev.get("tool") or "") in write_tool_names:
                phases[phase_idx]["write_tool_calls"] += 1

        # Bucket preflight errors from events.
        for ev in event_io:
            if str(ev.get("type") or "") != "tool_preflight_error":
                continue
            if not self._task_id_matches_run(
                ev.get("task_id"), run_id, attempt_task_ids
            ):
                continue
            ts_val = self._parse_ts_value(ev.get("ts") or ev.get("timestamp"))
            phase_idx = _phase_index_for_ts(ts_val)
            phases[phase_idx]["preflight_errors"] += 1

        for phase in phases:
            start = phase.pop("started_at_ts", None)
            end = phase.pop("ended_at_ts", None)
            phase["started_at"] = iso_utc(start) if start else None
            phase["ended_at"] = iso_utc(end) if end else None
            if start and end:
                phase["duration_ms"] = max(0, int((float(end) - float(start)) * 1000))
            else:
                phase["duration_ms"] = 0

        # Stamp the LAST phase with the final verification result, if any.
        full_result = (
            run.get("full_result") if isinstance(run.get("full_result"), dict) else {}
        )
        verification = full_result.get("verification_report") if full_result else None
        if isinstance(verification, dict) and phases:
            if verification.get("skipped"):
                phases[-1]["verification_status"] = "skipped"
            elif verification.get("passed"):
                phases[-1]["verification_status"] = "passed"
            else:
                phases[-1]["verification_status"] = "failed"

        return {
            "run_id": run_id,
            "workspace_id": ws_id,
            "phases": phases,
        }

    def cancel_run(
        self,
        run_id: str,
        *,
        wait_seconds: float = 15.0,
        force_after_seconds: float | None = 5.0,
    ) -> dict[str, Any]:
        """Cancel a running web bridge run.

        Two phases:

        1. Cooperative: write ``stop_requested.json`` files (which the
           Ouroboros loop polls between rounds), call
           ``HarnessOrchestrator.cancel()`` when one is registered for
           this run, then ``Thread.join`` for ``wait_seconds`` so the
           worker can drain the current LLM/tool turn.
        2. Forced (best-effort in pure-Python): if the worker is still
           alive after ``wait_seconds + force_after_seconds``, mark the
           run as ``cancelled`` in the store immediately and detach the
           thread. Python threads cannot be safely killed without a
           process-group rewrite, so this guarantees the UI sees a
           cancelled state at once while the abandoned thread drains in
           the background.
        """
        web_run = self._get_web_run(run_id) or {}
        resolved_run = self.get_run(run_id) or {}
        run = {**resolved_run, **web_run}
        if not run.get("workspace_id") and resolved_run.get("workspace_id"):
            run["workspace_id"] = resolved_run["workspace_id"]
        ws_id = str(run.get("workspace_id") or "")
        attempt_task_ids = sorted(self._attempt_task_ids_for_run(run_id, run))
        active_task_id = self._attempt_task_id(
            run_id,
            self._coerce_int(run.get("attempt"), 1, min_value=1) or 1,
        )
        # If this is a harness parent, fan the cancel out to each child
        # candidate run so their stop-request files exist too.
        harness_meta = (
            run.get("harness_meta") if isinstance(run.get("harness_meta"), dict) else {}
        )
        candidate_run_ids = [
            str(item)
            for item in (harness_meta.get("candidate_run_ids") or [])
            if str(item or "").strip()
        ]
        for child in candidate_run_ids:
            if child and child != run_id:
                attempt_task_ids.append(child)
        payload = {
            "run_id": run_id,
            "task_id": active_task_id,
            "attempt_task_ids": sorted(set(attempt_task_ids)),
            "candidate_run_ids": candidate_run_ids,
            "reason": "stop requested from the web UI",
            "ts": now_ts(),
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        for path in self._stop_request_paths(ws_id):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(encoded, encoding="utf-8")
            except OSError:
                continue
        self._upsert_web_run(
            run_id,
            {
                "stop_requested": True,
                "stop_requested_at": iso_utc(now_ts()),
                "result_preview": "Stop requested from the web UI; waiting for the agent to finish the current step.",
                "updated_at": iso_utc(now_ts()),
            },
        )

        # Phase 1a: notify the harness orchestrator (if any) so its
        # internal cancel-event flips immediately. Without this the
        # orchestrator only checks for stop on stage boundaries.
        with self._run_lock:
            worker_entry = dict(self._workers.get(run_id) or {})
        orchestrator = worker_entry.get("orchestrator")
        if orchestrator is not None:
            try:
                orchestrator.cancel()
            except Exception:
                log.debug("Failed to cancel orchestrator for %s", run_id, exc_info=True)

        # Phase 1b: cooperative wait.
        worker = self._run_threads.get(run_id)
        cooperative = float(wait_seconds or 0.0)
        if worker is not None and worker.is_alive() and cooperative > 0:
            worker.join(timeout=cooperative)

        # Phase 2: forced detach. Python threads cannot be killed
        # safely; what we *can* do is flip the user-visible state right
        # away and stop pretending the run is still running. The
        # abandoned thread will eventually finish cooperatively (the
        # stop_requested files are still on disk) and clean up its own
        # entry in ``_workers`` / ``_run_threads`` via its ``finally``.
        stop_method = "cooperative"
        if worker is not None and worker.is_alive():
            grace = float(force_after_seconds or 0.0)
            if grace > 0:
                worker.join(timeout=grace)
            if worker.is_alive():
                stop_method = "forced_detach"
                self._upsert_web_run(
                    run_id,
                    {
                        "status": "cancelled",
                        "stop_method": stop_method,
                        "result_preview": (
                            "Run cancelled from the web UI; the worker thread "
                            "did not yield within the grace period and was "
                            "detached. It will drain in the background."
                        ),
                        "updated_at": iso_utc(now_ts()),
                        "finished_at": iso_utc(now_ts()),
                    },
                )

        final_run = self._get_web_run(run_id) or {}
        if worker is None or not worker.is_alive():
            if final_run.get("status") in {"queued", "running"}:
                self._upsert_web_run(
                    run_id,
                    {
                        "status": "cancelled",
                        "stop_method": stop_method,
                        "result_preview": "Run cancelled from the web UI.",
                        "updated_at": iso_utc(now_ts()),
                        "finished_at": iso_utc(now_ts()),
                    },
                )
                final_run = self._get_web_run(run_id) or {}
        return {
            "ok": True,
            "run_id": run_id,
            "status": final_run.get("status") or "cancelled",
            "stop_requested": True,
            "stop_method": stop_method,
            "worker_alive": bool(worker is not None and worker.is_alive()),
        }

    def list_logs(
        self,
        ws_id: str | None,
        severity: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not ws_id:
            return {"logs": [], "total": 0}
        events = self._events(ws_id, limit=limit * 8)
        tool_events = read_jsonl(
            self.workspaces_root / ws_id / ".memory" / "drive" / "logs" / "tools.jsonl",
            limit=limit * 8,
        )
        logs: list[dict[str, Any]] = []
        for idx, ev in enumerate(events):
            ev_type = str(ev.get("type") or "info")
            if "error" in ev_type or ev.get("error"):
                sev = "error"
            elif "warning" in ev_type or "warn" in ev_type:
                sev = "warn"
            elif "debug" in ev_type:
                sev = "debug"
            else:
                sev = "info"
            ts = ev.get("ts") or ev.get("timestamp") or iso_utc(now_ts())
            pieces = [ev_type]
            for key in ("reason", "message", "msg", "summary", "status", "phase"):
                if ev.get(key):
                    pieces.append(f"{key}={ev[key]}")
            if ev.get("error"):
                pieces.append(f"error={ev['error']}")
            if ev.get("tool_name"):
                pieces.append(f"tool={ev['tool_name']}")
            elif ev.get("tool"):
                pieces.append(f"tool={ev['tool']}")
            message = " | ".join(str(p) for p in pieces if p)
            if severity and severity != "all" and sev != severity:
                continue
            if query and query.lower() not in message.lower():
                continue
            logs.append(
                {
                    "id": f"log-{idx}",
                    "workspace_id": ws_id,
                    "severity": sev,
                    "message": message,
                    "timestamp": ts,
                    "created_at": ts,
                    "type": ev_type,
                    "task_id": ev.get("task_id"),
                }
            )
        for idx, ev in enumerate(tool_events):
            tool_name = ev.get("tool") or ev.get("tool_name") or "tool"
            ts = ev.get("ts") or ev.get("timestamp") or iso_utc(now_ts())
            message = " | ".join(
                str(p)
                for p in (
                    "tool_call",
                    f"tool={tool_name}",
                    f"task_id={ev.get('task_id')}" if ev.get("task_id") else "",
                    short_text(
                        str(ev.get("result_preview") or ev.get("result") or ""), 500
                    ),
                )
                if p
            )
            if severity and severity != "all" and severity != "info":
                continue
            if query and query.lower() not in message.lower():
                continue
            logs.append(
                {
                    "id": f"tool-log-{idx}",
                    "workspace_id": ws_id,
                    "severity": "info",
                    "message": message,
                    "timestamp": ts,
                    "created_at": ts,
                    "type": "tool_call",
                    "task_id": ev.get("task_id"),
                }
            )
        terminal_path = (
            self.workspaces_root
            / ws_id
            / ".memory"
            / "drive"
            / "memory"
            / "terminal_scrollback.md"
        )
        if terminal_path.exists():
            try:
                terminal_text = terminal_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                terminal_mtime = terminal_path.stat().st_mtime
            except OSError:
                terminal_text = ""
                terminal_mtime = now_ts()
            if terminal_text.strip():
                message = "terminal_scrollback | " + short_text(
                    terminal_text[-1500:], 1500
                )
                if (not severity or severity in {"all", "info"}) and (
                    not query or query.lower() in message.lower()
                ):
                    logs.append(
                        {
                            "id": "terminal-scrollback",
                            "workspace_id": ws_id,
                            "severity": "info",
                            "message": message,
                            "timestamp": iso_utc(terminal_mtime),
                            "created_at": iso_utc(terminal_mtime),
                            "type": "terminal_scrollback",
                            "task_id": None,
                            "path": str(terminal_path),
                        }
                    )
        total = len(logs)
        logs.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        logs = logs[:limit]
        return {"logs": logs, "total": total, "limit": limit}

    def list_memory_nodes(
        self, ws_id: str | None, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not ws_id:
            return []
        selected_run_id = str(run_id or "").strip()
        if selected_run_id.lower() in {"all", "__all__", "workspace"}:
            selected_run_id = ""
        selected_attempt_task_ids = (
            self._attempt_task_ids_for_run(selected_run_id)
            if selected_run_id
            else set()
        )
        memory_root = self.workspaces_root / ws_id / ".memory"
        rows = read_jsonl(memory_root / "lessons.jsonl")
        ideas = read_jsonl(memory_root / "ideas.jsonl")
        task_results = self._list_task_results(ws_id)
        nodes_by_id: dict[str, dict[str, Any]] = {}

        def safe_id(prefix: str, value: Any) -> str:
            raw = str(value or uuid.uuid4().hex)
            cleaned = "".join(ch if ch.isalnum() or ch in "-_:" else "_" for ch in raw)
            return f"{prefix}:{cleaned[:140]}"

        def rel_path(path: Path) -> str:
            try:
                return str(path.resolve().relative_to(self.repo_root)).replace(
                    "\\", "/"
                )
            except Exception:
                return str(path).replace("\\", "/")

        def ts_iso(value: Any) -> str:
            try:
                if isinstance(value, (int, float)):
                    return iso_utc(float(value))
                if isinstance(value, str) and value:
                    return value
            except Exception:
                pass
            return iso_utc(now_ts())

        def add_node(node: dict[str, Any]) -> str:
            node_id = str(node.get("id") or safe_id("memory", node.get("title")))
            node["id"] = node_id
            node_type = str(node.get("node_type") or node.get("type") or "concept")
            node["type"] = node_type
            node["node_type"] = node_type
            node.setdefault("title", node.get("label") or node_id)
            node["label"] = short_text(
                str(node.get("label") or node.get("title") or node_id), 140
            )
            node["content"] = str(
                node.get("content") or node.get("summary") or node.get("details") or ""
            )
            node.setdefault("reference_count", 0)
            node.setdefault("workspace_id", ws_id)
            node.setdefault("connections", [])
            node.setdefault("edges", node.get("connections") or [])
            node.setdefault("tags", [])
            node.setdefault("priority", 5)
            node.setdefault("created_at", iso_utc(now_ts()))
            node.setdefault("updated_at", node.get("created_at"))
            existing = nodes_by_id.get(node_id)
            if existing:
                merged_connections = list(
                    dict.fromkeys(
                        (existing.get("connections") or [])
                        + (node.get("connections") or [])
                    )
                )
                merged_tags = list(
                    dict.fromkeys(
                        (existing.get("tags") or []) + (node.get("tags") or [])
                    )
                )
                existing.update(
                    {k: v for k, v in node.items() if v not in (None, "", [])}
                )
                existing["connections"] = merged_connections
                existing["edges"] = merged_connections
                existing["tags"] = merged_tags
            else:
                nodes_by_id[node_id] = node
            return node_id

        def connect(source_id: str | None, target_id: str | None) -> None:
            if not source_id or not target_id or source_id == target_id:
                return
            if source_id not in nodes_by_id or target_id not in nodes_by_id:
                return
            connections = list(nodes_by_id[source_id].get("connections") or [])
            if target_id not in connections:
                connections.append(target_id)
            nodes_by_id[source_id]["connections"] = connections
            nodes_by_id[source_id]["edges"] = connections

        def read_text(path: Path) -> str | None:
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None

        def add_source_node(
            node_id: str,
            title: str,
            summary: str,
            *,
            parent_id: str | None = None,
            source: str,
            path: Path | None = None,
            tags: list[str] | None = None,
        ) -> str:
            created = add_node(
                {
                    "id": node_id,
                    "type": "source",
                    "title": title,
                    "summary": summary,
                    "details": summary,
                    "source": source,
                    "scope": "workspace" if "workspace" in source else "system",
                    "path": rel_path(path) if path else "",
                    "tags": ["source"] + (tags or []),
                    "priority": 8,
                    "created_at": iso_utc(now_ts()),
                    "updated_at": iso_utc(now_ts()),
                    "connections": [],
                }
            )
            connect(parent_id, created)
            return created

        root_id = add_source_node(
            safe_id("source", f"context:{ws_id}"),
            f"Context graph: {ws_id}",
            "All context sources visible to the web UI for this workspace.",
            source="context graph",
            tags=["root", "context"],
        )
        workspace_source_id = add_source_node(
            safe_id("source", f"workspace-memory:{ws_id}"),
            "Workspace .memory",
            "Workspace-scoped lessons, ideas, drive memory and prompt overrides.",
            parent_id=root_id,
            source="workspace .memory",
            path=memory_root,
            tags=["workspace-memory"],
        )
        umbrella_source_id = add_source_node(
            safe_id("source", f"umbrella-system:{ws_id}"),
            ".umbrella system memory",
            "System-level run results, competency signals, gaps and web run artifacts.",
            parent_id=root_id,
            source=".umbrella",
            path=self.repo_root / ".umbrella",
            tags=["system-memory"],
        )
        runs_source_id = add_source_node(
            safe_id("source", f"runs:{ws_id}"),
            "Runs and verification",
            "Recent task results and verification reports linked back to run/task nodes.",
            parent_id=umbrella_source_id,
            source=".umbrella task results",
            path=self.repo_root / ".umbrella",
            tags=["runs", "verification"],
        )
        logs_source_id = add_source_node(
            safe_id("source", f"logs:{ws_id}"),
            "Logs and tool events",
            "Recent runtime log lines and tool-call events surfaced for this workspace.",
            parent_id=umbrella_source_id,
            source="logs",
            path=memory_root / "runtime",
            tags=["logs", "tools"],
        )
        prompt_source_id = add_source_node(
            safe_id("source", f"prompt-stack:{ws_id}"),
            "Prompt stack",
            "Prompt files, workspace prompt overlays and run-time prompt snapshots.",
            parent_id=root_id,
            source="prompt stack",
            path=self.repo_root,
            tags=["prompt"],
        )
        gmas_source_id = add_source_node(
            safe_id("source", f"gmas-context:{ws_id}"),
            "GMAS context",
            "GMAS authoring prompt and selected GMAS documentation used by retrieval/context tools.",
            parent_id=prompt_source_id,
            source="gmas",
            path=self.repo_root / "gmas",
            tags=["gmas", "context"],
        )
        ideas_source_id = add_source_node(
            safe_id("source", f"ideas:{ws_id}"),
            "Idea memory",
            "Entries from workspace ideas.jsonl and memory palace paths.",
            parent_id=workspace_source_id,
            source="workspace .memory ideas",
            path=memory_root / "ideas.jsonl",
            tags=["ideas"],
        )
        lessons_source_id = add_source_node(
            safe_id("source", f"lessons:{ws_id}"),
            "Lessons memory",
            "Learned lessons from previous workspace runs.",
            parent_id=workspace_source_id,
            source="workspace .memory lessons",
            path=memory_root / "lessons.jsonl",
            tags=["lessons"],
        )
        knowledge_source_id = add_source_node(
            safe_id("source", f"knowledge:{ws_id}"),
            "Drive knowledge",
            "Markdown knowledge files under workspace drive memory.",
            parent_id=workspace_source_id,
            source="workspace drive memory",
            path=memory_root / "drive" / "memory",
            tags=["drive", "knowledge"],
        )
        scratchpad_source_id = add_source_node(
            safe_id("source", f"scratchpad:{ws_id}"),
            "Scratchpad memory",
            "Scratchpad and scratchpad journal from workspace drive memory.",
            parent_id=workspace_source_id,
            source="workspace scratchpad",
            path=memory_root / "drive" / "memory",
            tags=["scratchpad"],
        )
        palace_source_id = add_source_node(
            safe_id("source", f"memory-palace:{ws_id}"),
            "Memory palace",
            "Hierarchical memory paths plus the local vector palace store.",
            parent_id=workspace_source_id,
            source="workspace memory palace",
            path=memory_root / "palace",
            tags=["palace", "hierarchy"],
        )

        if not memory_root.exists():
            missing_id = add_node(
                {
                    "id": safe_id("empty-source", f"missing-memory:{ws_id}"),
                    "type": "empty_source",
                    "title": "Workspace .memory missing",
                    "summary": (
                        "No workspace .memory directory was found. Remaining graph nodes come "
                        "from system artifacts, logs, prompt files or run snapshots."
                    ),
                    "details": "",
                    "source": "workspace .memory",
                    "scope": "workspace",
                    "path": rel_path(memory_root),
                    "tags": ["workspace-memory", "missing"],
                    "created_at": iso_utc(now_ts()),
                    "updated_at": iso_utc(now_ts()),
                    "connections": [],
                }
            )
            connect(workspace_source_id, missing_id)

        def add_file_node(
            path: Path,
            parent_id: str | None,
            *,
            node_type: str,
            title: str | None = None,
            source: str,
            scope: str = "repository",
            tags: list[str] | None = None,
            summary_limit: int = 700,
        ) -> str | None:
            if not path.exists() or not path.is_file():
                return None
            content = read_text(path)
            if not content or not content.strip():
                return None
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = now_ts()
            node_id = add_node(
                {
                    "id": safe_id(node_type, rel_path(path)),
                    "type": node_type,
                    "title": title or path.name,
                    "summary": short_text(content.strip(), summary_limit),
                    "details": content,
                    "source": source,
                    "scope": scope,
                    "path": rel_path(path),
                    "tags": tags or [],
                    "created_at": ts_iso(mtime),
                    "updated_at": ts_iso(mtime),
                    "connections": [],
                }
            )
            connect(parent_id, node_id)
            return node_id

        def add_directory_block_node(
            path: Path,
            parent_id: str | None,
            *,
            node_type: str,
            title: str,
            source: str,
            scope: str,
            tags: list[str] | None = None,
            patterns: tuple[str, ...] = ("*",),
            max_entries: int = 10,
        ) -> str | None:
            if not path.exists() or not path.is_dir():
                return None
            files: list[Path] = []
            for pattern in patterns:
                files.extend(p for p in path.glob(pattern) if p.is_file())
            files = sorted(
                set(files),
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            dirs = [p for p in path.iterdir() if p.is_dir()]
            if not files and not dirs:
                return None
            try:
                mtime = max(
                    (p.stat().st_mtime for p in [*files[:20], *dirs[:20]]),
                    default=path.stat().st_mtime,
                )
            except OSError:
                mtime = now_ts()
            sample = ", ".join(p.name for p in files[:max_entries])
            if dirs and len(files) < max_entries:
                remaining = max_entries - len(files)
                sample = ", ".join(
                    filter(
                        None,
                        [sample, ", ".join(p.name + "/" for p in dirs[:remaining])],
                    )
                )
            summary = f"{len(files)} files"
            if dirs:
                summary += f", {len(dirs)} directories"
            if sample:
                summary += f": {sample}"
            node_id = add_node(
                {
                    "id": safe_id(node_type, rel_path(path)),
                    "type": node_type,
                    "title": title,
                    "summary": summary,
                    "details": summary,
                    "source": source,
                    "scope": scope,
                    "path": rel_path(path),
                    "tags": tags or [],
                    "created_at": ts_iso(mtime),
                    "updated_at": ts_iso(mtime),
                    "connections": [],
                }
            )
            connect(parent_id, node_id)
            return node_id

        def add_jsonl_block_node(
            path: Path,
            parent_id: str | None,
            *,
            title: str,
            source: str,
            scope: str,
            tags: list[str] | None = None,
        ) -> str | None:
            if not path.exists() or not path.is_file():
                return None
            rows_for_file = read_jsonl(path)
            if not rows_for_file:
                return None
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = now_ts()
            task_ids = sorted(
                {
                    str(row.get("task_id") or row.get("run_id") or "")
                    for row in rows_for_file
                    if str(row.get("task_id") or row.get("run_id") or "").strip()
                }
            )
            latest = rows_for_file[-1]
            summary = (
                f"{len(rows_for_file)} rows"
                + (f" across {len(task_ids)} tasks" if task_ids else "")
                + f". Latest: {short_text(json.dumps(latest, ensure_ascii=False, default=str), 220)}"
            )
            node_id = add_node(
                {
                    "id": safe_id("log-block", rel_path(path)),
                    "type": "log_block",
                    "title": title,
                    "summary": summary,
                    "details": "\n".join(
                        json.dumps(row, ensure_ascii=False, default=str)
                        for row in rows_for_file[-30:]
                    ),
                    "source": source,
                    "scope": scope,
                    "path": rel_path(path),
                    "tags": ["log-block"] + (tags or []),
                    "created_at": ts_iso(mtime),
                    "updated_at": ts_iso(mtime),
                    "connections": [],
                }
            )
            connect(parent_id, node_id)
            return node_id

        def markdown_section(text: str, heading: str) -> str:
            lines = text.splitlines()
            start_index: int | None = None
            heading_norm = heading.strip().lower()
            heading_level = len(heading) - len(heading.lstrip("#"))
            for idx, line in enumerate(lines):
                if line.strip().lower() == heading_norm:
                    start_index = idx + 1
                    break
            if start_index is None:
                return ""
            section_lines: list[str] = []
            for line in lines[start_index:]:
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    level = len(stripped) - len(stripped.lstrip("#"))
                    if level <= heading_level:
                        break
                section_lines.append(line)
            return "\n".join(section_lines).strip()

        def add_prompt_blocks(
            path: Path,
            parent_id: str | None,
            *,
            source: str,
            scope: str,
            max_blocks: int = 6,
        ) -> None:
            content = read_text(path)
            if not content:
                return
            lines = content.splitlines()
            headings: list[tuple[int, int, str]] = []
            for idx, line in enumerate(lines):
                stripped = line.strip()
                if not stripped.startswith("#"):
                    continue
                level = len(stripped) - len(stripped.lstrip("#"))
                if level > 2:
                    continue
                title = stripped.lstrip("#").strip()
                if title:
                    headings.append((idx, level, title))
            for idx, (line_index, level, title) in enumerate(headings[:max_blocks]):
                end_index = len(lines)
                for next_index, next_level, _next_title in headings[idx + 1 :]:
                    if next_level <= level:
                        end_index = next_index
                        break
                body = "\n".join(lines[line_index + 1 : end_index]).strip()
                if not body:
                    continue
                block_id = add_node(
                    {
                        "id": safe_id("prompt-block", f"{rel_path(path)}:{title}"),
                        "type": "prompt_block",
                        "title": title,
                        "summary": short_text(body, 450),
                        "details": body,
                        "source": source,
                        "scope": scope,
                        "path": f"{rel_path(path)}#{title.lower().replace(' ', '-')[:80]}",
                        "tags": ["prompt", "block"],
                        "created_at": iso_utc(now_ts()),
                        "updated_at": iso_utc(now_ts()),
                        "connections": [],
                    }
                )
                connect(parent_id, block_id)

        def add_task_node(task_id: Any, *, title: str | None = None) -> str | None:
            if not task_id:
                return None
            task_id_str = str(task_id)
            node_id = add_node(
                {
                    "id": safe_id("task", task_id_str),
                    "type": "task",
                    "title": title or f"Run {task_id_str}",
                    "summary": f"Ouroboros run/task memory for {task_id_str}.",
                    "details": "",
                    "task_id": task_id_str,
                    "source": ".umbrella task results",
                    "scope": "workspace",
                    "tags": ["task"],
                    "created_at": iso_utc(now_ts()),
                    "updated_at": iso_utc(now_ts()),
                }
            )
            connect(runs_source_id, node_id)
            return node_id

        workspace_log_blocks = {
            "events": add_jsonl_block_node(
                memory_root / "drive" / "logs" / "events.jsonl",
                logs_source_id,
                title="Workspace events.jsonl",
                source="workspace runtime logs",
                scope="workspace",
                tags=["events"],
            ),
            "round_io": add_jsonl_block_node(
                memory_root / "drive" / "logs" / "round_io.jsonl",
                logs_source_id,
                title="Workspace round_io.jsonl",
                source="workspace runtime logs",
                scope="workspace",
                tags=["round_io", "llm"],
            ),
            "tools": add_jsonl_block_node(
                memory_root / "drive" / "logs" / "tools.jsonl",
                logs_source_id,
                title="Workspace tools.jsonl",
                source="workspace runtime logs",
                scope="workspace",
                tags=["tools"],
            ),
        }
        system_log_blocks = {
            "events": add_jsonl_block_node(
                self.repo_root
                / ".umbrella"
                / "ouroboros_drive"
                / "logs"
                / "events.jsonl",
                logs_source_id,
                title=".umbrella events.jsonl",
                source=".umbrella runtime logs",
                scope="system",
                tags=["events", "system"],
            ),
            "round_io": add_jsonl_block_node(
                self.repo_root
                / ".umbrella"
                / "ouroboros_drive"
                / "logs"
                / "round_io.jsonl",
                logs_source_id,
                title=".umbrella round_io.jsonl",
                source=".umbrella runtime logs",
                scope="system",
                tags=["round_io", "system"],
            ),
            "tools": add_jsonl_block_node(
                self.repo_root
                / ".umbrella"
                / "ouroboros_drive"
                / "logs"
                / "tools.jsonl",
                logs_source_id,
                title=".umbrella tools.jsonl",
                source=".umbrella runtime logs",
                scope="system",
                tags=["tools", "system"],
            ),
        }
        workspace_task_results_block_id = add_directory_block_node(
            memory_root / "drive" / "task_results",
            runs_source_id,
            node_type="artifact_block",
            title="Workspace task results",
            source="workspace runtime artifacts",
            scope="workspace",
            tags=["task-results", "artifacts"],
            patterns=("*.json", "*.md"),
        )
        workspace_task_plans_block_id = add_directory_block_node(
            memory_root / "drive" / "task_plans",
            runs_source_id,
            node_type="artifact_block",
            title="Workspace task plans",
            source="workspace runtime artifacts",
            scope="workspace",
            tags=["task-plans", "artifacts"],
            patterns=("*.json",),
        )
        system_task_results_block_id = add_directory_block_node(
            self.repo_root / ".umbrella" / "ouroboros_drive" / "task_results",
            runs_source_id,
            node_type="artifact_block",
            title=".umbrella task results",
            source=".umbrella runtime artifacts",
            scope="system",
            tags=["task-results", "artifacts", "system"],
            patterns=("*.json", "*.md"),
        )
        add_directory_block_node(
            self.repo_root
            / ".umbrella"
            / "meta_harness"
            / "experiments"
            / "_default"
            / "candidates",
            umbrella_source_id,
            node_type="artifact_block",
            title="Meta-harness candidates",
            source=".umbrella meta_harness",
            scope="system",
            tags=["meta-harness", "candidates", "artifacts"],
        )
        add_directory_block_node(
            memory_root / "palace",
            palace_source_id,
            node_type="memory_path",
            title="Vector palace store",
            source="workspace memory palace",
            scope="workspace",
            tags=["palace", "vector-store"],
        )

        static_prompt_specs = [
            (
                self.repo_root / "ouroboros" / "BIBLE.md",
                "Ouroboros BIBLE.md",
                "prompt",
                "ouroboros prompt",
                ["prompt", "bible", "ouroboros"],
            ),
            (
                self.repo_root / "ouroboros" / "prompts" / "SYSTEM.md",
                "Ouroboros SYSTEM.md",
                "prompt",
                "ouroboros prompt",
                ["prompt", "system", "ouroboros"],
            ),
            (
                self.repo_root / "ouroboros" / "prompts" / "CONSCIOUSNESS.md",
                "Ouroboros CONSCIOUSNESS.md",
                "prompt",
                "ouroboros prompt",
                ["prompt", "consciousness", "ouroboros"],
            ),
            (
                self.repo_root / "umbrella" / "prompts" / "ouroboros_workspace_task.md",
                "Umbrella workspace task prompt",
                "prompt",
                "umbrella prompt",
                ["prompt", "task"],
            ),
            (
                self.repo_root / "umbrella" / "prompts" / "gmas_agent_authoring.md",
                "GMAS agent authoring prompt",
                "gmas_context",
                "gmas",
                ["prompt", "gmas"],
            ),
            (
                self.repo_root / "umbrella" / "prompts" / "polymarket_e2e_task.md",
                "Polymarket E2E task prompt",
                "prompt",
                "umbrella prompt",
                ["prompt", "task"],
            ),
        ]
        for path, title, node_type, source, tags in static_prompt_specs:
            parent_id = gmas_source_id if "gmas" in tags else prompt_source_id
            file_node_id = add_file_node(
                path,
                parent_id,
                node_type=node_type,
                title=title,
                source=source,
                tags=tags,
            )
            if file_node_id and path.name in {
                "BIBLE.md",
                "SYSTEM.md",
                "CONSCIOUSNESS.md",
            }:
                add_prompt_blocks(
                    path, file_node_id, source=source, scope="repository", max_blocks=6
                )

        workspace_prompts = memory_root / "prompts"
        for name in ("SYSTEM.md", "BIBLE.md", "CONSCIOUSNESS.md"):
            add_file_node(
                workspace_prompts / name,
                prompt_source_id,
                node_type="workspace_prompt",
                title=f"Workspace prompt override: {name}",
                source="workspace prompt overlay",
                scope="workspace",
                tags=["prompt", "workspace-override"],
            )

        gmas_doc_candidates = [
            self.repo_root / "gmas" / "README.md",
            self.repo_root / "gmas" / "docs" / "README.md",
            self.repo_root / "gmas" / "docs" / "index.md",
            self.repo_root / "gmas" / "docs" / "user-guide" / "key-concepts.md",
            self.repo_root / "gmas" / "docs" / "user-guide" / "memory.md",
            self.repo_root / "gmas" / "docs" / "user-guide" / "tools.md",
            self.repo_root / "gmas" / "docs" / "api" / "tools.md",
            self.repo_root / "gmas" / "docs" / "api" / "execution.md",
        ]
        for path in gmas_doc_candidates:
            add_file_node(
                path,
                gmas_source_id,
                node_type="gmas_context",
                title=f"GMAS docs: {path.stem.replace('-', ' ').title()}",
                source="gmas docs",
                tags=["gmas", "docs"],
                summary_limit=500,
            )

        for row in rows:
            tags = row.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            task_node_id = add_task_node(row.get("task_id"))
            node_id = add_node(
                {
                    "id": row.get("id") or uuid.uuid4().hex,
                    "workspace_id": ws_id,
                    "type": "lesson",
                    "title": short_text(
                        row.get("conclusion") or row.get("change_summary") or "lesson",
                        100,
                    ),
                    "summary": short_text(
                        row.get("conclusion") or row.get("observed_effect") or "", 400
                    ),
                    "details": row.get("evidence_summary") or "",
                    "source": "workspace .memory lessons",
                    "scope": "workspace",
                    "path": rel_path(memory_root / "lessons.jsonl"),
                    "tags": list(tags) + (row.get("repeat_tags") or []),
                    "priority": row.get("priority", 5),
                    "created_at": ts_iso(row.get("created_at")),
                    "updated_at": ts_iso(row.get("created_at")),
                    "task_id": row.get("task_id"),
                    "connections": [],
                }
            )
            connect(lessons_source_id, node_id)
            if task_node_id:
                connect(task_node_id, node_id)
        for row in ideas:
            tags = row.get("tags") or []
            if isinstance(tags, str):
                tags = [tags]
            palace_path = str(row.get("palace_path") or "")
            task_node_id = add_task_node(row.get("task_id"))
            parent_for_idea = ideas_source_id
            palace_segments = [
                segment.strip()
                for segment in re.split(r"\s*(?:/|\\|>|::)\s*", palace_path)
                if segment.strip()
            ]
            palace_accumulator: list[str] = []
            for segment in palace_segments[:8]:
                palace_accumulator.append(segment)
                path_label = " / ".join(palace_accumulator)
                path_node_id = add_node(
                    {
                        "id": safe_id("memory-path", f"{ws_id}:{path_label}"),
                        "type": "memory_path",
                        "title": segment,
                        "summary": f"Memory palace path: {path_label}",
                        "details": palace_path,
                        "source": "workspace .memory ideas",
                        "scope": "workspace",
                        "path": path_label,
                        "tags": ["ideas", "palace", "hierarchy"],
                        "created_at": ts_iso(row.get("created_at")),
                        "updated_at": ts_iso(row.get("created_at")),
                        "connections": [],
                    }
                )
                connect(parent_for_idea, path_node_id)
                connect(palace_source_id, path_node_id)
                parent_for_idea = path_node_id
            node_id = add_node(
                {
                    "id": row.get("id") or uuid.uuid4().hex,
                    "workspace_id": ws_id,
                    "type": row.get("kind") or "memory",
                    "title": short_text(
                        row.get("title") or palace_path or "memory", 120
                    ),
                    "summary": short_text(row.get("content") or "", 500),
                    "details": row.get("content") or "",
                    "source": "workspace .memory ideas",
                    "scope": "workspace",
                    "path": rel_path(memory_root / "ideas.jsonl"),
                    "tags": list(tags),
                    "priority": 5,
                    "created_at": ts_iso(row.get("created_at")),
                    "updated_at": ts_iso(row.get("created_at")),
                    "task_id": row.get("task_id"),
                    "palace_path": palace_path,
                    "connections": [],
                }
            )
            connect(parent_for_idea, node_id)
            if task_node_id:
                connect(task_node_id, node_id)

        drive_memory = memory_root / "drive" / "memory"
        for path in (
            sorted((drive_memory / "knowledge").glob("*.md"))
            if (drive_memory / "knowledge").exists()
            else []
        ):
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            add_node(
                {
                    "id": safe_id("knowledge", path.stem),
                    "type": "knowledge",
                    "title": path.stem.replace("_", " ").title(),
                    "summary": short_text(content.strip(), 500),
                    "details": content,
                    "source": "workspace drive memory",
                    "scope": "workspace",
                    "path": rel_path(path),
                    "tags": ["drive", "knowledge"],
                    "created_at": ts_iso(path.stat().st_mtime),
                    "updated_at": ts_iso(path.stat().st_mtime),
                    "connections": [],
                }
            )
            connect(knowledge_source_id, safe_id("knowledge", path.stem))
        for name in ("scratchpad.md", "scratchpad_journal.jsonl"):
            path = drive_memory / name
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not content.strip():
                continue
            add_node(
                {
                    "id": safe_id("scratchpad", name),
                    "type": "scratchpad",
                    "title": name,
                    "summary": short_text(content.strip(), 500),
                    "details": content,
                    "source": "workspace scratchpad",
                    "scope": "workspace",
                    "path": rel_path(path),
                    "tags": ["drive", "scratchpad"],
                    "created_at": ts_iso(path.stat().st_mtime),
                    "updated_at": ts_iso(path.stat().st_mtime),
                    "connections": [],
                }
            )
            connect(scratchpad_source_id, safe_id("scratchpad", name))

        for raw in task_results[:30]:
            task_id = str(raw.get("task_id") or raw.get("id") or "")
            task_node_id = add_task_node(task_id)
            if task_node_id:
                connect(workspace_task_plans_block_id, task_node_id)
            verification = (
                raw.get("verification_report")
                if isinstance(raw.get("verification_report"), dict)
                else None
            )
            title_status = raw.get("status") or "result"
            summary = str(
                raw.get("final_message") or raw.get("result") or raw.get("error") or ""
            )
            if verification and verification.get("summary"):
                summary = str(verification.get("summary"))
            result_node_id = add_node(
                {
                    "id": safe_id("result", task_id or raw.get("_path")),
                    "type": "verification" if verification else "run_result",
                    "title": short_text(
                        f"{title_status}: {task_id or 'run result'}", 120
                    ),
                    "summary": short_text(summary, 700),
                    "details": json.dumps(
                        raw, ensure_ascii=False, indent=2, default=str
                    ),
                    "source": ".umbrella task results",
                    "scope": "workspace",
                    "path": rel_path(Path(str(raw.get("_path") or "")))
                    if raw.get("_path")
                    else "",
                    "tags": ["run", str(title_status)],
                    "task_id": task_id,
                    "created_at": ts_iso(raw.get("ts") or raw.get("_mtime")),
                    "updated_at": ts_iso(raw.get("ts") or raw.get("_mtime")),
                    "connections": [],
                }
            )
            if task_node_id:
                connect(task_node_id, result_node_id)
            connect(workspace_task_results_block_id, result_node_id)
            connect(system_task_results_block_id, result_node_id)

        seen_snapshot_dirs: set[str] = set()
        for raw in task_results[:8]:
            candidate_manifest_path = raw.get("candidate_manifest_path")
            if not candidate_manifest_path:
                continue
            manifest_path = Path(str(candidate_manifest_path))
            if not manifest_path.is_absolute():
                manifest_path = self.repo_root / manifest_path
            snapshot_dir = manifest_path.parent / "prompt_snapshot"
            if not snapshot_dir.exists() or not snapshot_dir.is_dir():
                continue
            snapshot_key = rel_path(snapshot_dir)
            if snapshot_key in seen_snapshot_dirs:
                continue
            seen_snapshot_dirs.add(snapshot_key)
            task_id = str(
                raw.get("task_id") or raw.get("id") or snapshot_dir.parent.name
            )
            task_node_id = add_task_node(task_id)
            snapshot_parent_id = add_node(
                {
                    "id": safe_id("prompt-snapshot", snapshot_key),
                    "type": "prompt_snapshot",
                    "title": f"Prompt snapshot: {short_text(task_id, 42)}",
                    "summary": "Rendered prompt files captured for this candidate/run.",
                    "details": snapshot_key,
                    "source": "prompt snapshot",
                    "scope": "run",
                    "path": snapshot_key,
                    "tags": ["prompt", "snapshot"],
                    "created_at": ts_iso(raw.get("ts") or raw.get("_mtime")),
                    "updated_at": ts_iso(raw.get("ts") or raw.get("_mtime")),
                    "task_id": task_id,
                    "connections": [],
                }
            )
            connect(prompt_source_id, snapshot_parent_id)
            connect(snapshot_parent_id, task_node_id)

            rendered_path = snapshot_dir / "rendered_task_prompt.md"
            rendered_node_id = add_file_node(
                rendered_path,
                snapshot_parent_id,
                node_type="prompt_snapshot_file",
                title=f"Rendered task prompt: {short_text(task_id, 32)}",
                source="prompt snapshot",
                scope="run",
                tags=["prompt", "rendered", "prior-knowledge"],
                summary_limit=900,
            )
            add_file_node(
                snapshot_dir / "ouroboros_workspace_task.md",
                snapshot_parent_id,
                node_type="prompt_snapshot_file",
                title=f"Workspace task template snapshot: {short_text(task_id, 32)}",
                source="prompt snapshot",
                scope="run",
                tags=["prompt", "template"],
                summary_limit=700,
            )
            rendered_text = read_text(rendered_path) or ""
            for heading, tag in (
                ("## Prior knowledge", "prior-knowledge"),
                ("### Detected skills", "detected-skills"),
            ):
                section = markdown_section(rendered_text, heading)
                if not section:
                    continue
                block_id = add_node(
                    {
                        "id": safe_id("prompt-block", f"{snapshot_key}:{heading}"),
                        "type": "prompt_block",
                        "title": f"{heading.lstrip('#').strip()}: {short_text(task_id, 28)}",
                        "summary": short_text(section, 700),
                        "details": section,
                        "source": "prompt snapshot",
                        "scope": "run",
                        "path": f"{rel_path(rendered_path)}#{tag}",
                        "tags": ["prompt", tag],
                        "created_at": ts_iso(raw.get("ts") or raw.get("_mtime")),
                        "updated_at": ts_iso(raw.get("ts") or raw.get("_mtime")),
                        "task_id": task_id,
                        "connections": [],
                    }
                )
                connect(rendered_node_id or snapshot_parent_id, block_id)
                if "gmas" in section.lower():
                    connect(block_id, gmas_source_id)
            if "gmas" in rendered_text.lower():
                connect(snapshot_parent_id, gmas_source_id)

        for row in read_jsonl(
            memory_root / "drive" / "logs" / "round_io.jsonl", limit=12
        ):
            timestamp = row.get("ts") or row.get("timestamp")
            task_node_id = add_task_node(row.get("task_id"))
            payload_preview = short_text(
                str(
                    row.get("prompt")
                    or row.get("input")
                    or row.get("output")
                    or row.get("response")
                    or row
                ),
                700,
            )
            round_id = add_node(
                {
                    "id": safe_id(
                        "log",
                        f"round:{row.get('task_id')}:{timestamp}:{payload_preview[:40]}",
                    ),
                    "type": "log",
                    "title": short_text(
                        f"Round IO: {row.get('phase') or row.get('type') or 'llm'}", 90
                    ),
                    "summary": payload_preview,
                    "details": json.dumps(
                        row, ensure_ascii=False, indent=2, default=str
                    ),
                    "source": "workspace runtime logs",
                    "scope": "workspace",
                    "path": rel_path(memory_root / "drive" / "logs" / "round_io.jsonl"),
                    "tags": ["log", "round_io", str(row.get("phase") or "")],
                    "created_at": ts_iso(timestamp),
                    "updated_at": ts_iso(timestamp),
                    "task_id": row.get("task_id"),
                    "connections": [],
                }
            )
            connect(workspace_log_blocks.get("round_io"), round_id)
            if task_node_id:
                connect(task_node_id, round_id)

        try:
            recent_logs = self.list_logs(ws_id, limit=20).get("logs", [])
        except Exception:
            recent_logs = []
        for log_row in recent_logs[:20]:
            timestamp = log_row.get("timestamp") or log_row.get("created_at")
            message = str(log_row.get("message") or "")
            log_id = add_node(
                {
                    "id": safe_id("log", f"{timestamp}:{message[:80]}"),
                    "type": "log",
                    "title": short_text(
                        str(log_row.get("type") or log_row.get("severity") or "log"), 80
                    ),
                    "summary": short_text(message, 600),
                    "details": json.dumps(
                        log_row, ensure_ascii=False, indent=2, default=str
                    ),
                    "source": "logs",
                    "scope": "workspace",
                    "path": str(log_row.get("path") or ""),
                    "tags": [
                        "log",
                        str(log_row.get("severity") or ""),
                        str(log_row.get("type") or ""),
                    ],
                    "created_at": ts_iso(timestamp),
                    "updated_at": ts_iso(timestamp),
                    "connections": [],
                }
            )
            source_block_id = (
                workspace_log_blocks.get("tools")
                if str(log_row.get("type") or "") == "tool_call"
                else workspace_log_blocks.get("events")
            )
            connect(source_block_id or logs_source_id, log_id)
            connect(add_task_node(log_row.get("task_id")), log_id)

        for row in read_jsonl(
            self.repo_root / ".umbrella" / "memory" / "signals.jsonl"
        )[-80:]:
            if str(row.get("workspace_id") or "") != ws_id:
                continue
            task_node_id = add_task_node(row.get("task_id"))
            signal_node_id = add_node(
                {
                    "id": row.get("id") or safe_id("signal", row.get("timestamp")),
                    "type": "signal",
                    "title": short_text(
                        str(row.get("category") or "competency signal"), 120
                    ),
                    "summary": short_text(str(row.get("evidence_summary") or ""), 500),
                    "details": json.dumps(
                        row, ensure_ascii=False, indent=2, default=str
                    ),
                    "source": ".umbrella signals",
                    "scope": "system",
                    "path": rel_path(
                        self.repo_root / ".umbrella" / "memory" / "signals.jsonl"
                    ),
                    "tags": ["competency", str(row.get("category") or "")],
                    "priority": 3 if float(row.get("strength") or 0) < 0 else 6,
                    "created_at": ts_iso(row.get("timestamp")),
                    "updated_at": ts_iso(row.get("timestamp")),
                    "task_id": row.get("task_id"),
                    "connections": [],
                }
            )
            connect(umbrella_source_id, signal_node_id)
            if task_node_id:
                connect(task_node_id, signal_node_id)

        for row in read_jsonl(self.repo_root / ".umbrella" / "memory" / "gaps.jsonl")[
            -40:
        ]:
            if not row.get("is_workspace_level"):
                continue
            gap_ws = str(row.get("workspace_id") or "")
            if gap_ws and gap_ws != ws_id:
                continue
            gap_node_id = add_node(
                {
                    "id": row.get("id") or safe_id("gap", row.get("last_seen_at")),
                    "type": "gap",
                    "title": short_text(
                        str(row.get("severity") or "gap") + " capability gap", 120
                    ),
                    "summary": short_text(str(row.get("description") or ""), 600),
                    "details": json.dumps(
                        row, ensure_ascii=False, indent=2, default=str
                    ),
                    "source": ".umbrella gaps",
                    "scope": "system",
                    "path": rel_path(
                        self.repo_root / ".umbrella" / "memory" / "gaps.jsonl"
                    ),
                    "tags": [
                        "gap",
                        str(row.get("severity") or ""),
                        str(row.get("status") or ""),
                    ],
                    "priority": 2,
                    "created_at": ts_iso(row.get("first_seen_at")),
                    "updated_at": ts_iso(row.get("last_seen_at")),
                    "connections": [
                        sid
                        for sid in (row.get("evidence_signals") or [])
                        if sid in nodes_by_id
                    ],
                }
            )
            connect(umbrella_source_id, gap_node_id)

        if selected_run_id:
            included: set[str] = {
                node_id
                for node_id, node in nodes_by_id.items()
                if self._task_id_matches_run(
                    node.get("task_id"), selected_run_id, selected_attempt_task_ids
                )
            }
            # Keep source ancestors so a run-scoped graph still shows where
            # each memory/log/result node came from without showing sibling runs.
            changed = True
            while changed:
                changed = False
                for node_id, node in nodes_by_id.items():
                    if node_id in included:
                        continue
                    connections = {
                        str(target) for target in (node.get("connections") or [])
                    }
                    if connections.intersection(included):
                        included.add(node_id)
                        changed = True
            nodes_by_id = {
                node_id: node
                for node_id, node in nodes_by_id.items()
                if node_id in included
            }
            for node in nodes_by_id.values():
                node["connections"] = [
                    target
                    for target in (node.get("connections") or [])
                    if target in nodes_by_id
                ]
                node["edges"] = node["connections"]

        incoming_counts = {node_id: 0 for node_id in nodes_by_id}
        for node in nodes_by_id.values():
            for target in node.get("connections") or []:
                if target in incoming_counts:
                    incoming_counts[target] += 1
        for node_id, node in nodes_by_id.items():
            node["reference_count"] = int(
                node.get("reference_count") or 0
            ) + incoming_counts.get(node_id, 0)
            node["label"] = short_text(
                str(node.get("label") or node.get("title") or node_id), 140
            )
            node["node_type"] = str(
                node.get("node_type") or node.get("type") or "concept"
            )
            node["content"] = str(
                node.get("content") or node.get("summary") or node.get("details") or ""
            )

        nodes = list(nodes_by_id.values())
        nodes.sort(key=lambda node: str(node.get("updated_at") or ""), reverse=True)

        try:
            import pathlib as _pl
            import sqlite3 as _sq3
            from umbrella.memory.palace.facade import MemPalace
            _repo = _pl.Path(os.environ.get("UMBRELLA_REPO_ROOT", str(self.repo_root)))
            _palace = MemPalace(_repo, ws_id or "")
            _palace_nodes = _palace.list_all(n=300)

            _edge_map: dict[str, list[str]] = {}
            try:
                _g_path = _palace._stores._graph._conn
                _rows = _g_path.execute("SELECT src_id, dst_id FROM edges").fetchall()
                for _s, _d in _rows:
                    _edge_map.setdefault(_s, []).append(_d)
                    _edge_map.setdefault(_d, []).append(_s)
            except Exception:
                pass

            _STORE_NODE_TYPE = {
                "palace.charter": "reference",
                "palace.lesson": "lesson",
                "palace.idea": "concept",
                "palace.codeptr": "knowledge",
                "palace.skill_index": "prompt",
                "palace.run": "run_result",
                "palace.phase": "task",
                "palace.subtask": "subtask_result",
                "palace.transient": "log",
            }

            for _n in _palace_nodes:
                _store = _n.get("store", "palace.idea")
                _nt = _STORE_NODE_TYPE.get(_store, "concept")
                _content = str(_n.get("content") or "")
                _nid = str(_n.get("id") or "")
                _ts = _n.get("created_at")
                _created = iso_utc(float(_ts)) if isinstance(_ts, (int, float)) else iso_utc(now_ts())
                _tags_raw = _n.get("tags") or ""
                _tags = [t for t in str(_tags_raw).split(",") if t]
                _conns = list(dict.fromkeys(_edge_map.get(_nid, [])))
                add_node({
                    "id": _nid,
                    "type": _nt,
                    "node_type": _nt,
                    "label": _content[:80] or _nid[:40],
                    "content": _content,
                    "source": _store,
                    "scope": _n.get("scope") or _n.get("tier") or "",
                    "path": _n.get("source_path") or "",
                    "tags": _tags,
                    "connections": _conns,
                    "edges": _conns,
                    "reference_count": 0,
                    "priority": 5,
                    "workspace_id": ws_id,
                    "verified": bool(_n.get("verified")),
                    "tier": _n.get("tier") or "",
                    "phase": _n.get("phase") or "",
                    "created_at": _created,
                    "updated_at": _created,
                })
        except Exception:
            pass

        return nodes

    def get_memory_node(self, node_id: str) -> dict[str, Any] | None:
        for ws in self.list_workspaces():
            for node in self.list_memory_nodes(ws["id"]):
                if node["id"] == node_id:
                    return node
        return None

    def update_memory_node(
        self, node_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        return self.get_memory_node(node_id)

    def delete_memory_node(
        self, node_id: str, ws_id: str | None = None
    ) -> dict[str, Any]:
        node = self.get_memory_node(node_id)
        target_ws = str(ws_id or "").strip() or (
            str(node.get("workspace_id") or "") if isinstance(node, dict) else ""
        )
        report = wipe_memory_node(
            self.repo_root,
            target_ws or None,
            node_id,
        )
        ok = not report.errors and bool(report.removed_paths)
        reason = ""
        if not report.removed_paths and report.errors:
            reason = report.errors[0]
        elif not report.removed_paths:
            reason = "node_type_not_deletable"
        return {
            "ok": ok,
            "removed": bool(report.removed_paths),
            "node_id": node_id,
            "workspace_id": target_ws,
            "reason": reason,
            "report": report.to_dict(),
        }

    def list_threads(self, ws_id: str | None) -> list[dict[str, Any]]:
        threads = load_store("threads.json", [])
        if ws_id:
            threads = [t for t in threads if t.get("workspace_id") == ws_id]
        threads.sort(key=lambda t: t.get("updated_at", ""), reverse=True)
        return threads

    def create_thread(self, payload: dict[str, Any]) -> dict[str, Any]:
        threads = load_store("threads.json", [])
        ts = iso_utc(now_ts())
        thread = {
            "id": f"thread_{uuid.uuid4().hex[:12]}",
            "workspace_id": payload.get("workspace_id"),
            "title": payload.get("title") or "New thread",
            "created_at": ts,
            "updated_at": ts,
            "message_count": 0,
        }
        threads.insert(0, thread)
        save_store("threads.json", threads)
        return thread

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        for t in load_store("threads.json", []):
            if t.get("id") == thread_id:
                return t
        return None

    def delete_thread(self, thread_id: str) -> dict[str, Any]:
        threads = load_store("threads.json", [])
        target = next(
            (t for t in threads if isinstance(t, dict) and t.get("id") == thread_id),
            None,
        )
        ws_id = (
            str(target.get("workspace_id") or "").strip()
            if isinstance(target, dict)
            else ""
        )
        messages = load_store(f"messages_{thread_id}.json", [])
        run_ids: list[str] = []
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                run_id = str(message.get("run_id") or "").strip()
                if run_id and run_id not in run_ids:
                    run_ids.append(run_id)
        for run in self._web_runs().values():
            if not isinstance(run, dict):
                continue
            if str(run.get("thread_id") or "") != thread_id:
                continue
            run_id = str(run.get("id") or "").strip()
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)

        run_results: list[dict[str, Any]] = []
        detached_run_ids: list[str] = []
        for run_id in run_ids:
            result = self.delete_run(run_id, ws_id or None)
            if not result.get("ok") and "worker still alive after cancel" in str(
                result.get("reason") or ""
            ):
                result = {
                    **result,
                    "ok": True,
                    "removed": False,
                    "detached": True,
                    "reason": (
                        "linked run was cancelled/detached and left in Runs because "
                        "its worker is still stopping"
                    ),
                }
                detached_run_ids.append(run_id)
            run_results.append(result)
        failed = [
            item
            for item in run_results
            if not item.get("ok")
            and "not found and no matching artifacts"
            not in str(item.get("reason") or "")
        ]
        if failed:
            return {
                "ok": False,
                "removed": False,
                "thread_id": thread_id,
                "run_ids": run_ids,
                "run_results": run_results,
                "reason": "one or more linked runs could not be deleted",
            }

        threads = load_store("threads.json", [])
        new_threads = [
            t for t in threads if not (isinstance(t, dict) and t.get("id") == thread_id)
        ]
        removed_thread = len(new_threads) != len(threads)
        save_store("threads.json", new_threads)
        msg_path = store_path(f"messages_{thread_id}.json")
        removed_messages = msg_path.exists()
        if msg_path.exists():
            msg_path.unlink()
        return {
            "ok": True,
            "removed": removed_thread or bool(run_results) or removed_messages,
            "thread_id": thread_id,
            "run_ids": run_ids,
            "detached_run_ids": detached_run_ids,
            "run_results": run_results,
        }

    def list_messages(self, thread_id: str) -> list[dict[str, Any]]:
        return load_store(f"messages_{thread_id}.json", [])

    def send_message(self, thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        thread = self.get_thread(thread_id)
        if thread is None:
            raise ValueError("thread not found")
        ws_id = thread.get("workspace_id")
        content = (payload.get("content") or "").strip()
        if not content:
            raise ValueError("content is required")
        messages = load_store(f"messages_{thread_id}.json", [])
        ts = iso_utc(now_ts())
        user_msg = {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "thread_id": thread_id,
            "role": "user",
            "content": content,
            "created_at": ts,
        }
        messages.append(user_msg)
        run = self.start_workspace_run(
            {
                "workspace_id": ws_id,
                "append_message": content,
                "thread_id": thread_id,
                "model": payload.get("model"),
                "tools": payload.get("tools") or [],
                "max_rounds": payload.get("max_rounds"),
                "max_verify_retries": payload.get("max_verify_retries"),
                "harness_mode": bool(payload.get("harness_mode")),
                "harness_candidates": payload.get("harness_candidates"),
            }
        )
        assistant_msg = {
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "thread_id": thread_id,
            "role": "assistant",
            "content": (
                f"TASK_MAIN.md updated. Real Ouroboros run started for `{ws_id}`: `{run['id']}`. "
                "Watch progress in Runs / Logs."
            ),
            "run_id": run["id"],
            "created_at": ts,
        }
        messages.append(assistant_msg)
        save_store(f"messages_{thread_id}.json", messages)
        thread["updated_at"] = ts
        thread["message_count"] = len(messages)
        threads = load_store("threads.json", [])
        for i, t in enumerate(threads):
            if t.get("id") == thread_id:
                threads[i] = thread
                break
        save_store("threads.json", threads)
        return {"user_message": user_msg, "message": assistant_msg, "run": run}

    def get_settings(self, ws_id: str) -> dict[str, Any]:
        current_model = self._current_model_id()
        defaults = {
            "workspace_id": ws_id,
            "default_model": current_model,
            "enabled_tools": [t["id"] for t in DEFAULT_TOOLS],
            "budget_limit": 10.0,
            "auto_approve_safe_tools": False,
            "stream_responses": True,
            "max_rounds": self._current_max_rounds(),
            "max_verify_retries": self._current_max_verify_retries(),
            "verify": True,
            "verification_timeout_seconds": self._coerce_int(
                os.environ.get("UMBRELLA_VERIFY_TIMEOUT_SECONDS"), 1800, min_value=0
            ),
            "require_instance": False,
            "quality_threshold": None,
        }
        stored = load_store(f"settings_{ws_id}.json", defaults)
        merged = {**defaults, **stored}
        model_ids = {model["id"] for model in self.list_models()}
        if merged.get("default_model") not in model_ids:
            merged["default_model"] = current_model
        return merged

    def update_settings(self, ws_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get_settings(ws_id)
        for key, value in patch.items():
            if value is not None:
                current[key] = value
        current["workspace_id"] = ws_id
        save_store(f"settings_{ws_id}.json", current)
        return current

    def dashboard_stats(self, ws_id: str) -> dict[str, Any]:
        runs = self.list_runs(ws_id, limit=200)["runs"]
        total = len(runs)
        completed = sum(1 for r in runs if r["status"] == "completed")
        failed = sum(1 for r in runs if r["status"] == "failed")
        active = sum(1 for r in runs if r["status"] in ("running", "queued"))
        cost = round(sum(r.get("total_cost", 0.0) for r in runs), 4)
        success_rate = round((completed / total) * 100, 1) if total else 0.0
        return {
            "workspace_id": ws_id,
            "total_runs": total,
            "completed_runs": completed,
            "failed_runs": failed,
            "active_runs": active,
            "total_cost": cost,
            "success_rate": success_rate,
            "recent_runs": runs[:10],
        }

    def list_models(self) -> list[dict[str, Any]]:
        current = self._current_model_id()
        models = [dict(model) for model in DEFAULT_MODELS]
        if current and all(model.get("id") != current for model in models):
            models.insert(
                0,
                {
                    "id": current,
                    "name": current,
                    "provider": "env",
                    "context": int(
                        os.environ.get("OUROBOROS_MODEL_CONTEXT_TOKENS", "128000")
                        or 128000
                    ),
                },
            )
        else:
            models.sort(key=lambda model: 0 if model.get("id") == current else 1)
        for model in models:
            model.setdefault("context_window", model.get("context"))
        return models

    def list_tools(self) -> list[dict[str, Any]]:
        try:
            import sys

            ouroboros_src = str((self.repo_root / "ouroboros").resolve())
            if ouroboros_src not in sys.path:
                sys.path.insert(0, ouroboros_src)
            from ouroboros.tools.registry import CORE_TOOL_NAMES, ToolRegistry

            drive_root = self.repo_root / ".umbrella" / "ouroboros_drive"
            registry = ToolRegistry(
                repo_dir=self.repo_root,
                drive_root=drive_root,
                host_repo_root=self.repo_root,
            )
            tools: list[dict[str, Any]] = []
            for name in registry.available_tools():
                schema = registry.get_schema_by_name(name) or {}
                fn = schema.get("function") if isinstance(schema, dict) else {}
                fn = fn if isinstance(fn, dict) else {}
                tools.append(
                    {
                        "id": name,
                        "name": str(fn.get("name") or name),
                        "desc": str(fn.get("description") or ""),
                        "core": name in CORE_TOOL_NAMES,
                    }
                )
            tools.sort(
                key=lambda tool: (0 if tool.get("core") else 1, str(tool.get("id")))
            )
            return tools
        except Exception:
            return list(DEFAULT_TOOLS)

    def list_user_input_requests(
        self, run_id: str | None, status: str | None
    ) -> list[dict[str, Any]]:
        items = load_store("user_input.json", [])
        if run_id:
            items = [i for i in items if i.get("run_id") == run_id]
        if status:
            items = [i for i in items if i.get("status") == status]
        return items

    def answer_user_input_request(self, req_id: str, answer: str) -> dict[str, Any]:
        items = load_store("user_input.json", [])
        for i, item in enumerate(items):
            if item.get("id") == req_id:
                items[i] = {
                    **item,
                    "status": "answered",
                    "answer": answer,
                    "answered_at": iso_utc(now_ts()),
                }
                save_store("user_input.json", items)
                return items[i]
        return {"id": req_id, "status": "answered", "answer": answer}

    def list_permission_requests(
        self, run_id: str | None, status: str | None
    ) -> list[dict[str, Any]]:
        items = load_store("permission_requests.json", [])
        if run_id:
            items = [i for i in items if i.get("run_id") == run_id]
        if status:
            items = [i for i in items if i.get("status") == status]
        return items

    def resolve_permission_request(self, req_id: str, granted: bool) -> dict[str, Any]:
        items = load_store("permission_requests.json", [])
        for i, item in enumerate(items):
            if item.get("id") == req_id:
                items[i] = {
                    **item,
                    "status": "granted" if granted else "denied",
                    "granted": granted,
                    "resolved_at": iso_utc(now_ts()),
                }
                save_store("permission_requests.json", items)
                return items[i]
        return {
            "id": req_id,
            "status": "granted" if granted else "denied",
            "granted": granted,
        }

    # ------------------------------------------------------------------
    # MCP registry
    # ------------------------------------------------------------------

    def _mcp_registry(self):
        from umbrella.mcp.registry import McpRegistry

        return McpRegistry(self.repo_root)

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        try:
            return [spec.to_dict() for spec in self._mcp_registry().list_servers()]
        except Exception:
            return []

    def add_mcp_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        from umbrella.mcp.registry import McpServerSpec

        registry = self._mcp_registry()
        spec = McpServerSpec.from_dict(payload)
        if not spec.id:
            spec = registry.add_new(
                name=spec.name,
                transport=spec.transport,
                command=spec.command,
                args=spec.args,
                url=spec.url,
                env=spec.env,
                source=spec.source or "user",
                description=spec.description,
                install_notes=spec.install_notes,
                status=spec.status or "disabled",
            )
        else:
            spec = registry.upsert(spec)
        return spec.to_dict()

    def update_mcp_server(
        self, server_id: str, patch: dict[str, Any]
    ) -> dict[str, Any] | None:
        registry = self._mcp_registry()
        spec = registry.get(server_id)
        if spec is None:
            return None
        for key in (
            "name",
            "transport",
            "command",
            "url",
            "description",
            "install_notes",
            "status",
            "source",
        ):
            if key in patch and patch[key] is not None:
                setattr(spec, key, patch[key])
        if isinstance(patch.get("args"), list):
            spec.args = list(patch["args"])
        if isinstance(patch.get("env"), dict):
            spec.env = dict(patch["env"])
        return registry.upsert(spec).to_dict()

    def delete_mcp_server(self, server_id: str) -> dict[str, Any]:
        ok = self._mcp_registry().delete(server_id)
        return {"ok": ok, "removed": ok, "id": server_id}

    def discover_mcp_servers(self, payload: dict[str, Any]) -> dict[str, Any]:
        from umbrella.mcp.discovery import discover_servers

        query = str(payload.get("query") or "").strip()
        max_results = self._coerce_int(payload.get("max_results"), 5, min_value=1) or 5
        if not query:
            return {"ok": False, "reason": "query is required", "results": []}
        try:
            results = discover_servers(query, max_results=max_results)
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "results": []}
        return {"ok": True, "query": query, "results": results}
