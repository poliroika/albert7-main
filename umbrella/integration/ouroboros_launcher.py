"""
Ouroboros launcher for Umbrella.

When Umbrella starts, this launches Ouroboros as the AI brain
that uses Umbrella's infrastructure.
"""

import logging
import hashlib
import pathlib
import threading
import time
from typing import Any

from umbrella.integration.ouroboros_bridge import (
    ensure_drive_layout,
    resolve_ouroboros_repo_root,
    seed_workspace_prompts,
    sync_umbrella_context_to_drive,
    workspace_drive_root,
)
from umbrella.control_plane.sandbox_self_edit import (
    capture_changed_files,
    enter_sandbox,
    exit_sandbox,
    SandboxSession,
)

log = logging.getLogger(__name__)

_WORKSPACE_SOURCE_SUFFIXES = {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_WORKSPACE_SOURCE_DIRS = {"src", "tests", "test", "frontend", "backend", "docs"}


def _task_requires_product_self_edit_sandbox(task: dict[str, Any]) -> bool:
    """Return True only for explicit product-code self-edit / isolation tasks.

    Normal ``phase_run`` and generated-workspace execute must not enter rollback
    sandbox (``git stash`` / ``git clean -fd``) or untracked workspace sources
    under ``workspaces/<id>/`` can disappear between subtasks.
    """
    task_type = str(task.get("type") or "").strip().lower()
    if task_type == "phase_run":
        return False
    if task.get("_is_direct_chat"):
        return False
    if task.get("product_self_edit") or task.get("umbrella_self_edit"):
        return True
    if task.get("self_improve") or task.get("self_improvement"):
        return True
    if task.get("candidate_isolation"):
        return True
    return False


def _workspace_source_manifest(workspace_root: pathlib.Path) -> dict[str, int]:
    """Map relative paths to file sizes for generated source-like files."""
    if not workspace_root.is_dir():
        return {}
    manifest: dict[str, int] = {}
    for path in workspace_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace_root).as_posix()
        parts = rel.split("/")
        if parts[0] in {".git", ".memory", ".umbrella", ".umbrella_scratch"}:
            continue
        if parts[0] not in _WORKSPACE_SOURCE_DIRS and path.suffix.lower() not in _WORKSPACE_SOURCE_SUFFIXES:
            continue
        try:
            manifest[rel] = path.stat().st_size
        except OSError:
            continue
    return manifest


def _unlogged_workspace_source_loss(
    before: dict[str, int], after: dict[str, int]
) -> dict[str, Any] | None:
    missing = sorted(path for path, size in before.items() if size > 0 and path not in after)
    if not missing:
        return None
    return {
        "status": "blocked",
        "reason": "unlogged_workspace_source_loss",
        "missing_files": missing[:50],
        "recommended_action": "stop_run_and_investigate_sandbox_or_external_cleanup",
    }


def _task_artifact_stem(task_id: str | None, *, max_len: int = 120) -> str:
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


def resolve_drive_root(
    repo_root: pathlib.Path, workspace_id: str | None = None
) -> pathlib.Path:
    """Return the canonical Ouroboros drive used by Umbrella."""
    return workspace_drive_root(repo_root, workspace_id)


def _coerce_positive_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


