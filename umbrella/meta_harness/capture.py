"""Candidate capture after each Ouroboros run.

Saves a full bundle: manifest, execution events, prompt/policy/memory
snapshots, diffs, and a controlled source snapshot of harness files.
"""

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from umbrella.meta_harness.models import (
    CandidateManifest,
    CandidateStatus,
    generate_candidate_id,
)
from umbrella.meta_harness.store import get_default_store

log = logging.getLogger(__name__)

HARNESS_SOURCE_DIRS = [
    "umbrella/prompts",
    "umbrella/orchestration",
    "umbrella/integration",
    "umbrella/control_plane",
    "umbrella/memory",
    "umbrella/evals",
]

HARNESS_SOURCE_FILES = [
    "ouroboros/ouroboros/tools/umbrella_tools.py",
]


_GIT_RUN_DEFAULTS = dict(
    capture_output=True, text=True, encoding="utf-8", errors="replace"
)


def _safe_git_info(repo_root: Path) -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            timeout=10,
            **_GIT_RUN_DEFAULTS,
        )
        info["sha"] = sha.stdout.strip() if sha.returncode == 0 else ""
    except Exception:
        info["sha"] = ""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root),
            timeout=10,
            **_GIT_RUN_DEFAULTS,
        )
        info["branch"] = branch.stdout.strip() if branch.returncode == 0 else ""
    except Exception:
        info["branch"] = ""
    return info


_DIFF_SIZE_LIMIT = 200_000
_DIFF_TRUNCATED_MARKER = "[truncated_diff_unsafe_for_apply]"


def _safe_worktree_diff(repo_root: Path, baseline_sha: str = "") -> str:
    """Return full unified diff of the worktree relative to *baseline_sha*.

    If *baseline_sha* is provided, includes committed changes since that SHA
    plus any staged/unstaged changes.  Otherwise falls back to plain
    ``git diff`` (unstaged only).
    """
    try:
        if baseline_sha:
            subprocess.run(
                ["git", "add", "-N", "."],
                cwd=str(repo_root),
                timeout=15,
                **_GIT_RUN_DEFAULTS,
            )
            committed = subprocess.run(
                ["git", "diff", baseline_sha, "HEAD", "--", ":/"],
                cwd=str(repo_root),
                timeout=30,
                **_GIT_RUN_DEFAULTS,
            )
            live = subprocess.run(
                ["git", "diff", "--", ":/"],
                cwd=str(repo_root),
                timeout=30,
                **_GIT_RUN_DEFAULTS,
            )
            staged = subprocess.run(
                ["git", "diff", "--cached", "--", ":/"],
                cwd=str(repo_root),
                timeout=30,
                **_GIT_RUN_DEFAULTS,
            )
            if any(r.returncode != 0 for r in (committed, live, staged)):
                return ""
            diff = "\n".join(
                part for part in (committed.stdout, live.stdout, staged.stdout) if part
            )
        else:
            result = subprocess.run(
                ["git", "diff"],
                cwd=str(repo_root),
                timeout=30,
                **_GIT_RUN_DEFAULTS,
            )
            if result.returncode != 0:
                return ""
            diff = result.stdout
        if len(diff) > _DIFF_SIZE_LIMIT:
            log.warning(
                "Worktree diff is %d bytes (>%d); marking as unsafe-for-apply.",
                len(diff),
                _DIFF_SIZE_LIMIT,
            )
            diff = (
                diff[:_DIFF_SIZE_LIMIT] + f"\n\n{_DIFF_TRUNCATED_MARKER}\n"
                "# original diff exceeded the size limit; truncated copy "
                "stored for inspection only.\n"
            )
        return diff
    except Exception:
        return ""


def _safe_read(path: Path, limit: int = 100000) -> str:
    try:
        if path.exists() and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            return text[:limit]
    except Exception:
        pass
    return ""


def _reconcile_artifacts(
    *,
    repo_root: Path,
    instance_path: Path | None,
    changed_files: list[str],
    promoted_files: list[str],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    roots = [p for p in (instance_path, repo_root) if p is not None]

    def _exists(raw: str) -> bool:
        text = str(raw or "").strip()
        if not text:
            return False
        path = Path(text)
        candidates = [path] if path.is_absolute() else [root / text for root in roots]
        return any(candidate.exists() for candidate in candidates)

    write_tools = [
        event
        for event in events
        if isinstance(event, dict)
        and str(event.get("type") or "") == "tool_call"
        and str(event.get("tool") or event.get("tool_name") or "").startswith(
            ("update_workspace", "commit_workspace", "delete_workspace")
        )
    ]
    missing_changed = [p for p in changed_files if not _exists(p)]
    missing_promoted = [p for p in promoted_files if not _exists(p)]
    status = (
        "ok" if not missing_changed and not missing_promoted else "artifact_mismatch"
    )
    return {
        "status": status,
        "missing_changed_files": missing_changed[:100],
        "missing_promoted_files": missing_promoted[:100],
        "tool_write_event_count": len(write_tools),
        "changed_file_count": len(changed_files),
        "promoted_file_count": len(promoted_files),
    }


def _snapshot_harness_sources(repo_root: Path, dest: Path) -> None:
    """Copy a controlled subset of harness source files."""
    for rel_dir in HARNESS_SOURCE_DIRS:
        src = repo_root / rel_dir
        if not src.is_dir():
            continue
        dst = dest / rel_dir
        try:
            shutil.copytree(
                src,
                dst,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".mypy_cache"),
                dirs_exist_ok=True,
            )
        except Exception:
            log.debug("Failed to snapshot %s", rel_dir, exc_info=True)

    for rel_file in HARNESS_SOURCE_FILES:
        src = repo_root / rel_file
        if not src.is_file():
            continue
        dst = dest / rel_file
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except Exception:
            log.debug("Failed to snapshot %s", rel_file, exc_info=True)


