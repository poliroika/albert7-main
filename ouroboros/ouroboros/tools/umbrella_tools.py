"""Umbrella tools exposed to Ouroboros.

Umbrella is intentionally a launcher/tool/memory layer here. It gives
Ouroboros host-repo workspace access, GMAS context retrieval, and local
memory. It does not run the old Umbrella manager loop for Ouroboros.
"""

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import ast
import tomllib
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from umbrella.file_preview import read_file_preview
from ouroboros.tools import background_jobs as _bg_jobs
from ouroboros.tools.terminal_session import (
    RunResult,
    get_or_create_session,
)

log = logging.getLogger(__name__)


_PYTHON_COMMAND_NAMES = {
    "python",
    "python3",
    "py",
    "python.exe",
    "python3.exe",
    "py.exe",
}
_SCROLLBACK_REL_PATH = Path("memory") / "terminal_scrollback.md"
_SCROLLBACK_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB before rotation
_SCROLLBACK_TRIM_FRACTION = 0.25  # drop oldest 25% on rotation


def _git_commit_disabled_payload(tool_name: str, workspace_id: str = "") -> str:
    if str(os.environ.get("OUROBOROS_ALLOW_GIT_COMMIT") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return ""
    return _json(
        {
            "status": "blocked",
            "reason": "git_commit_disabled_by_policy",
            "tool": tool_name,
            "workspace_id": workspace_id,
            "next_step": (
                "Leave changes in the working tree. A human can inspect and commit them, "
                "or set OUROBOROS_ALLOW_GIT_COMMIT=1 to re-enable local commits."
            ),
        }
    )


def _scrollback_path(ctx: Any) -> Path | None:
    """Locate ``<drive_root>/memory/terminal_scrollback.md`` for ``ctx``.

    Returns ``None`` if the drive root is unknown (e.g. unit-test contexts
    that don't set it).
    """
    drive_root = getattr(ctx, "drive_root", None)
    if not drive_root:
        return None
    try:
        path = Path(drive_root) / _SCROLLBACK_REL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        log.debug("scrollback path resolution failed", exc_info=True)
        return None


def _maybe_rotate_scrollback(path: Path) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    except Exception:
        log.debug("scrollback stat failed", exc_info=True)
        return
    if size <= _SCROLLBACK_MAX_BYTES:
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        keep_from = int(len(data) * _SCROLLBACK_TRIM_FRACTION)
        new_text = (
            "<!-- scrollback rotated, oldest 25% dropped -->\n" + data[keep_from:]
        )
        path.write_text(new_text, encoding="utf-8")
    except Exception:
        log.debug("scrollback rotation failed", exc_info=True)


def _append_scrollback(
    ctx: Any,
    *,
    workspace_id: str,
    command: list[str] | str,
    result: RunResult,
    cwd: str,
) -> None:
    """Append a fenced block to ``terminal_scrollback.md`` for the LLM to re-read."""
    path = _scrollback_path(ctx)
    if path is None:
        return
    if isinstance(command, list):
        cmd_repr = shlex.join(command)
    else:
        cmd_repr = str(command)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    full_output = result.raw_output or result.output
    body = (
        f"\n## ws={workspace_id} ts={ts} exit={result.exit_code} backend={getattr(result, 'marker', '')[:6]}\n"
        f"cwd: {cwd}\n"
        f"$ {cmd_repr}\n"
        "```\n"
        f"{full_output}\n"
        "```\n"
    )
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(body)
    except Exception:
        log.debug("scrollback append failed", exc_info=True)
        return
    _maybe_rotate_scrollback(path)


def _resolve_umbrella_repo_root(ctx: Any) -> Path:
    candidates: list[Path] = []
    host_repo_root = getattr(ctx, "host_repo_root", None)
    if host_repo_root:
        candidates.append(Path(host_repo_root))

    repo_dir = Path(getattr(ctx, "repo_dir", Path.cwd()))
    candidates.extend([repo_dir.parent, repo_dir, Path.cwd()])

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "umbrella").exists() and (resolved / "workspaces").exists():
            return resolved

    return (Path(host_repo_root) if host_repo_root else Path.cwd()).resolve()


def _memory_store(repo_root: Path, workspace_id: str = "") -> Any:
    from umbrella.memory.paths import get_workspace_store

    return get_workspace_store(repo_root, workspace_id)


def _palace_backend(repo_root: Path, workspace_id: str = "") -> Any:
    from umbrella.memory.palace_backend import get_palace_backend
    from umbrella.memory.paths import palace_path_for

    return get_palace_backend(palace_path_for(repo_root, workspace_id))


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _split_tags(tags: str) -> list[str]:
    return [tag.strip() for tag in tags.replace(";", ",").split(",") if tag.strip()]


def _workspace_root(repo_root: Path, workspace_id: str, ctx: Any | None = None) -> Path:
    clean = str(workspace_id or "").strip().replace("\\", "/").strip("/")
    if not clean or ".." in Path(clean).parts:
        raise ValueError("workspace_id must be a safe workspace directory name")
    overrides = (
        getattr(ctx, "workspace_root_overrides", None) if ctx is not None else None
    )
    if isinstance(overrides, dict):
        override = str(overrides.get(clean) or "").strip()
        if override:
            return Path(override).resolve()
    root = (repo_root / "workspaces" / clean).resolve()
    root.relative_to((repo_root / "workspaces").resolve())
    return root


def _venv_python(venv_root: Path) -> Path:
    return venv_root / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )


def _workspace_python_command(
    repo_root: Path, workspace_root: Path
) -> list[str] | None:
    """Choose the interpreter workspace commands should use for plain `python`.

    On Windows the PATH often points at a user Python that does not have the
    repo's editable dependencies (notably gmas). The repo venv is the closest
    equivalent to `uv run python` for one-shot commands.
    """

    for candidate in (
        _venv_python(workspace_root / ".venv"),
        _venv_python(repo_root / ".venv"),
    ):
        if candidate.exists():
            return [str(candidate)]
    if (workspace_root / "pyproject.toml").exists() or (
        repo_root / "pyproject.toml"
    ).exists():
        return ["uv", "run", "python"]
    return None


def _rewrite_python_command_for_workspace(
    cmd: list[str],
    *,
    repo_root: Path,
    workspace_root: Path,
) -> list[str]:
    if not cmd:
        return cmd
    first = Path(str(cmd[0])).name.lower()
    if first not in _PYTHON_COMMAND_NAMES:
        return cmd
    python_cmd = _workspace_python_command(repo_root, workspace_root)
    if not python_cmd:
        return cmd
    return [*python_cmd, *cmd[1:]]