class OuroborosLauncher:
    """Launches and manages Ouroboros agent alongside Umbrella."""

    def __init__(
        self,
        repo_root: pathlib.Path,
        drive_root: pathlib.Path | None = None,
        workspace_id: str | None = None,
    ):
        self.repo_root = repo_root.resolve()
        self.ouroboros_repo_root = resolve_ouroboros_repo_root(self.repo_root)
        self.drive_root = (
            drive_root or resolve_drive_root(self.repo_root, workspace_id)
        ).resolve()
        ensure_drive_layout(self.drive_root)
        seed_workspace_prompts(self.repo_root, workspace_id)

        self._agent = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._task_queue: list[dict] = []
        self._results: dict[str, Any] = {}
        self._queue_lock = threading.Lock()
        self._results_ready = threading.Condition()
        self._sandbox_session: SandboxSession | None = None

    def start(self) -> None:
        """Start Ouroboros agent in background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info("Ouroboros launcher started")

    def stop(self) -> None:
        """Stop Ouroboros agent."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Ouroboros launcher stopped")

    def submit_task(self, task: dict) -> str:
        """Submit a task for Ouroboros to process."""
        import uuid

        if not self._running:
            self.start()

        task_copy = dict(task)
        task_id = str(task_copy.get("id") or f"task_{uuid.uuid4().hex[:8]}")
        task_copy["id"] = task_id
        normalized_task = self._normalize_task(task_copy)
        with self._queue_lock:
            self._task_queue.append(normalized_task)
        return task_id

    def get_result(self, task_id: str) -> dict | None:
        """Get result of a completed task."""
        with self._results_ready:
            return self._results.pop(task_id, None)

    def wait_for_result(
        self, task_id: str, timeout: float | None = 300.0
    ) -> dict | None:
        """Block until a submitted task completes or the timeout elapses.

        Pass ``None`` to wait indefinitely.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._results_ready:
            while task_id not in self._results:
                if deadline is None:
                    self._results_ready.wait(timeout=30.0)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._results_ready.wait(timeout=remaining)
            return self._results.pop(task_id, None)

    def _run_loop(self) -> None:
        """Background loop that processes tasks."""
        while self._running:
            try:
                task = None
                with self._queue_lock:
                    if self._task_queue:
                        task = self._task_queue.pop(0)
                if task is None:
                    time.sleep(0.5)
                    continue
                result = self._process_task(task)
                with self._results_ready:
                    self._results[task["id"]] = result
                    self._results_ready.notify_all()
            except Exception as e:
                log.error(f"Ouroboros task failed: {e}")

    def _enter_task_sandbox(
        self,
        task: dict[str, Any],
        *,
        force_snapshot_method: str | None = None,
        workspace_id: str = "",
    ) -> SandboxSession | None:
        """Enter a rollback self-edit session.

        Persistent/no-rollback self-edit is reserved for explicit approved
        self-improvement modes. Normal ``phase_run`` tasks must not enter
        rollback sandbox.
        """
        task_id = str(task.get("id") or "")
        if not _task_requires_product_self_edit_sandbox(task):
            log.info(
                "Skipping product self-edit sandbox for task %s (type=%s)",
                task_id,
                task.get("type"),
            )
            return None
        try:
            from umbrella.policies.defaults import load_default_policy

            policy = load_default_policy()
            if not policy.sandbox_self_edit.enabled and force_snapshot_method is None:
                return None

            import subprocess as _sp

            baseline = ""
            try:
                r = _sp.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(self.repo_root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                baseline = r.stdout.strip() if r.returncode == 0 else ""
            except Exception:
                pass

            session = enter_sandbox(
                repo_root=self.repo_root,
                task_id=task_id,
                snapshot_method=force_snapshot_method
                or policy.sandbox_self_edit.snapshot_method,
                workspace_id=workspace_id,
            )
            session.baseline_sha = baseline
            self._sandbox_session = session
            return session
        except Exception:
            log.warning("Failed to enter sandbox (non-fatal)", exc_info=True)
            return None

    def _capture_and_exit_sandbox(self) -> tuple[str, list[str]]:
        """Capture changed file names, then close the sandbox session.

        Returns ``(diff_text, changed_files)`` so the caller can attach
        them to the result before rollback.
        """
        session = self._sandbox_session
        if session is None:
            return "", []
        diff_text = ""
        changed: list[str] = []
        try:
            changed = capture_changed_files(session)
        except Exception:
            log.warning("Pre-exit diff capture failed (non-fatal)", exc_info=True)
        try:
            exit_sandbox(session)
            if session.rollback_ok:
                log.info(
                    "Sandbox session closed without rollback; %d recorded self-edit file(s) kept for task %s",
                    len(session.edited_files),
                    session.task_id,
                )
            else:
                log.error(
                    "Sandbox session close FAILED for task %s: %s",
                    session.task_id,
                    session.error,
                )
        except Exception:
            log.error("Sandbox exit crashed", exc_info=True)
        finally:
            self._sandbox_session = None
        return diff_text, changed

    def _process_task(self, task: dict) -> dict:
        """Process a single task using Ouroboros agent.

        The sandbox is entered via ``_enter_task_sandbox`` and MUST be
        released even on ``BaseException`` (``KeyboardInterrupt``,
        ``SystemExit``). Previously we only caught ``ImportError`` and
        ``Exception``; Ctrl-C during ``handle_task`` could strand the stash
        and leave the worktree in sandbox state. We now wrap the whole body
        in try/finally and guard against double-release.
        """
        sandbox_session: SandboxSession | None = None
        exited = False
        candidate_diff = ""
        candidate_changed: list[str] = []
        try:
            normalized_task = self._normalize_task(task)
            workspace_id = str(normalized_task.get("workspace_id") or "")
            task_drive_root = resolve_drive_root(self.repo_root, workspace_id).resolve()
            ensure_drive_layout(task_drive_root)
            seed_workspace_prompts(self.repo_root, workspace_id)
            log.info(
                "Ouroboros chosen_drive_root=%s workspace_id=%s",
                task_drive_root,
                workspace_id or "<none>",
            )
            max_runtime_seconds = _coerce_positive_float(
                normalized_task.get("max_runtime_seconds")
            )
            if max_runtime_seconds is not None:
                normalized_task["_deadline_monotonic"] = (
                    time.monotonic() + max_runtime_seconds
                )

            force_method: str | None = None
            if normalized_task.get("candidate_isolation"):
                force_method = "git_branch"

            workspace_root = (
                (self.repo_root / "workspaces" / workspace_id).resolve()
                if workspace_id
                else None
            )
            workspace_manifest_before = (
                _workspace_source_manifest(workspace_root)
                if workspace_root is not None
                else {}
            )

            sandbox_session = self._enter_task_sandbox(
                normalized_task,
                force_snapshot_method=force_method,
                workspace_id=workspace_id,
            )

            # Load env variables FIRST - this is critical!
            from umbrella.env import load_env

            load_env(repo_root=self.repo_root)

            # Add ouroboros package to path (the outer ouroboros/ contains the package).
            #
            # Important: dashboard/control-plane code may already have attempted
            # ``import ouroboros.llm`` before this point while only the host repo
            # root was importable. That can leave a namespace-package
            # ``ouroboros`` in sys.modules which does NOT resolve
            # ``ouroboros.agent``. Force the standalone Ouroboros repo to the
            # front and clear stale parent modules before importing the agent.
            import sys
            import os

            ouroboros_pkg_root = self.ouroboros_repo_root
            ouroboros_pkg_path = str(ouroboros_pkg_root)
            sys.path[:] = [
                p
                for p in sys.path
                if pathlib.Path(p or ".").resolve() != ouroboros_pkg_root
            ]
            sys.path.insert(0, ouroboros_pkg_path)

            if "ouroboros.agent" not in sys.modules:
                parent = sys.modules.get("ouroboros")
                expected_pkg_dir = str((ouroboros_pkg_root / "ouroboros").resolve())
                parent_paths = (
                    [
                        str(pathlib.Path(str(p)).resolve())
                        for p in getattr(parent, "__path__", []) or []
                    ]
                    if parent is not None
                    else []
                )
                if parent is not None and expected_pkg_dir not in parent_paths:
                    for name in list(sys.modules):
                        if name == "ouroboros" or name.startswith("ouroboros."):
                            sys.modules.pop(name, None)

            # Ensure Umbrella env vars are propagated to Ouroboros
            # These MUST be set before importing Ouroboros modules
            # Only propagate non-empty values to avoid overriding Ouroboros defaults with ""
            if not os.environ.get("OUROBOROS_LLM_API_KEY"):
                _api_key = os.environ.get("LLM_API_KEY", "").strip()
                if _api_key:
                    os.environ["OUROBOROS_LLM_API_KEY"] = _api_key
            if not os.environ.get("OUROBOROS_LLM_BASE_URL"):
                _base_url = os.environ.get("LLM_BASE_URL", "").strip()
                if _base_url:
                    os.environ["OUROBOROS_LLM_BASE_URL"] = _base_url
            if not os.environ.get("OUROBOROS_MODEL"):
                _model = os.environ.get("LLM_MODEL", "").strip()
                if _model:
                    os.environ["OUROBOROS_MODEL"] = _model

            # Aggressive progress guards for delivery runs (can be overridden via env).
            os.environ.setdefault("OUROBOROS_NO_WRITE_TOOL_NUDGE_AFTER_ROUNDS", "3")
            os.environ.setdefault("OUROBOROS_NO_WRITE_TOOL_NUDGE_INTERVAL", "1")
            os.environ.setdefault("OUROBOROS_NO_WRITE_TOOL_ABORT_AFTER_NUDGES", "1")
            os.environ.setdefault("OUROBOROS_PLANNER_PHASE_ROUNDS", "14")
            os.environ.setdefault("OUROBOROS_MAX_ROUNDS", "200")
            # Leave headroom for prompt history on 200k-class models; override in .env if needed.
            os.environ.setdefault("OUROBOROS_MAX_TOKENS", "32768")
            os.environ.setdefault("OUROBOROS_TOOL_MAX_TOKENS", "8192")

            # Debug: check env presence without logging secret-bearing endpoints.
            log.info(
                "Ouroboros env check: model=%s, api_key=%s, base_url=%s",
                os.environ.get("OUROBOROS_MODEL") or "<unset>",
                "set" if os.environ.get("OUROBOROS_LLM_API_KEY") else "unset",
                "set" if os.environ.get("OUROBOROS_LLM_BASE_URL") else "unset",
            )
            log.info(
                f"Ouroboros task input: {str(normalized_task.get('input', ''))[:200]}..."
            )

            # Set repo root for Umbrella tools
            os.environ["UMBRELLA_REPO_ROOT"] = str(self.repo_root)

            if sandbox_session is not None:
                os.environ["UMBRELLA_SANDBOX_SESSION_ID"] = sandbox_session.session_id
            else:
                os.environ.pop("UMBRELLA_SANDBOX_SESSION_ID", None)

            # Import Ouroboros with detailed logging
            try:
                log.info("Importing ouroboros.agent...")
                from ouroboros.agent import make_agent

                log.info("make_agent imported successfully")
            except ImportError as e:
                log.error(f"Failed to import make_agent: {e}", exc_info=True)
                import traceback

                log.error(f"Traceback: {traceback.format_exc()}")
                raise  # Re-raise to trigger fallback

            # Extract task input - prioritize 'input' field
            task_input_value = str(
                normalized_task.get("input")
                or normalized_task.get("text")
                or normalized_task.get("task")
                or normalized_task.get("user_message")
                or ""
            )
            log.info(f"Final task_input for sync: {task_input_value[:200]}...")

            sync_umbrella_context_to_drive(
                self.repo_root,
                task_drive_root,
                workspace_id=workspace_id or None,
                task_input=task_input_value or None,
                task_id=str(normalized_task.get("id") or "") or None,
                user_message=str(normalized_task.get("user_message") or "") or None,
                memory_payload=normalized_task.get("memory")
                if isinstance(normalized_task.get("memory"), dict)
                else None,
            )

            agent = make_agent(
                repo_dir=str(self.repo_root),
                drive_root=str(task_drive_root),
                host_repo_root=str(self.repo_root),
                memory_hooks=None,
            )

            # Umbrella tools are now auto-discovered by Ouroboros via ouroboros/tools/umbrella_tools.py
            log.info("Umbrella tools available for auto-discovery by Ouroboros")

            events = agent.handle_task(normalized_task)

            candidate_diff, candidate_changed = self._capture_and_exit_sandbox()
            exited = True

            result: dict[str, Any] = {
                "task_id": normalized_task["id"],
                "status": "complete",
                "events": events,
                "result": self._load_task_result_text(
                    normalized_task["id"], task_drive_root
                ),
                "candidate_diff": candidate_diff,
                "candidate_changed_files": candidate_changed,
            }
            if workspace_root is not None:
                loss = _unlogged_workspace_source_loss(
                    workspace_manifest_before,
                    _workspace_source_manifest(workspace_root),
                )
                if loss is not None:
                    result["unlogged_workspace_source_loss"] = loss
                    result["status"] = "blocked"
                    log.error(
                        "Unlogged workspace source loss for task %s: %s",
                        normalized_task["id"],
                        loss.get("missing_files"),
                    )
            return result
        except ImportError as e:
            log.error(f"Ouroboros ImportError: {e}", exc_info=True)
            import traceback

            log.error(f"Traceback: {traceback.format_exc()}")
            self._capture_and_exit_sandbox()
            exited = True
            return self._fallback_process(task)
        except Exception as e:
            log.error(f"Task processing error: {e}")
            self._capture_and_exit_sandbox()
            exited = True
            return {"task_id": task["id"], "status": "error", "error": str(e)}
        finally:
            # Last-resort release on BaseException (KeyboardInterrupt,
            # SystemExit, ...): make sure we never leak a live sandbox.
            if not exited and self._sandbox_session is not None:
                try:
                    self._capture_and_exit_sandbox()
                except Exception:  # noqa: BLE001
                    log.error(
                        "Sandbox emergency-release failed during BaseException unwind",
                        exc_info=True,
                    )

    def _normalize_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """Fill launcher defaults and normalize chat/task metadata."""
        normalized = dict(task)
        normalized.setdefault("created_at", time.time())
        normalized.setdefault("depth", 0)
        normalized.setdefault("_is_direct_chat", False)
        if not normalized.get("input"):
            normalized["input"] = (
                normalized.get("text")
                or normalized.get("task")
                or normalized.get("user_message")
                or ""
            )

        raw_chat_id = normalized.get("chat_id")
        if raw_chat_id in (None, "", 0, "0"):
            normalized.pop("chat_id", None)
        else:
            try:
                normalized["chat_id"] = int(raw_chat_id)
            except (TypeError, ValueError):
                log.warning("Ignoring invalid Ouroboros chat_id: %r", raw_chat_id)
                normalized.pop("chat_id", None)

        return normalized

    def _load_task_result_text(
        self, task_id: str, drive_root: pathlib.Path | None = None
    ) -> str:
        """Read the persisted task result text emitted by the agent."""
        result_file = (
            (drive_root or self.drive_root)
            / "task_results"
            / f"{_task_artifact_stem(task_id)}.json"
        )
        if not result_file.exists():
            return ""
        try:
            import json

            payload = json.loads(result_file.read_text(encoding="utf-8"))
            return str(payload.get("result") or "")
        except Exception:
            log.debug(
                "Failed to read persisted Ouroboros task result for %s",
                task_id,
                exc_info=True,
            )
            return ""

    def _fallback_process(self, task: dict) -> dict:
        """Fallback when Ouroboros is not available."""
        # Use Umbrella's own LLM to analyze workspace
        from umbrella.control_plane.code_analyzer import analyze_workspace_code

        task_input = task.get("input", "")

        # Find relevant workspace
        workspace_id = task.get("workspace_id", "agent_research")
        workspace_path = self.repo_root / "workspaces" / workspace_id

        if workspace_path.exists():
            analysis = analyze_workspace_code(
                workspace_path=workspace_path,
                task_description=task_input,
            )
            return {
                "task_id": task["id"],
                "status": "analyzed",
                "analysis": analysis,
            }

        return {"task_id": task["id"], "status": "no_workspace"}


# Global launcher instance
_launcher: OuroborosLauncher | None = None


def get_launcher(repo_root: pathlib.Path | None = None) -> OuroborosLauncher:
    """Get or create the global Ouroboros launcher."""
    global _launcher
    effective_root = (repo_root or pathlib.Path.cwd()).resolve()
    if _launcher is None or _launcher.repo_root != effective_root:
        if _launcher is not None:
            _launcher.stop()
        _launcher = OuroborosLauncher(effective_root)
    return _launcher


def start_ouroboros(repo_root: pathlib.Path | None = None) -> OuroborosLauncher:
    """Start Ouroboros alongside Umbrella."""
    launcher = get_launcher(repo_root)
    launcher.start()
    return launcher


def stop_ouroboros() -> None:
    """Stop Ouroboros."""
    global _launcher
    if _launcher:
        _launcher.stop()
        _launcher = None
