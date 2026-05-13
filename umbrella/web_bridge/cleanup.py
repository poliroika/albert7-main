"""Centralised hard-delete helpers for the web bridge.

This module is the single source of truth for what gets removed from disk
when the user clicks a delete button in the web UI.  ``delete_run``,
``delete_thread``, ``delete_workspace`` and ``delete_memory_node`` in
``umbrella.web_bridge.app`` should all delegate here so we have one place
that knows about every artifact directory (workspace ``.memory``,
``.umbrella/web``, ``.umbrella/backups``, ``.umbrella/meta_harness`` and so on).

Design notes:
- Every wipe function returns a :class:`CleanupReport` with a list of
  removed paths, kept paths and any non-fatal errors.  The HTTP layer
  surfaces those so the user sees what actually happened, instead of an
  unconditional ``{"ok": true}``.
- Functions are deliberately tolerant of missing files: deleting an
  already-gone artifact is not an error.
- Safety: every absolute path that could escape the repo root is checked
  with :meth:`Path.resolve` + :meth:`Path.relative_to` before deletion.
"""

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from collections.abc import Callable, Iterable

__all__ = [
    "CleanupReport",
    "wipe_run_artifacts",
    "wipe_thread_artifacts",
    "wipe_workspace_artifacts",
    "wipe_memory_node",
]


@dataclass
class CleanupReport:
    """Structured summary of a hard-delete operation."""

    removed_paths: list[str] = field(default_factory=list)
    kept_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def add_removed(self, path: Path | str, *, kind: str = "file") -> None:
        self.removed_paths.append(str(path))
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def add_kept(self, path: Path | str, *, reason: str = "") -> None:
        entry = str(path)
        if reason:
            entry = f"{entry} ({reason})"
        self.kept_paths.append(entry)

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def add_jsonl_rows(self, count: int, *, kind: str = "jsonl_rows") -> None:
        if count <= 0:
            return
        self.counts[kind] = self.counts.get(kind, 0) + count

    def merge(self, other: "CleanupReport") -> None:
        self.removed_paths.extend(other.removed_paths)
        self.kept_paths.extend(other.kept_paths)
        self.errors.extend(other.errors)
        for key, value in other.counts.items():
            self.counts[key] = self.counts.get(key, 0) + value

    def to_dict(self) -> dict[str, Any]:
        return {
            "removed_paths": list(self.removed_paths),
            "kept_paths": list(self.kept_paths),
            "errors": list(self.errors),
            "counts": dict(self.counts),
            "removed_count": len(self.removed_paths),
        }

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok


def _release_palace_cache(palace_path: Path) -> None:
    """Best-effort release of cached PalaceBackend / ChromaDB handles.

    On Windows ChromaDB keeps the SQLite WAL files mmap-locked which makes
    ``shutil.rmtree`` fail with ``PermissionError``. Calling this before
    deleting the palace directory closes the cached client so the OS can
    release the file handles.
    """
    try:
        from umbrella.memory.palace_backend import (
            clear_palace_backend_cache,  # local import to avoid hard dep
        )
    except Exception:
        return
    candidates = {palace_path, palace_path.resolve()}
    for candidate in candidates:
        try:
            clear_palace_backend_cache(candidate)
        except Exception:
            pass
    try:
        from chromadb.api.shared_system_client import SharedSystemClient

        SharedSystemClient._identifier_to_system.clear()
    except Exception:
        pass


def _rmtree_with_retry(
    path: Path,
    report: CleanupReport,
    *,
    kind: str = "dir",
    attempts: int = 8,
    sleep_seconds: float = 0.3,
) -> bool:
    """Windows-friendly ``shutil.rmtree`` with retry on transient lock errors.

    Antivirus, the file indexer and the ChromaDB writer can briefly hold
    handles open even after we release the cache. A handful of short
    sleeps gives the OS time to drop them.
    """
    if not path.exists() and not path.is_symlink():
        return False
    import gc as _gc

    last_exc: OSError | None = None
    for attempt in range(max(1, attempts)):
        try:
            shutil.rmtree(path)
            report.add_removed(path, kind=kind)
            return True
        except OSError as exc:
            last_exc = exc
            _gc.collect()
            time.sleep(sleep_seconds * (attempt + 1))
            continue
    if last_exc is not None:
        report.add_error(f"failed to remove {path}: {last_exc}")
        report.add_kept(path, reason=str(last_exc))
    return False


