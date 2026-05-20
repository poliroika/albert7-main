"""File tools: repo_read, repo_list, drive_read, drive_list, drive_write, codebase_digest, summarize_dialogue."""

import ast
import json
import logging
import os
import pathlib
import uuid
from typing import List, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import read_text, safe_relpath, utc_now_iso

log = logging.getLogger(__name__)


def _list_dir(root: pathlib.Path, rel: str, max_entries: int = 500) -> list[str]:
    target = (root / safe_relpath(rel)).resolve()
    if not target.exists():
        return [f"⚠️ Directory not found: {rel}"]
    if not target.is_dir():
        return [f"⚠️ Not a directory: {rel}"]
    items = []
    try:
        for entry in sorted(target.iterdir()):
            if len(items) >= max_entries:
                items.append(f"...(truncated at {max_entries})")
                break
            suffix = "/" if entry.is_dir() else ""
            items.append(str(entry.relative_to(root)) + suffix)
    except Exception as e:
        items.append(f"⚠️ Error listing: {e}")
    return items


def _coerce_nonnegative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value or default))
    except (TypeError, ValueError):
        return max(0, int(default))


def _slice_text_by_lines(text: str, line_start: int, line_count: int = 160) -> str:
    start = _coerce_nonnegative_int(line_start)
    if start <= 0:
        return text
    count = _coerce_nonnegative_int(line_count, 160) or 160
    lines = text.splitlines(keepends=True)
    return "".join(lines[start - 1 : start - 1 + count])


def _repo_read(
    ctx: ToolContext,
    path: str,
    offset: int = 0,
    max_chars: int = 0,
    line_start: int = 0,
    line_count: int = 160,
) -> str:
    target = ctx.repo_path(path)
    content = read_text(target)
    if _coerce_nonnegative_int(line_start) > 0:
        content = _slice_text_by_lines(content, line_start, line_count)
        start = 0
    else:
        start = _coerce_nonnegative_int(offset)
    cap = _coerce_nonnegative_int(max_chars)
    if start or cap > 0:
        end = start + cap if cap > 0 else None
        content = content[start:end]
    _mark_workspace_read_from_repo_path(ctx, path, target)
    return content


def _mark_workspace_read_from_repo_path(
    ctx: ToolContext, raw_path: str, target: pathlib.Path
) -> None:
    """Let repo_read satisfy workspace read-before-patch when it reads a workspace file."""
    try:
        view = getattr(ctx, "loop_state_view", None)
        workspace_id = ""
        if isinstance(view, dict):
            workspace_id = str(
                view.get("active_workspace_id")
                or view.get("workspace_id")
                or ""
            ).strip()
        workspace_id = workspace_id or str(
            getattr(ctx, "active_workspace_id", "") or ""
        ).strip()
        raw = str(raw_path or "").replace("\\", "/").strip().lstrip("/")
        if not workspace_id and raw.startswith("workspaces/"):
            parts = raw.split("/", 2)
            if len(parts) >= 3 and parts[1].strip():
                workspace_id = parts[1].strip()
        if not workspace_id:
            return
        repo_root = (ctx.host_repo_root or ctx.repo_dir).resolve()
        workspace_root = (repo_root / "workspaces" / workspace_id).resolve()
        rel_path = ""
        prefix = f"workspaces/{workspace_id}/"
        if raw.startswith(prefix):
            rel_path = raw[len(prefix) :]
        else:
            try:
                rel_path = str(target.resolve().relative_to(workspace_root)).replace(
                    "\\", "/"
                )
            except ValueError:
                return
        if not rel_path:
            return
        from ouroboros.tools import umbrella_tools

        umbrella_tools._mark_workspace_file_read(ctx, workspace_id, rel_path)
    except Exception:
        log.debug("Failed to mark repo_read workspace file read", exc_info=True)


def _repo_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    return json.dumps(
        _list_dir(ctx.repo_dir, dir, max_entries), ensure_ascii=False, indent=2
    )


def _drive_read(ctx: ToolContext, path: str) -> str:
    return read_text(ctx.drive_path(path))


