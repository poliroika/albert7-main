"""
Ouroboros agent core — thin orchestrator.

Delegates to: loop.py (LLM tool loop), tools/ (tool schemas/execution),
llm.py (LLM calls), memory.py (scratchpad/identity),
context.py (context building), review.py (code collection/metrics).
"""

import json
import logging
import os
import pathlib
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

from ouroboros.utils import (
    utc_now_iso,
    read_text,
    append_jsonl,
    safe_relpath,
    truncate_for_log,
    get_git_info,
    sanitize_task_for_event,
)
from ouroboros.llm import LLMClient
from ouroboros.tools import ToolRegistry
from ouroboros.tools.registry import ToolContext
from ouroboros.memory import Memory
from ouroboros.context import build_llm_messages
from ouroboros.loop import run_llm_loop


# ---------------------------------------------------------------------------
# Module-level guard for one-time worker boot logging
# ---------------------------------------------------------------------------
_worker_boot_logged = False
_worker_boot_lock = threading.Lock()


def _is_effective_write_tool_call(
    tc: dict[str, Any], write_tool_names: frozenset[str]
) -> bool:
    """Return True only when a write-style tool actually changed persisted state."""
    if not isinstance(tc, dict):
        return False
    if str(tc.get("tool") or "") not in write_tool_names:
        return False
    if tc.get("is_error"):
        return False

    result = str(tc.get("result") or "").lstrip()
    if "GIT_NO_CHANGES" in result[:120]:
        return False
    if result.startswith(("ERROR:", "GIT_ERROR", "WARNING:")):
        return False
    return True