def capture_ouroboros_candidate(
    *,
    repo_root: Path,
    task_id: str,
    workspace_id: str,
    task_description: str = "",
    instance_path: Path | None = None,
    launcher_result: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    changes_made: list[str] | None = None,
    promoted_files: list[str] | None = None,
    llm_tool_invocations: int = 0,
    workspace_write_tool_calls: int = 0,
    final_message: str = "",
    run_status: str = "",
    error: str = "",
    experiment_id: str = "",
    cost_usd: float = 0.0,
    total_tokens: int = 0,
    duration_seconds: float = 0.0,
    candidate_diff: str = "",
    baseline_sha: str = "",
    verification_report: dict[str, Any] | None = None,
) -> CandidateManifest:
    """Capture a full candidate bundle after an Ouroboros run.

    When *candidate_diff* is provided (from an isolated sandbox run), it is
    stored directly instead of computing a live worktree diff.
    *baseline_sha* is recorded in the manifest for traceability.
    *verification_report*, when supplied, is stored in
    ``manifest.metadata["verification_report"]`` so Meta-Harness evaluators
    can reuse the exact runtime-gate outcome instead of re-running.
    """
    store = get_default_store(repo_root)
    git_info = _safe_git_info(repo_root)

    candidate_id = generate_candidate_id()
    exp_id = experiment_id or "_default"

    event_rows = list(events or [])
    changed_rows = list(changes_made or [])
    promoted_rows = list(promoted_files or [])
    reconciliation = _reconcile_artifacts(
        repo_root=repo_root,
        instance_path=instance_path,
        changed_files=changed_rows,
        promoted_files=promoted_rows,
        events=event_rows,
    )
    metadata: dict[str, Any] = {"artifact_reconciliation": reconciliation}
    if verification_report:
        metadata["verification_report"] = verification_report

    manifest = CandidateManifest(
        candidate_id=candidate_id,
        experiment_id=exp_id,
        task_id=task_id,
        workspace_id=workspace_id,
        task_description=task_description[:2000],
        git_sha_before=baseline_sha or git_info.get("sha", ""),
        branch=git_info.get("branch", ""),
        instance_path=str(instance_path) if instance_path else "",
        status=CandidateStatus.CAPTURED,
        run_status=run_status,
        events_count=len(event_rows),
        tool_calls=llm_tool_invocations,
        write_calls=workspace_write_tool_calls,
        changed_files=changed_rows,
        promoted_files=promoted_rows,
        cost_usd=cost_usd,
        total_tokens=total_tokens,
        duration_seconds=duration_seconds,
        final_message=final_message[:4000],
        error=error[:2000],
        finished_at=time.time(),
        metadata=metadata,
    )

    cand_dir = store.save_candidate(manifest)

    # Execution events
    if events:
        store.save_execution_events(exp_id, candidate_id, events)

    # Task result from drive
    drive_task_result = (
        repo_root / ".umbrella" / "ouroboros_drive" / "task_results" / f"{task_id}.json"
    )
    if drive_task_result.exists():
        content = _safe_read(drive_task_result)
        if content:
            store.save_text_snapshot(
                exp_id, candidate_id, "execution", "task_result.json", content
            )

    # Prompt snapshots
    prompt_dir = repo_root / "umbrella" / "prompts"
    workspace_task_prompt = prompt_dir / "ouroboros_workspace_task.md"
    if workspace_task_prompt.exists():
        store.save_text_snapshot(
            exp_id,
            candidate_id,
            "prompt_snapshot",
            "ouroboros_workspace_task.md",
            _safe_read(workspace_task_prompt),
        )
    if task_description:
        store.save_text_snapshot(
            exp_id,
            candidate_id,
            "prompt_snapshot",
            "rendered_task_prompt.md",
            task_description[:100000],
        )

    # Policy snapshot
    policy_path = repo_root / "umbrella" / "policies" / "default_policy.yaml"
    if policy_path.exists():
        store.save_text_snapshot(
            exp_id,
            candidate_id,
            "policy_snapshot",
            "default_policy.yaml",
            _safe_read(policy_path),
        )

    # Memory input snapshots
    knowledge_dir = repo_root / ".umbrella" / "ouroboros_drive" / "memory" / "knowledge"
    if knowledge_dir.is_dir():
        for md_file in knowledge_dir.glob("*.md"):
            content = _safe_read(md_file, limit=50000)
            if content:
                store.save_text_snapshot(
                    exp_id,
                    candidate_id,
                    "memory_input",
                    md_file.name,
                    content,
                )

    # Worktree diff -- prefer pre-captured diff from isolated sandbox
    diff_text = candidate_diff or _safe_worktree_diff(
        repo_root, baseline_sha=baseline_sha
    )
    if diff_text:
        store.save_text_snapshot(
            exp_id, candidate_id, "diffs", "worktree.diff", diff_text
        )

    # Source snapshot
    source_dest = cand_dir / "source_snapshot"
    source_dest.mkdir(parents=True, exist_ok=True)
    _snapshot_harness_sources(repo_root, source_dest)

    log.info("Captured candidate %s for task %s", candidate_id, task_id)
    return manifest
