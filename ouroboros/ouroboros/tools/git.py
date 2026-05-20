"""Git tools: local commit helpers, git_status, git_diff."""

import json
import logging
import os
import pathlib
import re
import subprocess
import time
from collections import Counter
from typing import Any, List, Optional

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


def _coerce_repo_write_content(content: Any) -> tuple[str | None, str | None]:
    if isinstance(content, str):
        stripped = content.strip()
        if _looks_like_preview_metadata_text(stripped):
            return (
                None,
                (
                    "ERROR: content_must_be_source: `content` looks like a "
                    "truncated tool-result preview, not file source. Re-read the "
                    "source artifact and send the full text."
                ),
            )
        return content, None
    if isinstance(content, dict):
        inner = content.get("content")
        if isinstance(inner, str):
            return inner, None
        keys = ",".join(sorted(map(str, content.keys()))[:8])
        if any(str(key).startswith("_") for key in content):
            return (
                None,
                (
                    "ERROR: content_must_be_string: `content` looks like a "
                    f"tool-result preview/metadata object (keys={keys}), not file "
                    "source. Re-read the file or provide the full source as a string."
                ),
            )
        return (
            None,
            (
                "ERROR: content_must_be_string: `content` must be the full file "
                f"source as a string, or an object with a string `content` field "
                f"(keys={keys})."
            ),
        )
    return (
        None,
        (
            "ERROR: content_must_be_string: `content` must be file source text, "
            f"got {type(content).__name__}."
        ),
    )


def _looks_like_preview_metadata_text(text: str) -> bool:
    if not text:
        return False
    preview_keys = {
        "_truncated",
        "content_truncated",
        "patch_truncated",
        "content_sha256",
        "patch_sha256",
        "content_len",
        "patch_len",
    }
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        keys = set(map(str, parsed.keys()))
        if keys and keys.issubset(preview_keys) and (
            parsed.get("_truncated")
            or parsed.get("content_truncated")
            or parsed.get("patch_truncated")
        ):
            return True
    if '"_truncated"' not in text and "'_truncated'" not in text:
        return False
    if len(text) > 500:
        return False
    source_tokens = ("def ", "class ", "import ", "from ", "export ", "function ")
    return not any(token in text for token in source_tokens)


def _workspace_id_from_repo_rel(rel: str) -> str:
    parts = [part for part in str(rel or "").replace("\\", "/").split("/") if part]
    if len(parts) >= 2 and parts[0].casefold() == "workspaces":
        return parts[1]
    return ""


def _workspace_rel_from_repo_rel(rel: str) -> tuple[str, str]:
    parts = [part for part in str(rel or "").replace("\\", "/").split("/") if part]
    if len(parts) >= 2 and parts[0].casefold() == "workspaces":
        return parts[1], "/".join(parts[2:])
    return "", ""


_HOST_REPO_WRITE_PREFIXES = frozenset({"ouroboros", "umbrella", "gmas", ".github"})


def _current_workspace_id_from_drive(ctx: ToolContext) -> str:
    view = getattr(ctx, "loop_state_view", None)
    if isinstance(view, dict):
        workspace_id = str(view.get("active_workspace_id") or "").strip()
        if workspace_id:
            return workspace_id

    drive_root = pathlib.Path(getattr(ctx, "drive_root", "") or "")
    if drive_root:
        try:
            state_path = drive_root / "state" / "state.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            current = payload.get("current_task") if isinstance(payload, dict) else None
            if isinstance(current, dict):
                workspace_id = str(current.get("workspace_id") or "").strip()
                if workspace_id:
                    return workspace_id
        except Exception:
            pass
        parts = list(drive_root.parts)
        lowered = [part.casefold() for part in parts]
        for idx, part in enumerate(lowered):
            if (
                part == "workspaces"
                and idx + 3 < len(parts)
                and lowered[idx + 2] == ".memory"
                and lowered[idx + 3] == "drive"
            ):
                return str(parts[idx + 1]).strip()
    return ""


