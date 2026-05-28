"""Deep-agent bridge context helpers owned by Umbrella.

These helpers intentionally do not depend on Ouroboros internals. Deep-agent
adapters such as Ouroboros can import them to resolve workspaces, drive state,
stop requests, and prompt overlay paths without carrying Umbrella system logic
inside the agent package.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PYTHON_COMMAND_NAMES = {
    "python",
    "python3",
    "py",
    "python.exe",
    "python3.exe",
    "py.exe",
}

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
    from umbrella.memory.paths import _safe_workspace_segment

    clean = _safe_workspace_segment(workspace_id)
    if not clean:
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
    if not current:
        return False
    if payload.get("internal_recovery_route") or str(payload.get("scope") or "") == "task":
        requested = str(
            payload.get("task_id") or payload.get("target_task_id") or ""
        ).strip()
        return bool(requested and current == requested)
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
    return any(
        current == requested
        or current.startswith(f"{requested}:")
        or current.startswith(f"{requested}__")
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
    if isinstance(payload, dict) and payload.get("internal_recovery_route"):
        try:
            stop_path.unlink(missing_ok=True)
        except OSError:
            pass
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
    while text.startswith("./"):
        text = text[2:]
    if text.casefold().startswith(".workspaces/"):
        text = text[1:]
    if not text:
        return text
    wid = str(workspace_id or "").strip().strip("/")
    if not wid:
        return text
    for exact in (f"workspaces/{wid}", wid):
        if text.casefold() == exact.casefold():
            return ""
        prefix = f"{exact}/"
        while text.casefold().startswith(prefix.casefold()):
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