# ---------------------------------------------------------------------------
# Environment + Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Env:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    host_repo_root: pathlib.Path | None = None
    branch_dev: str = "ouroboros"

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class OuroborosAgent:
    """One agent instance per worker process. Mostly stateless; long-term state lives on Drive."""

    def __init__(self, env: Env, event_queue: Any = None, memory_hooks: Any = None):
        self.env = env
        self._pending_events: list[dict[str, Any]] = []
        self._event_queue: Any = event_queue
        self.memory_hooks = memory_hooks
        self._current_chat_id: int | None = None
        self._current_task_type: str | None = None

        # Message injection: owner can send messages while agent is busy
        self._incoming_messages: queue.Queue = queue.Queue()
        self._busy = False
        self._last_progress_ts: float = 0.0
        self._task_started_ts: float = 0.0

        # SSOT modules
        self.llm = LLMClient()
        self.tools = ToolRegistry(
            repo_dir=env.repo_dir,
            drive_root=env.drive_root,
            host_repo_root=env.host_repo_root,
        )
        self.memory = Memory(drive_root=env.drive_root, repo_dir=env.repo_dir)

        self._log_worker_boot_once()

    def inject_message(self, text: str) -> None:
        """Thread-safe: inject owner message into the active conversation."""
        self._incoming_messages.put(text)

    def _log_worker_boot_once(self) -> None:
        global _worker_boot_logged
        try:
            with _worker_boot_lock:
                if _worker_boot_logged:
                    return
                _worker_boot_logged = True
            git_branch, git_sha = get_git_info(self.env.repo_dir)
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "worker_boot",
                    "pid": os.getpid(),
                    "git_branch": git_branch,
                    "git_sha": git_sha,
                },
            )
            self._verify_restart(git_sha)
            self._verify_system_state(git_sha)
        except Exception:
            log.warning("Worker boot logging failed", exc_info=True)
            return

    def _verify_restart(self, git_sha: str) -> None:
        """Best-effort restart verification."""
        try:
            pending_path = self.env.drive_path("state") / "pending_restart_verify.json"
            claim_path = pending_path.with_name(
                f"pending_restart_verify.claimed.{os.getpid()}.json"
            )
            try:
                os.rename(str(pending_path), str(claim_path))
            except (FileNotFoundError, Exception):
                return
            try:
                claim_data = json.loads(read_text(claim_path))
                expected_sha = str(claim_data.get("expected_sha", "")).strip()
                ok = bool(expected_sha and expected_sha == git_sha)
                append_jsonl(
                    self.env.drive_path("logs") / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "restart_verify",
                        "pid": os.getpid(),
                        "ok": ok,
                        "expected_sha": expected_sha,
                        "observed_sha": git_sha,
                    },
                )
            except Exception:
                log.debug("Failed to log restart verify event", exc_info=True)
                pass
            try:
                claim_path.unlink()
            except Exception:
                log.debug("Failed to delete restart verify claim file", exc_info=True)
                pass
        except Exception:
            log.debug("Restart verification failed", exc_info=True)
            pass

    def _check_uncommitted_changes(self) -> tuple[dict, int]:
        """Check for uncommitted changes without mutating git by default."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.env.repo_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=True,
            )
            dirty_files = [
                l.strip() for l in result.stdout.strip().split("\n") if l.strip()
            ]
            if dirty_files:
                auto_committed = False
                auto_rescue_enabled = str(
                    os.environ.get("OUROBOROS_AUTO_RESCUE_ON_STARTUP", "")
                ).strip().lower() in {"1", "true", "yes", "on"}
                message = (
                    "Uncommitted changes detected on startup; refusing to mutate git state "
                    "without OUROBOROS_AUTO_RESCUE_ON_STARTUP=1."
                )

                if auto_rescue_enabled:
                    try:
                        subprocess.run(
                            ["git", "add", "-u"],
                            cwd=str(self.env.repo_dir),
                            timeout=10,
                            check=True,
                        )
                        commit = subprocess.run(
                            [
                                "git",
                                "commit",
                                "-m",
                                "startup-rescue: tracked changes detected on agent boot",
                            ],
                            cwd=str(self.env.repo_dir),
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=30,
                        )
                        auto_committed = commit.returncode == 0
                        if auto_committed:
                            message = (
                                "Created a local tracked-files rescue commit because "
                                "OUROBOROS_AUTO_RESCUE_ON_STARTUP=1 was set."
                            )
                        else:
                            stderr = (commit.stderr or commit.stdout or "").strip()
                            if stderr:
                                message = f"Startup rescue commit was skipped: {stderr}"
                    except Exception as e:
                        message = f"Failed to create startup rescue commit: {e}"
                        log.warning(message, exc_info=True)

                return {
                    "status": "warning",
                    "files": dirty_files[:20],
                    "auto_committed": auto_committed,
                    "auto_rescue_enabled": auto_rescue_enabled,
                    "message": message,
                }, 1
            else:
                return {"status": "ok"}, 0
        except Exception as e:
            return {"status": "error", "error": str(e)}, 0

    def _check_version_sync(self) -> tuple[dict, int]:
        """Check VERSION file sync with git tags and pyproject.toml."""
        import subprocess
        import re

        try:
            version_file = read_text(self.env.repo_path("VERSION")).strip()
            issue_count = 0
            result_data = {"version_file": version_file}

            # Check pyproject.toml version
            pyproject_path = self.env.repo_path("pyproject.toml")
            pyproject_content = read_text(pyproject_path)
            match = re.search(
                r'^version\s*=\s*["\']([^"\']+)["\']', pyproject_content, re.MULTILINE
            )
            if match:
                pyproject_version = match.group(1)
                result_data["pyproject_version"] = pyproject_version
                if version_file != pyproject_version:
                    result_data["status"] = "warning"
                    issue_count += 1

            # Check README.md version (Bible P7: VERSION == README version)
            try:
                readme_content = read_text(self.env.repo_path("README.md"))
                readme_match = re.search(
                    r"\*\*Version:\*\*\s*(\d+\.\d+\.\d+)", readme_content
                )
                if readme_match:
                    readme_version = readme_match.group(1)
                    result_data["readme_version"] = readme_version
                    if version_file != readme_version:
                        result_data["status"] = "warning"
                        issue_count += 1
            except Exception:
                log.debug("Failed to check README.md version", exc_info=True)

            # Check git tags
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=str(self.env.repo_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode != 0:
                result_data["status"] = "warning"
                result_data["message"] = "no_tags"
                return result_data, issue_count
            else:
                latest_tag = result.stdout.strip().lstrip("v")
                result_data["latest_tag"] = latest_tag
                if version_file != latest_tag:
                    result_data["status"] = "warning"
                    issue_count += 1

            if issue_count == 0:
                result_data["status"] = "ok"

            return result_data, issue_count
        except Exception as e:
            return {"status": "error", "error": str(e)}, 0

    def _check_budget(self) -> tuple[dict, int]:
        """Check budget remaining with warning thresholds."""
        try:
            state_path = self.env.drive_path("state") / "state.json"
            state_data = json.loads(read_text(state_path))
            total_budget_str = os.environ.get("TOTAL_BUDGET", "")

            # Handle unset or zero budget gracefully
            if not total_budget_str or float(total_budget_str) == 0:
                return {"status": "unconfigured"}, 0
            else:
                total_budget = float(total_budget_str)
                spent = float(state_data.get("spent_usd", 0))
                remaining = max(0, total_budget - spent)

                if remaining < 10:
                    status = "emergency"
                    issues = 1
                elif remaining < 50:
                    status = "critical"
                    issues = 1
                elif remaining < 100:
                    status = "warning"
                    issues = 0
                else:
                    status = "ok"
                    issues = 0

                return {
                    "status": status,
                    "remaining_usd": round(remaining, 2),
                    "total_usd": total_budget,
                    "spent_usd": round(spent, 2),
                }, issues
        except Exception as e:
            return {"status": "error", "error": str(e)}, 0

    def _verify_system_state(self, git_sha: str) -> None:
        """Bible Principle 1: verify system state on every startup.

        Checks:
        - Uncommitted changes (auto-rescue commit & push)
        - VERSION file sync with git tags
        - Budget remaining (warning thresholds)
        """
        checks = {}
        issues = 0
        drive_logs = self.env.drive_path("logs")

        # 1. Uncommitted changes
        checks["uncommitted_changes"], issue_count = self._check_uncommitted_changes()
        issues += issue_count

        # 2. VERSION vs git tag
        checks["version_sync"], issue_count = self._check_version_sync()
        issues += issue_count

        # 3. Budget check
        checks["budget"], issue_count = self._check_budget()
        issues += issue_count

        # Log verification result
        event = {
            "ts": utc_now_iso(),
            "type": "startup_verification",
            "checks": checks,
            "issues_count": issues,
            "git_sha": git_sha,
        }
        append_jsonl(drive_logs / "events.jsonl", event)

        if issues > 0:
            log.warning(f"Startup verification found {issues} issue(s): {checks}")

    # =====================================================================
    # Main entry point
    # =====================================================================

    def _prepare_task_context(
        self, task: dict[str, Any]
    ) -> tuple[ToolContext, list[dict[str, Any]], dict[str, Any]]:
        """Set up ToolContext, build messages, return (ctx, messages, cap_info)."""
        drive_logs = self.env.drive_path("logs")
        sanitized_task = sanitize_task_for_event(task, drive_logs)
        append_jsonl(
            drive_logs / "events.jsonl",
            {"ts": utc_now_iso(), "type": "task_received", "task": sanitized_task},
        )
        raw_overrides = task.get("workspace_root_overrides")
        workspace_root_overrides = (
            {str(k): str(v) for k, v in raw_overrides.items()}
            if isinstance(raw_overrides, dict)
            else {}
        )

        # Set tool context for this task
        ctx = ToolContext(
            repo_dir=self.env.repo_dir,
            drive_root=self.env.drive_root,
            host_repo_root=self.env.host_repo_root,
            branch_dev=self.env.branch_dev,
            pending_events=self._pending_events,
            current_chat_id=self._current_chat_id,
            current_task_type=self._current_task_type,
            workspace_root_overrides=workspace_root_overrides,
            emit_progress_fn=self._emit_progress,
            event_queue=self._event_queue,
            task_id=str(task.get("id") or ""),
            task_depth=int(task.get("depth", 0)),
            is_direct_chat=bool(task.get("_is_direct_chat")),
        )
        self.tools.set_context(ctx)

        # Typing indicator via event queue (no direct Telegram API)
        self._emit_typing_start()

        # --- Build context (delegated to context.py) ---
        messages, cap_info = build_llm_messages(
            env=self.env,
            memory=self.memory,
            task=task,
            review_context_builder=self._build_review_context,
        )

        if cap_info.get("trimmed_sections"):
            try:
                append_jsonl(
                    drive_logs / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "context_soft_cap_trim",
                        "task_id": task.get("id"),
                        **cap_info,
                    },
                )
            except Exception:
                log.warning("Failed to log context soft cap trim event", exc_info=True)
                pass

        # Read budget remaining for cost guard
        budget_remaining = None
        try:
            state_path = self.env.drive_path("state") / "state.json"
            state_data = json.loads(read_text(state_path))
            total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
            spent = float(state_data.get("spent_usd", 0))
            if total_budget > 0:
                budget_remaining = max(0, total_budget - spent)
        except Exception:
            pass

        cap_info["budget_remaining"] = budget_remaining
        return ctx, messages, cap_info

    def handle_task(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        self._busy = True
        start_time = time.time()
        self._task_started_ts = start_time
        self._last_progress_ts = start_time
        self._pending_events = []
        try:
            self._current_chat_id = int(task.get("chat_id") or 0) or None
        except (TypeError, ValueError):
            self._current_chat_id = None
        self._current_task_type = str(task.get("type") or "")

        drive_logs = self.env.drive_path("logs")
        heartbeat_stop = self._start_task_heartbeat_loop(str(task.get("id") or ""))

        try:
            # --- Prepare task context ---
            ctx, messages, cap_info = self._prepare_task_context(task)
            budget_remaining = cap_info.get("budget_remaining")

            # --- LLM loop (delegated to loop.py) ---
            usage: dict[str, Any] = {}
            llm_trace: dict[str, Any] = {"assistant_notes": [], "tool_calls": []}

            # Set initial reasoning effort based on task type
            task_type_str = str(task.get("type") or "").lower()
            if task_type_str in (
                "evolution",
                "review",
                "sync_improvement",
                "self_improvement",
                "workspace_delivery",
            ):
                initial_effort = "high"
            else:
                initial_effort = "medium"

            try:
                # When the harness is re-driving the SAME task_id for a
                # verification-remediation cycle, it stamps the task with
                # ``verification_remediation_attempt`` (1..N). The loop
                # uses this to skip the planner phase entirely and route
                # straight into the remediation-flavored subtask loop,
                # otherwise the agent gets contradictory instructions
                # (the user prompt says "same run, do not re-plan" while
                # the loop simultaneously injects [PLANNER PHASE] and
                # forces propose_task_plan).
                remediation_attempt = 0
                try:
                    remediation_attempt = int(
                        task.get("verification_remediation_attempt") or 0
                    )
                except (TypeError, ValueError):
                    remediation_attempt = 0
                self_review_attempt = 0
                try:
                    self_review_attempt = int(task.get("self_review_attempt") or 0)
                except (TypeError, ValueError):
                    self_review_attempt = 0
                text, usage, llm_trace = run_llm_loop(
                    messages=messages,
                    tools=self.tools,
                    llm=self.llm,
                    drive_logs=drive_logs,
                    emit_progress=self._emit_progress,
                    incoming_messages=self._incoming_messages,
                    task_type=task_type_str,
                    task_id=str(task.get("id") or ""),
                    budget_remaining_usd=budget_remaining,
                    event_queue=self._event_queue,
                    initial_effort=initial_effort,
                    drive_root=self.env.drive_root,
                    deadline_monotonic=task.get("_deadline_monotonic"),
                    remediation_attempt=remediation_attempt,
                    prebuilt_plan_id=str(task.get("prebuilt_plan_id") or ""),
                    memory_hooks=self.memory_hooks,
                    self_review_attempt=self_review_attempt,
                )
            except Exception as e:
                tb = traceback.format_exc()
                append_jsonl(
                    drive_logs / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "task_error",
                        "task_id": task.get("id"),
                        "error": repr(e),
                        "traceback": truncate_for_log(tb, 2000),
                    },
                )
                text = f"⚠️ Error during processing: {type(e).__name__}: {e}"

            # Empty response guard
            if not isinstance(text, str) or not text.strip():
                text = (
                    "⚠️ Model returned an empty response. Try rephrasing your request."
                )

            # Emit events for supervisor
            self._emit_task_results(
                task, text, usage, llm_trace, start_time, drive_logs
            )
            return list(self._pending_events)

        finally:
            self._busy = False
            # Clean up browser if it was used during this task
            try:
                from ouroboros.tools.browser import cleanup_browser

                cleanup_browser(self.tools._ctx)
            except Exception:
                log.debug("Failed to cleanup browser", exc_info=True)
                pass
            while not self._incoming_messages.empty():
                try:
                    self._incoming_messages.get_nowait()
                except queue.Empty:
                    break
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            self._current_task_type = None

    # =====================================================================
    # Task result emission
    # =====================================================================

    def _emit_task_results(
        self,
        task: dict[str, Any],
        text: str,
        usage: dict[str, Any],
        llm_trace: dict[str, Any],
        start_time: float,
        drive_logs: pathlib.Path,
    ) -> None:
        """Emit all end-of-task events to supervisor."""
        # NOTE: per-round llm_usage events are already emitted in loop.py
        # (_emit_llm_usage_event). Do NOT emit an aggregate llm_usage here —
        # that would double-count in update_budget_from_usage.
        # Cost/token summaries are carried by task_metrics and task_done events.

        if self._current_chat_id is not None:
            self._pending_events.append(
                {
                    "type": "send_message",
                    "chat_id": self._current_chat_id,
                    "text": text or "\u200b",
                    "log_text": text or "",
                    "format": "markdown",
                    "task_id": task.get("id"),
                    "ts": utc_now_iso(),
                }
            )

        duration_sec = round(time.time() - start_time, 3)
        trace_tools = llm_trace.get("tool_calls") or []
        n_tool_calls = len(trace_tools)
        n_tool_errors = sum(
            1 for tc in trace_tools if isinstance(tc, dict) and tc.get("is_error")
        )
        # Surfaces actual workspace/repo writes for host integrations (Umbrella app_ouroboros).
        _write_tool_names = frozenset(
            {
                "repo_write_commit",
                "repo_commit_push",
                "update_workspace_seed",
                "update_workspace_from_instance",
                "commit_workspace_changes",
            }
        )
        write_tool_calls = [
            tc
            for tc in trace_tools
            if _is_effective_write_tool_call(tc, _write_tool_names)
        ]
        _write_event = {
            "type": "workspace_write_tools",
            "task_id": task.get("id"),
            "count": len(write_tool_calls),
            "tools": [str(tc.get("tool") or "") for tc in write_tool_calls],
            "ts": utc_now_iso(),
        }
        self._pending_events.append(_write_event)
        append_jsonl(drive_logs / "events.jsonl", _write_event)
        try:
            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "task_eval",
                    "ok": True,
                    "task_id": task.get("id"),
                    "task_type": task.get("type"),
                    "duration_sec": duration_sec,
                    "tool_calls": n_tool_calls,
                    "tool_errors": n_tool_errors,
                    "response_len": len(text),
                },
            )
        except Exception:
            log.warning("Failed to log task eval event", exc_info=True)
            pass

        _metrics_event = {
            "type": "task_metrics",
            "task_id": task.get("id"),
            "task_type": task.get("type"),
            "duration_sec": duration_sec,
            "tool_calls": n_tool_calls,
            "tool_errors": n_tool_errors,
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_rounds": int(usage.get("rounds") or 0),
            "ts": utc_now_iso(),
        }
        self._pending_events.append(_metrics_event)
        append_jsonl(drive_logs / "events.jsonl", _metrics_event)

        _done_event = {
            "type": "task_done",
            "task_id": task.get("id"),
            "task_type": task.get("type"),
            "tool_calls": n_tool_calls,
            "tool_errors": n_tool_errors,
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "total_rounds": int(usage.get("rounds") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "ts": utc_now_iso(),
        }
        self._pending_events.append(_done_event)
        append_jsonl(drive_logs / "events.jsonl", _done_event)

        # Store task result for parent task retrieval (and for the web
        # bridge to surface the actual model used per remediation/attempt).
        try:
            results_dir = pathlib.Path(self.env.drive_root) / "task_results"
            results_dir.mkdir(parents=True, exist_ok=True)
            active_model = (
                str(usage.get("model") or "").strip()
                or str(os.environ.get("OUROBOROS_MODEL") or "").strip()
                or str(os.environ.get("LLM_MODEL") or "").strip()
            )
            result_data = {
                "task_id": task.get("id"),
                "parent_task_id": task.get("parent_task_id"),
                "status": "completed",
                "result": text[:4000] if text else "",  # Truncate to avoid huge files
                "cost_usd": round(float(usage.get("cost") or 0), 6),
                "total_rounds": int(usage.get("rounds") or 0),
                "model": active_model or None,
                "ts": utc_now_iso(),
            }
            result_file = results_dir / f"{task.get('id')}.json"
            tmp_file = results_dir / f"{task.get('id')}.json.tmp"
            tmp_file.write_text(
                json.dumps(result_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_file, result_file)
        except Exception as e:
            log.warning("Failed to store task result: %s", e)

    # =====================================================================
    # Review context builder
    # =====================================================================

    def _build_review_context(self) -> str:
        """Collect code snapshot + complexity metrics for review tasks."""
        try:
            from ouroboros.review import (
                collect_sections,
                compute_complexity_metrics,
                format_metrics,
            )

            sections, stats = collect_sections(self.env.repo_dir, self.env.drive_root)
            metrics = compute_complexity_metrics(sections)

            parts = [
                "## Code Review Context\n",
                format_metrics(metrics),
                f"\nFiles: {stats['files']}, chars: {stats['chars']}\n",
                "\nUse repo_read to inspect specific files. "
                "Use run_shell for tests. Key files below:\n",
            ]

            total_chars = 0
            max_chars = 80_000
            files_added = 0
            for path, content in sections:
                if total_chars >= max_chars:
                    parts.append(
                        f"\n... ({len(sections) - files_added} more files, use repo_read)"
                    )
                    break
                preview = content[:2000] if len(content) > 2000 else content
                file_block = f"\n### {path}\n```\n{preview}\n```\n"
                total_chars += len(file_block)
                parts.append(file_block)
                files_added += 1

            return "\n".join(parts)
        except Exception as e:
            return f"## Code Review Context\n\n(Failed to collect: {e})\nUse repo_read and repo_list to inspect code."

    # =====================================================================
    # Event emission helpers
    # =====================================================================

    def _emit_progress(self, text: str) -> None:
        self._last_progress_ts = time.time()
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put(
                {
                    "type": "send_message",
                    "chat_id": self._current_chat_id,
                    "text": f"💬 {text}",
                    "format": "markdown",
                    "is_progress": True,
                    "ts": utc_now_iso(),
                }
            )
        except Exception:
            log.warning("Failed to emit progress event", exc_info=True)
            pass

    def _emit_typing_start(self) -> None:
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put(
                {
                    "type": "typing_start",
                    "chat_id": self._current_chat_id,
                    "ts": utc_now_iso(),
                }
            )
        except Exception:
            log.warning("Failed to emit typing start event", exc_info=True)
            pass

    def _emit_task_heartbeat(self, task_id: str, phase: str) -> None:
        if self._event_queue is None:
            return
        try:
            self._event_queue.put(
                {
                    "type": "task_heartbeat",
                    "task_id": task_id,
                    "phase": phase,
                    "ts": utc_now_iso(),
                }
            )
        except Exception:
            log.warning("Failed to emit task heartbeat event", exc_info=True)
            pass

    def _start_task_heartbeat_loop(self, task_id: str) -> threading.Event | None:
        if self._event_queue is None or not task_id.strip():
            return None
        interval = 30
        stop = threading.Event()
        self._emit_task_heartbeat(task_id, "start")

        def _loop() -> None:
            while not stop.wait(interval):
                self._emit_task_heartbeat(task_id, "running")

        threading.Thread(target=_loop, daemon=True).start()
        return stop


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_agent(
    repo_dir: str,
    drive_root: str,
    event_queue: Any = None,
    host_repo_root: str | None = None,
    memory_hooks: Any = None,
) -> OuroborosAgent:
    env = Env(
        repo_dir=pathlib.Path(repo_dir),
        drive_root=pathlib.Path(drive_root),
        host_repo_root=pathlib.Path(host_repo_root) if host_repo_root else None,
    )
    return OuroborosAgent(env, event_queue=event_queue, memory_hooks=memory_hooks)