def _repo_write_normalize_rel_for_context(ctx: ToolContext, rel: str) -> str:
    """Scope workspace-phase relative writes to the active workspace.

    `repo_write_commit` is a host-repo tool, but Umbrella workspace phases
    present files to the model as workspace-relative paths. Without this
    normalization a call like `path="tests/foo.py"` writes to host `tests/`
    while the agent believes it updated `workspaces/<id>/tests/foo.py`.
    """

    norm = str(rel or "").replace("\\", "/").strip("/")
    if not norm:
        return rel
    parts = [part for part in norm.split("/") if part]
    first = parts[0].casefold() if parts else ""
    if first in _HOST_REPO_WRITE_PREFIXES:
        return norm
    workspace_id = _current_workspace_id_from_drive(ctx)
    if not workspace_id:
        return norm
    workspace_cf = workspace_id.casefold()
    if first == "workspaces":
        if len(parts) >= 2 and parts[1].casefold() == workspace_cf:
            rest = parts[2:]
            while rest and rest[0].casefold() == workspace_cf:
                rest = rest[1:]
            return "/".join(["workspaces", workspace_id, *rest])
        return norm
    if first == workspace_cf:
        rest = parts[1:]
        while rest and rest[0].casefold() == workspace_cf:
            rest = rest[1:]
        return "/".join(["workspaces", workspace_id, *rest])
    return f"workspaces/{workspace_id}/{norm}"


def _repo_write_is_umbrella_phase_context(ctx: ToolContext) -> bool:
    task_type = str(getattr(ctx, "current_task_type", "") or "").strip().lower()
    if task_type == "phase_run":
        return True
    view = getattr(ctx, "loop_state_view", None)
    phase = ""
    if isinstance(view, dict):
        phase = str(view.get("phase_label") or "").strip().lower()
    if not phase:
        return False
    if phase.startswith(("subtask_", "remediation_", "review_")):
        return True
    return phase in {
        "preflight",
        "research",
        "research_review",
        "plan",
        "plan_review",
        "execute",
        "linear",
        "final_review",
        "final_aggregation",
        "verify",
    }


