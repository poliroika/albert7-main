"""Git tools: local commit helpers, git_status, git_diff."""

import logging
import os
import pathlib
import subprocess
import time
from typing import List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, write_text, safe_relpath, run_cmd


def _workspace_only_paths(paths: list[str] | None) -> bool:
    """True when every path is under workspaces/ (Ouroboros product), not agent code."""
    if not paths:
        return False
    for p in paths:
        if not p or not str(p).strip():
            continue
        try:
            rel = safe_relpath(str(p))
        except ValueError:
            return False
        norm = rel.replace("\\", "/").strip("/")
        if not norm.startswith("workspaces/"):
            return False
    return True


log = logging.getLogger(__name__)


def _git_commit_disabled_message(tool_name: str) -> str:
    if str(os.environ.get("OUROBOROS_ALLOW_GIT_COMMIT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return ""
    return (
        f"⚠️ GIT_COMMIT_DISABLED_BY_POLICY ({tool_name}): local commits are disabled. "
        "Leave changes in the working tree for human review, or set "
        "OUROBOROS_ALLOW_GIT_COMMIT=1 to re-enable local commits."
    )


# --- Git lock ---


def _acquire_git_lock(ctx: ToolContext, timeout_sec: int = 120) -> pathlib.Path:
    lock_dir = ctx.drive_path("locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "git.lock"
    stale_sec = 600
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if lock_path.exists():
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_sec:
                    lock_path.unlink()
                    continue
            except (FileNotFoundError, OSError):
                pass
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, f"locked_at={utc_now_iso()}\n".encode())
            finally:
                os.close(fd)
            return lock_path
        except FileExistsError:
            time.sleep(0.5)
    raise TimeoutError(f"Git lock not acquired within {timeout_sec}s: {lock_path}")


def _release_git_lock(lock_path: pathlib.Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


# --- Local commit test gate ---

MAX_TEST_OUTPUT = 8000


def _run_pre_push_tests(
    ctx: ToolContext,
    *,
    commit_paths: list[str] | None = None,
) -> str | None:
    """Run pre-push tests if enabled. Returns None if tests pass, error string if they fail."""
    # Guard against ctx=None
    if ctx is None:
        log.warning("_run_pre_push_tests called with ctx=None, skipping tests")
        return None

    if os.environ.get("OUROBOROS_PRE_PUSH_TESTS", "1") != "1":
        return None

    if _workspace_only_paths(commit_paths):
        log.info(
            "Skipping pre-push tests for workspace-only commit_paths=%s", commit_paths
        )
        return None

    tests_dir = pathlib.Path(ctx.repo_dir) / "tests"
    if not tests_dir.exists():
        return None

    try:
        result = subprocess.run(
            ["pytest", "tests/", "-q", "--tb=line", "--no-header"],
            cwd=ctx.repo_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode == 0:
            return None

        # Truncate output if too long
        output = result.stdout + result.stderr
        if len(output) > MAX_TEST_OUTPUT:
            output = output[:MAX_TEST_OUTPUT] + "\n...(truncated)..."
        return output

    except subprocess.TimeoutExpired:
        return "⚠️ PRE_PUSH_TEST_ERROR: pytest timed out after 30 seconds"

    except FileNotFoundError:
        return "⚠️ PRE_PUSH_TEST_ERROR: pytest not installed or not found in PATH"

    except Exception as e:
        log.warning(f"Pre-push tests failed with exception: {e}", exc_info=True)
        return f"⚠️ PRE_PUSH_TEST_ERROR: Unexpected error running tests: {e}"


def _git_push_with_tests(
    ctx: ToolContext,
    *,
    commit_paths: list[str] | None = None,
) -> str | None:
    """Compatibility shim: run local tests, but never pull or push."""
    test_error = _run_pre_push_tests(ctx, commit_paths=commit_paths)
    if test_error:
        log.error("Pre-commit tests failed")
        ctx.last_push_succeeded = False
        return f"WARNING: PRE_COMMIT_TESTS_FAILED: Tests failed.\n{test_error}\nCommitted locally but NOT pushed."

    return None


# --- Tool implementations ---


def _repo_write_commit(
    ctx: ToolContext, path: str, content: str, commit_message: str
) -> str:
    ctx.last_push_succeeded = False
    disabled = _git_commit_disabled_message("repo_write_commit")
    if disabled:
        return disabled
    if not commit_message.strip():
        return "⚠️ ERROR: commit_message must be non-empty."
    repo_root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir)
    lock = _acquire_git_lock(ctx)
    try:
        try:
            run_cmd(["git", "checkout", ctx.branch_dev], cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (checkout): {e}"
        try:
            rel = safe_relpath(path)
            write_text(repo_root / rel, content)
        except Exception as e:
            return f"⚠️ FILE_WRITE_ERROR: {e}"
        try:
            run_cmd(["git", "add", safe_relpath(path)], cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (add): {e}"
        try:
            run_cmd(["git", "commit", "-m", commit_message], cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (commit): {e}"

        push_error = _git_push_with_tests(ctx, commit_paths=[safe_relpath(path)])
        if push_error:
            return push_error
    finally:
        _release_git_lock(lock)
    ctx.last_push_succeeded = False
    return f"OK: committed locally on {ctx.branch_dev}; push disabled by Umbrella policy: {commit_message}"


def _repo_commit_push(
    ctx: ToolContext, commit_message: str, paths: list[str] | None = None
) -> str:
    ctx.last_push_succeeded = False
    disabled = _git_commit_disabled_message("repo_commit_push")
    if disabled:
        return disabled
    if not commit_message.strip():
        return "⚠️ ERROR: commit_message must be non-empty."
    repo_root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir)
    lock = _acquire_git_lock(ctx)
    try:
        try:
            run_cmd(["git", "checkout", ctx.branch_dev], cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (checkout): {e}"
        safe_paths: list[str] | None = None
        if paths:
            try:
                safe_paths = [safe_relpath(p) for p in paths if str(p).strip()]
            except ValueError as e:
                return f"⚠️ PATH_ERROR: {e}"
            add_cmd = ["git", "add"] + safe_paths
        else:
            add_cmd = ["git", "add", "-A"]
        try:
            run_cmd(add_cmd, cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (add): {e}"
        try:
            status = run_cmd(["git", "status", "--porcelain"], cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (status): {e}"
        if not status.strip():
            return "⚠️ GIT_NO_CHANGES: nothing to commit."
        try:
            run_cmd(["git", "commit", "-m", commit_message], cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (commit): {e}"

        push_error = _git_push_with_tests(ctx, commit_paths=safe_paths)
        if push_error:
            return push_error
    finally:
        _release_git_lock(lock)
    ctx.last_push_succeeded = False
    result = f"OK: committed locally on {ctx.branch_dev}; push disabled by Umbrella policy: {commit_message}"
    if paths is not None:
        try:
            untracked = run_cmd(
                ["git", "ls-files", "--others", "--exclude-standard"], cwd=repo_root
            )
            if untracked.strip():
                files = ", ".join(untracked.strip().split("\n"))
                result += f"\n⚠️ WARNING: untracked files remain: {files} — they are NOT in git. Use repo_commit_push without paths to add everything."
        except Exception:
            log.debug(
                "Failed to check for untracked files after repo_commit_push",
                exc_info=True,
            )
            pass
    return result


def _git_status(ctx: ToolContext) -> str:
    try:
        return run_cmd(["git", "status", "--porcelain"], cwd=ctx.repo_dir)
    except Exception as e:
        return f"⚠️ GIT_ERROR: {e}"


def _git_diff(ctx: ToolContext, staged: bool = False) -> str:
    try:
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--staged")
        return run_cmd(cmd, cwd=ctx.repo_dir)
    except Exception as e:
        return f"⚠️ GIT_ERROR: {e}"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "repo_write_commit",
            {
                "name": "repo_write_commit",
                "description": "Write one file and create a local git commit on the Ouroboros branch. Never pushes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "commit_message": {"type": "string"},
                    },
                    "required": ["path", "content", "commit_message"],
                },
            },
            _repo_write_commit,
            is_code_tool=True,
        ),
        ToolEntry(
            "repo_commit_push",
            {
                "name": "repo_commit_push",
                "description": "Compatibility name: commit already-changed files locally. Never pushes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "commit_message": {"type": "string"},
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Files to add (empty = git add -A)",
                        },
                    },
                    "required": ["commit_message"],
                },
            },
            _repo_commit_push,
            is_code_tool=True,
        ),
        ToolEntry(
            "git_status",
            {
                "name": "git_status",
                "description": "git status --porcelain",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            _git_status,
            is_code_tool=True,
        ),
        ToolEntry(
            "git_diff",
            {
                "name": "git_diff",
                "description": "git diff (use staged=true to see staged changes after git add)",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "staged": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, show staged changes (--staged)",
                        },
                    },
                    "required": [],
                },
            },
            _git_diff,
            is_code_tool=True,
        ),
    ]