def _safe_remove(path: Path, report: CleanupReport, *, kind: str = "file") -> bool:
    """Best-effort delete of *path* (file or directory)."""
    if not path.exists() and not path.is_symlink():
        return False
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
            report.add_removed(path, kind="dir" if kind == "file" else kind)
        else:
            path.unlink(missing_ok=True)
            report.add_removed(path, kind=kind)
        return True
    except OSError as exc:
        # Directories of palace/Chroma stores can keep transient locks even
        # after the cache is released, so retry once with backoff before
        # giving up.
        if path.is_dir() and not path.is_symlink():
            _release_palace_cache(path)
            if _rmtree_with_retry(path, report, kind="dir" if kind == "file" else kind):
                return True
        report.add_error(f"failed to remove {path}: {exc}")
        report.add_kept(path, reason=str(exc))
        return False


def _is_inside(path: Path, root: Path) -> bool:
    """True when ``path`` resolves under ``root`` (defensive guard)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _rewrite_jsonl(
    path: Path,
    keep_row: Callable[[dict[str, Any]], bool],
    report: CleanupReport,
) -> int:
    """Drop rows from a JSONL file, rewriting it in place. Returns removed count."""
    if not path.exists():
        return 0
    kept: list[str] = []
    removed = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        report.add_error(f"failed to read {path}: {exc}")
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
    try:
        if kept:
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
            report.add_removed(path, kind="jsonl_emptied")
    except OSError as exc:
        report.add_error(f"failed to rewrite {path}: {exc}")
    if removed:
        report.add_jsonl_rows(removed)
    return removed


def _task_id_matches_run(task_id: Any, run_id: str, attempt_task_ids: set[str]) -> bool:
    value = str(task_id or "")
    if not value:
        return False
    # Match the parent run id, every "<run>__<suffix>" descendant
    # (attempts, remediation rounds, harness candidates, stages), and any
    # explicitly-tracked attempt task id from the parent web run record.
    if value == run_id or value.startswith(f"{run_id}__"):
        return True
    if value in attempt_task_ids:
        return True
    for parent in attempt_task_ids:
        parent_str = str(parent or "")
        if parent_str and value.startswith(f"{parent_str}__"):
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def wipe_run_artifacts(
    repo_root: Path,
    ws_id: str | None,
    run_id: str,
    attempt_task_ids: Iterable[str] | None = None,
    *,
    candidate_manifest_path: str | None = None,
    candidate_id: str | None = None,
    candidate_run_ids: Iterable[str] | None = None,
) -> CleanupReport:
    """Hard-delete every disk artifact tied to a single run.

    Idempotent: safe to call multiple times.  Returns a structured report
    so the HTTP layer can surface what was actually removed.
    """
    report = CleanupReport()
    repo_root = repo_root.resolve()
    workspaces_root = repo_root / "workspaces"
    umbrella_root = repo_root / ".umbrella"
    task_ids: set[str] = {run_id}
    for value in attempt_task_ids or ():
        text = str(value or "").strip()
        if text:
            task_ids.add(text)

    workspace_memory: Path | None = None
    if ws_id:
        workspace_memory = workspaces_root / ws_id / ".memory"

    # --- 1) Workspace .memory artifacts (per-task) ------------------------
    if workspace_memory is not None and workspace_memory.exists():
        task_results_dir = workspace_memory / "drive" / "task_results"
        for task_id in task_ids:
            for path in (
                task_results_dir / f"{task_id}.json",
                task_results_dir / f"{task_id}.verification.md",
            ):
                if path.exists():
                    _safe_remove(path, report, kind="task_result")
        # ``run_quality.json`` is the per-run quality telemetry blob
        # (overwritten each run). After deleting the run we should drop
        # it so the next list/dashboard read does not surface stale
        # numbers attributed to the removed run.
        run_quality_path = task_results_dir / "run_quality.json"
        if run_quality_path.exists():
            _safe_remove(run_quality_path, report, kind="run_quality_telemetry")
        # Sweep any leftover task_result artefacts that match the
        # parent run id by glob (e.g. stages without explicit ids).
        if task_results_dir.exists():
            try:
                for path in task_results_dir.glob(f"{run_id}*"):
                    if not _is_inside(path, workspace_memory):
                        continue
                    if path.is_file():
                        _safe_remove(path, report, kind="task_result_glob")
            except OSError as exc:
                report.add_error(f"glob failed in {task_results_dir}: {exc}")

        task_plans_dir = workspace_memory / "drive" / "task_plans"
        for task_id in task_ids:
            plan_path = task_plans_dir / f"{task_id}.json"
            if plan_path.exists():
                _safe_remove(plan_path, report, kind="task_plan")

        # JSONL row pruning (events / tools / round_io / ideas / lessons)
        for path in (
            workspace_memory / "drive" / "logs" / "events.jsonl",
            workspace_memory / "drive" / "logs" / "round_io.jsonl",
            workspace_memory / "drive" / "logs" / "tools.jsonl",
            workspace_memory / "ideas.jsonl",
            workspace_memory / "lessons.jsonl",
        ):
            _rewrite_jsonl(
                path,
                lambda row, ids=task_ids: (
                    not _task_id_matches_run(
                        row.get("task_id") or row.get("run_id"), run_id, ids
                    )
                ),
                report,
            )

        # Verification context lives under the workspace .memory tree.  It
        # is per-run state that becomes stale immediately after the run is
        # removed, so always wipe it.
        for path in (
            workspace_memory / "drive" / "memory" / "verification_failure_context.md",
            workspace_memory / "drive" / "state" / "verification_failure_context.json",
        ):
            if path.exists():
                _safe_remove(path, report, kind="verification_context")

        # Stop-request files for this workspace.
        stop_paths = [
            workspace_memory / "drive" / "state" / "stop_requested.json",
        ]
        for path in stop_paths:
            if path.exists():
                _safe_remove(path, report, kind="stop_request")

    # --- 2) System .umbrella/* artifacts -----------------------------------
    if umbrella_root.exists():
        for path in (
            umbrella_root / "launcher" / "logs" / "events.jsonl",
            umbrella_root / "ouroboros_drive" / "logs" / "events.jsonl",
            umbrella_root / "ouroboros_drive" / "logs" / "round_io.jsonl",
            umbrella_root / "ouroboros_drive" / "logs" / "tools.jsonl",
            umbrella_root / "ouroboros_drive" / "memory" / "ideas.jsonl",
            umbrella_root / "memory" / "signals.jsonl",
            umbrella_root / "memory" / "gaps.jsonl",
        ):
            _rewrite_jsonl(
                path,
                lambda row, ids=task_ids: (
                    not _task_id_matches_run(
                        row.get("task_id") or row.get("run_id"), run_id, ids
                    )
                ),
                report,
            )

        for task_id in task_ids:
            for path in (
                umbrella_root / "ouroboros_drive" / "task_results" / f"{task_id}.json",
                umbrella_root
                / "ouroboros_drive"
                / "task_results"
                / f"{task_id}.verification.md",
                umbrella_root / "ouroboros_drive" / "task_plans" / f"{task_id}.json",
            ):
                if path.exists():
                    _safe_remove(path, report, kind="umbrella_task_artifact")

        # Per-task glob directories.
        for task_id in task_ids:
            for directory in (
                umbrella_root / "task_updates",
                umbrella_root / "escalations",
                umbrella_root / "traces",
                umbrella_root / "checkpoints",
                umbrella_root / "web" / "launcher_logs",
                umbrella_root / "launcher" / "logs",
            ):
                if not directory.exists():
                    continue
                try:
                    for path in directory.glob(f"{task_id}*"):
                        if not _is_inside(path, umbrella_root):
                            continue
                        _safe_remove(path, report, kind="umbrella_glob")
                except OSError as exc:
                    report.add_error(f"glob failed in {directory}: {exc}")

        # Sandbox sessions keyed by task id.
        sandbox_dir = umbrella_root / "sandbox_sessions"
        if sandbox_dir.exists():
            for path in sandbox_dir.glob("*.json"):
                try:
                    payload = json.loads(
                        path.read_text(encoding="utf-8", errors="replace")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict) and _task_id_matches_run(
                    payload.get("task_id"), run_id, task_ids
                ):
                    _safe_remove(path, report, kind="sandbox_session")

        # Stop-request matching this run.
        for path in (
            umbrella_root / "launcher" / "stop_requested.json",
            umbrella_root / "ouroboros_drive" / "state" / "stop_requested.json",
        ):
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict) and _task_id_matches_run(
                payload.get("run_id") or payload.get("task_id"),
                run_id,
                task_ids,
            ):
                _safe_remove(path, report, kind="stop_request")

        # Per-run launcher_logs glob (covers harness candidate ids too).
        for sub in (
            umbrella_root / "web" / "launcher_logs",
            umbrella_root / "launcher" / "logs",
        ):
            if not sub.exists():
                continue
            for path in sub.glob(f"{run_id}*"):
                if _is_inside(path, umbrella_root):
                    _safe_remove(path, report, kind="launcher_log")

        # Backups created by update_workspace_seed for this workspace.
        backups_dir = umbrella_root / "backups"
        if ws_id and backups_dir.exists():
            for path in backups_dir.glob(f"seed_backup_*{ws_id}*"):
                if _is_inside(path, umbrella_root):
                    _safe_remove(path, report, kind="seed_backup")

        # Meta-harness candidate directory (single).
        if candidate_manifest_path:
            manifest_path = Path(str(candidate_manifest_path))
            if not manifest_path.is_absolute():
                manifest_path = repo_root / manifest_path
            candidate_dir = manifest_path.parent
            if candidate_dir.exists() and _is_inside(candidate_dir, umbrella_root):
                _safe_remove(candidate_dir, report, kind="harness_candidate_dir")
        elif candidate_id:
            candidate_dir = (
                umbrella_root
                / "meta_harness"
                / "experiments"
                / "_default"
                / "candidates"
                / candidate_id
            )
            if candidate_dir.exists() and _is_inside(candidate_dir, umbrella_root):
                _safe_remove(candidate_dir, report, kind="harness_candidate_dir")

        # Harness orchestrator candidates (one dir per harness run).
        harness_dir = umbrella_root / "harness" / "candidates" / run_id
        if harness_dir.exists() and _is_inside(harness_dir, umbrella_root):
            _safe_remove(harness_dir, report, kind="harness_run_dir")

        # Legacy/alternate meta_harness candidate layout:
        # ``.umbrella/meta_harness/candidates/<run_id>*`` (web harness parent id).
        meta_candidates = umbrella_root / "meta_harness" / "candidates"
        if meta_candidates.exists():
            try:
                for path in meta_candidates.glob(f"{run_id}*"):
                    if _is_inside(path, umbrella_root):
                        _safe_remove(path, report, kind="meta_harness_candidate")
            except OSError as exc:
                report.add_error(f"glob failed in {meta_candidates}: {exc}")

        # ``.umbrella/meta_harness/workspaces/<run_id>*`` — per-candidate
        # ephemeral workspace clones used by harness runs. Catches both
        # the parent run id and harness child ids like
        # ``<parent>__s1__c1``.
        meta_ws_dir = umbrella_root / "meta_harness" / "workspaces"
        if meta_ws_dir.exists():
            try:
                for path in meta_ws_dir.glob(f"{run_id}*"):
                    if _is_inside(path, umbrella_root):
                        _safe_remove(path, report, kind="meta_harness_workspace")
            except OSError as exc:
                report.add_error(f"glob failed in {meta_ws_dir}: {exc}")

    # --- 3) Recurse into harness child runs ------------------------------
    for child_run_id in candidate_run_ids or ():
        text = str(child_run_id or "").strip()
        if not text or text == run_id:
            continue
        child_report = wipe_run_artifacts(repo_root, ws_id, text)
        report.merge(child_report)

    return report


def wipe_thread_artifacts(
    repo_root: Path,
    ws_id: str | None,
    thread_id: str,
    message_run_ids: Iterable[str] | None = None,
) -> CleanupReport:
    """Hard-delete artifacts tied to a chat thread.

    The web bridge calls this in addition to its existing run-deletion
    flow.  Run-level cleanup is kept on the caller (so it can preserve
    the "detached" semantics for runs whose worker is still alive); we
    only reach the per-thread artifacts here.  Today there are no
    thread-only files outside ``messages_*.json`` (handled by the
    caller), so the report stays minimal but the function exists for
    symmetry and future expansion.
    """
    report = CleanupReport()
    # Place-holder: future per-thread artifacts (thread-scoped knowledge,
    # exported transcripts, etc.) go here.  Run-level wipes happen in
    # ``wipe_run_artifacts`` and the messages JSON is removed by the
    # caller.
    if not thread_id:
        report.add_error("empty thread_id")
    return report


def wipe_workspace_artifacts(repo_root: Path, ws_id: str) -> CleanupReport:
    """Hard-delete every artifact tied to a workspace.

    This is the most aggressive wipe: workspace folder itself, all
    ``.umbrella`` traces (backups, meta_harness candidates, signals/gaps
    rows, launcher logs).
    """
    report = CleanupReport()
    if not ws_id:
        report.add_error("empty ws_id")
        return report

    repo_root = repo_root.resolve()
    workspaces_root = repo_root / "workspaces"
    umbrella_root = repo_root / ".umbrella"

    workspace_dir = workspaces_root / ws_id
    if workspace_dir.exists() and _is_inside(workspace_dir, repo_root):
        _safe_remove(workspace_dir, report, kind="workspace_dir")
    elif workspace_dir.exists():
        report.add_kept(workspace_dir, reason="workspace_dir not inside repo_root")

    if not umbrella_root.exists():
        return report

    # Nested ``.umbrella/<component>/<ws_id>/`` trees (signals cache, harness, etc.).
    try:
        for child in umbrella_root.iterdir():
            if not child.is_dir():
                continue
            nested = child / ws_id
            if nested.exists() and _is_inside(nested, umbrella_root):
                _safe_remove(nested, report, kind="umbrella_nested_ws")
    except OSError as exc:
        report.add_error(
            f"failed to enumerate .umbrella for nested workspace dirs: {exc}"
        )

    # Filter system-wide JSONL stores by workspace.
    for path in (
        umbrella_root / "memory" / "signals.jsonl",
        umbrella_root / "memory" / "gaps.jsonl",
    ):
        _rewrite_jsonl(
            path,
            lambda row, target=ws_id: str(row.get("workspace_id") or "") != target,
            report,
        )

    # Backups created by update_workspace_seed for this workspace.
    backups_dir = umbrella_root / "backups"
    if backups_dir.exists():
        for path in backups_dir.glob(f"seed_backup_*{ws_id}*"):
            if _is_inside(path, umbrella_root):
                _safe_remove(path, report, kind="seed_backup")

    # Meta-harness experiments folder for this workspace.
    for experiments_dir in (
        umbrella_root / "meta_harness" / "experiments",
        umbrella_root / "harness" / "experiments",
    ):
        if not experiments_dir.exists():
            continue
        for path in experiments_dir.glob(f"*{ws_id}*"):
            if _is_inside(path, umbrella_root):
                _safe_remove(path, report, kind="harness_experiment")

    # Stop-request payloads scoped to this workspace.
    for path in (
        umbrella_root / "launcher" / "stop_requested.json",
        umbrella_root / "ouroboros_drive" / "state" / "stop_requested.json",
    ):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if (
            isinstance(payload, dict)
            and str(payload.get("workspace_id") or "") == ws_id
        ):
            _safe_remove(path, report, kind="stop_request")

    return report


# ---------------------------------------------------------------------------
# Memory node deletion: maps synthetic graph IDs back to disk artifacts.
# ---------------------------------------------------------------------------

# Synthetic node IDs are produced by `WebBridgeApp.list_memory_nodes` via
# ``safe_id(prefix, value)``: "<prefix>:<value>".  We support deleting a
# small number of node types where the underlying disk artifact is
# unambiguous.  Anything else returns ``ok=False, reason=node_type_not_deletable``.

_DELETABLE_PREFIXES: dict[str, str] = {
    "knowledge": "drive_knowledge_md",
    "scratchpad": "drive_scratchpad",
    "result": "task_result",
    "task": "task_artifacts",
    "log-block": "log_jsonl_truncate",
}


def wipe_memory_node(
    repo_root: Path,
    ws_id: str | None,
    node_id: str,
    *,
    node_lookup: Callable[[str], dict[str, Any] | None] | None = None,
) -> CleanupReport:
    """Best-effort hard-delete for a synthetic memory-graph node.

    We only act on nodes that map cleanly to one disk artifact.  For the
    rest we return a kept-only report with a structured ``reason`` so the
    UI can explain that nothing was removed.
    """
    report = CleanupReport()
    if not node_id:
        report.add_error("empty node_id")
        return report

    prefix, _, raw_value = node_id.partition(":")
    if not prefix or not raw_value:
        report.add_kept(node_id, reason="unrecognised id format")
        report.add_error("node_type_not_deletable")
        return report

    kind = _DELETABLE_PREFIXES.get(prefix)
    if kind is None:
        report.add_kept(node_id, reason=f"prefix {prefix!r} is read-only in graph")
        report.add_error("node_type_not_deletable")
        return report

    repo_root = repo_root.resolve()

    if prefix == "knowledge":
        if not ws_id:
            report.add_error("workspace_id required for knowledge node deletion")
            return report
        path = (
            repo_root
            / "workspaces"
            / ws_id
            / ".memory"
            / "drive"
            / "memory"
            / "knowledge"
            / f"{raw_value}.md"
        )
        if path.exists():
            _safe_remove(path, report, kind=kind)
        else:
            report.add_kept(path, reason="missing on disk")
        return report

    if prefix == "scratchpad":
        if not ws_id:
            report.add_error("workspace_id required for scratchpad node deletion")
            return report
        path = (
            repo_root
            / "workspaces"
            / ws_id
            / ".memory"
            / "drive"
            / "memory"
            / raw_value
        )
        if path.exists():
            _safe_remove(path, report, kind=kind)
        else:
            report.add_kept(path, reason="missing on disk")
        return report

    if prefix == "result":
        # raw_value is the task_id.  Cascade through wipe_run_artifacts
        # so we get the matching task_results JSON, verification md, and
        # JSONL rows in one shot.
        sub_report = wipe_run_artifacts(repo_root, ws_id, raw_value, [raw_value])
        report.merge(sub_report)
        return report

    if prefix == "task":
        sub_report = wipe_run_artifacts(repo_root, ws_id, raw_value, [raw_value])
        report.merge(sub_report)
        return report

    if prefix == "log-block":
        # raw_value is the relative path to the JSONL file.  Truncate it
        # rather than deleting, so the runtime can keep appending.
        path = repo_root / raw_value.replace("\\", "/")
        if path.exists():
            try:
                path.write_text("", encoding="utf-8")
                report.add_removed(path, kind=kind)
            except OSError as exc:
                report.add_error(f"failed to truncate {path}: {exc}")
                report.add_kept(path, reason=str(exc))
        else:
            report.add_kept(path, reason="missing on disk")
        return report

    # Should be unreachable.
    report.add_error("unhandled deletable prefix")
    return report