def _stop_request_matches_task(payload: Any, task_id: str) -> bool:
    if not isinstance(payload, dict):
        return True
    current = str(task_id or "").strip()
    requested_ids: set[str] = set()
    for key in ("run_id", "task_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            requested_ids.add(value)
    for key in ("attempt_task_ids", "candidate_run_ids"):
        values = payload.get(key)
        if isinstance(values, list):
            requested_ids.update(
                str(item).strip() for item in values if str(item or "").strip()
            )
    if not requested_ids:
        return True
    if not current:
        return False
    return any(
        current == requested or current.startswith(f"{requested}__")
        for requested in requested_ids
    )


def _matching_stop_request(ctx: Any) -> dict[str, Any] | None:
    drive_root = getattr(ctx, "drive_root", None)
    if not drive_root:
        return None
    stop_path = Path(drive_root) / "state" / "stop_requested.json"
    if not stop_path.exists():
        return None
    try:
        payload = json.loads(stop_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        payload = {}
    if not _stop_request_matches_task(payload, str(getattr(ctx, "task_id", "") or "")):
        return None
    return payload if isinstance(payload, dict) else {}


def _stop_requested_block(
    ctx: Any, *, tool_name: str, workspace_id: str = ""
) -> dict[str, Any] | None:
    payload = _matching_stop_request(ctx)
    if payload is None:
        return None
    return {
        "status": "blocked",
        "reason": "stop_requested",
        "tool": tool_name,
        "workspace_id": workspace_id,
        "run_id": payload.get("run_id") or "",
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "message": "Stop was requested from the web UI; refusing to start new workspace work.",
    }


def _current_workspace_id_from_drive(ctx: Any) -> str:
    try:
        state_path = Path(getattr(ctx, "drive_root", "")) / "state" / "state.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        current = payload.get("current_task") if isinstance(payload, dict) else None
        if isinstance(current, dict):
            return str(current.get("workspace_id") or "").strip()
    except Exception:
        pass
    return ""


def _drive_state_path(ctx: Any) -> Path | None:
    drive_root = getattr(ctx, "drive_root", None)
    if not drive_root:
        return None
    return Path(drive_root) / "state" / "state.json"


def _read_drive_state(ctx: Any) -> dict[str, Any]:
    path = _drive_state_path(ctx)
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_drive_state(ctx: Any, state: dict[str, Any]) -> None:
    path = _drive_state_path(ctx)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def _set_workspace_verification_state(
    ctx: Any,
    *,
    workspace_id: str,
    passed: bool,
    summary: str = "",
) -> None:
    state = _read_drive_state(ctx)
    state["verification_passed"] = bool(passed)
    state["verification_workspace_id"] = workspace_id
    state["verification_summary"] = summary[:2000]
    state["verification_checked_at"] = datetime.now(timezone.utc).isoformat()
    _write_drive_state(ctx, state)


def _workspace_verification_passed(ctx: Any, workspace_id: str) -> bool:
    state = _read_drive_state(ctx)
    return bool(state.get("verification_passed")) and str(
        state.get("verification_workspace_id") or ""
    ) == str(workspace_id)


def _workspace_memory_root(
    repo_root: Path, workspace_id: str, ctx: Any | None = None
) -> Path:
    return _workspace_root(repo_root, workspace_id, ctx) / ".memory"


_PROMPT_NAME_TO_FILE = {
    "SYSTEM": "SYSTEM.md",
    "BIBLE": "BIBLE.md",
    "CONSCIOUSNESS": "CONSCIOUSNESS.md",
}


def _resolve_prompt_name(name: str) -> str:
    normalized = str(name or "").strip().upper().removesuffix(".MD")
    if normalized not in _PROMPT_NAME_TO_FILE:
        allowed = ", ".join(sorted(_PROMPT_NAME_TO_FILE))
        raise ValueError(f"prompt name must be one of: {allowed}")
    return normalized


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort int coercion for tool args.

    LLMs (notably DeepSeek-V*-Flash) frequently emit numeric arguments as
    strings (``"30000"`` instead of ``30000``). Strict typing then crashes
    downstream comparisons (``len(entries) >= max_entries`` ⇒ TypeError when
    ``max_entries`` is ``"50"``). This helper accepts both shapes and
    falls back to ``default`` only when the value is genuinely unusable.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _workspace_path(workspace_root: Path, rel: str = "") -> Path:
    raw = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if ".." in Path(raw).parts:
        raise ValueError("workspace-relative path traversal is not allowed")
    target = (workspace_root / raw).resolve()
    target.relative_to(workspace_root.resolve())
    return target


def _strip_workspace_prefix(workspace_id: str, raw_path: str) -> str:
    """Drop a leading ``workspaces/<workspace_id>/`` from a tool-supplied path.

    Models occasionally pass a repo-relative ``workspaces/<id>/foo/bar.py``
    while workspace tools expect a workspace-relative path.  Without this
    normalisation we end up creating
    ``workspaces/<id>/workspaces/<id>/foo/bar.py`` on disk.
    """
    text = str(raw_path or "").strip().replace("\\", "/").lstrip("/")
    if not text:
        return text
    wid = str(workspace_id or "").strip().strip("/")
    if not wid:
        return text
    prefix = f"workspaces/{wid}/"
    while text.startswith(prefix):
        text = text[len(prefix) :]
    return text


def _resolve_workspace_file(workspace_root: Path, rel: str) -> Path | None:
    """Return the actual on-disk path for ``rel`` even when unicode forms differ.

    Filenames coming from macOS-saved ``.docx`` / ``.pptx`` documents are
    usually stored on disk in NFD form (decomposed combining marks like
    U+0306 after a letter). Most LLMs and most clipboards normalise to
    NFC. On Windows ``Path.exists()`` is byte-level, so the two forms
    look like different files even though a human (and any modern editor)
    sees the same name. This helper:

    1. Tries the direct path (fast common case).
    2. Tries NFC and NFD variants of the supplied name.
    3. Falls back to a directory scan that compares NFC-normalised forms,
       which catches arbitrary mixed-form names without blowing up.

    Returns the resolved absolute ``Path`` if a match is found, else
    ``None``. Path-traversal is still enforced via ``_workspace_path``.
    """
    import unicodedata

    direct = _workspace_path(workspace_root, rel)
    if direct.exists():
        return direct

    raw = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        return None

    for form in ("NFC", "NFD"):
        candidate_rel = unicodedata.normalize(form, raw)
        if candidate_rel == raw:
            continue
        candidate = _workspace_path(workspace_root, candidate_rel)
        if candidate.exists():
            return candidate

    parent_rel, _, leaf = raw.rpartition("/")
    try:
        parent = (
            _workspace_path(workspace_root, parent_rel)
            if parent_rel
            else workspace_root
        )
    except ValueError:
        return None
    if not parent.is_dir():
        return None

    target_nfc = unicodedata.normalize("NFC", leaf)
    target_nfd = unicodedata.normalize("NFD", leaf)
    try:
        for entry in parent.iterdir():
            name_nfc = unicodedata.normalize("NFC", entry.name)
            if name_nfc == target_nfc or name_nfc == target_nfd:
                return entry
    except OSError:
        return None
    return None


def _rank_lessons_for_query(lessons: list[Any], query: str, limit: int) -> list[Any]:
    query_tokens = {token for token in query.lower().split() if token}
    if not query_tokens:
        return lessons[:limit]

    def score(lesson: Any) -> tuple[int, int, float]:
        haystack = " ".join(
            [
                str(getattr(lesson, "workspace_id", "") or ""),
                str(getattr(lesson, "change_summary", "") or ""),
                str(getattr(lesson, "expected_effect", "") or ""),
                str(getattr(lesson, "observed_effect", "") or ""),
                str(getattr(lesson, "conclusion", "") or ""),
                " ".join(sorted(getattr(lesson, "tags", set()) or set())),
            ]
        ).lower()
        overlap = sum(1 for token in query_tokens if token in haystack)
        return (
            overlap,
            int(getattr(lesson, "priority", 0) or 0),
            float(getattr(lesson, "created_at", 0.0) or 0.0),
        )

    return sorted(lessons, key=score, reverse=True)[:limit]


def search_gmas_knowledge(
    ctx: Any,
    query: str,
    max_results: int = 6,
    max_chars_per_hit: int = 8000,
) -> str:
    """Search GMAS docs/examples/code and return rich implementation context."""
    try:
        from umbrella.retrieval.gmas_context import build_gmas_context

        _mark_explicit_gmas_context_call(ctx)
        repo_root = _resolve_umbrella_repo_root(ctx)
        result = build_gmas_context(
            repo_root,
            query,
            max_results=max(1, min(int(max_results), 12)),
            max_chars_per_hit=max(1000, min(int(max_chars_per_hit), 30000)),
        )
        return _json(result)
    except Exception as e:
        log.error("GMAS search failed: %s", e, exc_info=True)
        return f"WARNING: GMAS search error: {e}"


def get_gmas_context(
    ctx: Any,
    query: str,
    max_results: int = 6,
    max_chars_per_hit: int = 12000,
) -> str:
    """Alias with a more explicit name for full GMAS context retrieval."""
    return search_gmas_knowledge(
        ctx,
        query=query,
        max_results=max_results,
        max_chars_per_hit=max_chars_per_hit,
    )


def _mark_explicit_gmas_context_call(ctx: Any) -> None:
    try:
        setattr(
            ctx,
            "explicit_gmas_context_calls",
            int(getattr(ctx, "explicit_gmas_context_calls", 0) or 0) + 1,
        )
        view = getattr(ctx, "loop_state_view", None)
        if isinstance(view, dict):
            view["explicit_gmas_context_calls"] = (
                int(view.get("explicit_gmas_context_calls") or 0) + 1
            )
    except Exception:
        log.debug("Failed to mark explicit GMAS context call", exc_info=True)


def _workspace_has_gmas_skill(
    ctx: Any, repo_root: Path, workspace_id: str, workspace_root: Path
) -> bool:
    try:
        data = tomllib.loads(
            (workspace_root / "workspace.toml").read_text(encoding="utf-8")
        )
        skills = data.get("skills") if isinstance(data, dict) else None
        if isinstance(skills, dict) and skills.get("multi_agent_gmas") is True:
            return True
    except Exception:
        pass

    drive_root = getattr(ctx, "drive_root", None)
    candidates: list[Path] = []
    if drive_root:
        candidates.append(Path(drive_root) / "state" / "active_skills.json")
    candidates.append(
        workspace_root / ".memory" / "drive" / "state" / "active_skills.json"
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        text = json.dumps(payload, ensure_ascii=False).lower()
        if str(workspace_id).lower() in text and "multi_agent_gmas" in text:
            return True
    return False


def _gmas_context_before_write_block(
    ctx: Any, workspace_id: str, workspace_root: Path
) -> dict[str, Any] | None:
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        if not _workspace_has_gmas_skill(ctx, repo_root, workspace_id, workspace_root):
            return None
        explicit_calls = int(getattr(ctx, "explicit_gmas_context_calls", 0) or 0)
        view = getattr(ctx, "loop_state_view", None)
        if isinstance(view, dict):
            explicit_calls = max(
                explicit_calls, int(view.get("explicit_gmas_context_calls") or 0)
            )
            last_write = int(view.get("last_write_round") or -1)
        else:
            last_write = -1
        if explicit_calls > 0 or last_write >= 0:
            return None
        return {
            "status": "blocked",
            "reason": "gmas_context_before_first_write",
            "workspace_id": workspace_id,
            "message": (
                "This workspace has multi_agent_gmas active. Before the first "
                "workspace write, make an explicit GMAS retrieval tool call."
            ),
            "next_step": (
                "Call `get_gmas_context(query=...)` or `search_gmas_knowledge(query=...)` "
                "with a query tied to the implementation you are about to write, then retry this write."
            ),
        }
    except Exception:
        log.debug("GMAS before-write gate failed open", exc_info=True)
        return None


def load_skill(
    ctx: Any,
    slug: str,
    max_chars: int = 40000,
) -> str:
    """Load full procedural skill pack text by slug."""
    try:
        from umbrella.skills.loader import load_skill_text

        repo_root = _resolve_umbrella_repo_root(ctx)
        text = load_skill_text(repo_root, slug.strip())
        if not text:
            return _json(
                {
                    "status": "not_found",
                    "slug": slug,
                    "hint": "Skill not found in umbrella/skills/library/<slug>/SKILL.md",
                }
            )
        limited = text[: max(1000, int(max_chars))]
        return _json(
            {
                "status": "ok",
                "slug": slug,
                "truncated": len(limited) < len(text),
                "content": limited,
            }
        )
    except Exception as e:
        log.error("Skill load failed: %s", e, exc_info=True)
        return f"WARNING: load_skill error: {e}"


def list_workspace_files(
    ctx: Any,
    workspace_id: str,
    subdir: str = "",
    max_entries: int = 300,
) -> str:
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        subdir = _strip_workspace_prefix(workspace_id, subdir)
        target = _workspace_path(workspace_root, subdir)
        if not target.exists():
            return f"Workspace path not found: {target}"
        if not target.is_dir():
            return f"Workspace path is not a directory: {target}"

        cap = max(1, _coerce_int(max_entries, 300))
        entries = []
        for entry in sorted(target.iterdir()):
            if len(entries) >= cap:
                entries.append("...(truncated)")
                break
            suffix = "/" if entry.is_dir() else ""
            entries.append(
                str(entry.relative_to(workspace_root)).replace("\\", "/") + suffix
            )
        return _json(
            {"workspace_id": workspace_id, "subdir": subdir, "entries": entries}
        )
    except Exception as e:
        return f"WARNING: workspace list error: {e}"


# In-process LRU cache for ``read_workspace_file``. Keyed by
# (workspace_id, resolved_path, mtime_ns, max_chars) → serialized JSON
# response string. Saves substantial token cost in long runs that
# re-read the same files repeatedly (typical pattern: agent reads a
# file, decides on a fix, edits via update_workspace_seed, then reads
# it again to verify the new content; or remediation cycles re-read
# the same source repeatedly to triangulate the diagnosis).
#
# The mtime_ns key is invalidation-correct: any successful write
# changes the file's mtime, so the next read sees a fresh entry.
# Cache is also bounded in entry count to keep memory predictable;
# we evict oldest insertion when full (FIFO is fine here, the access
# pattern is "recent files dominate").
#
# Disabled by setting OUROBOROS_READ_CACHE_DISABLE=1 in the env.
_READ_CACHE_MAX_ENTRIES = 256
_read_cache: "OrderedDict[tuple[str, str, int, int], str]" = OrderedDict()


def _read_cache_enabled() -> bool:
    return os.environ.get("OUROBOROS_READ_CACHE_DISABLE", "").strip() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def _read_cache_get(key: tuple[str, str, int, int]) -> str | None:
    if not _read_cache_enabled():
        return None
    value = _read_cache.get(key)
    if value is None:
        return None
    # Move to end so the bound-eviction is roughly LRU.
    _read_cache.move_to_end(key)
    return value


def _read_cache_put(key: tuple[str, str, int, int], value: str) -> None:
    if not _read_cache_enabled():
        return
    _read_cache[key] = value
    _read_cache.move_to_end(key)
    while len(_read_cache) > _READ_CACHE_MAX_ENTRIES:
        _read_cache.popitem(last=False)


def _read_cache_clear() -> None:
    """Test hook + manual flush (e.g. after a workspace reset)."""
    _read_cache.clear()


def _mark_workspace_file_read(ctx: Any, workspace_id: str, rel_path: str) -> None:
    try:
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            view = {}
            setattr(ctx, "loop_state_view", view)
        files_read = view.setdefault("files_read", {})
        if not isinstance(files_read, dict):
            files_read = {}
            view["files_read"] = files_read
        ws_reads = files_read.setdefault(str(workspace_id), [])
        norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
        if norm and norm not in ws_reads:
            ws_reads.append(norm)
    except Exception:
        log.debug("Failed to mark workspace file read", exc_info=True)


def _workspace_file_was_read(ctx: Any, workspace_id: str, rel_path: str) -> bool:
    try:
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            return False
        files_read = view.get("files_read")
        norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
        if isinstance(files_read, dict):
            raw = files_read.get(str(workspace_id)) or files_read.get("*") or []
            return norm in {
                str(item).replace("\\", "/").strip().lstrip("/") for item in raw
            }
        if isinstance(files_read, (list, set, tuple)):
            return norm in {
                str(item).replace("\\", "/").strip().lstrip("/") for item in files_read
            }
    except Exception:
        log.debug("Failed to inspect workspace read set", exc_info=True)
    return False


def read_workspace_file(
    ctx: Any,
    workspace_id: str,
    file_path: str,
    max_chars: int = 30000,
) -> str:
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        file_path = _strip_workspace_prefix(workspace_id, file_path)
        target = _resolve_workspace_file(workspace_root, file_path)
        if target is None or not target.is_file():
            return (
                f"Workspace file not found: {file_path}. "
                "Hint: call `list_workspace_files` first to see exact filenames "
                "(unicode normalization differences between NFC/NFD have already "
                "been tried automatically; if you still get 'not found', the "
                "file genuinely is not there)."
            )
        cap = max(500, _coerce_int(max_chars, 30000))
        # Build cache key including ``mtime_ns`` so any write through
        # ``update_workspace_seed`` (or any other path) invalidates the
        # cached content automatically — no manual invalidation needed.
        try:
            mtime_ns = target.stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        cache_key = (
            str(workspace_id),
            str(target.resolve()),
            int(mtime_ns),
            int(cap),
        )
        cached = _read_cache_get(cache_key)
        if cached is not None:
            _mark_workspace_file_read(ctx, workspace_id, file_path)
            return cached
        content, truncated, content_kind = read_file_preview(target, max_chars=cap)
        payload = _json(
            {
                "workspace_id": workspace_id,
                "file_path": file_path,
                "resolved_name": target.name,
                "content_kind": content_kind,
                "truncated": truncated,
                "content": content,
            }
        )
        _read_cache_put(cache_key, payload)
        _mark_workspace_file_read(ctx, workspace_id, file_path)
        return payload
    except Exception as e:
        return f"WARNING: workspace read error: {e}"


_RUN_WORKSPACE_DEFAULT_TIMEOUT_S = 180
# Hard cap on per-tool-call wall time. Even if the LLM passes a huge value,
# we never let a single shell invocation eat more than this. This is the
# core defense against interactive commands (e.g. `frotz zork1.z5`,
# `python -i`, `psql`, `vim`) that block on a TTY and would otherwise
# silently consume the full task budget.
_RUN_WORKSPACE_MAX_TIMEOUT_S = 600


_SECRET_PATH_MARKERS = (
    ".env",
    "credentials",
    "credential",
    "secrets",
    "secret",
    "api_key",
    "apikey",
)


def _command_secret_read_reason(cmd: list[str]) -> str:
    lowered = " ".join(str(part).lower().replace("\\", "/") for part in cmd)
    for marker in _SECRET_PATH_MARKERS:
        if marker in lowered:
            return (
                f"command references secret-like path/token `{marker}`; "
                "use a dedicated config reader that redacts values instead of shelling it"
            )
    return ""


def _command_workspace_mutation_reason(cmd: list[str]) -> str:
    """Detect common shell-write patterns that bypass code-write guards."""
    lowered = [str(part).strip().lower() for part in cmd]
    joined = " ".join(lowered)

    if not lowered:
        return ""

    if lowered[0] in {"powershell", "pwsh", "powershell.exe", "pwsh.exe"}:
        write_verbs = (
            "set-content",
            "add-content",
            "out-file",
            "remove-item",
            "copy-item",
            "move-item",
            "new-item",
            "clear-content",
        )
        if any(verb in joined for verb in write_verbs):
            return "PowerShell file mutation is blocked; next use update_workspace_seed for edits or delete_workspace_file for cleanup"

    if lowered[0] in {"cmd", "cmd.exe"}:
        cmdline = " ".join(
            lowered[2:] if len(lowered) >= 2 and lowered[1] == "/c" else lowered[1:]
        )
        mutating = ("del ", "erase ", "copy ", "move ", "ren ", "rename ", "echo ")
        if ">" in cmdline or any(token in f" {cmdline} " for token in mutating):
            return "cmd.exe file mutation is blocked; next use update_workspace_seed for edits or delete_workspace_file for cleanup"

    if (
        lowered[0] in {"python", "python.exe", "py"}
        and len(lowered) >= 3
        and lowered[1] == "-c"
    ):
        reason = _python_c_workspace_mutation_reason(str(cmd[2]))
        if reason:
            return reason

    return ""


def _python_c_workspace_mutation_reason(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ""
    mutating_attrs = {
        "write",
        "writelines",
        "write_text",
        "write_bytes",
        "unlink",
        "remove",
        "rmdir",
        "rename",
        "replace",
        "mkdir",
        "touch",
    }
    mutating_modules = {"shutil", "subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "open":
                if _open_call_is_mutating(node):
                    return "python -c file mutation is blocked; next use update_workspace_seed for edits, delete_workspace_file for cleanup, or read_workspace_file/probe_input_file for reads"
            if isinstance(fn, ast.Attribute):
                if fn.attr in mutating_attrs:
                    return "python -c file mutation is blocked; use update_workspace_seed/delete_workspace_file for workspace mutations"
                if isinstance(fn.value, ast.Name) and fn.value.id in mutating_modules:
                    return "python -c subprocess/shutil is blocked; use sanctioned workspace tools instead"
    return ""


def _open_call_is_mutating(node: ast.Call) -> bool:
    mode = ""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = str(node.args[1].value or "")
    for kw in node.keywords or []:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = str(kw.value.value or "")
    if not mode:
        return False
    return any(flag in mode for flag in ("w", "a", "x", "+"))


def _strip_redundant_workspace_cd_script(script: str, workspace_id: str) -> str:
    """Remove ``cd workspaces/<id>`` when cwd is already the workspace root."""

    wid = workspace_id.strip()
    if not wid:
        return script
    esc = re.escape(wid)
    pat = re.compile(rf"(?is)^\s*cd\s+((?:\./)?)workspaces/{esc}(?:/)?\s*(&&|;)\s*")
    new_script, n = pat.subn("", script, count=1)
    return new_script if n else script


def _rewrite_pip_install_script(script: str) -> str:
    """Prefer ``python -m pip install`` so the same interpreter sees packages."""

    s = script.strip()
    m = re.match(r"(?is)^(?P<prefix>\s*)(?P<pip>pip3?)\s+(?P<rest>install\b.*)$", s)
    if not m:
        return script
    return f"{m.group('prefix')}python -m pip {m.group('rest')}"


def _normalize_workspace_shell_script(script: str, workspace_id: str) -> str:
    inner = _strip_redundant_workspace_cd_script(script, workspace_id)
    inner = _rewrite_pip_install_script(inner)
    return inner


def _maybe_rewrite_workspace_command(cmd: list[str], workspace_id: str) -> list[str]:
    if len(cmd) < 3:
        return cmd
    prog, flag = cmd[0].lower(), cmd[1].lower()
    if prog not in {"bash", "sh"} or flag not in {"-c", "-lc"}:
        return cmd
    inner = cmd[2]
    new_inner = _normalize_workspace_shell_script(inner, workspace_id)
    if new_inner == inner:
        return cmd
    return [cmd[0], cmd[1], new_inner]


def run_workspace_command(
    ctx: Any,
    workspace_id: str,
    argv: list[str] | str | None = None,
    command: list[str] | str | None = None,
    subdir: str = "",
    timeout_seconds: int = _RUN_WORKSPACE_DEFAULT_TIMEOUT_S,
    allow_dependency_install: bool = False,
) -> str:
    """Run non-interactive checks/tests inside a host-repo workspace."""
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        cwd = _workspace_path(workspace_root, subdir)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="run_workspace_command", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        raw_command = argv if argv is not None else command
        if raw_command is None:
            return _json(
                {
                    "status": "invalid_command",
                    "workspace_id": workspace_id,
                    "hint": "Missing command payload. Pass either `argv` or `command`.",
                    "next_step": (
                        "Use any command shape that fits the task: "
                        "`argv` as a JSON array of strings is preferred, but "
                        "`command` is also accepted for backward compatibility."
                    ),
                }
            )
        cmd, norm_err = _try_normalize_command(raw_command)
        if norm_err:
            return _json(
                {
                    "status": "invalid_command",
                    "workspace_id": workspace_id,
                    "hint": norm_err,
                    "next_step": (
                        "Pass either `argv` as a JSON array of strings or `command` "
                        "as a string/list. `argv` is preferred because it is easier "
                        "for the repair layer to preserve, but the tool intentionally "
                        "does not restrict what program or flags you can run."
                    ),
                }
            )
        cmd = _maybe_rewrite_workspace_command(cmd, workspace_id)
        secret_reason = _command_secret_read_reason(cmd)
        if secret_reason:
            return _json(
                {
                    "status": "blocked",
                    "reason": "secret_path_guard",
                    "command": cmd,
                    "hint": secret_reason,
                    "next_step": (
                        "Do not read secret/config files through shell. If a test needs "
                        "environment variables, run the test directly and rely on the "
                        "process environment instead of printing secret files."
                    ),
                }
            )
        mutation_reason = _command_workspace_mutation_reason(cmd)
        if mutation_reason:
            return _json(
                {
                    "status": "blocked",
                    "reason": "workspace_mutation_guard",
                    "command": cmd,
                    "hint": mutation_reason,
                    "next_step": (
                        "Use `update_workspace_seed` for workspace file edits so the "
                        "harness can validate syntax, create backups, and record changes. "
                        "Use `run_workspace_command` only for read-only checks/tests."
                    ),
                }
            )
        if _looks_like_dependency_install(cmd) and not allow_dependency_install:
            return _json(
                {
                    "status": "blocked",
                    "reason": "dependency_install_guard",
                    "command": cmd,
                    "next_step": (
                        "Record the missing dependency and either use existing project tooling "
                        "or call again with allow_dependency_install=true after explaining why "
                        "the install is required."
                    ),
                }
            )
        is_server, server_hint = _looks_like_blocking_server(
            cmd, cwd=cwd, workspace_root=workspace_root
        )
        if is_server:
            return _json(
                {
                    "status": "blocked",
                    "reason": "blocking_server_in_foreground",
                    "command": cmd,
                    "matched": server_hint,
                    "hint": (
                        f"This command looks like a long-running server "
                        f"(matched `{server_hint}`). run_workspace_command is FOREGROUND -- "
                        "it would block until the per-call timeout fires and possibly "
                        "leak a port-bound process."
                    ),
                    "next_step": (
                        "Use the workspace verification/http_boot path or a background "
                        "server-aware tool. Do not run this server in the foreground."
                    ),
                }
            )
        is_interactive_launch, launch_hint = _looks_like_interactive_app_launch(cmd)
        if is_interactive_launch:
            return _json(
                {
                    "status": "blocked",
                    "reason": "interactive_app_launch_guard",
                    "command": cmd,
                    "matched": launch_hint,
                    "hint": (
                        "Interactive local app launches are blocked in run_workspace_command "
                        "to avoid hanging/broken foreground sessions."
                    ),
                    "next_step": (
                        "Use non-interactive checks only: pytest/smoke commands, CLI test mode, "
                        "or import checks like `python -c \"import main; print('ok')\"`."
                    ),
                }
            )
        py_c_problem = _python_c_compound_problem(cmd)
        if py_c_problem:
            return _json(
                {
                    "status": "blocked",
                    "reason": "python_c_compound_statement",
                    "command": cmd,
                    "hint": py_c_problem,
                    "next_step": (
                        "Call `run_python_code` with `code` set to the multi-line script. "
                        "Don't try to cram `def`/`async def`/`for`/`if` into a single "
                        "`python -c` argument."
                    ),
                }
            )
        # Always clamp to the per-call hard cap, regardless of what the LLM
        # asked for. This prevents one bad interactive command from burning
        # the entire task wall-clock budget.
        try:
            requested = int(timeout_seconds)
        except (TypeError, ValueError):
            requested = _RUN_WORKSPACE_DEFAULT_TIMEOUT_S
        timeout = max(1, min(requested, _RUN_WORKSPACE_MAX_TIMEOUT_S))
        cmd = _rewrite_python_command_for_workspace(
            cmd, repo_root=repo_root, workspace_root=workspace_root
        )
        session = get_or_create_session(ctx, workspace_id)
        result = session.run(cmd, cwd=str(cwd), timeout=timeout)

        output = result.output
        # Scrollback gets the *full* slice (head/tail-truncated only as a
        # last resort), so the model can re-read prior terminal state in
        # the next round even after `_maybe_compact_history` drops the raw
        # tool message.
        try:
            _append_scrollback(
                ctx,
                workspace_id=workspace_id,
                command=cmd,
                result=result,
                cwd=str(cwd),
            )
        except Exception:
            log.debug("scrollback hook failed", exc_info=True)

        severity = "info" if result.exit_code == 0 else "error"
        if result.timed_out:
            severity = "error"
        event_tags = "command,validation,terminal,session"
        if result.session_recovered:
            event_tags += ",terminal_session_recovered"
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="command",
            summary=f"{' '.join(cmd)} -> exit {result.exit_code}",
            details=(output[:4000] if output else ""),
            severity=severity,
            tags=event_tags,
        )
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "cwd": str(cwd),
            "command": cmd,
            "exit_code": result.exit_code,
            "output": output,
            "backend": session.backend_name,
            "duration_seconds": round(result.duration_seconds, 3),
        }
        if result.timed_out:
            payload["timed_out"] = True
        if result.session_recovered:
            payload["terminal_session_recovered"] = True
        if result.truncated_head or result.truncated_tail:
            payload["truncated"] = True
        return _json(payload)
    except Exception as e:
        return f"WARNING: workspace command error: {e}"


def terminal_view(
    ctx: Any,
    workspace_id: str,
    last_lines: int = 200,
    grep: str = "",
) -> str:
    """Return the recent scrollback of the persistent shell for ``workspace_id``.

    Read-only. Use this to re-read what an earlier ``run_workspace_command``
    printed when the raw tool message was already compacted out of history.
    """
    try:
        get_or_create_session  # ensure import is alive even if session import fails
        session = get_or_create_session(ctx, workspace_id)
        try:
            requested = int(last_lines)
        except (TypeError, ValueError):
            requested = 200
        capped = max(1, min(requested, 4000))
        text = session.view(last_lines=capped, grep=(grep.strip() or None))
        # Hard cap so a noisy session can't blow past the tool-result limit.
        if len(text) > 60000:
            text = text[:30000] + "\n...(truncated)...\n" + text[-30000:]
        return _json(
            {
                "workspace_id": workspace_id,
                "backend": session.backend_name,
                "last_lines": capped,
                "grep": grep or None,
                "scrollback": text,
            }
        )
    except Exception as e:
        return f"WARNING: terminal_view error: {e}"


def run_python_code(
    ctx: Any,
    workspace_id: str,
    code: str,
    args: list[str] | None = None,
    subdir: str = "",
    timeout_seconds: int = _RUN_WORKSPACE_DEFAULT_TIMEOUT_S,
    use_uv: bool = True,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run a multi-line Python script inside a workspace via a temp file.

    This is the ergonomic way to execute scripts that contain ``def``,
    ``async def``, ``class``, loops, ``with`` blocks, ``try/except``, etc.
    Don't try to cram those into ``python -c "...; ...; ..."`` -- CPython
    parses the ``-c`` body as a single simple statement and SyntaxErrors
    on any compound block keyword joined with ``;``.

    The script is written to ``<workspace>/.umbrella_scratch/run_<id>.py``,
    then executed (``uv run python <file> [args]`` if ``use_uv`` else
    ``python <file> [args]``). Stdout+stderr are returned exactly as
    ``run_workspace_command`` would. The temp file is left in place for
    debugging; the directory is gitignored via ``.umbrella_scratch/``.
    """
    try:
        if not isinstance(code, str) or not code.strip():
            return _json(
                {
                    "status": "blocked",
                    "reason": "empty_code",
                    "hint": "Pass `code` as a non-empty string with the Python source to run.",
                }
            )
        # Validate the script *before* we spawn an interpreter -- the
        # SyntaxError shape mirrors what update_workspace_seed already
        # returns, so the model can fix the script in-place.
        import ast as _ast

        try:
            _ast.parse(code)
        except SyntaxError as syn:
            line_no = int(syn.lineno or 0)
            snippet = ""
            try:
                if line_no:
                    lines = code.splitlines()
                    snippet = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
            except Exception:
                snippet = ""
            return _json(
                {
                    "status": "blocked",
                    "reason": "python_syntax_error",
                    "error": f"{syn.msg} (line {syn.lineno}, col {syn.offset})",
                    "offending_line": snippet,
                    "next_step": "Fix the script and re-call run_python_code.",
                }
            )

        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        cwd = _workspace_path(workspace_root, subdir)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="run_python_code", workspace_id=workspace_id
        ):
            return _json(stop_payload)

        scratch_dir = workspace_root / ".umbrella_scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        # Keep .umbrella_scratch out of git.
        gi = scratch_dir / ".gitignore"
        if not gi.exists():
            try:
                gi.write_text("*\n", encoding="utf-8")
            except Exception:
                pass
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        # Short hash to make filenames unique across rapid calls.
        import hashlib

        digest = hashlib.sha1(code.encode("utf-8", errors="replace")).hexdigest()[:8]
        script_path = scratch_dir / f"run_{ts}_{digest}.py"
        # Always write with a trailing newline so the interpreter is happy
        # even if the LLM omitted one.
        body = code if code.endswith("\n") else code + "\n"
        script_path.write_text(body, encoding="utf-8")

        argv: list[str] = []
        if use_uv:
            argv = ["uv", "run", "python", str(script_path)]
        else:
            argv = ["python", str(script_path)]
        if args:
            argv.extend(str(a) for a in args)

        try:
            requested = int(timeout_seconds)
        except (TypeError, ValueError):
            requested = _RUN_WORKSPACE_DEFAULT_TIMEOUT_S
        timeout = max(1, min(requested, _RUN_WORKSPACE_MAX_TIMEOUT_S))

        session = get_or_create_session(ctx, workspace_id)
        env_overrides = dict(extra_env or {})
        if env_overrides:
            # OneShotBackend.run accepts env_overrides via kwargs only on
            # some backends; fall back to setting via os.environ for the
            # subprocess at the call site.
            saved = {k: os.environ.get(k) for k in env_overrides}
            os.environ.update({str(k): str(v) for k, v in env_overrides.items()})
            try:
                result = session.run(argv, cwd=str(cwd), timeout=timeout)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        else:
            result = session.run(argv, cwd=str(cwd), timeout=timeout)

        try:
            _append_scrollback(
                ctx,
                workspace_id=workspace_id,
                command=argv,
                result=result,
                cwd=str(cwd),
            )
        except Exception:
            log.debug("scrollback hook failed", exc_info=True)

        severity = "info" if result.exit_code == 0 else "error"
        if result.timed_out:
            severity = "error"
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="command",
            summary=f"run_python_code {script_path.name} -> exit {result.exit_code}",
            details=(result.output[:4000] if result.output else ""),
            severity=severity,
            tags="command,validation,terminal,session,python_script",
        )
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "cwd": str(cwd),
            "script_path": str(script_path.relative_to(workspace_root)).replace(
                "\\", "/"
            ),
            "argv": argv,
            "exit_code": result.exit_code,
            "output": result.output,
            "duration_seconds": round(result.duration_seconds, 3),
            "backend": session.backend_name,
        }
        if result.timed_out:
            payload["timed_out"] = True
        return _json(payload)
    except Exception as e:
        log.error("run_python_code failed: %s", e, exc_info=True)
        return f"WARNING: run_python_code error: {e}"


def terminal_reset(
    ctx: Any,
    workspace_id: str,
    reason: str = "",
) -> str:
    """Kill the persistent shell for ``workspace_id`` and start a fresh one.

    All state (cwd, env vars, background jobs) is dropped. The agent must
    pass ``reason`` so the reset is recorded as an explicit decision.
    """
    try:
        if not str(reason).strip():
            return _json(
                {
                    "status": "blocked",
                    "reason": "missing_reason",
                    "hint": (
                        "terminal_reset destroys all in-shell state. Pass `reason` "
                        "explaining why a reset is justified before calling again."
                    ),
                }
            )
        session = get_or_create_session(ctx, workspace_id)
        old_backend = session.backend_name
        session.reset()
        try:
            record_workspace_event(
                ctx,
                workspace_id=workspace_id,
                event_type="terminal_reset",
                summary=f"terminal_reset: {reason.strip()[:160]}",
                details=(
                    f"Backend before reset: {old_backend}\n"
                    f"Backend after reset:  {session.backend_name}\n"
                    f"Reason: {reason.strip()}"
                ),
                severity="warning",
                tags="terminal,session,reset",
            )
        except Exception:
            log.debug("terminal_reset event log failed", exc_info=True)
        return _json(
            {
                "status": "reset",
                "workspace_id": workspace_id,
                "backend": session.backend_name,
                "reason": reason.strip(),
            }
        )
    except Exception as e:
        return f"WARNING: terminal_reset error: {e}"


def _try_normalize_command(command: list[str] | str) -> tuple[list[str] | None, str]:
    """Return (argv, error_message). error_message empty on success."""
    try:
        cmd = _normalize_command(command)
    except ValueError as e:
        return None, str(e)
    for part in cmd:
        if "\n" in part or "\r" in part:
            return (
                None,
                "command argv contains embedded newlines; use a list of strings or a one-line shell command.",
            )
    if not cmd:
        return None, "command parsed to an empty argv."
    return cmd, ""


def _strip_balanced_outer_quotes(value: str) -> str:
    stripped = value.strip()
    if (
        len(stripped) < 2
        or stripped[0] != stripped[-1]
        or stripped[0] not in {'"', "'"}
    ):
        return value
    inner = stripped[1:-1]
    if stripped[0] == '"':
        return inner.replace('\\"', '"')
    return inner.replace("\\'", "'")


def _repair_interpreter_payload_quotes(argv: list[str]) -> list[str]:
    normalized = [str(part) for part in argv]

    def _unwrap_at(index: int) -> None:
        if index < len(normalized):
            normalized[index] = _strip_balanced_outer_quotes(normalized[index])

    lowered = [part.lower() for part in normalized[:4]]
    if (
        len(normalized) >= 3
        and lowered[:2]
        and lowered[0] in {"python", "python3", "py"}
        and lowered[1] == "-c"
    ):
        _unwrap_at(2)
    elif (
        len(normalized) >= 5
        and lowered[:4]
        and lowered[0] == "uv"
        and lowered[1] == "run"
        and lowered[2] in {"python", "python3", "py"}
        and lowered[3] == "-c"
    ):
        _unwrap_at(4)
    elif (
        len(normalized) >= 3
        and lowered[:2]
        and lowered[0] in {"powershell", "pwsh"}
        and lowered[1] == "-command"
    ):
        _unwrap_at(2)
    elif (
        len(normalized) >= 3
        and lowered[:2]
        and lowered[0] in {"bash", "sh"}
        and lowered[1] in {"-c", "-lc"}
    ):
        _unwrap_at(2)
    return normalized


def _normalize_command(command: list[str] | str) -> list[str]:
    def _strip_balanced_outer_quotes(value: str) -> str:
        stripped = value.strip()
        if (
            len(stripped) < 2
            or stripped[0] != stripped[-1]
            or stripped[0] not in {'"', "'"}
        ):
            return value
        inner = stripped[1:-1]
        if stripped[0] == '"':
            return inner.replace('\\"', '"')
        return inner.replace("\\'", "'")

    def _repair_interpreter_payload_quotes(argv: list[str]) -> list[str]:
        normalized = [str(part) for part in argv]

        def _unwrap_at(index: int) -> None:
            if index < len(normalized):
                normalized[index] = _strip_balanced_outer_quotes(normalized[index])

        lowered = [part.lower() for part in normalized[:4]]
        if (
            len(normalized) >= 3
            and lowered[:2]
            and lowered[0] in {"python", "python3", "py"}
            and lowered[1] == "-c"
        ):
            _unwrap_at(2)
        elif (
            len(normalized) >= 5
            and lowered[:4]
            and lowered[0] == "uv"
            and lowered[1] == "run"
            and lowered[2] in {"python", "python3", "py"}
            and lowered[3] == "-c"
        ):
            _unwrap_at(4)
        elif (
            len(normalized) >= 3
            and lowered[:2]
            and lowered[0] in {"powershell", "pwsh"}
            and lowered[1] == "-command"
        ):
            _unwrap_at(2)
        elif (
            len(normalized) >= 3
            and lowered[:2]
            and lowered[0] in {"bash", "sh"}
            and lowered[1] in {"-c", "-lc"}
        ):
            _unwrap_at(2)
        return normalized

    if isinstance(command, str):
        stripped = command.strip()
        if "\n" in command and not stripped.startswith("["):
            raise ValueError(
                "command string contains raw newlines; use a JSON array of strings instead."
            )
        try:
            parsed = json.loads(command)
            if isinstance(parsed, list):
                return _repair_interpreter_payload_quotes(
                    [str(part) for part in parsed]
                )
            if isinstance(parsed, str):
                return _repair_interpreter_payload_quotes(
                    shlex.split(parsed, posix=os.name != "nt")
                )
        except json.JSONDecodeError:
            pass
        try:
            return _repair_interpreter_payload_quotes(
                shlex.split(command, posix=os.name != "nt")
            )
        except ValueError as e:
            raise ValueError(f"cannot parse command as shell: {e}") from e
    if isinstance(command, list):
        return _repair_interpreter_payload_quotes([str(part) for part in command])
    raise ValueError("command must be a list of strings or a shell-like string")


def _looks_like_dependency_install(cmd: list[str]) -> bool:
    lowered = [part.lower() for part in cmd]
    if not lowered:
        return False
    package_managers = {"pip", "pip3", "uv", "poetry", "npm", "pnpm", "yarn"}
    if lowered[0] in package_managers and any(
        part in {"install", "add", "sync"} for part in lowered[1:]
    ):
        return True
    if len(lowered) >= 4 and lowered[1:3] == ["-m", "pip"] and "install" in lowered[3:]:
        return True
    return False


# Patterns that almost always mean "long-running blocking server" --
# these are footguns when launched via the foreground run_workspace_command
# because they only return after the per-call timeout fires.
_SERVER_TOKEN_PATTERNS = (
    "uvicorn",
    "gunicorn",
    "hypercorn",
    "daphne",
    "vllm",
    "streamlit",
    "tensorboard",
    "jupyter",
    "nodemon",
    "vite",
    "webpack-dev-server",
)
# Substrings inside any token that look like "server" intent without
# matching the patterns above (e.g. `python -m my.server`).
_SERVER_TOKEN_SUBSTRINGS = ("runserver", "serve_forever")
_SERVER_SOURCE_MARKERS = (
    "uvicorn.run(",
    "app.run(",
    ".serve_forever(",
    "fastapi(",
)
_INTERACTIVE_APP_ENTRY_NAMES = {
    "main.py",
    "app.py",
    "game.py",
    "run.py",
    "play.py",
}
_INTERACTIVE_APP_MODULE_NAMES = {
    "main",
    "app",
    "game",
    "play",
}


def _looks_like_blocking_server(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    workspace_root: Path | None = None,
) -> tuple[bool, str]:
    """Return (looks_like_server, matched_hint).

    The check is deliberately conservative: we want to refuse the *common*
    footguns (uvicorn, gunicorn, dev servers) without blocking legitimate
    short-lived commands that happen to mention these names.
    """
    if not cmd:
        return False, ""
    lowered = [str(part).lower() for part in cmd]

    # Python inline checks like:
    #   python -c "import fastapi; print('ok')"
    # must be allowed (they are short-lived import probes, not servers).
    def _python_c_body(parts: list[str]) -> str:
        if (
            len(parts) >= 3
            and parts[0] in {"python", "python3", "py"}
            and parts[1] == "-c"
        ):
            return parts[2]
        if (
            len(parts) >= 5
            and parts[0] == "uv"
            and parts[1] == "run"
            and parts[2] in {"python", "python3", "py"}
            and parts[3] == "-c"
        ):
            return parts[4]
        return ""

    body = _python_c_body(lowered)
    if body:
        if "uvicorn.run(" in body or "app.run(" in body or ".serve(" in body:
            return True, "python -c with .run("
        return False, ""

    # Direct server launchers by executable name.
    for pat in _SERVER_TOKEN_PATTERNS:
        if lowered[0] == pat:
            return True, pat
        if (
            len(lowered) >= 3
            and lowered[0] == "uv"
            and lowered[1] == "run"
            and lowered[2] == pat
        ):
            return True, pat

    # Common multi-token foreground server launches.
    if len(lowered) >= 2 and lowered[0] == "flask" and lowered[1] == "run":
        return True, "flask run"
    if len(lowered) >= 2 and lowered[0] == "next" and lowered[1] in {"dev", "start"}:
        return True, f"next {lowered[1]}"
    if len(lowered) >= 2 and lowered[0] == "rails" and lowered[1] == "server":
        return True, "rails server"
    if len(lowered) >= 2 and lowered[0] == "manage.py" and lowered[1] == "runserver":
        return True, "manage.py runserver"
    if len(lowered) >= 2 and lowered[0] == "ray" and lowered[1] == "start":
        return True, "ray start"
    if len(lowered) >= 2 and lowered[0] == "celery" and lowered[1] == "worker":
        return True, "celery worker"
    if len(lowered) >= 2 and lowered[0] == "rq" and lowered[1] == "worker":
        return True, "rq worker"
    if len(lowered) >= 2 and lowered[0] == "ollama" and lowered[1] in {"serve", "run"}:
        return True, f"ollama {lowered[1]}"

    # Generic token check still useful for obvious "runserver" binaries.
    for tok in lowered:
        for sub in _SERVER_TOKEN_SUBSTRINGS:
            if sub in tok:
                return True, sub
    script = _python_script_target(cmd)
    if script:
        if workspace_root is not None and _workspace_declares_http_boot_for_script(
            workspace_root, script
        ):
            return True, f"workspace verification declares server entry {script}"
        if cwd is not None and _script_contains_server_entry(cwd / script):
            return True, f"{script} contains server entrypoint"
    return False, ""


def _python_module_target(cmd: list[str]) -> str:
    """Return module target for `python -m package.module` commands."""
    if not cmd:
        return ""
    lowered = [part.lower() for part in cmd]
    start = 0
    if len(lowered) >= 3 and lowered[0] == "uv" and lowered[1] == "run":
        start = 2
        while start < len(lowered) and lowered[start].startswith("-"):
            start += 1
            if start < len(lowered) and lowered[start - 1] in {"--python", "-p"}:
                start += 1
    if start >= len(lowered) or lowered[start] not in {"python", "python3", "py"}:
        return ""
    idx = start + 1
    while idx < len(lowered):
        token = lowered[idx]
        if token == "-m" and idx + 1 < len(cmd):
            return str(cmd[idx + 1]).strip()
        if token == "-c":
            return ""
        idx += 1
    return ""


def _looks_like_interactive_app_launch(cmd: list[str]) -> tuple[bool, str]:
    """Detect likely interactive app entrypoint launches (game/UI loops)."""
    script = _python_script_target(cmd)
    if script:
        script_name = Path(script).name.lower()
        if script_name in _INTERACTIVE_APP_ENTRY_NAMES:
            return True, f"python script entrypoint `{script_name}`"
    module = _python_module_target(cmd)
    if module:
        tail = module.split(".")[-1].lower()
        if tail in _INTERACTIVE_APP_MODULE_NAMES:
            return True, f"python -m `{module}`"
    return False, ""


def _python_script_target(cmd: list[str]) -> str:
    """Return the Python script target for `python main.py`-style commands."""
    if not cmd:
        return ""
    lowered = [part.lower() for part in cmd]
    start = 0
    if len(lowered) >= 3 and lowered[0] == "uv" and lowered[1] == "run":
        start = 2
        while start < len(lowered) and lowered[start].startswith("-"):
            start += 1
            if start < len(lowered) and lowered[start - 1] in {"--python", "-p"}:
                start += 1
    if start >= len(lowered) or lowered[start] not in {"python", "python3", "py"}:
        return ""
    idx = start + 1
    while idx < len(cmd):
        token = cmd[idx]
        low = lowered[idx]
        if low == "-c" or low == "-m":
            return ""
        if low.startswith("-"):
            idx += 1
            continue
        if token.endswith(".py"):
            return token.replace("\\", "/")
        return ""
    return ""


def _script_contains_server_entry(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size > 512_000:
            return False
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(marker in text for marker in _SERVER_SOURCE_MARKERS)


def _workspace_declares_http_boot_for_script(workspace_root: Path, script: str) -> bool:
    config_path = workspace_root / "workspace.toml"
    if not config_path.exists():
        return False
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    script_name = Path(script).name.lower()
    server = data.get("server")
    if isinstance(server, dict):
        candidates = [
            server.get("entry"),
            server.get("entrypoint"),
            server.get("command"),
        ]
        if any(
            _command_mentions_script(candidate, script_name) for candidate in candidates
        ):
            return True
    verification = data.get("verification")
    steps = verification.get("steps") if isinstance(verification, dict) else None
    if isinstance(steps, list):
        for step in steps:
            if (
                isinstance(step, dict)
                and str(step.get("kind") or "").lower() == "http_boot"
            ):
                if _command_mentions_script(step.get("command"), script_name):
                    return True
    return False


def _command_mentions_script(command: Any, script_name: str) -> bool:
    if command is None:
        return False
    if isinstance(command, list):
        return any(
            Path(str(part).replace("\\", "/")).name.lower() == script_name
            for part in command
        )
    text = str(command).replace("\\", "/").lower()
    return (
        f"/{script_name}" in text
        or text.split()[-1:] == [script_name]
        or script_name in text.split()
    )


def _python_c_compound_problem(cmd: list[str]) -> str:
    """Detect the classic `python -c "import x; def foo(): ..."` footgun.

    `python -c` parses its body as a single ``simple_stmt``-style line and
    chokes on compound statements joined with ``;`` (``def``, ``async def``,
    ``class``, ``for``, ``while``, ``if``, ``try``). This function returns a
    short human-readable problem description, or an empty string if no
    issue was detected.

    Implementation notes:
      * We delegate to ``compile(body, '<string>', 'exec')`` so legitimate
        expressions that *contain* the keyword (list / set / dict
        comprehensions, generator expressions, conditional expressions) are
        not falsely rejected. The previous substring heuristic blocked e.g.
        ``python -c "from x import Y; doc=Y(p); print('\\n'.join(t.text for t in doc.paragraphs))"``
        because it spotted the ``for `` substring inside a generator
        expression.
      * Only ``SyntaxError`` is treated as a structural problem; other
        compile errors (NameError etc. don't happen at compile time) are
        ignored — we want to let the actual runtime decide.
    """
    body: str | None = None
    if (
        len(cmd) >= 3
        and cmd[0].lower() in {"python", "python3", "py"}
        and cmd[1] == "-c"
    ):
        body = cmd[2]
    elif (
        len(cmd) >= 5
        and cmd[0].lower() == "uv"
        and cmd[1].lower() == "run"
        and cmd[2].lower() in {"python", "python3", "py"}
        and cmd[3] == "-c"
    ):
        body = cmd[4]
    if not body or "\n" in body:
        return ""
    if ";" not in body:
        # No statement separator => python -c handles single-statement
        # bodies fine. Even ``python -c "for i in range(3): print(i)"``
        # parses as one compound statement on a single line.
        return ""
    try:
        compile(body, "<python -c>", "exec")
    except SyntaxError:
        return (
            "`python -c` cannot parse compound statements (def/async def/class/for/while/if/try) "
            "joined with `;` -- it parses the body as a single simple statement and raises "
            "SyntaxError on the first block keyword. Use `run_python_code` instead, which "
            "writes your code to a temp file and runs it."
        )
    except Exception:
        # Any non-syntax compile failure is not our concern; let the real
        # interpreter surface it.
        return ""
    return ""


def commit_workspace_changes(
    ctx: Any,
    workspace_id: str,
    commit_message: str,
    paths: list[str] | None = None,
    include_data: bool = False,
) -> str:
    """Commit workspace changes in the host repository. Never pushes."""
    try:
        disabled = _git_commit_disabled_payload(
            "commit_workspace_changes", workspace_id
        )
        if disabled:
            return disabled
        if not commit_message.strip():
            return "ERROR: commit_message must be non-empty."

        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="commit_workspace_changes", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not _workspace_verification_passed(ctx, workspace_id):
            return _json(
                {
                    "status": "blocked",
                    "reason": "verification_required_before_commit",
                    "workspace_id": workspace_id,
                    "next_step": (
                        "Run `run_workspace_verify` and fix any failures before "
                        "calling `commit_workspace_changes`. Local commits are only "
                        "allowed after a passing verification report."
                    ),
                }
            )
        workspace_prefix = workspace_root.relative_to(repo_root).as_posix()
        stagable = _collect_filtered_workspace_paths(
            repo_root,
            workspace_root,
            workspace_prefix,
            paths,
            include_data=include_data,
        )
        _enc = dict(encoding="utf-8", errors="replace")
        if not stagable:
            return _json(
                {
                    "status": "nothing_to_commit",
                    "workspace_id": workspace_id,
                    "reason": "no_stagable_paths_after_filter",
                    "filtered_out": (
                        ".memory/, __pycache__/, *.pyc, and (unless include_data=true) data/"
                    ),
                }
            )
        subprocess.run(
            ["git", "add", "--", *stagable],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            **_enc,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", *stagable],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            **_enc,
        )
        if not status.stdout.strip():
            return _json(
                {
                    "status": "nothing_to_commit",
                    "workspace_id": workspace_id,
                    "reason": "git_status_empty_after_add",
                }
            )
        commit = subprocess.run(
            ["git", "commit", "-m", commit_message, "--", *stagable],
            cwd=repo_root,
            capture_output=True,
            text=True,
            **_enc,
        )
        if commit.returncode != 0:
            return f"GIT_ERROR (commit): {commit.stderr or commit.stdout}"
        return _json(
            {
                "status": "committed_locally",
                "workspace_id": workspace_id,
                "commit_message": commit_message,
                "paths_committed": stagable,
                "push": "disabled_by_umbrella_policy",
                "stdout": commit.stdout.strip(),
            }
        )
    except Exception as e:
        return f"WARNING: workspace commit error: {e}"


def _excluded_workspace_rel(ws_rel: str, *, include_data: bool) -> bool:
    """ws_rel is path under workspaces/<id>/ (posix, no leading slash)."""
    try:
        from umbrella.verification.workspace_path_policy import (
            BUILTIN_SKIP_PATH_GLOBS,
            glob_matches_any,
        )

        if glob_matches_any(ws_rel, BUILTIN_SKIP_PATH_GLOBS):
            return True
    except Exception:
        pass
    norm = ws_rel.replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p]
    if ".memory" in parts:
        return True
    if ".umbrella_scratch" in parts:
        # Temp scripts created by run_python_code -- never commit them.
        return True
    if "__pycache__" in parts:
        return True
    if norm.endswith(".pyc") or norm.endswith(".pyo"):
        return True
    if not include_data and parts and parts[0] == "data":
        return True
    if ".venv" in parts or "node_modules" in parts or "vendor" in parts:
        return True
    return False


def _collect_filtered_workspace_paths(
    repo_root: Path,
    workspace_root: Path,
    workspace_prefix: str,
    paths: list[str] | None,
    *,
    include_data: bool,
) -> list[str]:
    """Repo-relative posix paths under workspace_prefix safe to `git add`."""
    candidates: list[str] = []
    if paths:
        for rel in paths:
            target = _workspace_path(workspace_root, rel)
            if target.is_file():
                candidates.append(target.relative_to(repo_root).as_posix())
            elif target.is_dir():
                # enumerate files under this subdir
                for f in target.rglob("*"):
                    if f.is_file():
                        candidates.append(f.relative_to(repo_root).as_posix())
    else:
        if not workspace_root.exists():
            return []
        for f in workspace_root.rglob("*"):
            if f.is_file():
                candidates.append(f.relative_to(repo_root).as_posix())

    seen: set[str] = set()
    out: list[str] = []
    for repo_rel in candidates:
        posix = repo_rel.replace("\\", "/")
        if not posix.startswith(workspace_prefix + "/") and posix != workspace_prefix:
            continue
        under = posix[len(workspace_prefix) :].lstrip("/")
        if _excluded_workspace_rel(under, include_data=include_data):
            continue
        if posix not in seen:
            seen.add(posix)
            out.append(posix)
    return sorted(out)


def _workspace_add_paths(
    repo_root: Path, workspace_root: Path, paths: list[str] | None
) -> list[str]:
    if not paths:
        return [workspace_root.relative_to(repo_root).as_posix()]
    result = []
    for rel in paths:
        target = _workspace_path(workspace_root, rel)
        result.append(target.relative_to(repo_root).as_posix())
    return result


def get_workspace_metrics(ctx: Any, workspace_id: str = "") -> str:
    try:
        from umbrella.telemetry import get_metrics_registry
        from umbrella.workspace_registry import WorkspaceRegistry

        repo_root = _resolve_umbrella_repo_root(ctx)
        metrics_registry = get_metrics_registry()
        registry = WorkspaceRegistry(root=repo_root)
        ws_ids = (
            [workspace_id] if workspace_id else registry.get_all_workspace_ids()[:10]
        )
        metrics = {}
        for ws_id in ws_ids:
            run_metrics = metrics_registry.get_run_metrics(ws_id)
            metrics[ws_id] = {
                "total_runs": run_metrics.total_runs,
                "successful_runs": run_metrics.successful_runs,
                "failed_runs": run_metrics.failed_runs,
                "partial_tasks": run_metrics.partial_tasks,
                "total_cost_usd": run_metrics.total_cost_usd,
                "avg_score": run_metrics.average_score,
            }
        return _json(metrics)
    except Exception as e:
        log.error("Metrics fetch failed: %s", e, exc_info=True)
        return f"WARNING: metrics error: {e}"


def get_workspace_logs(
    ctx: Any, workspace_id: str, run_id: str = "", tail: int = 100
) -> str:
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        instances_dir = repo_root / "workspaces" / workspace_id / "instances"
        if not instances_dir.exists():
            return f"No instances found for {workspace_id}"
        latest_log = _find_workspace_log(instances_dir, run_id=run_id)
        if not latest_log:
            return f"No logs found for {workspace_id}"
        lines = latest_log.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-max(1, min(int(tail), 2000)) :])
    except Exception as e:
        return f"WARNING: error reading logs: {e}"


def _find_workspace_log(instances_dir: Path, *, run_id: str = "") -> Path | None:
    latest_log = None
    latest_time = 0.0
    for instance_dir in instances_dir.iterdir():
        if run_id:
            log_file = instance_dir / "runs" / run_id / "agent.log"
            if log_file.exists():
                return log_file
        runs_dir = instance_dir / "runs"
        if not runs_dir.exists():
            continue
        for run_dir in runs_dir.iterdir():
            log_file = run_dir / "agent.log"
            if log_file.exists() and log_file.stat().st_mtime > latest_time:
                latest_time = log_file.stat().st_mtime
                latest_log = log_file
    return latest_log


_ROOT_DIAGNOSTIC_WRITE_RE = re.compile(
    r"(?i)^(?:"
    r"(?:check|debug|diagnose|extract|find|fix|inspect|probe|read|scan|scratch|search|verify|validate)_.*\.py|"
    r"run_(?:check|checks|verification|dry_run|manual_.*|news_.*)\.py|"
    r"test_minimal_.*\.py|real_test_.*\.py|test_.*_output\.py"
    r")$"
)
_ROOT_DOC_WRITE_RE = re.compile(
    r"(?i)^(?:handoff.*|agent_topology.*|architecture|agent_.*|.*_handoff|.*_topology)\.md$"
)
# Files matching this regex are diagnostic / probe scripts no matter
# where they live in the workspace. ``docs/check_format.py``,
# ``src/scripts/probe_docx.py``, ``src/scripts/read_requirements.py``
# etc. all fall here — they are the classic agent-debugging debris that
# the noise sweep flags but the write-time guard previously let through.
_DIAGNOSTIC_SCRIPT_BASENAME_RE = re.compile(
    r"(?i)^(?:"
    r"(?:check|debug|diagnose|extract|find|fix|inspect|probe|read|scan|scratch|search|verify|validate)_.*\.py|"
    r"run_(?:check|checks|verification|dry_run|manual_.*|news_.*)\.py|"
    r"test_minimal_.*\.py|real_test_.*\.py|test_.*_output\.py"
    r")$"
)
# Raw-extracted artefacts that should never be checked in as a
# deliverable. ``docs/requirements_raw.txt``, ``docs/template_analysis_raw.md``
# etc. The agent should produce a clean ``docs/requirements.md`` instead
# and delete these via ``delete_workspace_file``.
_RAW_ARTIFACT_BASENAME_RE = re.compile(
    r"(?i).*(?:_raw|_raw_extracted|_extracted)\.(?:txt|md|json|csv|tsv)$"
)


def _workspace_layout_policy_block(rel_path: str) -> dict[str, Any] | None:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts:
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": rel_path,
            "message": "file_path must name a workspace-relative file.",
        }

    name = parts[-1]
    lower_name = name.lower()
    top = parts[0].lower()
    if len(parts) == 1 and _ROOT_DIAGNOSTIC_WRITE_RE.match(name):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": (
                "Ad-hoc diagnostic/test scripts must not be written into the "
                "workspace root."
            ),
            "next_step": (
                "Use `run_workspace_command` to inspect data live (no file), "
                "fold reusable logic into the package under `src/`, or call "
                "`delete_workspace_file` if a leftover probe is no longer "
                "needed. Real tests belong under `tests/`."
            ),
        }
    if len(parts) == 1 and _ROOT_DOC_WRITE_RE.match(name):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": "Non-README architecture/handoff docs belong under `docs/`.",
            "next_step": f"Use `docs/{name}` unless this file is the workspace README.",
        }
    if (
        len(parts) >= 2
        and top == "src"
        and lower_name.startswith("test_")
        and lower_name.endswith(".py")
    ):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": "Pytest test modules belong under `tests/`, not `src/`.",
            "next_step": f"Write this as `tests/{name}` or keep only production code under `src/`.",
        }
    # Diagnostic/probe Python scripts under ``docs/`` or ``src/scripts/``
    # are the production failure mode the user explicitly called out
    # (extract_requirements.py, probe_docx.py, read_requirements.py,
    # check_format.py). They get checked in, the noise sweep flags
    # them, the agent can't delete them, the run gets stuck.
    if len(parts) >= 2 and top in {"docs", "doc"} and lower_name.endswith(".py"):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": (
                "Python files do not belong under `docs/`. `docs/` is for "
                "Markdown/spec documentation only."
            ),
            "next_step": (
                "Run analysis with `run_workspace_command` (no script "
                "checked in) or move reusable code into `src/<pkg>/...`. "
                "If a leftover script is already on disk, remove it with "
                "`delete_workspace_file`."
            ),
        }
    if (
        len(parts) >= 3
        and top == "src"
        and parts[1].lower() == "scripts"
        and _DIAGNOSTIC_SCRIPT_BASENAME_RE.match(name)
    ):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": (
                "Ad-hoc `check_*/debug_*/probe_*/read_*/extract_*` etc. "
                "scripts must not live under `src/scripts/` either. Real "
                "CLI entrypoints can keep their name; one-off probes "
                "should be live `run_workspace_command` invocations, not "
                "checked-in files."
            ),
            "next_step": (
                "If the logic is reusable, give it a non-diagnostic name "
                "and place it under the package (e.g. "
                "`src/<pkg>/io/docx_loader.py`). Otherwise delete it with "
                "`delete_workspace_file`."
            ),
        }
    # Raw-extracted artefacts (``*_raw.txt`` etc.) are never deliverables.
    # Block them everywhere except inside ``.memory/`` (legitimate
    # scratch).
    if top not in {
        ".memory",
        ".umbrella",
        ".umbrella_scratch",
    } and _RAW_ARTIFACT_BASENAME_RE.match(name):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": (
                "Raw-extracted artefacts (`*_raw.*` / `*_extracted.*`) are "
                "scratch output, not deliverables."
            ),
            "next_step": (
                "Produce a clean version (`docs/requirements.md` etc.) and "
                "discard the raw blob. If it is already on disk, remove it "
                "with `delete_workspace_file`."
            ),
        }
    return None


def _python_syntax_block(file_path: str, content: str) -> dict[str, Any] | None:
    if not str(file_path or "").endswith(".py"):
        return None
    try:
        ast.parse(content)
    except SyntaxError as syn:
        snippet = ""
        try:
            line_no = int(syn.lineno or 0)
            if line_no:
                lines = content.splitlines()
                snippet = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        except Exception:
            snippet = ""
        return {
            "status": "blocked",
            "reason": "python_syntax_error",
            "file_path": file_path,
            "error": f"{syn.msg} (line {syn.lineno}, col {syn.offset})",
            "offending_line": snippet,
            "next_step": (
                "Re-emit Python source without escaped quotes; JSON encoding is "
                "handled by the transport layer automatically."
            ),
        }
    return None


def _workspace_line_delta(old_content: str, new_content: str) -> int:
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    return max(0, len(new_lines) - len(old_lines))


def _record_workspace_diff(
    ctx: Any,
    *,
    file_path: str,
    old_content: str,
    new_content: str,
    added_file: bool = False,
    deleted_file: bool = False,
) -> None:
    try:
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            view = {}
            setattr(ctx, "loop_state_view", view)
        diff = view.setdefault("subtask_diff", {})
        if not isinstance(diff, dict):
            diff = {}
            view["subtask_diff"] = diff
        norm = str(file_path or "").replace("\\", "/").strip().lstrip("/")
        added = (
            len(new_content.splitlines())
            if added_file
            else _workspace_line_delta(old_content, new_content)
        )
        entry = diff.setdefault(
            norm, {"lines_added": 0, "added_file": False, "deleted_file": False}
        )
        if isinstance(entry, dict):
            entry["lines_added"] = int(entry.get("lines_added") or 0) + int(added)
            entry["added_file"] = bool(entry.get("added_file")) or bool(added_file)
            entry["deleted_file"] = bool(entry.get("deleted_file")) or bool(
                deleted_file
            )
    except Exception:
        log.debug("Failed to record workspace diff", exc_info=True)


def _record_subtask_discovery_tool_call(ctx: Any, tool_name: str) -> None:
    try:
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            return
        counts = view.setdefault("subtask_discovery_calls_by_tool", {})
        if not isinstance(counts, dict):
            counts = {}
            view["subtask_discovery_calls_by_tool"] = counts
        counts[tool_name] = int(counts.get(tool_name) or 0) + 1
    except Exception:
        log.debug("Failed to record subtask discovery call", exc_info=True)


_STRONG_VERIFICATION_KINDS = {"shell", "pytest", "smoke_run"}


def _verification_steps_from_toml(text: str) -> list[dict[str, Any]]:
    try:
        data = tomllib.loads(text or "")
    except Exception:
        return []
    verification = data.get("verification") if isinstance(data, dict) else None
    if not isinstance(verification, dict):
        return []
    steps = verification.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _verification_step_name(step: dict[str, Any], index: int) -> str:
    value = (
        step.get("name")
        or step.get("id")
        or step.get("command")
        or step.get("path")
        or index
    )
    return str(value).strip()


def _verification_step_kind(step: dict[str, Any]) -> str:
    return str(step.get("kind") or step.get("type") or "").strip().lower()


_PHASE_MEMORY_TAGS: dict[str, set[str]] = {
    "planner": {"design", "architecture", "discovery", "prior_art"},
    "subtask": {"implementation", "skill", "gmas", "code_pattern", "subtask"},
    "implement": {"implementation", "skill", "gmas", "code_pattern", "subtask"},
    "review": {"review", "defect_pattern"},
    "remediation": {"verification_failure", "lesson", "bug_fix", "verify", "fail"},
}


def _preferred_memory_tags_for_phase(phase: str) -> set[str]:
    normalized = str(phase or "").lower()
    tags: set[str] = set()
    for key, values in _PHASE_MEMORY_TAGS.items():
        if key in normalized:
            tags.update(values)
    return tags


def _memory_hit_tags(hit: Any) -> set[str]:
    if isinstance(hit, dict):
        metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        raw = hit.get("tags") or metadata.get("tags")
        text = " ".join(
            str(hit.get(key) or "") for key in ("room", "hall", "content", "title")
        ).lower()
    else:
        raw = getattr(hit, "tags", None)
        text = " ".join(
            str(getattr(hit, attr, "") or "")
            for attr in (
                "change_summary",
                "expected_effect",
                "observed_effect",
                "context",
            )
        ).lower()
    tags: set[str] = set()
    if isinstance(raw, str):
        tags.update(
            part.strip().lower()
            for part in raw.replace(";", ",").split(",")
            if part.strip()
        )
    elif isinstance(raw, (list, tuple, set)):
        tags.update(str(part).strip().lower() for part in raw if str(part).strip())
    for marker in (
        "gmas",
        "verification_failure",
        "bug_fix",
        "implementation",
        "cleanup",
        "hygiene",
        "review",
    ):
        if marker in text:
            tags.add(marker)
    return tags


def _phase_rerank_memory_hits(items: list[Any], phase: str = "") -> list[Any]:
    preferred = _preferred_memory_tags_for_phase(phase)
    if not preferred or not items:
        return items
    scored = [
        (bool(_memory_hit_tags(item) & preferred), index, item)
        for index, item in enumerate(items)
    ]
    tagged = [item for matched, _index, item in scored if matched]
    if len(tagged) >= 3:
        return tagged
    return [
        item
        for _matched, _index, item in sorted(
            scored, key=lambda row: (not row[0], row[1])
        )
    ]


def _workspace_toml_verification_guard(
    seed_path: Path,
    rel_path: str,
    new_content: str,
) -> dict[str, Any] | None:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if norm != "workspace.toml":
        return None
    old_path = seed_path / "workspace.toml"
    if not old_path.is_file():
        return None
    try:
        old_content = old_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    old_steps = _verification_steps_from_toml(old_content)
    new_steps = _verification_steps_from_toml(new_content)
    if not old_steps:
        return None
    dropped_count = len(new_steps) < len(old_steps)
    old_by_name = {
        _verification_step_name(step, idx): _verification_step_kind(step)
        for idx, step in enumerate(old_steps)
    }
    new_by_name = {
        _verification_step_name(step, idx): _verification_step_kind(step)
        for idx, step in enumerate(new_steps)
    }
    missing_names = [name for name in old_by_name if name and name not in new_by_name]
    downgraded = [
        name
        for name, old_kind in old_by_name.items()
        if old_kind in _STRONG_VERIFICATION_KINDS
        and new_by_name.get(name) == "file_exists"
    ]
    replacement_strong_count = sum(
        1 for kind in new_by_name.values() if kind in _STRONG_VERIFICATION_KINDS
    )
    old_strong_count = sum(
        1 for kind in old_by_name.values() if kind in _STRONG_VERIFICATION_KINDS
    )
    dropped_strong = bool(missing_names) and replacement_strong_count < old_strong_count
    if dropped_count or downgraded or dropped_strong:
        return {
            "status": "blocked",
            "reason": "verification_self_weakening_blocked",
            "file_path": norm,
            "old_step_count": len(old_steps),
            "new_step_count": len(new_steps),
            "missing_steps": missing_names[:10],
            "downgraded_steps": downgraded[:10],
            "message": (
                "workspace.toml verification cannot be weakened during a run. "
                "Add stronger checks or fix existing checks instead of deleting/downgrading them."
            ),
            "next_step": (
                "Keep prior shell/pytest/smoke verification coverage and let "
                "umbrella.verification.spec_loader augment safety-critical local tests."
            ),
        }
    return None


def update_workspace_seed(
    ctx: Any,
    workspace_id: str,
    file_path: str,
    new_content: str,
    create_backup: bool = True,
    allow_large_overwrite: bool = False,
    validation_summary: str = "",
) -> str:
    try:
        from umbrella.control_plane.workspace_code_update import (
            update_seed_workspace_file,
        )

        coerced: Any = new_content
        if isinstance(coerced, dict) and isinstance(coerced.get("new_content"), str):
            coerced = coerced["new_content"]
        if not isinstance(coerced, str):
            actual_type = type(coerced).__name__
            sample = ""
            try:
                if isinstance(coerced, dict):
                    sample = "keys=" + ",".join(sorted(map(str, coerced.keys()))[:8])
            except Exception:
                sample = ""
            return _json(
                {
                    "status": "blocked",
                    "reason": "new_content_must_be_string",
                    "file_path": file_path,
                    "got_type": actual_type,
                    "sample": sample,
                    "hint": (
                        "`new_content` must be the raw Python/text source as a JSON string, "
                        "NOT an object. The wrapper fields you may have seen in past tool "
                        "results (new_content_len, new_content_sha256, new_content_truncated) "
                        "are OUTPUT-only metadata and must not be sent back as input."
                    ),
                    "next_step": (
                        "Re-emit the call with `new_content` as a single JSON string "
                        "containing the file contents."
                    ),
                }
            )

        new_content = coerced

        repo_root = _resolve_umbrella_repo_root(ctx)
        seed_path = _workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="update_workspace_seed", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not seed_path.exists():
            return f"Workspace not found: {workspace_id}"
        if gmas_block := _gmas_context_before_write_block(ctx, workspace_id, seed_path):
            return _json(gmas_block)
        file_path = _strip_workspace_prefix(workspace_id, file_path)
        if layout_block := _workspace_layout_policy_block(file_path):
            return _json(layout_block)
        if verification_block := _workspace_toml_verification_guard(
            seed_path, file_path, new_content
        ):
            return _json(verification_block)
        target = _workspace_path(seed_path, file_path)
        if syntax_block := _python_syntax_block(file_path, new_content):
            return _json(syntax_block)
        old_content_for_diff = ""
        added_file_for_diff = not target.exists()
        if target.exists() and target.is_file():
            old_content = target.read_text(encoding="utf-8", errors="replace")
            old_content_for_diff = old_content
            old_lines = old_content.count("\n") + 1
            new_lines = new_content.count("\n") + 1
            large_file = len(old_content) >= 20000 or old_lines >= 400
            suspicious_shrink = len(new_content) < max(
                12000, int(len(old_content) * 0.75)
            )
            suspicious_line_drop = new_lines < max(200, int(old_lines * 0.75))
            if large_file and (suspicious_shrink or suspicious_line_drop):
                if not allow_large_overwrite or not validation_summary.strip():
                    return _json(
                        {
                            "status": "blocked",
                            "reason": "large_file_overwrite_guard",
                            "file_path": file_path,
                            "old_chars": len(old_content),
                            "new_chars": len(new_content),
                            "old_lines": old_lines,
                            "new_lines": new_lines,
                            "next_step": (
                                "Read the file, make a smaller targeted change, or call again with "
                                "allow_large_overwrite=true and a validation_summary explaining why the "
                                "large replacement is correct."
                            ),
                        }
                    )
        result = update_seed_workspace_file(
            seed_path=seed_path,
            relative_file_path=file_path,
            new_content=new_content,
            create_backup=create_backup,
            backup_dir=repo_root / ".umbrella" / "backups",
        )
        if not result.applied:
            return f"Update failed: {result.error or 'unknown error'}"
        _record_workspace_diff(
            ctx,
            file_path=file_path,
            old_content=old_content_for_diff,
            new_content=new_content,
            added_file=added_file_for_diff,
        )
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="change",
            summary=f"Updated {file_path}",
            details=f"Backup: {result.backup_path or 'none'}",
            severity="info",
            tags="change,seed",
        )
        advisory = _gmas_first_write_advisory(
            ctx, repo_root=repo_root, workspace_id=workspace_id, file_path=file_path
        )
        body = f"Updated {file_path}\nBackup: {result.backup_path or 'none'}"
        if advisory:
            body += "\n\n" + advisory
        return body
    except Exception as e:
        log.error("Seed update failed: %s", e, exc_info=True)
        return f"WARNING: seed update error: {e}"


def apply_workspace_patch(
    ctx: Any,
    workspace_id: str,
    patch: str,
    validation_summary: str = "",
) -> str:
    try:
        from umbrella.control_plane.workspace_code_update import (
            update_seed_workspace_file,
        )
        from ouroboros.workspace_patch import (
            apply_update_to_text,
            parse_workspace_patch,
            text_from_add_lines,
        )

        repo_root = _resolve_umbrella_repo_root(ctx)
        seed_path = _workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="apply_workspace_patch", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not seed_path.exists():
            return f"Workspace not found: {workspace_id}"
        if gmas_block := _gmas_context_before_write_block(ctx, workspace_id, seed_path):
            return _json(gmas_block)
        try:
            operations = parse_workspace_patch(patch)
        except ValueError as exc:
            return _json(
                {
                    "status": "blocked",
                    "reason": "patch_parse_error",
                    "error": str(exc),
                    "next_step": "Re-emit an OpenAI-style patch envelope from *** Begin Patch to *** End Patch.",
                }
            )

        planned: list[dict[str, Any]] = []
        for op in operations:
            rel_path = _strip_workspace_prefix(workspace_id, op.path)
            if layout_block := _workspace_layout_policy_block(rel_path):
                return _json(layout_block)
            if op.action in {"update", "delete"} and not _workspace_file_was_read(
                ctx, workspace_id, rel_path
            ):
                return _json(
                    {
                        "status": "blocked",
                        "reason": "read_before_patch_required",
                        "file_path": rel_path,
                        "next_step": (
                            "Call `read_workspace_file` for this exact workspace-relative path "
                            "before using `apply_workspace_patch` to update or delete it."
                        ),
                    }
                )
            target = _workspace_path(seed_path, rel_path)
            old_content = ""
            new_content = ""
            if op.action == "update":
                if not target.is_file():
                    return _json(
                        {
                            "status": "blocked",
                            "reason": "patch_target_missing",
                            "file_path": rel_path,
                            "next_step": "Use `*** Add File:` for new files or read/list the workspace to find the right path.",
                        }
                    )
                old_content = target.read_text(encoding="utf-8", errors="replace")
                try:
                    new_content = apply_update_to_text(old_content, op.hunks, rel_path)
                except ValueError as exc:
                    return _json(
                        {
                            "status": "blocked",
                            "reason": "patch_hunk_mismatch",
                            "file_path": rel_path,
                            "error": str(exc),
                            "next_step": "Re-read the file and emit a patch with exact current context.",
                        }
                    )
            elif op.action == "add":
                if target.exists():
                    return _json(
                        {
                            "status": "blocked",
                            "reason": "patch_add_target_exists",
                            "file_path": rel_path,
                            "next_step": "Use `*** Update File:` after `read_workspace_file` for existing files.",
                        }
                    )
                new_content = text_from_add_lines(op.content_lines)
            elif op.action == "delete":
                if not target.is_file():
                    return _json(
                        {
                            "status": "blocked",
                            "reason": "patch_delete_target_missing",
                            "file_path": rel_path,
                        }
                    )
                old_content = target.read_text(encoding="utf-8", errors="replace")
            else:
                return _json(
                    {
                        "status": "blocked",
                        "reason": "unsupported_patch_action",
                        "action": op.action,
                    }
                )
            if op.action != "delete":
                if verification_block := _workspace_toml_verification_guard(
                    seed_path, rel_path, new_content
                ):
                    return _json(verification_block)
                if syntax_block := _python_syntax_block(rel_path, new_content):
                    return _json(syntax_block)
            planned.append(
                {
                    "action": op.action,
                    "path": rel_path,
                    "target": target,
                    "old_content": old_content,
                    "new_content": new_content,
                }
            )

        applied: list[str] = []
        backups: list[str] = []
        for item in planned:
            rel_path = str(item["path"])
            action = str(item["action"])
            if action == "delete":
                Path(item["target"]).unlink()
                _record_workspace_diff(
                    ctx,
                    file_path=rel_path,
                    old_content=str(item["old_content"]),
                    new_content="",
                    deleted_file=True,
                )
                applied.append(f"deleted {rel_path}")
                continue
            result = update_seed_workspace_file(
                seed_path=seed_path,
                relative_file_path=rel_path,
                new_content=str(item["new_content"]),
                create_backup=True,
                backup_dir=repo_root / ".umbrella" / "backups",
            )
            if not result.applied:
                return f"Patch update failed for {rel_path}: {result.error or 'unknown error'}"
            if result.backup_path:
                backups.append(str(result.backup_path))
            _record_workspace_diff(
                ctx,
                file_path=rel_path,
                old_content=str(item["old_content"]),
                new_content=str(item["new_content"]),
                added_file=(action == "add"),
            )
            verb = "added" if action == "add" else "updated"
            applied.append(f"{verb} {rel_path}")
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="change",
            summary="Applied workspace patch",
            details="; ".join(applied),
            severity="info",
            tags="change,seed,patch",
        )
        body = _json(
            {
                "status": "applied",
                "workspace_id": workspace_id,
                "applied": applied,
                "backups": backups[:5],
                "validation_summary": validation_summary,
            }
        )
        advisory = _gmas_first_write_advisory(
            ctx,
            repo_root=repo_root,
            workspace_id=workspace_id,
            file_path=applied[0] if applied else "",
        )
        return body + (("\n\n" + advisory) if advisory else "")
    except Exception as e:
        log.error("Workspace patch failed: %s", e, exc_info=True)
        return f"WARNING: workspace patch error: {e}"


def _gmas_first_write_advisory(
    ctx: Any,
    *,
    repo_root: Path,
    workspace_id: str,
    file_path: str,
) -> str:
    """Return a one-shot soft advisory when the first ``src/*.py`` write in
    a GMAS-active workspace happens without prior ``get_gmas_context`` /
    ``search_gmas_knowledge`` call inside the current task.

    Intentionally NON-BLOCKING. The hard pre-write gate that used to
    enforce this was removed because the agent learned to call
    ``get_gmas_context(query="placeholder")`` once just to satisfy it
    (cargo-cult behaviour observed in earlier synthetic GMAS gates).
    A soft, one-shot advisory keeps the signal — "you are about to
    write GMAS-relevant code, the in-repo GMAS library is the required
    stack, here is how to look up its API" — without creating a
    forced-ritual loop. Triggers at most once per task.
    """
    norm = str(file_path or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if (
        len(parts) < 2
        or parts[0].lower() != "src"
        or not parts[-1].lower().endswith(".py")
    ):
        return ""
    # Skip __init__ noise and tests (tests live under tests/ anyway,
    # but be defensive).
    if parts[-1].lower() in {"__init__.py"} or parts[-1].lower().startswith("test_"):
        return ""
    # The advisory only makes sense for GMAS-active workspaces. The
    # workspace skill detector caches its verdict in
    # ``workspaces/<id>/.memory/domains.json`` as
    # ``{"domains": ["multi_agent_gmas", ...]}``. Absent / unreadable
    # cache silently skips the advisory.
    try:
        domains_path = (
            repo_root / "workspaces" / workspace_id / ".memory" / "domains.json"
        )
        if not domains_path.is_file():
            return ""
        payload = json.loads(domains_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    raw = payload.get("domains") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return ""
    domains = {str(v).lower() for v in raw if str(v).strip()}
    if "multi_agent_gmas" not in domains:
        return ""
    # ctx accumulates already-emitted advisories so we don't repeat
    # ourselves across many writes.
    fired = getattr(ctx, "_gmas_advisory_fired_tasks", None)
    if fired is None:
        fired = set()
        try:
            setattr(ctx, "_gmas_advisory_fired_tasks", fired)
        except Exception:
            return ""
    task_id = str(getattr(ctx, "task_id", "") or "")
    if task_id in fired:
        return ""
    # Check the tools log to see whether the agent already called the
    # GMAS retrieval tools in this task. If they did, no advisory.
    try:
        drive_root = getattr(ctx, "drive_root", None)
        if drive_root is not None:
            tools_log = Path(drive_root) / "logs" / "tools.jsonl"
            if tools_log.is_file():
                gmas_tools = {"get_gmas_context", "search_gmas_knowledge"}
                with tools_log.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if task_id and str(event.get("task_id") or "") != task_id:
                            continue
                        if str(event.get("tool") or "") in gmas_tools:
                            fired.add(task_id)
                            return ""
    except Exception:
        log.debug("gmas advisory tools.jsonl scan failed", exc_info=True)
    fired.add(task_id)
    return (
        "[GMAS_FIRST_WRITE_ADVISORY]\n"
        "This is your first `src/*.py` write in a GMAS-active workspace "
        "and you have not called `get_gmas_context` / "
        "`search_gmas_knowledge` yet. The in-repo `gmas/` library is the "
        "required stack for LLM/agent/judge nodes; before the next "
        "write batch consider one call: "
        '`get_gmas_context(query="<the API you need — e.g. defining a '
        'tool agent, wiring a judge, registering a graph node>")`. '
        "This is an advisory, not a block — the current write went "
        "through. The advisory will not repeat in this task."
    )


# Workspace-relative paths that are NEVER allowed to be deleted via the
# sanctioned tool: they're either required by the contract (TASK_MAIN /
# README / workspace.toml) or part of the runtime substrate (.git,
# .umbrella, .memory). Matching is on the workspace-relative POSIX path
# with case-insensitive name compare for the basename.
_DELETE_PROTECTED_TOP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".umbrella",
        ".umbrella_scratch",
        ".memory",
        ".venv",
        "venv",
    }
)
_DELETE_PROTECTED_BASENAMES: frozenset[str] = frozenset(
    {
        "task_main.md",
        "workspace.toml",
        "verification.toml",
        "readme.md",
        "pyproject.toml",
        "requirements.txt",
    }
)


def _delete_validate_path(
    workspace_root: Path, workspace_id: str, file_path: str
) -> tuple[Path | None, str, dict[str, Any] | None]:
    """Return ``(resolved_path, rel_norm, blocked_payload)`` for the delete tool.

    Centralises every refusal reason so ``delete_workspace_file`` stays
    short and the AST size-budget test does not regress.
    """
    rel = _strip_workspace_prefix(workspace_id, file_path)
    rel_norm = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel_norm:
        return (
            None,
            "",
            {
                "status": "blocked",
                "reason": "file_path_required",
                "next_step": "Pass a non-empty workspace-relative file_path.",
            },
        )
    parts = [p for p in rel_norm.split("/") if p and p != "."]
    if not parts:
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "file_path_required",
                "file_path": rel_norm,
            },
        )
    if parts[0].lower() in _DELETE_PROTECTED_TOP_DIRS:
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "protected_directory",
                "file_path": rel_norm,
                "next_step": (
                    f"Files under `{parts[0]}/` are runtime substrate and "
                    "cannot be removed with this tool."
                ),
            },
        )
    if parts[-1].lower() in _DELETE_PROTECTED_BASENAMES:
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "protected_file",
                "file_path": rel_norm,
                "next_step": (
                    f"`{parts[-1]}` is required by the workspace contract; "
                    "edit via `update_workspace_seed`, never delete."
                ),
            },
        )
    try:
        target = _workspace_path(workspace_root, rel_norm)
    except ValueError as exc:
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "path_traversal",
                "file_path": rel_norm,
                "error": str(exc),
            },
        )
    if not target.exists():
        return (
            None,
            rel_norm,
            {
                "status": "not_found",
                "reason": "file_missing",
                "file_path": rel_norm,
            },
        )
    if target.is_dir():
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "is_directory",
                "file_path": rel_norm,
                "next_step": ("delete_workspace_file removes one file at a time."),
            },
        )
    if not target.is_file():
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "not_a_regular_file",
                "file_path": rel_norm,
            },
        )
    return target, rel_norm, None


def delete_workspace_file(
    ctx: Any,
    workspace_id: str,
    file_path: str,
    reason: str = "",
) -> str:
    """Sanctioned single-file delete for workspace cleanup.

    Without this, the agent has no way to remove the ad-hoc diagnostic
    scripts / extracted raw artifacts that the layout policy and final
    sweep flag during remediation: shell ``rm`` / ``del`` /
    ``Remove-Item`` and ``python -c "...unlink()..."`` are blocked on
    purpose, so the observed production failure mode was the agent
    identifying the noise correctly, attempting every shell variant,
    and surrendering with the pollution still on disk. The reason
    field is recommended (audit trail); empty reasons surface a
    warning but do not hard-fail.
    """
    try:
        if stop_payload := _stop_requested_block(
            ctx, tool_name="delete_workspace_file", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not workspace_id:
            return _json(
                {
                    "status": "blocked",
                    "reason": "workspace_id_required",
                    "next_step": "Pass the workspace_id of the workspace you are cleaning up.",
                }
            )
        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        if not workspace_root.exists():
            return _json(
                {
                    "status": "not_found",
                    "reason": "workspace_missing",
                    "workspace_id": workspace_id,
                }
            )
        target, rel_norm, blocked = _delete_validate_path(
            workspace_root, workspace_id, file_path
        )
        if blocked is not None or target is None:
            return _json(blocked or {"status": "error", "reason": "unknown"})
        try:
            byte_size = target.stat().st_size
        except OSError:
            byte_size = -1
        try:
            target.unlink()
        except OSError as exc:
            log.warning("delete_workspace_file: unlink failed for %s: %s", target, exc)
            return _json(
                {
                    "status": "error",
                    "reason": "unlink_failed",
                    "file_path": rel_norm,
                    "error": str(exc),
                }
            )
        reason_norm = (reason or "").strip()
        warning = (
            ""
            if reason_norm
            else (
                "delete_workspace_file called without a `reason`; future "
                "audits will not know why this file was removed."
            )
        )
        try:
            record_workspace_event(
                ctx,
                workspace_id=workspace_id,
                event_type="delete",
                summary=f"Deleted {rel_norm}",
                details=f"reason: {reason_norm or '(unspecified)'}\nbyte_size: {byte_size}",
                severity="warning",
                tags="cleanup,delete",
            )
        except Exception:
            log.debug("record_workspace_event after delete failed", exc_info=True)
        payload: dict[str, Any] = {
            "status": "deleted",
            "workspace_id": workspace_id,
            "file_path": rel_norm,
            "byte_size": byte_size,
            "reason": reason_norm,
        }
        if warning:
            payload["warning"] = warning
        return _json(payload)
    except Exception as e:
        log.error("delete_workspace_file failed: %s", e, exc_info=True)
        return f"WARNING: delete error: {e}"


_WORKSPACE_TOML_KNOWN_SKILLS: tuple[str, ...] = ("multi_agent_gmas",)


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _upsert_workspace_toml_skill(toml_text: str, skill_id: str, enabled: bool) -> str:
    """Add/update ``[skills] <skill_id> = <enabled>`` in TOML text.

    Hand-rolled because ``tomllib`` is read-only and we don't want to
    pull a 3rd-party writer just for this. Preserves surrounding
    formatting and comments by line-editing.
    """
    lines = toml_text.splitlines()
    in_skills = False
    skills_start: int | None = None
    skills_end: int | None = None
    rendered_value = _format_toml_value(enabled)

    for idx, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            if in_skills and skills_end is None:
                skills_end = idx
            if section.lower() == "skills":
                in_skills = True
                skills_start = idx
            else:
                in_skills = False
            continue
        if in_skills:
            key_part = stripped.split("=", 1)[0].strip()
            if key_part == skill_id:
                lines[idx] = f"{skill_id} = {rendered_value}"
                return "\n".join(lines) + ("\n" if toml_text.endswith("\n") else "")

    if skills_start is not None:
        insert_at = skills_end if skills_end is not None else len(lines)
        while insert_at > skills_start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"{skill_id} = {rendered_value}")
        return "\n".join(lines) + ("\n" if toml_text.endswith("\n") else "")

    block = ["", "[skills]", f"{skill_id} = {rendered_value}"]
    suffix = "\n" if toml_text.endswith("\n") or not toml_text else ""
    if not toml_text.strip():
        return "[skills]\n" + f"{skill_id} = {rendered_value}\n"
    return toml_text.rstrip("\n") + "\n" + "\n".join(block).lstrip("\n") + suffix


def _invalidate_skill_cache(repo_root: Path, workspace_id: str) -> None:
    """Drop ``active_skills.json`` so the next attempt re-detects skills."""
    try:
        from umbrella.integration.ouroboros_bridge import workspace_drive_root  # type: ignore
    except Exception:
        try:
            from umbrella.integration.ouroboros_bridge import (
                _drive_root_for as workspace_drive_root,
            )  # type: ignore
        except Exception:
            return
    try:
        drive_root = workspace_drive_root(repo_root, workspace_id)
        cache = Path(drive_root) / "state" / "active_skills.json"
        if cache.exists():
            cache.unlink()
    except Exception:
        log.debug("skill cache invalidation failed", exc_info=True)


def configure_workspace_skills(
    ctx: Any,
    workspace_id: str,
    skill_id: str,
    enabled: bool,
    reason: str = "",
) -> str:
    """Override a named workspace skill via ``workspace.toml``.

    GMAS (``multi_agent_gmas``) is auto-activated for tasks that touch
    an LLM/model/agent surface. Use this tool to record an explicit
    opt-out for pure non-LLM work, or to force-enable GMAS when the task
    wording is too sparse for detection.

    ``reason`` is stored in workspace event memory so future runs can
    audit the decision.
    """
    try:
        skill_id = (skill_id or "").strip().lower()
        if not skill_id:
            return _json({"status": "blocked", "reason": "skill_id_required"})
        if skill_id not in _WORKSPACE_TOML_KNOWN_SKILLS:
            return _json(
                {
                    "status": "blocked",
                    "reason": "unknown_skill",
                    "skill_id": skill_id,
                    "known": list(_WORKSPACE_TOML_KNOWN_SKILLS),
                    "hint": (
                        "This tool only knows skills that participate in "
                        "verification gates. For ad-hoc workspace.toml "
                        "edits use update_workspace_seed."
                    ),
                }
            )

        repo_root = _resolve_umbrella_repo_root(ctx)
        seed_path = _workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="configure_workspace_skills", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not seed_path.exists():
            return _json(
                {
                    "status": "blocked",
                    "reason": "workspace_not_found",
                    "workspace_id": workspace_id,
                }
            )

        toml_path = seed_path / "workspace.toml"
        original = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""

        try:
            current = tomllib.loads(original) if original.strip() else {}
        except Exception as exc:
            return _json(
                {
                    "status": "blocked",
                    "reason": "workspace_toml_unparseable",
                    "error": str(exc),
                    "next_step": (
                        "Read workspace.toml, fix the syntax with "
                        "update_workspace_seed, then call this tool again."
                    ),
                }
            )

        skills = current.get("skills") if isinstance(current, dict) else None
        existing_value = skills.get(skill_id) if isinstance(skills, dict) else None
        if existing_value is bool(enabled):
            _invalidate_skill_cache(repo_root, workspace_id)
            return _json(
                {
                    "status": "noop",
                    "skill_id": skill_id,
                    "enabled": bool(enabled),
                    "workspace_toml": str(toml_path.relative_to(repo_root)),
                    "note": "value already set; refreshed skill cache anyway",
                }
            )

        updated = _upsert_workspace_toml_skill(original, skill_id, bool(enabled))
        toml_path.write_text(updated, encoding="utf-8")
        _invalidate_skill_cache(repo_root, workspace_id)

        try:
            record_workspace_event(
                ctx,
                workspace_id=workspace_id,
                event_type="change",
                summary=f"workspace.toml: skills.{skill_id} = {bool(enabled)}",
                details=(reason or "no reason supplied"),
                severity="info",
                tags="change,workspace_toml,skill_opt",
            )
        except Exception:
            log.debug("record_workspace_event failed", exc_info=True)

        return _json(
            {
                "status": "ok",
                "skill_id": skill_id,
                "enabled": bool(enabled),
                "workspace_toml": str(toml_path.relative_to(repo_root)),
                "previous_value": existing_value,
                "reason": reason,
                "note": (
                    "Skill cache invalidated; the next attempt re-runs "
                    "detection with the new policy. GMAS verification "
                    "gates are removed — this only controls whether the "
                    "GMAS context artifact is built and whether the skill "
                    "shows up in the detected-skills banner."
                ),
            }
        )
    except Exception as exc:
        log.error("configure_workspace_skills failed: %s", exc, exc_info=True)
        return _json({"status": "error", "error": str(exc)})


def update_workspace_from_instance(
    ctx: Any,
    workspace_id: str,
    instance_name: str,
    files_to_copy: list[str],
) -> str:
    try:
        from umbrella.control_plane.workspace_code_update import (
            update_seed_workspace_from_instance,
        )
        from umbrella.workspace_runtime.models import WorkspaceInstance

        repo_root = _resolve_umbrella_repo_root(ctx)
        instance_path = (
            repo_root / "workspaces" / workspace_id / "instances" / instance_name
        )
        seed_path = _workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="update_workspace_from_instance", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not instance_path.exists():
            return f"Instance not found: {instance_name}"
        instance = WorkspaceInstance(
            path=instance_path,
            workspace_id=workspace_id,
            seed_workspace_id=workspace_id,
        )
        result = update_seed_workspace_from_instance(
            instance=instance,
            files_to_update=files_to_copy,
            seed_path=seed_path,
            create_backup=True,
        )
        if not result.applied:
            return f"Copy failed: {result.error or 'no files copied'}"
        return "Copied files:\n" + "\n".join(
            f"- {file}" for file in result.updated_files
        )
    except Exception as e:
        log.error("Instance-to-seed update failed: %s", e, exc_info=True)
        return f"WARNING: instance promotion error: {e}"


_UNVERIFIED_MEMORY_TAGS = {
    "candidate",
    "hypothesis",
    "unverified",
    "unverified_lesson",
}
_UNVERIFIED_MEMORY_ROOMS = {
    "ideas-hypothesis",
    "ideas-observation_from_log",
    "scratchpad",
    "terminal_scrollback",
}


def _memory_tags_from_value(value: Any) -> set[str]:
    raw_tags: Any = None
    if isinstance(value, dict):
        raw_tags = value.get("tags")
        meta = value.get("metadata")
        if raw_tags is None and isinstance(meta, dict):
            raw_tags = meta.get("tags")
    else:
        raw_tags = getattr(value, "tags", None)
    if raw_tags is None:
        return set()
    if isinstance(raw_tags, str):
        parts = raw_tags.replace(";", ",").split(",")
    else:
        try:
            parts = list(raw_tags)
        except TypeError:
            parts = [raw_tags]
    return {str(tag).strip().lower() for tag in parts if str(tag).strip()}


def _memory_evidence_kind(value: Any) -> str:
    if isinstance(value, dict):
        meta = value.get("metadata")
        if isinstance(meta, dict):
            return str(meta.get("evidence_kind") or "").strip().lower()
        return str(value.get("evidence_kind") or "").strip().lower()
    meta = getattr(value, "metadata", None)
    if isinstance(meta, dict):
        return str(meta.get("evidence_kind") or "").strip().lower()
    return ""


def _is_unverified_memory(value: Any) -> bool:
    tags = _memory_tags_from_value(value)
    if tags & _UNVERIFIED_MEMORY_TAGS:
        return True
    evidence_kind = _memory_evidence_kind(value)
    if evidence_kind and evidence_kind != "verified_outcome":
        return True
    if isinstance(value, dict):
        room = str(value.get("room") or "").strip().lower()
        if room in _UNVERIFIED_MEMORY_ROOMS:
            return True
    return False


def _split_verified_first(items: list[Any]) -> tuple[list[Any], list[Any]]:
    trusted: list[Any] = []
    unverified: list[Any] = []
    for item in items:
        if _is_unverified_memory(item):
            unverified.append(item)
        else:
            trusted.append(item)
    return trusted, unverified


def _lesson_is_verified(lesson: Any) -> bool:
    tags = _memory_tags_from_value(lesson)
    if tags & _UNVERIFIED_MEMORY_TAGS:
        return False
    try:
        priority = int(getattr(lesson, "priority", 0) or 0)
    except Exception:
        priority = 0
    return priority >= 5


def _resolve_memory_query_scope(palace_path: str, workspace_id: str) -> tuple[str, str]:
    room = ""
    if palace_path:
        parts = palace_path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "workspaces":
            workspace_id = workspace_id or parts[1]
            if len(parts) >= 3:
                room = parts[2]
        elif parts:
            room = parts[-1]
    return workspace_id, room


def _palace_lookup(
    repo_root: Path,
    *,
    workspace_id: str,
    room: str,
    query: str,
    limit: int,
    include_unverified: bool,
    phase: str = "",
) -> tuple[Any, list[Any], list[Any]]:
    palace = _palace_backend(repo_root, workspace_id)
    if query.strip():
        hits = palace.search(
            query, workspace_id=workspace_id, room=room, n_results=limit
        )
    else:
        hits = palace.recent(workspace_id=workspace_id, limit=limit)
    trusted_hits, unverified_hits = _split_verified_first(hits)
    palace_hits = (
        (trusted_hits + unverified_hits) if include_unverified else trusted_hits
    )
    palace_hits = _phase_rerank_memory_hits(palace_hits, phase)
    return palace, palace_hits, unverified_hits


def _lessons_lookup(
    repo_root: Path,
    *,
    workspace_id: str,
    query: str,
    limit: int,
    include_unverified: bool,
    phase: str = "",
) -> tuple[Any, list[Any], list[Any]]:
    from umbrella.memory.models import LessonType, MemoryQuery

    store_for_contrastive = _memory_store(repo_root, "")
    if workspace_id.strip():
        mq_ws = MemoryQuery(
            limit=max(limit * 4, 20), include_stale=False, workspace_id=workspace_id
        )
        lessons_ws = _memory_store(repo_root, workspace_id).query_lessons(mq_ws)
        mq_mgr = MemoryQuery(
            limit=max(limit * 4, 20),
            include_stale=False,
            lesson_type=LessonType.MANAGER,
        )
        lessons_mgr = store_for_contrastive.query_lessons(mq_mgr)
        lessons = _rank_lessons_for_query(lessons_ws + lessons_mgr, query, limit)
    else:
        mq = MemoryQuery(limit=max(limit * 4, 20), include_stale=False)
        lessons = store_for_contrastive.query_lessons(mq)
        lessons = _rank_lessons_for_query(lessons, query, limit)

    try:
        from umbrella.memory.relevance import deduplicate_lessons

        lessons = deduplicate_lessons(lessons)
    except Exception:
        log.debug("deduplicate_lessons skipped", exc_info=True)

    verified_lessons = [lesson for lesson in lessons if _lesson_is_verified(lesson)]
    unverified_lessons = [
        lesson for lesson in lessons if not _lesson_is_verified(lesson)
    ]
    lesson_hits = (
        (verified_lessons + unverified_lessons)
        if include_unverified
        else verified_lessons
    )
    lesson_hits = _phase_rerank_memory_hits(lesson_hits, phase)
    return store_for_contrastive, lesson_hits, unverified_lessons


def _hierarchical_ideas_lookup(
    repo_root: Path,
    *,
    workspace_id: str,
    query: str,
    palace_path: str,
    limit: int,
    include_unverified: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    from umbrella.memory.hierarchical import HierarchicalMemory
    from umbrella.memory.paths import manager_memory_root, workspace_memory_root

    hierarchical_ideas: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    if workspace_id.strip():
        hw = HierarchicalMemory(workspace_memory_root(repo_root, workspace_id))
        for rec in hw.query(
            query=query,
            palace_path=palace_path or "",
            workspace_id=workspace_id,
            limit=limit,
        ):
            d = rec.to_dict()
            if d["id"] not in seen_ids:
                seen_ids.add(d["id"])
                hierarchical_ideas.append(d)

    hm = HierarchicalMemory(manager_memory_root(repo_root))
    for rec in hm.query(
        query=query,
        palace_path=palace_path or "",
        workspace_id="",
        limit=limit,
    ):
        d = rec.to_dict()
        if d["id"] not in seen_ids:
            seen_ids.add(d["id"])
            hierarchical_ideas.append(d)

    verified_ideas = [
        idea for idea in hierarchical_ideas if not _is_unverified_memory(idea)
    ]
    unverified_ideas = [
        idea for idea in hierarchical_ideas if _is_unverified_memory(idea)
    ]
    idea_hits = (
        (verified_ideas + unverified_ideas) if include_unverified else verified_ideas
    )
    return idea_hits, verified_ideas, unverified_ideas


def _contrastive_lessons_lookup(
    store_for_contrastive: Any,
    *,
    query: str,
    workspace_id: str,
) -> dict[str, Any]:
    try:
        from umbrella.memory.contrastive import retrieve_contrastive_lessons

        return retrieve_contrastive_lessons(
            store_for_contrastive,
            query=query,
            workspace_id=workspace_id or None,
            limit_successes=3,
            limit_failures=3,
        )
    except Exception:
        log.debug("Contrastive retrieval failed in get_umbrella_memory", exc_info=True)
        return {}


def _publish_recall_state_to_ctx(
    ctx: Any,
    *,
    palace_hits: list[Any],
    lesson_hits: list[Any],
    verified_ideas: list[dict[str, Any]],
) -> None:
    try:
        total_verified = (
            (len(palace_hits) if isinstance(palace_hits, list) else 0)
            + (len(lesson_hits) if isinstance(lesson_hits, list) else 0)
            + (len(verified_ideas) if isinstance(verified_ideas, list) else 0)
        )
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            view = {}
            try:
                ctx.loop_state_view = view  # type: ignore[attr-defined]
            except Exception:
                view = None
        if isinstance(view, dict):
            view["last_memory_recall_empty"] = total_verified == 0
    except Exception:
        log.debug("get_umbrella_memory live-flag publish skipped", exc_info=True)


def get_umbrella_memory(
    ctx: Any,
    query: str = "",
    palace_path: str = "",
    limit: int = 10,
    workspace_id: str = "",
    include_unverified: bool = False,
) -> str:
    """Query Umbrella memory via MemPalace semantic search + structured lessons."""
    try:
        if isinstance(include_unverified, str):
            include_unverified = include_unverified.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        else:
            include_unverified = bool(include_unverified)
        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_id, room = _resolve_memory_query_scope(palace_path, workspace_id)
        phase = ""
        view = getattr(ctx, "loop_state_view", None)
        if isinstance(view, dict):
            phase = str(view.get("phase_label") or "")

        palace, palace_hits, unverified_hits = _palace_lookup(
            repo_root,
            workspace_id=workspace_id,
            room=room,
            query=query,
            limit=limit,
            include_unverified=include_unverified,
            phase=phase,
        )
        store_for_contrastive, lesson_hits, unverified_lessons = _lessons_lookup(
            repo_root,
            workspace_id=workspace_id,
            query=query,
            limit=limit,
            include_unverified=include_unverified,
            phase=phase,
        )
        idea_hits, verified_ideas, unverified_ideas = _hierarchical_ideas_lookup(
            repo_root,
            workspace_id=workspace_id,
            query=query,
            palace_path=palace_path,
            limit=limit,
            include_unverified=include_unverified,
        )
        contrastive = _contrastive_lessons_lookup(
            store_for_contrastive,
            query=query,
            workspace_id=workspace_id,
        )
        _publish_recall_state_to_ctx(
            ctx,
            palace_hits=palace_hits,
            lesson_hits=lesson_hits,
            verified_ideas=verified_ideas,
        )

        return _json(
            {
                "palace_memory": [
                    {
                        "id": h["id"],
                        "wing": h.get("wing", ""),
                        "room": h.get("room", ""),
                        "hall": h.get("hall", ""),
                        "content": h.get("content", "")[:2000],
                        "distance": round(h.get("distance", 1.0), 4),
                    }
                    for h in palace_hits[:limit]
                ],
                "lesson_memory": [
                    {
                        "id": lesson.id,
                        "workspace_id": lesson.workspace_id,
                        "change_summary": lesson.change_summary,
                        "expected_effect": lesson.expected_effect,
                        "observed_effect": lesson.observed_effect,
                        "conclusion": lesson.conclusion,
                        "tags": sorted(lesson.tags),
                    }
                    for lesson in lesson_hits[:limit]
                ],
                "hierarchical_ideas": idea_hits[: max(limit * 2, 20)],
                "unverified_candidates": {
                    "palace_memory": [
                        {
                            "id": h["id"],
                            "wing": h.get("wing", ""),
                            "room": h.get("room", ""),
                            "hall": h.get("hall", ""),
                            "content": h.get("content", "")[:1000],
                            "distance": round(h.get("distance", 1.0), 4),
                        }
                        for h in unverified_hits[: min(limit, 5)]
                    ],
                    "lesson_memory": [
                        {
                            "id": lesson.id,
                            "workspace_id": lesson.workspace_id,
                            "change_summary": lesson.change_summary,
                            "conclusion": lesson.conclusion,
                            "priority": getattr(lesson, "priority", 0),
                            "tags": sorted(lesson.tags),
                        }
                        for lesson in unverified_lessons[: min(limit, 5)]
                    ],
                    "hierarchical_ideas": unverified_ideas[: min(max(limit, 5), 10)],
                    "note": (
                        "These are candidates/hypotheses. Treat them as leads, "
                        "not facts, unless you verify them in the current run."
                    ),
                },
                "include_unverified": bool(include_unverified),
                "contrastive_lessons": contrastive,
                "stats": palace.stats(),
            }
        )
    except Exception as e:
        log.error("Memory query failed: %s", e, exc_info=True)
        return f"WARNING: memory error: {e}"


def list_memory_tree(ctx: Any, workspace_id: str = "") -> str:
    """Return hierarchical ``ideas`` tree stats (``palace_path`` → count) for a workspace or manager root."""
    try:
        from umbrella.memory.hierarchical import HierarchicalMemory
        from umbrella.memory.paths import manager_memory_root, workspace_memory_root

        repo_root = _resolve_umbrella_repo_root(ctx)
        if workspace_id.strip():
            root = workspace_memory_root(repo_root, workspace_id)
        else:
            root = manager_memory_root(repo_root)
        hm = HierarchicalMemory(root)
        return _json(
            {
                "workspace_id": workspace_id,
                "memory_root": str(root),
                "tree": hm.stats(),
            }
        )
    except Exception as e:
        log.error("list_memory_tree failed: %s", e, exc_info=True)
        return f"WARNING: list_memory_tree error: {e}"


def save_umbrella_memory(
    ctx: Any,
    palace_path: str,
    title: str,
    content: str,
    kind: str = "observation",
    workspace_id: str = "",
    tags: str = "",
) -> str:
    """Save a memory entry via MemPalace (semantic ChromaDB storage)."""
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        palace = _palace_backend(repo_root, workspace_id)

        room = ""
        event_type = kind
        if palace_path:
            parts = palace_path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "workspaces":
                workspace_id = workspace_id or parts[1]
                if len(parts) >= 3:
                    event_type = parts[2]
                    room = parts[2]
            elif parts:
                room = parts[-1]
                event_type = parts[0] if len(parts) > 1 else kind

        result = palace.add(
            workspace_id=workspace_id,
            event_type=event_type,
            room=room,
            title=title,
            content=content,
            kind=kind,
            tags=_split_tags(tags) or None,
            task_id=str(getattr(ctx, "task_id", "") or ""),
        )
        return _json({"saved": True, **result})
    except Exception as e:
        return f"WARNING: save memory error: {e}"


def record_workspace_event(
    ctx: Any,
    workspace_id: str,
    event_type: str,
    summary: str,
    details: str = "",
    severity: str = "info",
    tags: str = "",
) -> str:
    content = f"{summary.strip()}\n\n{details.strip()}".strip()
    return save_umbrella_memory(
        ctx,
        palace_path=f"workspaces/{workspace_id}/{event_type or 'events'}",
        title=summary[:180] or event_type,
        content=content,
        kind=severity or "info",
        workspace_id=workspace_id,
        tags=tags or event_type,
    )


_RECORD_IDEA_VALID_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"hypothesis", "observation_from_log", "verified_outcome"}
)


def record_idea(
    ctx: Any,
    content: str = "",
    tags: str = "",
    workspace_id: str = "",
    kind: str = "",
    title: str = "",
    body: str = "",
    palace_path: str = "",
    evidence_kind: str = "",
) -> str:
    """Record a structured idea/observation in workspace hierarchical memory.

    Tier 2.1 — write-time hygiene:

    - ``kind="lesson"`` is **rejected**. Lessons must go through
      ``save_umbrella_lesson`` so they get verify-run-id binding,
      priority/tags reflecting verification status, and proper recall
      surfacing. ``record_idea`` is for hypotheses and observations.
    - New parameter ``evidence_kind`` documents how the idea was obtained:
      ``hypothesis`` (default — agent guess), ``observation_from_log``
      (saw it in a tool output), or ``verified_outcome`` (post-verify
      confirmation). Only ``verified_outcome`` ideas are mirrored to
      semantic palace; hypotheses stay in hierarchical JSONL so they
      don't pollute recall search results.
    - Unknown ``evidence_kind`` values are normalised to ``hypothesis``
      with a warning in the response payload so the agent can correct.
    """

    try:
        from umbrella.memory.hierarchical import HierarchicalMemory

        repo_root = _resolve_umbrella_repo_root(ctx)
        ws = workspace_id or _current_workspace_id_from_drive(ctx)
        if not ws:
            return "ERROR: workspace_id is required or must be present in drive state"
        root = _workspace_memory_root(repo_root, ws, ctx)
        root.mkdir(parents=True, exist_ok=True)
        idea_body = str(body or content or "").strip()
        if not idea_body:
            return "ERROR: content or body is required"

        kind_norm = (
            re.sub(r"[^a-z0-9_-]+", "_", str(kind or "idea").strip().lower()).strip("_")
            or "idea"
        )
        if kind_norm == "lesson":
            return (
                "ERROR: record_idea does not accept kind='lesson'. "
                "Use `save_umbrella_lesson(workspace_id=..., change_summary=..., "
                "expected_effect=..., observed_effect=..., verification_passed=..., "
                "verify_run_id=...)` instead. record_idea is for hypotheses and "
                "observations; lessons carry verification-status invariants and "
                "must go through the lesson path so they rank correctly in recall."
            )
        evidence_kind_norm = str(evidence_kind or "").strip().lower()
        warning: str = ""
        if (
            evidence_kind_norm
            and evidence_kind_norm not in _RECORD_IDEA_VALID_EVIDENCE_KINDS
        ):
            warning = (
                f"evidence_kind={evidence_kind_norm!r} is not one of "
                f"{sorted(_RECORD_IDEA_VALID_EVIDENCE_KINDS)}; "
                "recorded as 'hypothesis'."
            )
            evidence_kind_norm = "hypothesis"
        if not evidence_kind_norm:
            evidence_kind_norm = "hypothesis"

        title_text = str(title or "").strip()
        if not title_text:
            first_line = (
                idea_body.splitlines()[0].strip() if idea_body.splitlines() else ""
            )
            title_text = first_line[:120] or f"{kind_norm} idea"
        tag_list = _split_tags(tags)
        for extra in ("idea", kind_norm, f"evidence:{evidence_kind_norm}"):
            if extra and extra not in tag_list:
                tag_list.append(extra)
        if evidence_kind_norm != "verified_outcome":
            # Mark unverified content so recall ranking can de-prioritise.
            for extra in ("candidate", "unverified"):
                if extra not in tag_list:
                    tag_list.append(extra)

        hier_path = str(palace_path or "").strip().strip("/")
        if not hier_path:
            hier_path = f"workspaces/{ws}/ideas/{kind_norm}"

        hm = HierarchicalMemory(root)
        record = hm.add(
            palace_path=hier_path,
            title=title_text,
            content=idea_body,
            kind=kind_norm,
            workspace_id=ws,
            task_id=str(getattr(ctx, "task_id", "") or ""),
            tags=tag_list,
            metadata={
                "source": "record_idea_tool",
                "ts": datetime.now(timezone.utc).isoformat(),
                "evidence_kind": evidence_kind_norm,
            },
        )

        palace_result: dict[str, Any] = {}
        # Mirror to semantic palace only after verification confirms the
        # outcome. Hypotheses and log observations remain in hierarchical
        # JSONL so they are auditable without crowding semantic recall.
        if evidence_kind_norm == "verified_outcome":
            try:
                palace_result = _palace_backend(repo_root, ws).add(
                    workspace_id=ws,
                    event_type=kind_norm,
                    room=f"ideas-{kind_norm}",
                    title=title_text,
                    content=idea_body,
                    kind=kind_norm,
                    tags=tag_list,
                    task_id=str(getattr(ctx, "task_id", "") or ""),
                    metadata_extra={
                        "hierarchical_id": record.id,
                        "palace_path": hier_path,
                        "evidence_kind": evidence_kind_norm,
                    },
                )
            except Exception:
                log.debug("record_idea semantic mirror skipped", exc_info=True)

        payload: dict[str, Any] = {
            "saved": True,
            "workspace_id": ws,
            "path": str(root / "ideas.jsonl"),
            "id": record.id,
            "palace_path": record.palace_path,
            "evidence_kind": evidence_kind_norm,
            "mirrored_to_semantic": bool(palace_result),
            "semantic_memory": palace_result,
        }
        if warning:
            payload["warning"] = warning
        return _json(payload)
    except Exception as e:
        log.error("record_idea failed: %s", e, exc_info=True)
        return f"WARNING: record idea error: {e}"


def update_prompt(
    ctx: Any,
    name: str,
    new_content: str,
    reason: str,
    workspace_id: str = "",
) -> str:
    """Update the workspace-scoped prompt overlay, never the repo seed prompt."""
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        ws = workspace_id or _current_workspace_id_from_drive(ctx)
        if not ws:
            return "ERROR: workspace_id is required or must be present in drive state"
        prompt_name = _resolve_prompt_name(name)
        prompt_dir = _workspace_memory_root(repo_root, ws, ctx) / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        path = prompt_dir / _PROMPT_NAME_TO_FILE[prompt_name]
        old_content = path.read_text(encoding="utf-8") if path.exists() else ""
        text = str(new_content or "")
        if not text.strip():
            return "ERROR: new_content must not be empty"
        path.write_text(text, encoding="utf-8")

        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                old_content.splitlines(),
                text.splitlines(),
                fromfile=f"{prompt_name}.old",
                tofile=f"{prompt_name}.new",
                lineterm="",
            )
        )
        log_path = (
            Path(getattr(ctx, "drive_root", prompt_dir.parent / "drive"))
            / "logs"
            / "prompt_changes.jsonl"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "task_id": str(getattr(ctx, "task_id", "") or ""),
                        "workspace_id": ws,
                        "name": prompt_name,
                        "reason": reason,
                        "path": str(path),
                        "diff": diff[:20000],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return _json(
            {
                "updated": True,
                "workspace_id": ws,
                "name": prompt_name,
                "path": str(path),
            }
        )
    except Exception as e:
        log.error("update_prompt failed: %s", e, exc_info=True)
        return f"WARNING: update prompt error: {e}"


_PYTHON_EVAL_FORBIDDEN_IMPORTS = {"subprocess", "shutil"}
_PYTHON_EVAL_FORBIDDEN_CALLS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("os", "replace"),
    ("os", "rename"),
}


def _python_eval_guard(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [
                alias.name.split(".", 1)[0] for alias in getattr(node, "names", [])
            ]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split(".", 1)[0])
            blocked = sorted(set(names).intersection(_PYTHON_EVAL_FORBIDDEN_IMPORTS))
            if blocked:
                return f"blocked import(s): {', '.join(blocked)}"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = ""
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    mode = str(node.args[1].value)
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = str(kw.value.value)
                if any(ch in (mode or "r") for ch in "wax+"):
                    return "open(..., write/append/create mode) is blocked"
            if isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Name
            ):
                pair = (node.func.value.id, node.func.attr)
                if pair in _PYTHON_EVAL_FORBIDDEN_CALLS:
                    return f"{pair[0]}.{pair[1]} is blocked"
    return ""


def python_eval(
    ctx: Any,
    code: str,
    timeout_seconds: int = 30,
    workspace_id: str = "",
) -> str:
    """Run guarded Python code from a string in workspace .memory/drive/tmp."""
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        ws = workspace_id or _current_workspace_id_from_drive(ctx)
        if not ws:
            return "ERROR: workspace_id is required or must be present in drive state"
        if stop_payload := _stop_requested_block(
            ctx, tool_name="python_eval", workspace_id=ws
        ):
            return _json(stop_payload)
        reason = _python_eval_guard(str(code or ""))
        if reason:
            return f"ERROR: python_eval guard rejected code: {reason}"
        tmp_dir = _workspace_memory_root(repo_root, ws, ctx) / "drive" / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        script_path = (
            tmp_dir / f"eval_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}.py"
        )
        script_path.write_text(str(code or ""), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(_workspace_root(repo_root, ws, ctx)),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, min(int(timeout_seconds or 30), 120)),
            check=False,
        )
        return _json(
            {
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-8000:],
                "stderr": proc.stderr[-8000:],
                "script_path": str(script_path),
            }
        )
    except subprocess.TimeoutExpired as exc:
        return _json(
            {
                "exit_code": None,
                "error": "timeout",
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
            }
        )
    except Exception as e:
        log.error("python_eval failed: %s", e, exc_info=True)
        return f"WARNING: python_eval error: {e}"


def save_umbrella_lesson(
    ctx: Any,
    workspace_id: str,
    change_summary: str,
    expected_effect: str,
    observed_effect: str = "",
    tags: str = "",
    candidate_id: str = "",
    raw_evidence_paths: list[str] | None = None,
    verification_passed: bool = False,
    critic_verdict: str = "",
    verify_run_id: str = "",
    failed_step_count: int = 0,
) -> str:
    """Record a workspace lesson bound to a verify run.

    Tier 2.2 — lessons are first-class verified knowledge. To be marked
    ``verified=True`` (priority 5, recall-eligible) the caller must
    supply ALL of:

    - ``verification_passed=True`` AND ``critic_verdict='pass'``
      (legacy contract — preserved),
    - ``verify_run_id`` — the id of the ``run_workspace_verify`` call
      that backed this lesson (or a stable identifier the operator can
      look up in the ``verify_runs`` palace later),
    - ``failed_step_count == 0`` — a lesson that claims success while
      verify reports failing required steps is incoherent.

    Lessons missing ``verify_run_id`` are demoted to priority 1 and
    tagged ``unverified_lesson`` even if the agent ticked the boolean
    flag. This breaks the pattern where multiple contradictory
    "lessons" pile up around a single verifier mismatch.
    """

    try:
        from umbrella.memory.models import WorkspaceLessonRecord, generate_lesson_id

        repo_root = _resolve_umbrella_repo_root(ctx)
        store = _memory_store(repo_root, workspace_id)
        verify_run_id_norm = str(verify_run_id or "").strip()
        if not verify_run_id_norm:
            view = getattr(ctx, "loop_state_view", None) or {}
            view_run_id = (
                view.get("last_verify_run_id") if isinstance(view, dict) else ""
            )
            view_passed = (
                view.get("last_verify_passed") if isinstance(view, dict) else False
            )
            view_failed = (
                view.get("last_verify_failed_count") if isinstance(view, dict) else 0
            )
            if (
                isinstance(view_run_id, str)
                and view_run_id.strip()
                and bool(view_passed)
                and int(view_failed or 0) == 0
            ):
                verify_run_id_norm = view_run_id.strip()
        verified_inputs = (
            bool(verification_passed) and str(critic_verdict).strip().lower() == "pass"
        )
        try:
            failed_count = max(0, int(failed_step_count))
        except (TypeError, ValueError):
            failed_count = 0
        verified = verified_inputs and bool(verify_run_id_norm) and failed_count == 0
        normalized_observed = observed_effect.strip() or (
            "Verified" if verified else "Unverified / avoid until proven"
        )
        tags_set = set(_split_tags(tags))
        metadata = {
            "source": "save_umbrella_lesson_tool",
            "verified_at": datetime.now(timezone.utc).isoformat() if verified else "",
            "evidence_sha": "",
            "critic_verdict": critic_verdict,
            "verification_passed": bool(verification_passed),
            "verify_run_id": verify_run_id_norm,
            "failed_step_count": failed_count,
        }
        evidence_blob = "\n".join(
            [
                change_summary,
                expected_effect,
                normalized_observed,
                *(raw_evidence_paths or []),
            ]
        )
        import hashlib

        metadata["evidence_sha"] = hashlib.sha256(
            evidence_blob.encode("utf-8", errors="replace")
        ).hexdigest()
        downgrade_reason = ""
        if not verified:
            tags_set.update({"avoid", "unverified_lesson"})
            if not verified_inputs:
                downgrade_reason = "verification_passed/critic_verdict not both true"
            elif not verify_run_id_norm:
                downgrade_reason = "verify_run_id missing"
            elif failed_count > 0:
                downgrade_reason = f"failed_step_count={failed_count} > 0"
            metadata["unverified_reason"] = downgrade_reason
        lesson = WorkspaceLessonRecord(
            id=generate_lesson_id(),
            task_id=str(getattr(ctx, "task_id", "") or "ouroboros_task"),
            workspace_id=workspace_id,
            change_summary=change_summary,
            expected_effect=expected_effect,
            observed_effect=normalized_observed,
            conclusion=(
                normalized_observed
                if verified
                else f"AVOID relying on this lesson until verification+critic pass: {normalized_observed}"
            ),
            evidence_summary=(
                f"Verified by runtime verification and critic (verify_run_id={verify_run_id_norm})"
                if verified
                else (
                    "Unverified lesson recorded as AVOID — "
                    + (downgrade_reason or "verification+critic evidence missing")
                )
            ),
            tags=tags_set,
            avoid_tags=[] if verified else ["unverified_lesson"],
            priority=5 if verified else 1,
            candidate_id=candidate_id or None,
            raw_evidence_paths=list(raw_evidence_paths or []),
            metadata=metadata,
        )
        store.add_lesson(lesson)
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="lessons",
            summary=change_summary,
            details=f"Expected: {expected_effect}\nObserved: {normalized_observed}",
            severity="lesson",
            tags=tags,
        )
        return _json(
            {
                "saved": True,
                "lesson_id": lesson.id,
                "verified": verified,
                "verify_run_id": verify_run_id_norm,
                "downgrade_reason": downgrade_reason or None,
            }
        )
    except Exception as e:
        log.error("Save lesson failed: %s", e, exc_info=True)
        return f"WARNING: save lesson error: {e}"


def probe_input_file(ctx: Any, path: str, workspace_id: str = "") -> str:
    """Probe an input file's actual format vs its extension.

    Tier 5.1 — read-only tool that returns the result of
    :func:`umbrella.utils.file_probe.probe_file` as JSON. The path is
    resolved against the active workspace and is **not** allowed to
    escape it. Use this before picking a parser for any input file
    mentioned in TASK_MAIN — a ``.docx`` that's actually a UTF-8 text
    dump is the classic failure mode this catches.
    """

    try:
        from umbrella.utils.file_probe import probe_file
    except Exception as exc:
        log.error("probe_input_file: probe_file import failed: %s", exc, exc_info=True)
        return f"WARNING: probe_input_file unavailable: {exc}"
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        ws_id = (workspace_id or "").strip() or (
            str(getattr(ctx, "active_workspace_id", "") or "")
        )
        if ws_id:
            workspace_root = _workspace_root(repo_root, ws_id, ctx)
            stripped = _strip_workspace_prefix(ws_id, path)
            target = _workspace_path(workspace_root, stripped)
        else:
            raw = str(path or "").strip()
            if not raw:
                return "WARNING: probe_input_file requires a path."
            target = Path(raw).resolve()
        result = probe_file(target)
        return _json(result.to_dict())
    except ValueError as exc:
        return f"WARNING: probe_input_file rejected path: {exc}"
    except Exception as exc:
        log.error("probe_input_file failed: %s", exc, exc_info=True)
        return f"WARNING: probe_input_file error: {exc}"


def _verification_next_actions(report_dict: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    results = report_dict.get("results")
    if not isinstance(results, list):
        return actions
    for raw in results:
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "").lower()
        optional = bool(raw.get("optional"))
        if optional or status not in {"failed", "error"}:
            continue
        name = str(
            raw.get("name")
            or raw.get("step_name")
            or raw.get("kind")
            or "verification step"
        )
        text = " ".join(
            str(raw.get(key) or "")
            for key in ("summary", "error", "stdout", "stderr", "command")
        ).lower()
        if "test_quality_guard" in name or "test_quality_guard" in text:
            actions.append(
                "Strengthen tests in `tests/`: cover behavior with real assertions; for web projects exercise endpoints with TestClient/requests/httpx."
            )
        elif "final_sweep" in name or "blocking noise" in text:
            actions.append(
                "Clean workspace layout: move root diagnostic scripts to `src/scripts/`, tests to `tests/`, docs to `docs/`, or delete throwaway files."
            )
        elif "no tests ran" in text or "file or directory not found: tests" in text:
            actions.append(
                "Create real pytest files under `tests/` and rerun the exact acceptance command."
            )
        elif "file_exists" in name or "missing required" in text:
            actions.append(
                "Create the missing required files named by the failing file/layout step."
            )
        else:
            actions.append(
                f"Fix required verification step `{name}` using its stderr/summary, then rerun `run_workspace_verify`."
            )
    deduped: list[str] = []
    for action in actions:
        if action not in deduped:
            deduped.append(action)
    return deduped[:5]


def run_workspace_verify(
    ctx: Any, workspace_id: str, timeout_seconds: int = 600
) -> str:
    """Run the workspace's verification spec and return a structured report.

    This is the agent-facing equivalent of the post-loop verification that
    Umbrella runs automatically. Exposing it as a tool lets the agent gate
    its own work mid-loop instead of discovering broken integrations only
    after MAX_ROUNDS — that failure mode is what the JKX run hit.

    The result is also persisted into MemPalace under ``room=verify_runs``
    so that subsequent periodic recall can show the agent what its last
    verify attempt looked like.

    Returns JSON with ``passed``, ``pass_rate``, per-step results, and a
    short rendered summary suitable for the model to read directly.
    """
    try:
        from umbrella.verification.models import VerificationStep, VerificationStepKind
        from umbrella.verification.runner import run_verification
        from umbrella.verification.spec_loader import load_verification_spec
        from ouroboros.memory_hooks import record_verify_outcome

        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)

        steps = load_verification_spec(workspace_root)
        if not steps:
            _set_workspace_verification_state(
                ctx,
                workspace_id=workspace_id,
                passed=False,
                summary="No verification steps found.",
            )
            return _json(
                {
                    "passed": False,
                    "pass_rate": 0.0,
                    "skipped": True,
                    "reason": (
                        "No verification steps found in workspace.toml or "
                        "verification.toml, and autodetect produced none. "
                        'Add [[verification.steps]] entries (or steps = ["..."]) '
                        "to workspace.toml so this tool can do its job."
                    ),
                    "next_actions": [
                        "Add deterministic verification steps to `workspace.toml` or `verification.toml`, including tests under `tests/` when code is changed."
                    ],
                    "results": [],
                }
            )

        # Local-vs-external verify parity (fixes the "agent sees 6/6 PASS,
        # external harness fails source_policy:mock_scaffold_scan" gap):
        # the external orchestrator always passes ``changed_files`` and
        # therefore appends a synthetic ``source_policy:mock_scaffold_scan``
        # step. When the agent calls this tool directly without an
        # explicit SOURCE_POLICY entry in workspace.toml, ensure we still
        # add one so the local self-gate matches what the harness uses
        # to decide on remediation. Without this, the agent fixes "6/6"
        # locally, declares done, and the harness immediately kicks
        # another remediation cycle for a check the agent never saw.
        steps_with_policy = list(steps)
        if not any(
            getattr(s, "kind", None) == VerificationStepKind.SOURCE_POLICY
            for s in steps_with_policy
        ):
            steps_with_policy.append(
                VerificationStep(
                    kind=VerificationStepKind.SOURCE_POLICY,
                    name="source_policy:mock_scaffold_scan",
                    optional=False,
                )
            )

        report = run_verification(
            workspace_root,
            steps_with_policy,
            workspace_id=workspace_id,
            overall_timeout_seconds=max(60, int(timeout_seconds)),
        )
        report_dict = report.to_dict()
        summary = report.render_summary(limit_chars=4000)
        next_actions = _verification_next_actions(report_dict)
        _set_workspace_verification_state(
            ctx,
            workspace_id=workspace_id,
            passed=bool(report.passed),
            summary=summary,
        )

        failed_required = sum(
            1
            for r in report.results
            if (not r.step.optional) and r.status.value in {"failed", "error"}
        )
        verify_run_id = ""
        try:
            verify_run_id = (
                record_verify_outcome(
                    workspace_id=workspace_id,
                    passed=bool(report.passed),
                    pass_rate=float(report.pass_rate),
                    summary=f"{sum(1 for r in report.results if r.status.value == 'passed')}/{len(report.results)} steps passed",
                    details=summary,
                    repo_root=repo_root,
                    failed_step_count=failed_required,
                )
                or ""
            )
        except Exception:
            log.debug("record_verify_outcome failed", exc_info=True)

        return _json(
            {
                "passed": report_dict["passed"],
                "pass_rate": report_dict["pass_rate"],
                "skipped": False,
                "duration_seconds": report_dict.get("duration_seconds", 0.0),
                "summary": summary,
                "next_actions": next_actions,
                "results": report_dict["results"],
                "verify_run_id": verify_run_id,
                "failed_step_count": failed_required,
            }
        )
    except Exception as e:
        log.error("run_workspace_verify failed: %s", e, exc_info=True)
        try:
            _set_workspace_verification_state(
                ctx,
                workspace_id=workspace_id,
                passed=False,
                summary=f"verify error: {e}",
            )
        except Exception:
            log.debug("failed to record verification error state", exc_info=True)
        return f"WARNING: verify error: {e}"


def run_workspace_task(
    ctx: Any, task_input: str, workspace_id: str = "", max_iterations: int = 5
) -> str:
    """Deprecated compatibility shim; the old Umbrella manager path is disabled."""
    return _json(
        {
            "status": "disabled",
            "reason": "Umbrella manager delegation is not part of the Ouroboros path anymore.",
            "use_instead": [
                "list_workspace_files",
                "read_workspace_file",
                "run_workspace_command",
                "update_workspace_seed",
                "commit_workspace_changes",
                "get_gmas_context",
                "get_umbrella_memory",
                "save_umbrella_memory",
            ],
            "workspace_id": workspace_id,
            "task_preview": task_input[:300],
            "ignored_max_iterations": max_iterations,
        }
    )


def search_meta_harness_experience(
    ctx: Any,
    query: str = "",
    workspace_id: str = "",
    limit: int = 10,
) -> str:
    """Search Meta-Harness experience store for past candidates and evaluations."""
    try:
        from umbrella.meta_harness.store import get_default_store

        repo_root = _resolve_umbrella_repo_root(ctx)
        store = get_default_store(repo_root)

        exp = store.get_latest_experiment()
        if exp is None:
            return _json(
                {"status": "empty", "message": "No meta-harness experiments found"}
            )

        pairs = store.top_candidates(
            exp.id, n=max(1, min(int(limit), 30)), sort_by="score"
        )

        candidates_data = []
        for cand, ev in pairs:
            if workspace_id and cand.workspace_id != workspace_id:
                continue
            entry: dict[str, Any] = {
                "candidate_id": cand.candidate_id,
                "workspace_id": cand.workspace_id,
                "run_status": cand.run_status,
                "write_calls": cand.write_calls,
                "changed_files": cand.changed_files[:10],
                "cost_usd": cand.cost_usd,
            }
            if ev:
                entry["avg_score"] = ev.avg_score
                entry["regressions"] = ev.regressions[:5]
                entry["improvements"] = ev.improvements[:5]
            candidates_data.append(entry)

        decision_data = (
            store.get_promotion_decision(pairs[0][0].candidate_id) if pairs else None
        )

        return _json(
            {
                "experiment_id": exp.id,
                "experiment_status": exp.status,
                "total_candidates": len(exp.candidate_ids),
                "best_score": exp.best_score,
                "candidates": candidates_data,
                "latest_promotion": decision_data.model_dump(mode="json")
                if decision_data
                else None,
            }
        )
    except Exception as e:
        log.error("Meta-harness search failed: %s", e, exc_info=True)
        return f"WARNING: meta-harness search error: {e}"


def inspect_candidate_trace(
    ctx: Any,
    candidate_id: str,
    selector: str = "errors",
    max_chars: int = 20000,
) -> str:
    """Inspect raw execution traces for a meta-harness candidate."""
    try:
        from umbrella.meta_harness.store import get_default_store

        repo_root = _resolve_umbrella_repo_root(ctx)
        store = get_default_store(repo_root)

        cand_dir = store.find_candidate_dir(candidate_id)
        if cand_dir is None:
            return _json({"error": f"Candidate {candidate_id} not found"})

        char_limit = max(1000, min(int(max_chars), 50000))

        if selector == "errors":
            events = store.get_execution_events(candidate_id)
            errors = [e for e in events if "error" in json.dumps(e).lower()]
            return _json(
                {
                    "candidate_id": candidate_id,
                    "selector": selector,
                    "events": errors[:20],
                }
            )[:char_limit]
        elif selector == "diff":
            diff_path = cand_dir / "diffs" / "worktree.diff"
            if diff_path.exists():
                return diff_path.read_text(encoding="utf-8")[:char_limit]
            return "No diff available."
        elif selector == "manifest":
            manifest_path = cand_dir / "manifest.json"
            if manifest_path.exists():
                return manifest_path.read_text(encoding="utf-8")[:char_limit]
            return "No manifest found."
        elif selector == "eval":
            eval_path = cand_dir / "evaluation" / "eval.json"
            if eval_path.exists():
                return eval_path.read_text(encoding="utf-8")[:char_limit]
            return "No evaluation found."
        elif selector == "all":
            events = store.get_execution_events(candidate_id)
            return _json({"candidate_id": candidate_id, "events": events[:50]})[
                :char_limit
            ]
        else:
            return (
                f"Unknown selector: {selector}. Use: errors, diff, manifest, eval, all"
            )
    except Exception as e:
        log.error("Candidate trace inspection failed: %s", e, exc_info=True)
        return f"WARNING: trace inspection error: {e}"


def sandbox_self_edit(
    ctx: Any,
    file_path: str,
    new_content: str,
    reason: str,
    surface: str = "ouroboros",
) -> str:
    """Edit agent-owned code (ouroboros/ or umbrella/) to fix a capability gap.

    Use this only when you cannot accomplish the task with existing tools and
    need to patch your own code to unblock yourself.
    """
    try:
        import os
        from umbrella.policies.engine import can_edit_path
        from umbrella.control_plane.sandbox_self_edit import (
            get_active_session,
            record_sandbox_edit,
        )

        repo_root = _resolve_umbrella_repo_root(ctx)

        session_id = os.environ.get("UMBRELLA_SANDBOX_SESSION_ID")
        if not session_id:
            return _json(
                {
                    "status": "blocked",
                    "reason": "no_sandbox_session",
                    "hint": "Sandbox self-edit is only available during a managed task run.",
                }
            )

        session = get_active_session(repo_root)
        if session is None or session.session_id != session_id:
            return _json(
                {
                    "status": "blocked",
                    "reason": "sandbox_session_mismatch",
                }
            )

        decision = can_edit_path(
            Path(file_path),
            actor="ouroboros",
            action="sandbox_self_edit",
            repo_root=repo_root,
        )
        if not decision.allowed:
            return _json(
                {
                    "status": "blocked",
                    "reason": decision.reason,
                    "policy_id": decision.policy_id,
                }
            )

        target = (repo_root / file_path).resolve()
        if not str(target).startswith(str(repo_root.resolve())):
            return "ERROR: path traversal detected"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")

        record_sandbox_edit(session, file_path)

        record_workspace_event(
            ctx,
            workspace_id="_self",
            event_type="sandbox_self_edit",
            summary=f"Sandbox edit: {file_path}",
            details=f"Reason: {reason}\nSurface: {surface}\nSession: {session_id}",
            severity="warning",
            tags="sandbox,self_edit,capability_gap",
        )

        return _json(
            {
                "status": "applied",
                "file_path": file_path,
                "session_id": session_id,
                "rollback_on_task_end": False,
                "edited_files_count": len(session.edited_files),
            }
        )
    except Exception as e:
        log.error("Sandbox self-edit failed: %s", e, exc_info=True)
        return f"WARNING: sandbox self-edit error: {e}"


def delegate_to_ouroboros(
    ctx: Any,
    task_description: str,
    workspace_id: str = "",
    code_updates: dict[str, str] | None = None,
) -> str:
    """Queue a separate Ouroboros task; avoid from the top-level Ouroboros task."""
    try:
        from umbrella.control_plane.ouroboros_integration import (
            create_ouroboros_self_improvement_task,
        )

        repo_root = _resolve_umbrella_repo_root(ctx)
        result = create_ouroboros_self_improvement_task(
            repo_root=repo_root,
            issue_description=task_description,
            context=f"Workspace: {workspace_id}; code_updates keys: {list((code_updates or {}).keys())}",
            workspace_id=workspace_id,
        )
        return _json(result)
    except Exception as e:
        log.error("Ouroboros delegation failed: %s", e, exc_info=True)
        return f"WARNING: Ouroboros delegation error: {e}"


def _resolve_drive_root(ctx: Any) -> Path:
    drive_root = getattr(ctx, "drive_root", None)
    if drive_root:
        return Path(drive_root)
    repo_root = _resolve_umbrella_repo_root(ctx)
    return Path(repo_root) / ".umbrella" / "ouroboros_drive"


def bg_start(
    ctx: Any,
    workspace_id: str,
    argv: list[str] | str | None = None,
    command: list[str] | str | None = None,
    subdir: str = "",
    label: str = "",
    env: dict[str, str] | None = None,
) -> str:
    """Spawn a long-running command (e.g. uvicorn) detached from this tool call.

    Returns ``job_id`` immediately. Use ``bg_status`` / ``bg_tail`` to observe
    and ``bg_kill`` to stop. Logs land in ``<drive>/logs/bg/<job_id>.log``.
    """
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        cwd = _workspace_path(workspace_root, subdir)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="bg_start", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        raw_command = argv if argv is not None else command
        if raw_command is None:
            return _json(
                {"status": "invalid_command", "hint": "Pass `argv` or `command`."}
            )
        cmd, norm_err = _try_normalize_command(raw_command)
        if norm_err:
            return _json({"status": "invalid_command", "hint": norm_err})

        drive_root = _resolve_drive_root(ctx)
        job = _bg_jobs.start_background(
            drive_root,
            argv=cmd,
            cwd=cwd,
            label=label or (cmd[0] if cmd else ""),
            env_overrides=dict(env or {}),
        )
        try:
            record_workspace_event(
                ctx,
                workspace_id=workspace_id,
                event_type="bg_start",
                summary=f"bg job {job.job_id} pid={job.pid}: {' '.join(cmd)[:160]}",
                details=f"label={job.label}\ncwd={job.cwd}\nlog={job.log_path}",
                severity="info",
                tags="background,server,terminal",
            )
        except Exception:
            log.debug("bg_start memory log failed", exc_info=True)
        return _json(
            {
                "status": "started",
                "job_id": job.job_id,
                "pid": job.pid,
                "log_path": job.log_path,
                "cwd": job.cwd,
                "argv": job.argv,
                "next_step": (
                    "Wait ~1-3s, then call bg_status / bg_tail to confirm the process "
                    "actually came up. If it crashed, the log will show the traceback."
                ),
            }
        )
    except Exception as e:
        log.error("bg_start failed: %s", e, exc_info=True)
        return f"WARNING: bg_start error: {e}"


def bg_status(ctx: Any, job_id: str) -> str:
    try:
        return _json(_bg_jobs.status(_resolve_drive_root(ctx), job_id))
    except Exception as e:
        return f"WARNING: bg_status error: {e}"


def bg_tail(ctx: Any, job_id: str, lines: int = 200) -> str:
    try:
        return _json(_bg_jobs.tail(_resolve_drive_root(ctx), job_id, lines=int(lines)))
    except Exception as e:
        return f"WARNING: bg_tail error: {e}"


def bg_list(ctx: Any) -> str:
    try:
        jobs = _bg_jobs.list_jobs(_resolve_drive_root(ctx))
        return _json([{**j.to_dict(), "alive": _bg_jobs.is_alive(j.pid)} for j in jobs])
    except Exception as e:
        return f"WARNING: bg_list error: {e}"


def bg_kill(ctx: Any, job_id: str) -> str:
    try:
        result = _bg_jobs.kill(_resolve_drive_root(ctx), job_id)
        try:
            record_workspace_event(
                ctx,
                workspace_id="_bg",
                event_type="bg_kill",
                summary=f"bg_kill {job_id} pid={result.get('pid')}",
                details=_json(result),
                severity="info",
                tags="background,server,terminal",
            )
        except Exception:
            log.debug("bg_kill memory log failed", exc_info=True)
        return _json(result)
    except Exception as e:
        return f"WARNING: bg_kill error: {e}"


def web_fetch(ctx: Any, url: str, max_chars: int = 20000) -> str:
    """Fetch a URL (GET) and return cleaned text (HTML stripped, head+tail truncated)."""
    try:
        _record_subtask_discovery_tool_call(ctx, "web_fetch")
        import re as _re
        import httpx

        u = (url or "").strip()
        if not u:
            return _json({"error": "empty url"})
        if not u.lower().startswith(("http://", "https://")):
            return _json({"error": "only http(s) urls are allowed", "url": u})

        cap = max(2000, min(int(max_chars), 200_000))
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        }
        try:
            r = httpx.get(u, headers=headers, timeout=25.0, follow_redirects=True)
        except Exception as net_err:
            return _json({"error": f"network: {net_err}", "url": u})

        ct = (r.headers.get("content-type") or "").lower()
        body = r.text
        if "html" in ct or body.lstrip().startswith("<"):
            body = _re.sub(r"<script[\s\S]*?</script>", " ", body, flags=_re.IGNORECASE)
            body = _re.sub(r"<style[\s\S]*?</style>", " ", body, flags=_re.IGNORECASE)
            body = _re.sub(r"<[^>]+>", " ", body)
            body = _re.sub(r"\s+", " ", body).strip()

        truncated = False
        if len(body) > cap:
            truncated = True
            half = cap // 2
            body = body[:half] + "\n...(truncated)...\n" + body[-half:]
        return _json(
            {
                "url": str(r.url),
                "status": r.status_code,
                "content_type": ct,
                "truncated": truncated,
                "content": body,
            }
        )
    except Exception as e:
        log.error("web_fetch failed: %s", e, exc_info=True)
        return f"WARNING: web_fetch error: {e}"


def get_tools():
    from ouroboros.tools.registry import ToolEntry

    return [
        ToolEntry(
            "search_gmas_knowledge",
            {
                "name": "search_gmas_knowledge",
                "description": "Search GMAS docs/examples/code and return rich snippets. Use before authoring GMAS agents.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 6},
                        "max_chars_per_hit": {"type": "integer", "default": 8000},
                    },
                    "required": ["query"],
                },
            },
            lambda ctx, **kw: search_gmas_knowledge(ctx, **kw),
            timeout_sec=300,
        ),
        ToolEntry(
            "get_gmas_context",
            {
                "name": "get_gmas_context",
                "description": "Return full-enough GMAS context for implementation: docs, examples, code windows, and usage hints.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 6},
                        "max_chars_per_hit": {"type": "integer", "default": 12000},
                    },
                    "required": ["query"],
                },
            },
            lambda ctx, **kw: get_gmas_context(ctx, **kw),
            timeout_sec=300,
        ),
        ToolEntry(
            "load_skill",
            {
                "name": "load_skill",
                "description": "Load full text of an Umbrella procedural skill by slug (L3 detail).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 40000},
                    },
                    "required": ["slug"],
                },
            },
            lambda ctx, **kw: load_skill(ctx, **kw),
            timeout_sec=60,
        ),
        ToolEntry(
            "list_workspace_files",
            {
                "name": "list_workspace_files",
                "description": "List files inside host repo workspaces/<workspace_id>.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "subdir": {"type": "string", "default": ""},
                        "max_entries": {"type": "integer", "default": 300},
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: list_workspace_files(ctx, **kw),
        ),
        ToolEntry(
            "read_workspace_file",
            {
                "name": "read_workspace_file",
                "description": (
                    "Read a file from host repo workspaces/<workspace_id> and return a text preview. "
                    "Handles UTF-8 text files AND natively previews `.docx` (returns paragraphs, "
                    "content_kind=`office_docx`) and `.pptx` (returns slide-by-slide text, "
                    "content_kind=`office_pptx`) WITHOUT shelling out — do NOT call "
                    "`run_workspace_command python -c 'import docx ...'` for these formats, just "
                    "use this tool. Binary files return a `[binary file preview unavailable]` "
                    "marker. The `file_path` is relative to `workspaces/<workspace_id>/` and may "
                    "contain non-ASCII (e.g. Cyrillic) characters — pass them verbatim."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "file_path": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 30000},
                    },
                    "required": ["workspace_id", "file_path"],
                },
            },
            lambda ctx, **kw: read_workspace_file(ctx, **kw),
        ),
        ToolEntry(
            "run_workspace_command",
            {
                "name": "run_workspace_command",
                "description": (
                    "Run a NON-INTERACTIVE, FOREGROUND command inside a host repo workspace. "
                    "You may pass either `argv` (preferred) or `command`. "
                    "Default per-call timeout is 180s; hard cap is 600s. "
                    "USE THE RIGHT TOOL FOR THE JOB:\n"
                    "  - Long-running server (uvicorn, fastapi, vllm, gunicorn, dev server, "
                    "ollama serve, etc.) -> use `bg_start` (this tool will REJECT such commands "
                    "to prevent timeout/zombie leaks).\n"
                    "  - Multi-line Python script with `def`/`async def`/`class`/`for`/`if`/"
                    '`try` joined by `;` -> use `run_python_code`. `python -c "..."` only '
                    "parses simple statements; this tool will REJECT compound `python -c` calls.\n"
                    "  - Need fresh info from the public web (current best library, model, API) "
                    "-> `web_search` + `web_fetch`.\n"
                    "  - Quick one-liner shell, build, test, curl, file utility -> this tool.\n"
                    "On POSIX with tmux/bash there is one persistent shell per workspace "
                    "(cd/export/background `&` survive across calls). On Windows there is "
                    "NO persistence: each call is a fresh process spawn. Always pass "
                    "absolute paths or use `subdir`. "
                    "On timeout the entire process tree is killed (taskkill /T on Windows, "
                    "killpg on POSIX), so a hung subprocess will not survive the call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Preferred exact argv vector to execute. Any program/flags are allowed.",
                        },
                        "command": {
                            "type": ["array", "string"],
                            "description": "Alternative free-form command payload. May be a string or argv-style list.",
                        },
                        "subdir": {"type": "string", "default": ""},
                        "timeout_seconds": {
                            "type": "integer",
                            "default": _RUN_WORKSPACE_DEFAULT_TIMEOUT_S,
                            "description": (
                                "Per-call wall-clock timeout in seconds. "
                                f"Default {_RUN_WORKSPACE_DEFAULT_TIMEOUT_S}, "
                                f"hard-capped at {_RUN_WORKSPACE_MAX_TIMEOUT_S}."
                            ),
                        },
                        "allow_dependency_install": {
                            "type": "boolean",
                            "default": False,
                        },
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: run_workspace_command(ctx, **kw),
            is_code_tool=True,
            timeout_sec=_RUN_WORKSPACE_MAX_TIMEOUT_S,
        ),
        ToolEntry(
            "run_python_code",
            {
                "name": "run_python_code",
                "description": (
                    "Run a multi-line Python script inside a workspace. Use this "
                    'INSTEAD OF `python -c "..."` for any non-trivial script: '
                    "`def`, `async def`, `class`, `for`, `while`, `if`, `with`, "
                    "`try/except` -- CPython's `-c` parses its body as a single "
                    "simple statement and SyntaxErrors on the first compound block "
                    "keyword joined with `;`. The script is written to "
                    "`<workspace>/.umbrella_scratch/run_<id>.py` and executed via "
                    "`uv run python <file>` (or plain `python` if `use_uv=false`). "
                    "Default per-call timeout 180s, hard-capped at 600s. "
                    "Stdout+stderr and exit_code are returned identically to "
                    "`run_workspace_command`. Don't use this for long-running "
                    "servers -- use `bg_start` instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "code": {
                            "type": "string",
                            "description": "Full Python source as a single multi-line string.",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional CLI arguments forwarded to the script.",
                        },
                        "subdir": {"type": "string", "default": ""},
                        "timeout_seconds": {
                            "type": "integer",
                            "default": _RUN_WORKSPACE_DEFAULT_TIMEOUT_S,
                            "description": (
                                f"Per-call timeout. Default {_RUN_WORKSPACE_DEFAULT_TIMEOUT_S}s, "
                                f"hard cap {_RUN_WORKSPACE_MAX_TIMEOUT_S}s."
                            ),
                        },
                        "use_uv": {
                            "type": "boolean",
                            "default": True,
                            "description": "Run via `uv run python` (recommended) so the workspace's pyproject is honored.",
                        },
                        "extra_env": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Extra env vars exported for this call only (PYTHONPATH, ports, etc.).",
                        },
                    },
                    "required": ["workspace_id", "code"],
                },
            },
            lambda ctx, **kw: run_python_code(ctx, **kw),
            is_code_tool=True,
            timeout_sec=_RUN_WORKSPACE_MAX_TIMEOUT_S,
        ),
        ToolEntry(
            "terminal_view",
            {
                "name": "terminal_view",
                "description": (
                    "Read recent scrollback from the persistent shell for a workspace. "
                    "Cheap, read-only -- use it to re-read what an earlier "
                    "run_workspace_command printed once the raw tool result has been "
                    "compacted out of the conversation history."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "last_lines": {
                            "type": "integer",
                            "default": 200,
                            "description": "Tail size in lines (1..4000).",
                        },
                        "grep": {
                            "type": "string",
                            "default": "",
                            "description": "Optional Python regex; only matching lines are returned.",
                        },
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: terminal_view(ctx, **kw),
            is_code_tool=False,
            timeout_sec=30,
        ),
        ToolEntry(
            "terminal_reset",
            {
                "name": "terminal_reset",
                "description": (
                    "Destroy and re-create the persistent shell for a workspace. "
                    "Drops cwd, env vars and background jobs. Use ONLY when the shell "
                    "is genuinely wedged or you must guarantee a clean environment; a "
                    "non-empty `reason` is required so the decision is auditable."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "reason": {
                            "type": "string",
                            "default": "",
                            "description": "Why the reset is necessary. Required.",
                        },
                    },
                    "required": ["workspace_id", "reason"],
                },
            },
            lambda ctx, **kw: terminal_reset(ctx, **kw),
            is_code_tool=True,
            timeout_sec=30,
        ),
        ToolEntry(
            "commit_workspace_changes",
            {
                "name": "commit_workspace_changes",
                "description": "Commit host repo workspace changes locally after run_workspace_verify passes. Never pushes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "commit_message": {"type": "string"},
                        "paths": {"type": "array", "items": {"type": "string"}},
                        "include_data": {
                            "type": "boolean",
                            "default": False,
                            "description": "If true, include workspaces/<id>/data/ cache files in the commit.",
                        },
                    },
                    "required": ["workspace_id", "commit_message"],
                },
            },
            lambda ctx, **kw: commit_workspace_changes(ctx, **kw),
            is_code_tool=True,
            timeout_sec=300,
        ),
        ToolEntry(
            "get_workspace_metrics",
            {
                "name": "get_workspace_metrics",
                "description": "Get performance metrics for workspaces.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string", "default": ""},
                    },
                    "required": [],
                },
            },
            lambda ctx, **kw: get_workspace_metrics(ctx, **kw),
        ),
        ToolEntry(
            "get_workspace_logs",
            {
                "name": "get_workspace_logs",
                "description": "Read recent logs from workspace instances.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "run_id": {"type": "string", "default": ""},
                        "tail": {"type": "integer", "default": 100},
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: get_workspace_logs(ctx, **kw),
        ),
        ToolEntry(
            "update_workspace_seed",
            {
                "name": "update_workspace_seed",
                "description": "Update a seed workspace file with backup. For existing files, prefer apply_workspace_patch after read_workspace_file; use this for new files or intentional full rewrites.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "file_path": {"type": "string"},
                        "new_content": {"type": "string"},
                        "create_backup": {"type": "boolean", "default": True},
                        "allow_large_overwrite": {"type": "boolean", "default": False},
                        "validation_summary": {"type": "string", "default": ""},
                    },
                    "required": ["workspace_id", "file_path", "new_content"],
                },
            },
            lambda ctx, **kw: update_workspace_seed(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "apply_workspace_patch",
            {
                "name": "apply_workspace_patch",
                "description": (
                    "Apply an OpenAI-style patch envelope to workspace files with audit/backups. "
                    "Use this for targeted edits to existing files after calling read_workspace_file "
                    "on each Update/Delete target. Add File operations do not require a prior read. "
                    "Patch format: *** Begin Patch, then *** Update File: path / *** Add File: path / "
                    "*** Delete File: path, hunks with @@ and lines prefixed by space/+/- where relevant, "
                    "then *** End Patch."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "patch": {
                            "type": "string",
                            "description": (
                                "*** Begin Patch\n"
                                "*** Update File: src/app.py\n"
                                "@@\n"
                                " old_line\n"
                                "-remove_this\n"
                                "+add_this\n"
                                "*** End Patch"
                            ),
                        },
                        "validation_summary": {"type": "string", "default": ""},
                    },
                    "required": ["workspace_id", "patch"],
                },
            },
            lambda ctx, **kw: apply_workspace_patch(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "delete_workspace_file",
            {
                "name": "delete_workspace_file",
                "description": (
                    "Sanctioned single-file delete for workspace cleanup. Use "
                    "this — and ONLY this — to remove ad-hoc diagnostic scripts, "
                    "raw-extracted artefacts, stray handoff docs, and similar "
                    "noise that the layout policy or final sweep flags. Shell "
                    '`rm` / `del` / `Remove-Item` and `python -c "...unlink()..."` '
                    "are blocked on purpose; this is the audited path. Protects "
                    ".git/.umbrella/.memory/.venv plus TASK_MAIN.md, workspace.toml, "
                    "README.md from accidental deletion. Records a workspace "
                    "event so the cleanup is part of the audit trail."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "file_path": {
                            "type": "string",
                            "description": "Workspace-relative POSIX path of the file to delete.",
                        },
                        "reason": {
                            "type": "string",
                            "default": "",
                            "description": (
                                "Short justification (e.g. 'ad-hoc probe script left "
                                "over from extraction', 'raw extract artefact replaced "
                                "by docs/requirements.md'). Recorded with the event."
                            ),
                        },
                    },
                    "required": ["workspace_id", "file_path"],
                },
            },
            lambda ctx, **kw: delete_workspace_file(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "update_workspace_from_instance",
            {
                "name": "update_workspace_from_instance",
                "description": "Copy improved files from an instance to seed workspace with backup.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "instance_name": {"type": "string"},
                        "files_to_copy": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["workspace_id", "instance_name", "files_to_copy"],
                },
            },
            lambda ctx, **kw: update_workspace_from_instance(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "configure_workspace_skills",
            {
                "name": "configure_workspace_skills",
                "description": (
                    "Override a named skill by editing [skills] in "
                    "workspace.toml. GMAS (multi_agent_gmas) is automatically "
                    "active for LLM/model/agent work; use this to record an "
                    "explicit opt-out for pure non-LLM work, or to force it "
                    "on when the task wording is too sparse. The skill cache "
                    "is invalidated so the next attempt picks up the change."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "skill_id": {
                            "type": "string",
                            "enum": list(_WORKSPACE_TOML_KNOWN_SKILLS),
                            "description": "Currently only `multi_agent_gmas` is wired into the verifier.",
                        },
                        "enabled": {"type": "boolean"},
                        "reason": {
                            "type": "string",
                            "default": "",
                            "description": "Short justification recorded in workspace event memory.",
                        },
                    },
                    "required": ["workspace_id", "skill_id", "enabled"],
                },
            },
            lambda ctx, **kw: configure_workspace_skills(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "get_umbrella_memory",
            {
                "name": "get_umbrella_memory",
                "description": "Semantic search over Umbrella memory (MemPalace ChromaDB). Returns palace memories ranked by relevance + structured lessons. Filter by workspace_id for workspace-scoped results.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "default": "",
                            "description": "Natural-language search query",
                        },
                        "workspace_id": {
                            "type": "string",
                            "default": "",
                            "description": "Scope to a workspace wing",
                        },
                        "palace_path": {
                            "type": "string",
                            "default": "",
                            "description": "Legacy path filter (workspaces/X/room)",
                        },
                        "limit": {"type": "integer", "default": 10},
                        "include_unverified": {
                            "type": "boolean",
                            "default": False,
                            "description": "Include candidate/hypothesis memories in main result lists instead of only in unverified_candidates.",
                        },
                    },
                    "required": [],
                },
            },
            lambda ctx, **kw: get_umbrella_memory(ctx, **kw),
        ),
        ToolEntry(
            "list_memory_tree",
            {
                "name": "list_memory_tree",
                "description": "List hierarchical ideas tree (JSONL-backed palace_path counts) for manager memory or a specific workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {
                            "type": "string",
                            "default": "",
                            "description": "Empty = manager root; else workspaces/<id>/.memory",
                        },
                    },
                    "required": [],
                },
            },
            lambda ctx, **kw: list_memory_tree(ctx, **kw),
        ),
        ToolEntry(
            "save_umbrella_memory",
            {
                "name": "save_umbrella_memory",
                "description": "Save a memory entry to MemPalace (semantic ChromaDB). Use for ideas, errors, logs, changes, decisions. Entries are auto-classified into wings (workspaces), halls (event types), and rooms (topics).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "palace_path": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "kind": {"type": "string", "default": "observation"},
                        "workspace_id": {"type": "string", "default": ""},
                        "tags": {"type": "string", "default": ""},
                    },
                    "required": ["palace_path", "title", "content"],
                },
            },
            lambda ctx, **kw: save_umbrella_memory(ctx, **kw),
        ),
        ToolEntry(
            "record_workspace_event",
            {
                "name": "record_workspace_event",
                "description": "Record a workspace change/log/error/idea into Umbrella hierarchical memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "event_type": {"type": "string"},
                        "summary": {"type": "string"},
                        "details": {"type": "string", "default": ""},
                        "severity": {"type": "string", "default": "info"},
                        "tags": {"type": "string", "default": ""},
                    },
                    "required": ["workspace_id", "event_type", "summary"],
                },
            },
            lambda ctx, **kw: record_workspace_event(ctx, **kw),
        ),
        ToolEntry(
            "record_idea",
            {
                "name": "record_idea",
                "description": (
                    "Record a hypothesis or observation in workspace hierarchical "
                    "memory. Use this for thinking out loud, noting patterns, or "
                    "capturing context the next round will need. Does NOT accept "
                    "kind='lesson' — for verified lessons call save_umbrella_lesson "
                    "instead. Only entries with evidence_kind='verified_outcome' "
                    "are mirrored to semantic search; hypotheses stay local to "
                    "the JSONL so recall stays clean."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "default": "",
                            "description": "Idea body. Use this or body.",
                        },
                        "kind": {
                            "type": "string",
                            "default": "idea",
                            "description": "idea, verification_fix, prompt_fix, tool_gap, etc. (NOT 'lesson').",
                        },
                        "title": {"type": "string", "default": ""},
                        "body": {
                            "type": "string",
                            "default": "",
                            "description": "Structured body. Use this or content.",
                        },
                        "palace_path": {
                            "type": "string",
                            "default": "",
                            "description": "Optional hierarchy path such as workspaces/<id>/ideas/verification.",
                        },
                        "tags": {"type": "string", "default": ""},
                        "workspace_id": {"type": "string", "default": ""},
                        "evidence_kind": {
                            "type": "string",
                            "default": "hypothesis",
                            "enum": [
                                "hypothesis",
                                "observation_from_log",
                                "verified_outcome",
                            ],
                            "description": (
                                "How was this idea obtained? 'hypothesis' = your guess "
                                "(default); 'observation_from_log' = you saw it in tool "
                                "output; 'verified_outcome' = you confirmed it after "
                                "running run_workspace_verify (PASS). Only "
                                "'verified_outcome' makes it into semantic recall."
                            ),
                        },
                    },
                    "required": [],
                },
            },
            lambda ctx, **kw: record_idea(ctx, **kw),
        ),
        ToolEntry(
            "update_prompt",
            {
                "name": "update_prompt",
                "description": (
                    "Update a workspace-scoped Ouroboros prompt overlay. Writes only to "
                    "workspaces/<id>/.memory/prompts/{SYSTEM,BIBLE,CONSCIOUSNESS}.md and logs a diff; "
                    "never edits the repo seed prompt."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "One of SYSTEM, BIBLE, CONSCIOUSNESS",
                        },
                        "new_content": {"type": "string"},
                        "reason": {"type": "string"},
                        "workspace_id": {"type": "string", "default": ""},
                    },
                    "required": ["name", "new_content", "reason"],
                },
            },
            lambda ctx, **kw: update_prompt(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "save_umbrella_lesson",
            {
                "name": "save_umbrella_lesson",
                "description": (
                    "Save a structured workspace lesson. To rank as a verified "
                    "lesson (priority 5, recall-eligible) the call MUST include "
                    "verify_run_id from a passing run_workspace_verify, "
                    "verification_passed=True, critic_verdict='pass' and "
                    "failed_step_count=0. Lessons missing any of these are "
                    "stored as 'unverified' / 'avoid' so they don't pollute "
                    "future recall."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "change_summary": {"type": "string"},
                        "expected_effect": {"type": "string"},
                        "observed_effect": {"type": "string", "default": ""},
                        "tags": {"type": "string", "default": ""},
                        "verification_passed": {"type": "boolean", "default": False},
                        "critic_verdict": {"type": "string", "default": ""},
                        "verify_run_id": {
                            "type": "string",
                            "default": "",
                            "description": (
                                "Id of the run_workspace_verify call that backs this "
                                "lesson. Required for verified=True; without it the "
                                "lesson is stored as candidate/avoid."
                            ),
                        },
                        "failed_step_count": {
                            "type": "integer",
                            "default": 0,
                            "description": "Number of failed required steps in the verify run that backs this lesson. Must be 0 for verified=True.",
                        },
                    },
                    "required": ["workspace_id", "change_summary", "expected_effect"],
                },
            },
            lambda ctx, **kw: save_umbrella_lesson(ctx, **kw),
        ),
        ToolEntry(
            "run_workspace_verify",
            {
                "name": "run_workspace_verify",
                "description": "Run the workspace verification spec and return a structured pass/fail report. Use every ~30-50 edits and before declaring a feature done. Resets the verify-gate counter.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "default": 600},
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: run_workspace_verify(ctx, **kw),
            is_code_tool=True,
            timeout_sec=900,
        ),
        ToolEntry(
            "probe_input_file",
            {
                "name": "probe_input_file",
                "description": (
                    "Magic-bytes probe for an input file. Call this BEFORE "
                    "choosing a parser when TASK_MAIN points you at a file "
                    "by extension — e.g. a '.docx' that might actually be "
                    "plain text, a '.xlsx' that might be CSV. Returns "
                    "{actual_format, mismatch, hint}: pick the parser that "
                    "matches actual_format, not declared_ext. Read-only, "
                    "no side effects."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path to the file. Workspace-relative paths are "
                                "resolved against the active workspace; absolute "
                                "paths must stay inside the workspace root."
                            ),
                        },
                        "workspace_id": {"type": "string", "default": ""},
                    },
                    "required": ["path"],
                },
            },
            lambda ctx, **kw: probe_input_file(ctx, **kw),
        ),
        ToolEntry(
            "python_eval",
            {
                "name": "python_eval",
                "description": "Run guarded Python code from a string in the workspace .memory/drive/tmp directory. Use instead of fragile python -c one-liners for read-only analysis.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "timeout_seconds": {"type": "integer", "default": 30},
                        "workspace_id": {"type": "string", "default": ""},
                    },
                    "required": ["code"],
                },
            },
            lambda ctx, **kw: python_eval(ctx, **kw),
            is_code_tool=True,
            timeout_sec=180,
        ),
        ToolEntry(
            "run_workspace_task",
            {
                "name": "run_workspace_task",
                "description": "Compatibility shim only. Old Umbrella manager execution is disabled; use workspace tools instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_input": {"type": "string"},
                        "workspace_id": {"type": "string", "default": ""},
                        "max_iterations": {"type": "integer", "default": 5},
                    },
                    "required": ["task_input"],
                },
            },
            lambda ctx, **kw: run_workspace_task(ctx, **kw),
        ),
        ToolEntry(
            "search_meta_harness_experience",
            {
                "name": "search_meta_harness_experience",
                "description": "Search Meta-Harness experience: past candidates, scores, regressions, and promotion decisions. Use to avoid repeating failed changes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "default": ""},
                        "workspace_id": {"type": "string", "default": ""},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": [],
                },
            },
            lambda ctx, **kw: search_meta_harness_experience(ctx, **kw),
        ),
        ToolEntry(
            "inspect_candidate_trace",
            {
                "name": "inspect_candidate_trace",
                "description": "Inspect raw execution traces, diffs, manifest, or evaluation for a specific meta-harness candidate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "selector": {
                            "type": "string",
                            "default": "errors",
                            "description": "One of: errors, diff, manifest, eval, all",
                        },
                        "max_chars": {"type": "integer", "default": 20000},
                    },
                    "required": ["candidate_id"],
                },
            },
            lambda ctx, **kw: inspect_candidate_trace(ctx, **kw),
        ),
        ToolEntry(
            "sandbox_self_edit",
            {
                "name": "sandbox_self_edit",
                "description": (
                    "Persistently edit your own code (ouroboros/ or umbrella/) to fix a capability gap. "
                    "Use only for harness/code bugs. Do not use this for prompt updates; use "
                    "update_prompt so changes stay scoped to the current workspace."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Repo-relative path, e.g. ouroboros/ouroboros/tools/my_fix.py",
                        },
                        "new_content": {
                            "type": "string",
                            "description": "Full file content to write",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why this self-edit is needed (capability gap description)",
                        },
                        "surface": {
                            "type": "string",
                            "default": "ouroboros",
                            "description": "ouroboros or umbrella",
                        },
                    },
                    "required": ["file_path", "new_content", "reason"],
                },
            },
            lambda ctx, **kw: sandbox_self_edit(ctx, **kw),
            is_code_tool=True,
        ),
        ToolEntry(
            "delegate_to_ouroboros",
            {
                "name": "delegate_to_ouroboros",
                "description": "Queue a separate Ouroboros task. Avoid unless explicitly decomposing work.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_description": {"type": "string"},
                        "workspace_id": {"type": "string", "default": ""},
                        "code_updates": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["task_description"],
                },
            },
            lambda ctx, **kw: delegate_to_ouroboros(ctx, **kw),
        ),
        ToolEntry(
            "bg_start",
            {
                "name": "bg_start",
                "description": (
                    "Start a long-running command (server, worker, watcher) DETACHED from "
                    "this tool call. Use this for uvicorn/fastapi/vllm/etc. instead of "
                    "run_workspace_command, which would block until timeout. "
                    "Returns a `job_id` immediately; stdout+stderr stream to "
                    "<drive>/logs/bg/<job_id>.log. Combine with `bg_status` (is it alive?), "
                    "`bg_tail` (read recent log lines) and `bg_kill` (stop it)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workspace_id": {"type": "string"},
                        "argv": {"type": "array", "items": {"type": "string"}},
                        "command": {"type": ["array", "string"]},
                        "subdir": {"type": "string", "default": ""},
                        "label": {
                            "type": "string",
                            "default": "",
                            "description": "Short human label (e.g. uvicorn-news-cards).",
                        },
                        "env": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "Extra env vars (PYTHONPATH, PORT, ...).",
                        },
                    },
                    "required": ["workspace_id"],
                },
            },
            lambda ctx, **kw: bg_start(ctx, **kw),
            is_code_tool=True,
            timeout_sec=60,
        ),
        ToolEntry(
            "bg_status",
            {
                "name": "bg_status",
                "description": "Check if a background job is alive and how big its log is. Cheap, read-only.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
            },
            lambda ctx, **kw: bg_status(ctx, **kw),
            timeout_sec=15,
        ),
        ToolEntry(
            "bg_tail",
            {
                "name": "bg_tail",
                "description": "Read the last N lines of a background job's combined stdout/stderr log.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "lines": {"type": "integer", "default": 200},
                    },
                    "required": ["job_id"],
                },
            },
            lambda ctx, **kw: bg_tail(ctx, **kw),
            timeout_sec=30,
        ),
        ToolEntry(
            "bg_list",
            {
                "name": "bg_list",
                "description": "List all background jobs registered for this drive (alive + exited).",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            lambda ctx, **kw: bg_list(ctx, **kw),
            timeout_sec=15,
        ),
        ToolEntry(
            "bg_kill",
            {
                "name": "bg_kill",
                "description": "Kill a background job (taskkill /F /T on Windows, killpg on POSIX) and remove its manifest. Always use this before re-binding the same port.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                    },
                    "required": ["job_id"],
                },
            },
            lambda ctx, **kw: bg_kill(ctx, **kw),
            is_code_tool=True,
            timeout_sec=30,
        ),
        ToolEntry(
            "web_fetch",
            {
                "name": "web_fetch",
                "description": (
                    "GET an HTTP(S) URL and return its (HTML-stripped) text body, "
                    "head+tail truncated. Use after `web_search` to read a docs page or "
                    "model card. Output is capped to ~20k chars by default."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 20000},
                    },
                    "required": ["url"],
                },
            },
            lambda ctx, **kw: web_fetch(ctx, **kw),
            timeout_sec=60,
        ),
    ]