def _repo_write_workspace_tool_bypass_block(ctx: ToolContext, rel: str) -> str:
    if not _repo_write_is_umbrella_phase_context(ctx):
        return ""
    workspace_id, workspace_rel = _workspace_rel_from_repo_rel(rel)
    if not workspace_id or not workspace_rel:
        return ""

    payload = {
        "status": "blocked",
        "reason": "workspace_write_tool_bypass",
        "tool": "repo_write_commit",
        "workspace_id": workspace_id,
        "active_workspace_id": _current_workspace_id_from_drive(ctx),
        "path": workspace_rel,
        "repo_path": rel,
        "message": (
            "repo_write_commit is a host-repo/self-edit tool and cannot write "
            "Umbrella-managed workspace files during phase runs. Workspace "
            "writes must go through workspace-aware tools so read-before-patch, "
            "GMAS context, .memory logs, and verification evidence stay "
            "authoritative."
        ),
        "next_step": (
            "Use list_files/read_file for the workspace-relative path, then "
            "apply_workspace_patch with exact current context. If the patch "
            "still mismatches after rereading, call request_watcher_review or "
            "mutate_phase_plan instead of using repo_write_commit as a "
            "full-file workspace writer."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _write_text_verified(target: pathlib.Path, content: str) -> str:
    try:
        write_text(target, content)
    except Exception as e:
        return f"⚠️ FILE_WRITE_ERROR: {e}"
    try:
        actual = target.read_text(encoding="utf-8")
    except Exception as e:
        return f"⚠️ FILE_WRITE_VERIFY_ERROR: could not read back written file: {e}"
    if actual.replace("\r\n", "\n") != content.replace("\r\n", "\n"):
        return (
            "⚠️ FILE_WRITE_VERIFY_ERROR: read-back content did not match requested "
            f"content for {target}."
        )
    return ""


_PY_TEST_ITEM_RE = re.compile(
    r"(?m)^\s*(?:(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(|class\s+(Test[A-Za-z0-9_]+)\s*[:(])"
)
_PY_ASSERT_LINE_RE = re.compile(r"(?m)^\s*assert\s+(.+)$")
_PY_ASSERT_SUBSCRIPT_KEY_RE = re.compile(r"\[['\"]([A-Za-z_][A-Za-z0-9_-]*)['\"]\]")


def _looks_like_python_test_file(rel: str) -> bool:
    norm = str(rel or "").replace("\\", "/").strip("/")
    name = pathlib.PurePosixPath(norm).name
    return (
        name.startswith("test_")
        and name.endswith(".py")
        or "/tests/" in f"/{norm}"
        and name.endswith(".py")
    )


def _test_items(text: str) -> set[str]:
    items: set[str] = set()
    for match in _PY_TEST_ITEM_RE.finditer(text or ""):
        name = match.group(1) or match.group(2)
        if name:
            items.add(name)
    return items


def _assertion_contract_keys(text: str) -> Counter[str]:
    keys: Counter[str] = Counter()
    for match in _PY_ASSERT_LINE_RE.finditer(text or ""):
        assertion = match.group(1)
        keys.update(_PY_ASSERT_SUBSCRIPT_KEY_RE.findall(assertion))
    return keys


def _repo_write_success_test_targets_rel(success_text: str, rel: str) -> bool:
    success_norm = str(success_text or "").replace("\\", "/").casefold()
    norm_rel = str(rel or "").replace("\\", "/").strip("/").casefold()
    if not success_norm or not norm_rel:
        return False
    _workspace_id, workspace_rel = _workspace_rel_from_repo_rel(norm_rel)
    candidates = [norm_rel, workspace_rel.casefold() if workspace_rel else ""]
    return any(candidate and candidate in success_norm for candidate in candidates)


def _repo_write_row_matches_success_test(
    row: dict[str, Any],
    *,
    groups: list[list[str]],
    rel: str,
) -> bool:
    norm_rel = str(rel or "").replace("\\", "/").strip("/").casefold()
    _workspace_id, workspace_rel = _workspace_rel_from_repo_rel(norm_rel)
    path_candidates = [norm_rel, workspace_rel.casefold() if workspace_rel else ""]
    raw_text = json.dumps(
        {
            "args": row.get("args"),
            "result_preview": row.get("result_preview"),
            "result": row.get("result"),
        },
        ensure_ascii=False,
        default=str,
    ).replace("\\", "/").casefold()
    if any(candidate and candidate in raw_text for candidate in path_candidates):
        return True
    try:
        from umbrella.deep_agent_tools.phase_control_completion import (
            _tool_row_command_norms,
        )

        norms = _tool_row_command_norms(row)
    except Exception:
        log.debug("active success-test guard could not normalise command row", exc_info=True)
        norms = []
    return any(
        alt and any(alt in norm for norm in norms)
        for alternatives in groups
        for alt in alternatives
    )


def _repo_write_phase_task_ids(ctx: ToolContext, plan: dict[str, Any]) -> list[str]:
    task_ids: list[str] = []
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if task_id:
        task_ids.append(task_id)
    run_id = str(plan.get("run_id") or "").strip()
    if run_id:
        task_ids.append(f"{run_id}:execute")
    return list(dict.fromkeys(task_ids))


def _repo_write_active_success_test_has_latest_failure(
    ctx: ToolContext,
    *,
    plan: dict[str, Any],
    groups: list[list[str]],
    rel: str,
) -> bool:
    try:
        from umbrella.deep_agent_tools.phase_control_base import _tool_log_rows_for_task
        from umbrella.deep_agent_tools.phase_control_common import (
            _PHASE_SUBTASK_COMMAND_TOOLS,
        )
        from umbrella.deep_agent_tools.phase_control_completion import (
            _tool_row_success_status,
        )
    except Exception:
        log.debug("active success-test guard could not import log helpers", exc_info=True)
        return False

    latest_failed: bool | None = None
    for task_id in _repo_write_phase_task_ids(ctx, plan):
        for row in _tool_log_rows_for_task(ctx, task_id):
            if str(row.get("tool") or "") not in _PHASE_SUBTASK_COMMAND_TOOLS:
                continue
            if not _repo_write_row_matches_success_test(row, groups=groups, rel=rel):
                continue
            ok, _reason = _tool_row_success_status(row)
            latest_failed = not ok
    return latest_failed is True


def _repo_write_active_declared_success_test_item_removal_block(
    ctx: ToolContext | None,
    *,
    rel: str,
    old_items: set[str],
    new_items: set[str],
) -> dict[str, Any] | None:
    if ctx is None:
        return None
    removed = sorted(old_items - new_items)
    if not removed:
        return None
    try:
        from umbrella.deep_agent_tools.phase_control_base import (
            _is_phase_run_context,
            _read_phase_plan,
            _subtask_success_test_text,
        )
        from umbrella.deep_agent_tools.phase_control_completion import (
            _current_phase_node,
            _first_incomplete_subtask,
            _phase_subtasks,
            _success_test_command_groups,
        )

        if not _is_phase_run_context(ctx):
            return None
        plan = _read_phase_plan(ctx)
        if not isinstance(plan, dict):
            return None
        current_phase = _current_phase_node(ctx, plan)
        if not isinstance(current_phase, dict):
            return None
        if str(current_phase.get("id") or "").strip() != "execute":
            return None
        first = _first_incomplete_subtask(_phase_subtasks(current_phase))
        if not isinstance(first, dict):
            return None
        success_text = _subtask_success_test_text(first)
        if not _repo_write_success_test_targets_rel(success_text, rel):
            return None
        groups = _success_test_command_groups(success_text)
        if not groups:
            return None
        if not _repo_write_active_success_test_has_latest_failure(
            ctx,
            plan=plan,
            groups=groups,
            rel=rel,
        ):
            return None
    except Exception:
        log.debug("active declared success-test item guard failed open", exc_info=True)
        return None

    return {
        "status": "blocked",
        "reason": "test_weakening_guard",
        "subreason": "declared_success_test_item_removal",
        "path": rel,
        "message": (
            "Refusing to overwrite the active declared success-test file after "
            "a failing run while removing or renaming existing test functions/classes. "
            "Repair the implementation against the existing test contract, or "
            "preserve the existing test items and add coverage for a scoped "
            "contract migration."
        ),
        "old_test_count": len(old_items),
        "new_test_count": len(new_items),
        "removed_test_items": removed[:25],
    }


def _repo_write_test_weakening_block(
    *,
    ctx: ToolContext | None = None,
    target: pathlib.Path,
    rel: str,
    content_text: str,
) -> str:
    if not _looks_like_python_test_file(rel) or not target.exists():
        return ""
    try:
        old_text = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    old_items = _test_items(old_text)
    if not old_items:
        return ""
    new_items = _test_items(content_text)
    if active_test_block := _repo_write_active_declared_success_test_item_removal_block(
        ctx,
        rel=rel,
        old_items=old_items,
        new_items=new_items,
    ):
        return json.dumps(active_test_block, ensure_ascii=False, indent=2)
    if len(old_items) < 3:
        return ""
    if not new_items:
        payload = {
            "status": "blocked",
            "reason": "test_weakening_guard",
            "path": rel,
            "message": (
                "Refusing to overwrite an existing Python test file with zero "
                "test functions/classes. Fix failing tests or intentionally "
                "delete/replace them through a scoped refactor instead of "
                "erasing coverage."
            ),
            "old_test_items": sorted(old_items)[:25],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    min_allowed = max(2, len(old_items) // 3)
    if len(old_items) >= 8 and len(new_items) < min_allowed:
        removed = sorted(old_items - new_items)
        payload = {
            "status": "blocked",
            "reason": "test_weakening_guard",
            "path": rel,
            "message": (
                "Refusing to overwrite a substantial Python test file with a "
                "much smaller suite. Keep behavioral coverage while repairing "
                "the implementation, or split an intentional test refactor into "
                "a clear delete/add operation."
            ),
            "old_test_count": len(old_items),
            "new_test_count": len(new_items),
            "removed_examples": removed[:25],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    old_contract_keys = _assertion_contract_keys(old_text)
    if sum(old_contract_keys.values()) >= 3:
        new_contract_keys = _assertion_contract_keys(content_text)
        removed_counts = {
            key: old_count - new_contract_keys.get(key, 0)
            for key, old_count in old_contract_keys.items()
            if new_contract_keys.get(key, 0) < old_count
        }
        removed_total = sum(removed_counts.values())
        if removed_total >= max(3, sum(old_contract_keys.values()) // 2):
            payload = {
                "status": "blocked",
                "reason": "test_weakening_guard",
                "path": rel,
                "message": (
                    "Refusing to overwrite an existing Python test file while "
                    "removing most asserted mapping-key contract checks. Repair "
                    "the implementation against the existing behavioral contract, "
                    "or make a clearly scoped contract migration instead of "
                    "weakening tests to match current behavior."
                ),
                "removed_assertion_keys": dict(
                    sorted(
                        removed_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )[:25]
                ),
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)
    return ""


def _repo_write_stop_block(ctx: ToolContext, *, rel: str) -> str:
    try:
        from ouroboros.tools.umbrella_tools import _stop_requested_block

        workspace_id = _workspace_id_from_repo_rel(rel) or _current_workspace_id_from_drive(ctx)
        block = _stop_requested_block(
            ctx,
            tool_name="repo_write_commit",
            workspace_id=workspace_id,
        )
    except Exception:
        log.debug("repo_write_commit stop gate failed open", exc_info=True)
        return ""
    if not block:
        return ""
    return json.dumps(block, ensure_ascii=False, indent=2)


def _repo_write_gmas_block(ctx: ToolContext, repo_root: pathlib.Path, rel: str) -> str:
    workspace_id = _workspace_id_from_repo_rel(rel)
    if not workspace_id:
        return ""
    try:
        from ouroboros.tools.umbrella_tools import _gmas_context_before_write_block

        block = _gmas_context_before_write_block(
            ctx,
            workspace_id,
            repo_root / "workspaces" / workspace_id,
        )
    except Exception:
        log.debug("repo_write_commit GMAS gate failed open", exc_info=True)
        return ""
    if not block:
        return ""
    return json.dumps(block, ensure_ascii=False, indent=2)


def _repo_write_retry_escalation_block(ctx: ToolContext) -> str:
    try:
        from ouroboros.tools import umbrella_tools

        block = umbrella_tools._phase_subtask_retry_escalation_block(
            ctx,
            tool_name="repo_write_commit",
        )
    except Exception:
        log.debug("repo_write_commit retry escalation gate failed open", exc_info=True)
        return ""
    if not block:
        return ""
    return json.dumps(block, ensure_ascii=False, indent=2)


def _repo_write_workspace_policy_block(
    rel: str,
    content_text: str,
) -> str:
    workspace_id, workspace_rel = _workspace_rel_from_repo_rel(rel)
    if not workspace_id or not workspace_rel:
        return ""
    try:
        from ouroboros.tools import umbrella_tools

        block = umbrella_tools._workspace_layout_policy_block(workspace_rel)
        if not block:
            block = umbrella_tools._llm_runtime_contract_block(
                workspace_rel, content_text
            )
        if not block:
            block = umbrella_tools._llm_behavior_fallback_contract_block(
                workspace_rel, content_text
            )
        if not block:
            block = umbrella_tools._python_syntax_block(workspace_rel, content_text)
    except Exception:
        log.debug("repo_write_commit workspace policy gate failed open", exc_info=True)
        return ""
    if not block:
        return ""
    return json.dumps(block, ensure_ascii=False, indent=2)


def _repo_write_source_truncation_block(
    *,
    repo_root: pathlib.Path,
    rel: str,
    content_text: str,
    allow_large_overwrite: bool = False,
    validation_summary: str = "",
) -> str:
    target = repo_root / rel
    if not target.is_file():
        return ""
    try:
        old_content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    try:
        from ouroboros.tools import umbrella_tools

        block = umbrella_tools._source_truncation_block(
            rel,
            old_content,
            content_text,
            allow_large_overwrite=allow_large_overwrite,
            validation_summary=validation_summary,
        )
    except Exception:
        log.debug("repo_write_commit source truncation gate failed open", exc_info=True)
        return ""
    if not block:
        return ""
    return json.dumps(block, ensure_ascii=False, indent=2)


def _repo_write_commit(
    ctx: ToolContext,
    path: str,
    content: Any,
    commit_message: str = "",
    allow_large_overwrite: bool = False,
    validation_summary: str = "",
) -> str:
    ctx.last_push_succeeded = False
    repo_root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir)
    try:
        rel = _repo_write_normalize_rel_for_context(ctx, safe_relpath(path))
    except Exception as e:
        return f"⚠️ PATH_ERROR: {e}"

    content_text, content_error = _coerce_repo_write_content(content)
    if content_error:
        return content_error

    if stop_block := _repo_write_stop_block(ctx, rel=rel):
        return stop_block
    if gmas_block := _repo_write_gmas_block(ctx, repo_root, rel):
        return gmas_block
    if retry_block := _repo_write_retry_escalation_block(ctx):
        return retry_block
    if policy_block := _repo_write_workspace_policy_block(rel, content_text or ""):
        return policy_block
    if truncation_block := _repo_write_source_truncation_block(
        repo_root=repo_root,
        rel=rel,
        content_text=content_text or "",
        allow_large_overwrite=allow_large_overwrite,
        validation_summary=validation_summary,
    ):
        return truncation_block
    if test_block := _repo_write_test_weakening_block(
        ctx=ctx,
        target=repo_root / rel,
        rel=rel,
        content_text=content_text or "",
    ):
        return test_block
    if workspace_tool_bypass_block := _repo_write_workspace_tool_bypass_block(
        ctx, rel
    ):
        return workspace_tool_bypass_block

    disabled = _git_commit_disabled_message("repo_write_commit")
    if disabled:
        write_error = _write_text_verified(repo_root / rel, content_text or "")
        if write_error:
            return write_error
        return (
            f"OK: wrote {rel}; local git commit skipped by policy. "
            f"{disabled}"
        )

    commit_message = commit_message.strip() or f"Update {rel}"
    lock = _acquire_git_lock(ctx)
    try:
        try:
            run_cmd(["git", "checkout", ctx.branch_dev], cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (checkout): {e}"
        try:
            write_error = _write_text_verified(repo_root / rel, content_text or "")
            if write_error:
                return write_error
        except Exception as e:
            return f"⚠️ FILE_WRITE_ERROR: {e}"
        try:
            run_cmd(["git", "add", rel], cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (add): {e}"
        try:
            run_cmd(["git", "commit", "-m", commit_message], cwd=repo_root)
        except Exception as e:
            return f"⚠️ GIT_ERROR (commit): {e}"

        push_error = _git_push_with_tests(ctx, commit_paths=[rel])
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
                "description": (
                    "Write one host-repo/self-edit file and create a local git "
                    "commit on the Ouroboros branch. Never pushes. During "
                    "Umbrella phase runs, active workspace files under "
                    "workspaces/<id>/ are blocked; use apply_workspace_patch "
                    "or other workspace-aware tools for generated workspace "
                    "edits."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "commit_message": {"type": "string"},
                        "allow_large_overwrite": {"type": "boolean", "default": False},
                        "validation_summary": {"type": "string"},
                    },
                    "required": ["path", "content"],
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