def _drive_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    return json.dumps(
        _list_dir(ctx.drive_root, dir, max_entries), ensure_ascii=False, indent=2
    )


def _drive_write(
    ctx: ToolContext, path: str, content: str, mode: str = "overwrite"
) -> str:
    p = ctx.drive_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "overwrite":
        p.write_text(content, encoding="utf-8")
    else:
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    return f"OK: wrote {mode} {path} ({len(content)} chars)"


# ---------------------------------------------------------------------------
# Send photo to owner
# ---------------------------------------------------------------------------


def _send_photo(ctx: ToolContext, image_base64: str, caption: str = "") -> str:
    """Send a base64-encoded image to the owner's Telegram chat."""
    if not ctx.current_chat_id:
        return "⚠️ No active chat — cannot send photo."

    # Resolve screenshot reference from stash
    actual_b64 = image_base64
    if image_base64 == "__last_screenshot__":
        if not ctx.browser_state.last_screenshot_b64:
            return "⚠️ No screenshot stored. Take one first with browse_page(output='screenshot')."
        actual_b64 = ctx.browser_state.last_screenshot_b64

    if not actual_b64 or len(actual_b64) < 100:
        return "⚠️ image_base64 is empty or too short. Take a screenshot first with browse_page(output='screenshot')."

    ctx.pending_events.append(
        {
            "type": "send_photo",
            "chat_id": ctx.current_chat_id,
            "image_base64": actual_b64,
            "caption": caption or "",
        }
    )
    return "OK: photo queued for delivery to owner."


# ---------------------------------------------------------------------------
# Codebase digest
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        "build",
        "dist",
    }
)


def _extract_python_symbols(file_path: pathlib.Path) -> tuple[list[str], list[str]]:
    """Extract class and function names from a Python file using AST."""
    try:
        code = file_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(file_path))
        classes = []
        functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(node.name)
        return list(dict.fromkeys(classes)), list(dict.fromkeys(functions))
    except Exception:
        log.warning(f"Failed to extract Python symbols from {file_path}", exc_info=True)
        return [], []


