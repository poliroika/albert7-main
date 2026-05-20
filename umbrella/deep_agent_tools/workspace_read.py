"""Workspace file listing, preview, and read-cache helpers."""

from umbrella.deep_agent_tools.workspace_common import *


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
    offset: int = 0,
    line_start: int = 0,
    line_count: int = 160,
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
        start = max(0, _coerce_int(offset, 0))
        line_start_int = max(0, _coerce_int(line_start, 0))
        line_count_int = max(1, _coerce_int(line_count, 160))
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
            int(start),
            int(line_start_int),
            int(line_count_int),
        )
        cached = _read_cache_get(cache_key)
        if cached is not None:
            _mark_workspace_file_read(ctx, workspace_id, file_path)
            return cached
        total_lines: int | None = None
        requested_end = 0
        observed_line_count = 0
        has_more_lines_after = False
        if line_start_int > 0 and target.suffix.lower() not in {".docx", ".pptx"}:
            raw = target.read_bytes()
            if b"\x00" in raw[:8192]:
                content, truncated, content_kind = (
                    "[binary file preview unavailable]",
                    False,
                    "binary",
                )
            else:
                text = raw.decode("utf-8", errors="replace")
                lines = text.splitlines(keepends=True)
                total_lines = len(lines)
                begin = line_start_int - 1
                end = begin + line_count_int
                content = "".join(lines[begin:end])
                requested_end = min(end, total_lines)
                observed_line_count = max(0, requested_end - begin)
                has_more_lines_after = end < total_lines
                if len(content) > cap:
                    content = content[:cap]
                    truncated = True
                    observed_line_count = len(content.splitlines())
                else:
                    truncated = False
                content_kind = "text"
        elif start > 0 and target.suffix.lower() not in {".docx", ".pptx"}:
            raw = target.read_bytes()
            if b"\x00" in raw[:8192]:
                content, truncated, content_kind = (
                    "[binary file preview unavailable]",
                    False,
                    "binary",
                )
            else:
                text = raw.decode("utf-8", errors="replace")
                content = text[start : start + cap]
                truncated = len(text) > start + cap
                total_lines = len(text.splitlines())
                content_kind = "text"
        else:
            if target.suffix.lower() in {".docx", ".pptx"}:
                content, truncated, content_kind = read_file_preview(
                    target, max_chars=cap + start
                )
                if start > 0:
                    content = content[start : start + cap]
            else:
                raw = target.read_bytes()
                if b"\x00" in raw[:8192]:
                    content, truncated, content_kind = (
                        "[binary file preview unavailable]",
                        False,
                        "binary",
                    )
                else:
                    text = raw.decode("utf-8", errors="replace")
                    total_lines = len(text.splitlines())
                    content = text[:cap]
                    truncated = len(text) > cap
                    content_kind = "text"
                    observed_line_count = len(content.splitlines())
                    requested_end = min(observed_line_count, total_lines)
                    has_more_lines_after = truncated or requested_end < total_lines
        full_text_read = (
            line_start_int == 0
            and start == 0
            and content_kind == "text"
            and total_lines is not None
        )
        payload = _json(
            {
                "workspace_id": workspace_id,
                "file_path": file_path,
                "resolved_name": target.name,
                "content_kind": content_kind,
                "truncated": truncated,
                "offset": start,
                "line_start": line_start_int,
                "line_count": (
                    line_count_int if line_start_int > 0 else observed_line_count
                ),
                "line_end": requested_end if (line_start_int > 0 or full_text_read) else 0,
                "total_lines": total_lines,
                "line_range_complete": (
                    (line_start_int > 0 or full_text_read)
                    and not truncated
                    and requested_end <= total_lines
                ),
                "has_more_lines_after": (
                    bool(has_more_lines_after)
                    if (line_start_int > 0 or full_text_read)
                    else False
                ),
                "content": content,
            }
        )
        _read_cache_put(cache_key, payload)
        _mark_workspace_file_read(ctx, workspace_id, file_path)
        return payload
    except Exception as e:
        return f"WARNING: workspace read error: {e}"


__all__ = [
    'list_workspace_files',
    '_read_cache_enabled',
    '_read_cache_get',
    '_read_cache_put',
    '_read_cache_clear',
    '_mark_workspace_file_read',
    '_workspace_file_was_read',
    'read_workspace_file',
]
