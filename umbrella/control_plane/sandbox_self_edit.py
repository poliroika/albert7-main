"""
Sandbox self-edit: managed code modifications without automatic rollback.

When the agent detects a capability gap mid-task it can patch its own code
(ouroboros/, umbrella/) to unblock itself. Historically this module always
created a git/copy snapshot and restored it at task end. That behavior made
live debugging painful because working fixes disappeared after the run.

The supported default is now a no-rollback session: the session still exists
so policy checks and ``sandbox_self_edit`` can work, but task-end cleanup does
not reset, checkout, clean, stash-pop, or otherwise undo the agent's edits.

Lifecycle managed by the launcher:
    1. ``enter_sandbox(repo_root)``  — create a session (normally no snapshot)
    2. agent runs, may call ``record_sandbox_edit(...)``
    3. ``exit_sandbox(session)``     — mark the session exited
"""

import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_STASH_MSG_PREFIX = "umbrella-sandbox-"
_COPY_SNAPSHOT_SURFACES = ("umbrella", "ouroboros")


@dataclass
class SandboxSession:
    """Tracks one sandbox self-edit session tied to a single task."""

    session_id: str
    task_id: str
    repo_root: Path
    snapshot_method: str  # git_stash | git_branch | copy
    stash_ref: str | None = None
    snapshot_dir: str | None = None
    original_branch: str | None = None
    sandbox_branch: str | None = None
    baseline_sha: str = ""
    workspace_id: str = ""
    owner_pid: int | None = None
    preserved_commits: list[str] = field(default_factory=list)
    cherry_pick_failures: list[str] = field(default_factory=list)
    edited_files: list[str] = field(default_factory=list)
    entered_at: float = 0.0
    exited_at: float | None = None
    rollback_ok: bool | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "snapshot_method": self.snapshot_method,
            "stash_ref": self.stash_ref,
            "snapshot_dir": self.snapshot_dir,
            "original_branch": self.original_branch,
            "sandbox_branch": self.sandbox_branch,
            "baseline_sha": self.baseline_sha,
            "workspace_id": self.workspace_id,
            "owner_pid": self.owner_pid,
            "preserved_commits": self.preserved_commits,
            "cherry_pick_failures": self.cherry_pick_failures,
            "edited_files": self.edited_files,
            "entered_at": self.entered_at,
            "exited_at": self.exited_at,
            "rollback_ok": self.rollback_ok,
            "error": self.error,
        }


def _git(
    repo_root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=check,
    )


def _current_branch(repo_root: Path) -> str:
    result = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def _has_changes(repo_root: Path) -> bool:
    result = _git(repo_root, "status", "--porcelain")
    return bool(result.stdout.strip())