def _codebase_digest(ctx: ToolContext) -> str:
    """Generate a compact digest of the codebase: files, sizes, classes, functions."""
    repo_dir = ctx.repo_dir
    py_files: list[pathlib.Path] = []
    md_files: list[pathlib.Path] = []
    other_files: list[pathlib.Path] = []

    for dirpath, dirnames, filenames in os.walk(str(repo_dir)):
        # Skip excluded directories
        dirnames[:] = [d for d in sorted(dirnames) if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            p = pathlib.Path(dirpath) / fn
            if not p.is_file():
                continue
            if p.suffix == ".py":
                py_files.append(p)
            elif p.suffix == ".md":
                md_files.append(p)
            elif p.suffix in (".txt", ".cfg", ".toml", ".yml", ".yaml", ".json"):
                other_files.append(p)

    total_lines = 0
    total_functions = 0
    sections: list[str] = []

    # Python files
    for pf in py_files:
        try:
            lines = pf.read_text(encoding="utf-8").splitlines()
            line_count = len(lines)
            total_lines += line_count
            classes, functions = _extract_python_symbols(pf)
            total_functions += len(functions)
            rel = pf.relative_to(repo_dir).as_posix()
            parts = [f"\n== {rel} ({line_count} lines) =="]
            if classes:
                cl = ", ".join(classes[:10])
                if len(classes) > 10:
                    cl += f", ... ({len(classes)} total)"
                parts.append(f"  Classes: {cl}")
            if functions:
                fn = ", ".join(functions[:20])
                if len(functions) > 20:
                    fn += f", ... ({len(functions)} total)"
                parts.append(f"  Functions: {fn}")
            sections.append("\n".join(parts))
        except Exception:
            log.debug(
                f"Failed to process Python file {pf} in codebase_digest", exc_info=True
            )
            pass

    # Markdown files
    for mf in md_files:
        try:
            line_count = len(mf.read_text(encoding="utf-8").splitlines())
            total_lines += line_count
            rel = mf.relative_to(repo_dir).as_posix()
            sections.append(f"\n== {rel} ({line_count} lines) ==")
        except Exception:
            log.debug(
                f"Failed to process markdown file {mf} in codebase_digest",
                exc_info=True,
            )
            pass

    # Other config files (just names + sizes)
    for of in other_files:
        try:
            line_count = len(of.read_text(encoding="utf-8").splitlines())
            total_lines += line_count
            rel = of.relative_to(repo_dir).as_posix()
            sections.append(f"\n== {rel} ({line_count} lines) ==")
        except Exception:
            log.debug(
                f"Failed to process config file {of} in codebase_digest", exc_info=True
            )
            pass

    total_files = len(py_files) + len(md_files) + len(other_files)
    header = f"Codebase Digest ({total_files} files, {total_lines} lines, {total_functions} functions)"
    return header + "\n" + "\n".join(sections)


# ---------------------------------------------------------------------------
# Summarize dialogue
# ---------------------------------------------------------------------------


def _summarize_dialogue(ctx: ToolContext, last_n: int = 200) -> str:
    """Summarize dialogue history into key moments, decisions, and creator preferences."""
    from ouroboros.llm import LLMClient, DEFAULT_LIGHT_MODEL

    # Read last_n messages from chat.jsonl
    chat_path = ctx.drive_root / "logs" / "chat.jsonl"
    if not chat_path.exists():
        return "⚠️ chat.jsonl not found"

    try:
        entries = []
        with chat_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.debug(
                            "Failed to parse chat.jsonl line in summarize_dialogue",
                            exc_info=True,
                        )
                        continue

        # Take last N entries
        entries = entries[-last_n:] if len(entries) > last_n else entries

        if not entries:
            return "⚠️ No chat entries found"

        # Format entries as text
        dialogue_text = []
        for entry in entries:
            ts = entry.get("ts", "")
            direction = entry.get("direction", "")
            role = "Creator" if direction == "in" else "Ouroboros"
            text = entry.get("text", "")
            dialogue_text.append(f"[{ts}] {role}: {text}")

        formatted_dialogue = "\n".join(dialogue_text)

        # Build summarization prompt
        prompt = f"""Summarize the following dialogue history between the creator and Ouroboros.

Extract:
1. Key decisions made (technical, architectural, strategic)
2. Creator's preferences and communication style
3. Important technical choices and their rationale
4. Recurring themes or patterns

For each key moment, include the timestamp.

Format as markdown with clear sections.

Dialogue history ({len(entries)} messages):

{formatted_dialogue}

Now write a comprehensive summary:"""

        # Call LLM
        llm = LLMClient()
        model = os.environ.get("OUROBOROS_MODEL_LIGHT", "") or DEFAULT_LIGHT_MODEL

        messages = [{"role": "user", "content": prompt}]

        response, usage = llm.chat(
            messages=messages,
            model=model,
            max_tokens=4096,
        )

        # Track cost in budget system
        if usage:
            usage_event = {
                "type": "llm_usage",
                "ts": utc_now_iso(),
                "task_id": ctx.task_id if ctx.task_id else "",
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "cost": usage.get("cost", 0),
                },
                "category": "summarize",
            }
            if ctx.event_queue is not None:
                try:
                    ctx.event_queue.put_nowait(usage_event)
                except Exception:
                    if hasattr(ctx, "pending_events"):
                        ctx.pending_events.append(usage_event)
            elif hasattr(ctx, "pending_events"):
                ctx.pending_events.append(usage_event)

        summary = response.get("content", "")
        if not summary:
            return "⚠️ LLM returned empty summary"

        # Write to memory/dialogue_summary.md
        summary_path = ctx.drive_root / "memory" / "dialogue_summary.md"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary, encoding="utf-8")

        cost = float(usage.get("cost", 0))
        return f"OK: Summarized {len(entries)} messages. Written to memory/dialogue_summary.md. Cost: ${cost:.4f}\n\n{summary[:500]}..."

    except Exception as e:
        log.warning("Failed to summarize dialogue", exc_info=True)
        return f"⚠️ Error: {repr(e)}"


# ---------------------------------------------------------------------------
# forward_to_worker — LLM-initiated message routing to worker tasks
# ---------------------------------------------------------------------------