def _has_changes_under(repo_root: Path, *paths: str) -> bool:
    if not paths:
        return _has_changes(repo_root)
    result = _git(repo_root, "status", "--porcelain", "--", *paths, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def _snapshot_dir(repo_root: Path, session_id: str) -> Path:
    return repo_root / ".umbrella" / "sandbox_snapshots" / session_id


def _remove_path(target: Path) -> None:
    if not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink(missing_ok=True)


def _prepare_copy_snapshot(session: SandboxSession) -> None:
    snapshot_root = _snapshot_dir(session.repo_root, session.session_id)
    _remove_path(snapshot_root)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    for rel in _COPY_SNAPSHOT_SURFACES:
        source = session.repo_root / rel
        if source.exists():
            shutil.copytree(source, snapshot_root / rel)
    session.snapshot_dir = str(snapshot_root)
    log.info("Sandbox entered via copy snapshot: %s", snapshot_root)


def _restore_copy_snapshot(session: SandboxSession) -> None:
    snapshot_root = Path(
        session.snapshot_dir or _snapshot_dir(session.repo_root, session.session_id)
    )
    for rel in _COPY_SNAPSHOT_SURFACES:
        target = session.repo_root / rel
        saved = snapshot_root / rel
        _remove_path(target)
        if saved.exists():
            shutil.copytree(saved, target)
    _remove_path(snapshot_root)


def _pid_is_running(pid: int | None) -> bool:
    try:
        resolved = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if resolved <= 0:
        return False
    if resolved == os.getpid():
        return True
    if os.name != "nt":
        try:
            os.kill(resolved, 0)
        except OSError:
            return False
        return True
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {resolved}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except Exception:
        log.debug(
            "Could not inspect Windows PID %s; treating as live",
            resolved,
            exc_info=True,
        )
        return True
    out = (result.stdout or "").strip()
    return bool(out) and not out.startswith("INFO:")


def resolve_snapshot_method(repo_root: Path, snapshot_method: str) -> str:
    """Choose the sandbox strategy for this repo state.

    ``git_stash`` hides every uncommitted change from the live run. That is
    useful for pure agent self-edits, but it breaks manual harness debugging:
    the operator's local fixes under ``umbrella/`` or ``ouroboros/`` disappear
    from the process being tested. When those surfaces are already dirty, use
    a lightweight copy snapshot instead so the run sees the local fixes while
    sandbox rollback still restores the original files on exit.
    """
    repo_root = repo_root.resolve()
    if snapshot_method != "git_stash":
        return snapshot_method
    try:
        if _has_changes_under(
            repo_root, *(f"{rel}/" for rel in _COPY_SNAPSHOT_SURFACES)
        ):
            log.info(
                "Sandbox: dirty agent surfaces detected; switching snapshot method "
                "from git_stash to copy so local harness fixes remain visible."
            )
            return "copy"
    except Exception:
        log.debug(
            "Snapshot-method auto-resolution failed; keeping requested method",
            exc_info=True,
        )
    return snapshot_method


def _find_stash_index(repo_root: Path, stash_ref: str) -> str | None:
    """Return the ``stash@{N}`` index for a stash whose message contains ``stash_ref``.

    Returns ``None`` if no such stash exists (or git fails).
    """
    try:
        result = _git(repo_root, "stash", "list", check=False)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if stash_ref in line:
            return line.split(":", 1)[0].strip()
    return None


def _list_sandbox_stashes(repo_root: Path) -> list[tuple[str, str]]:
    """Return list of ``(stash_index, stash_message)`` for umbrella-sandbox stashes."""
    try:
        result = _git(repo_root, "stash", "list", check=False)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    found: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if _STASH_MSG_PREFIX in line:
            idx, _, rest = line.partition(":")
            found.append((idx.strip(), rest.strip()))
    return found


# =========================================================================
# Public API
# =========================================================================


def enter_sandbox(
    repo_root: Path,
    task_id: str,
    snapshot_method: str = "none",
    workspace_id: str = "",
) -> SandboxSession:
    """Create a self-edit session.

    Supports two strategies:
    - ``none``: no rollback/snapshot; edits persist.
    - ``git_stash``: stash uncommitted changes, remember HEAD.
    - ``git_branch``: create a throwaway branch for sandbox edits.

    Before snapshotting, attempt to recover any orphaned sandbox stashes left
    over from a previous run where ``exit_sandbox`` failed to pop cleanly. This
    keeps user changes from silently piling up in the stash list.
    """
    repo_root = repo_root.resolve()

    # Rollback has been intentionally disabled for live Ouroboros runs. Always
    # return a no-op session, regardless of the requested snapshot method, so
    # agent self-edits remain visible after task exit.
    session_id = f"sandbox_{uuid.uuid4().hex[:8]}"
    log.info(
        "Sandbox entered in no-rollback mode (id=%s, repo=%s, requested_method=%s)",
        session_id,
        repo_root,
        snapshot_method,
    )
    session = SandboxSession(
        session_id=session_id,
        task_id=task_id,
        repo_root=repo_root,
        snapshot_method="none",
        workspace_id=str(workspace_id or "").strip(),
        owner_pid=os.getpid(),
        entered_at=time.time(),
    )
    try:
        _persist_session(repo_root, session)
    except Exception:
        log.debug("Could not persist no-op sandbox session (non-fatal)", exc_info=True)
    return session

    try:
        recovered = recover_orphan_sandbox_stashes(repo_root)
        if recovered:
            log.warning(
                "Sandbox: recovered %d orphan sandbox stash(es) before entering: %s",
                len(recovered),
                ", ".join(recovered),
            )
    except Exception:
        log.debug("Orphan-stash recovery failed (non-fatal)", exc_info=True)

    snapshot_method = resolve_snapshot_method(repo_root, snapshot_method)
    session_id = f"sandbox_{uuid.uuid4().hex[:8]}"
    session = SandboxSession(
        session_id=session_id,
        task_id=task_id,
        repo_root=repo_root,
        snapshot_method=snapshot_method,
        workspace_id=str(workspace_id or "").strip(),
        owner_pid=os.getpid(),
        entered_at=time.time(),
    )

    session.original_branch = _current_branch(repo_root)

    if snapshot_method == "git_branch":
        sandbox_branch = f"sandbox/{session_id}"
        _git(repo_root, "checkout", "-b", sandbox_branch)
        session.sandbox_branch = sandbox_branch
        log.info("Sandbox entered via branch: %s", sandbox_branch)
    elif snapshot_method == "copy":
        _prepare_copy_snapshot(session)
    else:
        stash_msg = f"{_STASH_MSG_PREFIX}{session_id}"
        if _has_changes(repo_root):
            _git(repo_root, "stash", "push", "-m", stash_msg, "--include-untracked")
            session.stash_ref = stash_msg
            log.info("Sandbox entered: stashed pre-existing changes as %s", stash_msg)
        else:
            log.info("Sandbox entered: repo clean, no stash needed")

    _persist_session(repo_root, session)
    return session


def record_sandbox_edit(session: SandboxSession, file_path: str) -> None:
    """Register a file that was modified during the sandbox session."""
    normalized = str(file_path).replace("\\", "/")
    if normalized not in session.edited_files:
        session.edited_files.append(normalized)
    _persist_session(session.repo_root, session)


# Pathspecs excluded from candidate snapshots: per-workspace runtime
# artifacts (MemPalace stores, bytecode caches, runtime data dumps) that
# are recreated on demand and should never become part of an "agent
# changes" diff. Anything genuinely produced by the agent (source files,
# tests, README, etc.) is still captured because these patterns only
# match transient state.
_SNAPSHOT_EXCLUDES = (
    ":(exclude,glob)workspaces/*/.memory",
    ":(exclude,glob)workspaces/*/.memory/**",
    ":(exclude,glob)workspaces/*/__pycache__",
    ":(exclude,glob)workspaces/*/__pycache__/**",
    ":(exclude,glob)workspaces/**/__pycache__",
    ":(exclude,glob)workspaces/**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)workspaces/*/.stdout.txt",
    ":(exclude,glob)workspaces/*/.stderr.txt",
)


def _add_filtered(repo_root: Path) -> None:
    """``git add -A`` with workspace runtime artifacts excluded.

    The pathspec exclusions keep ``.memory/`` (MemPalace), ``__pycache__``,
    ``*.pyc``, and helper stdout/stderr dumps out of the candidate
    snapshot.  Everything else still flows in via the leading ``.``.
    """
    _git(repo_root, "add", "-A", ".", *_SNAPSHOT_EXCLUDES, check=False)


def capture_candidate_diff(session: SandboxSession) -> str:
    """Capture the full unified diff of the candidate's work.

    This function is intentionally read-only. It must not create
    ``candidate-snapshot`` commits because rollback is disabled and such
    commits would pollute the user's branch.
    Returns an empty string on any error.
    """
    repo_root = session.repo_root
    try:
        parts: list[str] = []
        baseline = getattr(session, "baseline_sha", "") or ""
        if baseline:
            r = _git(repo_root, "diff", baseline, "HEAD", check=False)
            if r.stdout:
                parts.append(r.stdout)
        r = _git(repo_root, "diff", check=False)
        if r.stdout:
            parts.append(r.stdout)
        r = _git(repo_root, "diff", "--cached", check=False)
        if r.stdout:
            parts.append(r.stdout)
        diff = "\n".join(parts)

        if len(diff) > 200_000:
            diff = diff[:200_000] + "\n\n[truncated at 200KB]\n"
        return diff
    except Exception:
        log.debug("capture_candidate_diff failed (non-fatal)", exc_info=True)
        return ""


def capture_changed_files(session: SandboxSession) -> list[str]:
    """Collect all file paths changed by the candidate relative to baseline.

    Covers committed, staged, unstaged, and untracked files.
    """
    repo_root = session.repo_root
    paths: set[str] = set()
    try:
        if session.snapshot_method == "git_branch" and session.sandbox_branch:
            r = _git(
                repo_root,
                "diff",
                "--name-only",
                session.original_branch or "main",
                session.sandbox_branch,
                check=False,
            )
            if r.returncode == 0:
                paths.update(l.strip() for l in r.stdout.splitlines() if l.strip())
        else:
            baseline = getattr(session, "baseline_sha", "") or ""
            if baseline:
                r = _git(
                    repo_root, "diff", "--name-only", baseline, "HEAD", check=False
                )
                if r.returncode == 0:
                    paths.update(l.strip() for l in r.stdout.splitlines() if l.strip())
            r = _git(repo_root, "diff", "--name-only", check=False)
            if r.returncode == 0:
                paths.update(l.strip() for l in r.stdout.splitlines() if l.strip())
            r = _git(repo_root, "diff", "--name-only", "--cached", check=False)
            if r.returncode == 0:
                paths.update(l.strip() for l in r.stdout.splitlines() if l.strip())

        r = _git(repo_root, "ls-files", "--others", "--exclude-standard", check=False)
        if r.returncode == 0:
            paths.update(l.strip() for l in r.stdout.splitlines() if l.strip())
    except Exception:
        log.debug("capture_changed_files failed (non-fatal)", exc_info=True)
    return sorted(paths)


def exit_sandbox(session: SandboxSession) -> SandboxSession:
    """Close a self-edit session without rolling back edits.

    Rollback is intentionally disabled: no reset, checkout, clean, branch
    deletion, copy restore, or stash pop is performed here.
    """
    repo_root = session.repo_root
    session.exited_at = time.time()
    session.rollback_ok = True
    try:
        _persist_session(repo_root, session)
    except Exception:
        log.debug(
            "Could not persist no-rollback sandbox exit (non-fatal)", exc_info=True
        )
    return session


def get_active_session(repo_root: Path) -> SandboxSession | None:
    """Load the active (un-exited) sandbox session, if any."""
    session_dir = repo_root / ".umbrella" / "sandbox_sessions"
    if not session_dir.exists():
        return None
    for path in sorted(session_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("exited_at") is None:
                return _session_from_dict(data, repo_root)
        except Exception:
            continue
    return None


def _active_sandbox_stash_refs(repo_root: Path) -> set[str]:
    """Return stash refs belonging to sandbox sessions that are still active.

    A sandbox session is considered active if its on-disk JSON has
    ``exited_at is None``. Their stashes must NOT be touched by recovery,
    because another Umbrella process (possibly in another terminal / worktree)
    is still using them.
    """
    active: set[str] = set()
    session_dir = repo_root / ".umbrella" / "sandbox_sessions"
    if not session_dir.exists():
        return active
    for path in session_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if data.get("exited_at") is not None:
            continue
        owner_pid = data.get("owner_pid")
        if owner_pid not in (None, "") and not _pid_is_running(owner_pid):
            log.warning(
                "Sandbox recovery: session %s is still marked active but owner pid %s "
                "is gone; treating its stash as orphanable.",
                data.get("session_id"),
                owner_pid,
            )
            continue
        ref = data.get("stash_ref")
        if isinstance(ref, str) and ref:
            active.add(ref)
    return active


def recover_orphan_sandbox_stashes(repo_root: Path) -> list[str]:
    """Attempt to apply any orphan ``umbrella-sandbox-*`` stashes to the worktree.

    An orphan stash is one left over from a failed ``exit_sandbox`` (e.g.
    because ``git stash pop`` hit a conflict and the stash was silently
    retained). This function:

    - Inspects ``git stash list`` for entries whose message contains the
      sandbox prefix.
    - Skips any stash whose ``stash_ref`` matches a sandbox session that is
      still active (``exited_at is None`` in
      ``.umbrella/sandbox_sessions/*.json``). Those stashes belong to a live
      sibling process and must never be popped or applied by recovery.
    - For each remaining (truly orphan) stash, tries ``git stash apply``
      (never ``pop``) if the worktree is clean, so the user does not lose
      work. If apply fails or the worktree is dirty, the stash is left in
      place and a warning is logged instead.
    - Never drops stashes automatically — the user should inspect and drop
      them manually once they are sure the data is safe.

    Returns the list of stash messages that were successfully applied (and
    therefore can be dropped by the caller / the user).
    """
    repo_root = repo_root.resolve()
    orphans = _list_sandbox_stashes(repo_root)
    if not orphans:
        return []

    active_refs = _active_sandbox_stash_refs(repo_root)

    applied: list[str] = []
    for stash_index, stash_msg in orphans:
        if stash_msg in active_refs:
            log.info(
                "Sandbox recovery: stash %s (%s) belongs to a live sandbox "
                "session, skipping.",
                stash_index,
                stash_msg,
            )
            continue

        # ``stash apply`` would silently overwrite local changes; only do it
        # on a clean worktree.
        if _has_changes(repo_root):
            log.warning(
                "Sandbox recovery: worktree dirty, leaving orphan stash %s (%s) "
                "untouched. Run `git stash list` / `git stash apply %s` manually.",
                stash_index,
                stash_msg,
                stash_index,
            )
            continue

        apply_result = _git(repo_root, "stash", "apply", stash_index, check=False)
        if apply_result.returncode != 0:
            log.warning(
                "Sandbox recovery: `git stash apply %s` (%s) failed rc=%d; "
                "leaving stash in place. stderr=%s",
                stash_index,
                stash_msg,
                apply_result.returncode,
                (apply_result.stderr or "").strip()[:500],
            )
            continue

        # Double-check that apply did not leave merge conflicts behind.
        diff_check = _git(repo_root, "diff", "--check", check=False)
        if diff_check.returncode != 0:
            log.warning(
                "Sandbox recovery: `git stash apply %s` (%s) left conflict "
                "markers; leaving stash in place. Resolve manually and drop "
                "with `git stash drop %s`.",
                stash_index,
                stash_msg,
                stash_index,
            )
            continue

        log.warning(
            "Sandbox recovery: applied orphan stash %s (%s). "
            "Inspect `git status` and drop with `git stash drop %s` once verified.",
            stash_index,
            stash_msg,
            stash_index,
        )
        applied.append(stash_msg)

    return applied


# =========================================================================
# Persistence helpers
# =========================================================================


def _persist_session(repo_root: Path, session: SandboxSession) -> None:
    session_dir = repo_root / ".umbrella" / "sandbox_sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / f"{session.session_id}.json"
    path.write_text(
        json.dumps(session.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _session_from_dict(data: dict[str, Any], repo_root: Path) -> SandboxSession:
    return SandboxSession(
        session_id=data["session_id"],
        task_id=data["task_id"],
        repo_root=repo_root,
        snapshot_method=data.get("snapshot_method", "git_stash"),
        stash_ref=data.get("stash_ref"),
        snapshot_dir=data.get("snapshot_dir"),
        original_branch=data.get("original_branch"),
        sandbox_branch=data.get("sandbox_branch"),
        baseline_sha=data.get("baseline_sha", ""),
        workspace_id=data.get("workspace_id", ""),
        owner_pid=data.get("owner_pid"),
        preserved_commits=list(data.get("preserved_commits", [])),
        cherry_pick_failures=list(data.get("cherry_pick_failures", [])),
        edited_files=list(data.get("edited_files", [])),
        entered_at=float(data.get("entered_at", 0)),
        exited_at=data.get("exited_at"),
        rollback_ok=data.get("rollback_ok"),
        error=data.get("error", ""),
    )