def _forward_to_worker(ctx: ToolContext, task_id: str, message: str) -> str:
    """Forward a message to a running worker task's mailbox."""
    from ouroboros.owner_inject import write_owner_message

    write_owner_message(
        ctx.drive_root, message, task_id=task_id, msg_id=uuid.uuid4().hex
    )
    return f"Message forwarded to task {task_id}"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "repo_read",
            {
                "name": "repo_read",
                "description": (
                    "Read a UTF-8 text file from the GitHub repo (relative path). "
                    "Use optional `offset` (character offset) and `max_chars` to inspect "
                    "long files in chunks. When you have line numbers from pytest, rg, "
                    "findstr, or stack traces, use `line_start`/`line_count` instead of "
                    "`offset`; `offset` is not a line number. "
                    "For active workspace files, prefer phase `read_file`; "
                    "`repo_read(\"workspaces/<workspace_id>/<path>\")` also "
                    "counts as the required pre-read before `apply_workspace_patch`."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer", "default": 0},
                        "max_chars": {"type": "integer", "default": 0},
                        "line_start": {"type": "integer", "default": 0},
                        "line_count": {"type": "integer", "default": 160},
                    },
                    "required": ["path"],
                },
            },
            _repo_read,
        ),
        ToolEntry(
            "repo_list",
            {
                "name": "repo_list",
                "description": "List files under a repo directory (relative path).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dir": {"type": "string", "default": "."},
                        "max_entries": {"type": "integer", "default": 500},
                    },
                    "required": [],
                },
            },
            _repo_list,
        ),
        ToolEntry(
            "drive_read",
            {
                "name": "drive_read",
                "description": "Read a UTF-8 text file from Google Drive (relative to MyDrive/Ouroboros/).",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            _drive_read,
        ),
        ToolEntry(
            "drive_list",
            {
                "name": "drive_list",
                "description": "List files under a Drive directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dir": {"type": "string", "default": "."},
                        "max_entries": {"type": "integer", "default": 500},
                    },
                    "required": [],
                },
            },
            _drive_list,
        ),
        ToolEntry(
            "drive_write",
            {
                "name": "drive_write",
                "description": "Write a UTF-8 text file on Google Drive.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "enum": ["overwrite", "append"],
                            "default": "overwrite",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
            _drive_write,
        ),
        ToolEntry(
            "send_photo",
            {
                "name": "send_photo",
                "description": (
                    "Send a base64-encoded image (PNG) to the owner's Telegram chat. "
                    "Use after browse_page(output='screenshot') or browser_action(action='screenshot'). "
                    "Pass the base64 string from the screenshot result as image_base64."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_base64": {
                            "type": "string",
                            "description": "Base64-encoded PNG image data",
                        },
                        "caption": {
                            "type": "string",
                            "description": "Optional caption for the photo",
                        },
                    },
                    "required": ["image_base64"],
                },
            },
            _send_photo,
        ),
        ToolEntry(
            "codebase_digest",
            {
                "name": "codebase_digest",
                "description": "Get a compact digest of the entire codebase: files, sizes, classes, functions. One call instead of many repo_read calls.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            _codebase_digest,
        ),
        ToolEntry(
            "summarize_dialogue",
            {
                "name": "summarize_dialogue",
                "description": "Summarize dialogue history into key moments, decisions, and creator preferences. Writes to memory/dialogue_summary.md.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "last_n": {
                            "type": "integer",
                            "description": "Number of recent messages to summarize (default 200)",
                        },
                    },
                    "required": [],
                },
            },
            _summarize_dialogue,
        ),
        ToolEntry(
            "forward_to_worker",
            {
                "name": "forward_to_worker",
                "description": (
                    "Forward a message to a running worker task's mailbox. "
                    "Use when the owner sends a message during your active conversation "
                    "that is relevant to a specific running background task. "
                    "The worker will see it as [Owner message during task] on its next LLM round."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "ID of the running task to forward to",
                        },
                        "message": {
                            "type": "string",
                            "description": "Message text to forward",
                        },
                    },
                    "required": ["task_id", "message"],
                },
            },
            _forward_to_worker,
        ),
    ]
