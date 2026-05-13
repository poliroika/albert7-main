"""
Ouroboros — LLM tool loop.

Core loop: send messages to LLM, execute tool calls, repeat until final response.
Extracted from agent.py to keep the agent thin.
"""

import json
import os
import pathlib
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from collections.abc import Callable

import logging

from ouroboros.llm import LLMClient, normalize_reasoning_effort, add_usage
from ouroboros.tools.registry import ToolRegistry
from ouroboros.context import compact_tool_history, compact_tool_history_llm
from ouroboros.deadline import (
    DEADLINE_AFTER_LLM_RESPONSE,
    DEADLINE_BEFORE_NEXT_ROUND,
    check_runtime_deadline,
)
from ouroboros.utils import (
    utc_now_iso,
    append_jsonl,
    truncate_for_log,
    sanitize_tool_args_for_log,
    sanitize_tool_result_for_log,
    estimate_tokens,
)
from ouroboros.tool_args_repair import repair_tool_arguments
from ouroboros.discipline import VerifyGate, WRITE_TOOL_NAMES
from ouroboros.preflight_recovery import (
    PreflightErrorTracker,
    extract_pseudo_xml_args,
    format_examples_for_prompt,
    recent_successful_args,
)

# Process-wide preflight error tracker. Counters are keyed by
# (task_id, fn_name, phase_label). Reset whenever a successful tool
# call lands for the same task. Used to escalate the error message we
# return to the LLM after repeated stuck states (no successful args
# despite repeated tries on the same tool/phase).
_PREFLIGHT_TRACKER = PreflightErrorTracker()

log = logging.getLogger(__name__)
_MODEL_PRICING_STATIC = {
    "anthropic/claude-opus-4.6": (5.0, 0.5, 25.0),
    "anthropic/claude-opus-4": (15.0, 1.5, 75.0),
    "anthropic/claude-sonnet-4": (3.0, 0.30, 15.0),
    "anthropic/claude-sonnet-4.6": (3.0, 0.30, 15.0),
    "anthropic/claude-sonnet-4.5": (3.0, 0.30, 15.0),
    "openai/o3": (2.0, 0.50, 8.0),
    "openai/o3-pro": (20.0, 1.0, 80.0),
    "openai/o4-mini": (1.10, 0.275, 4.40),
    "openai/gpt-4.1": (2.0, 0.50, 8.0),
    "openai/gpt-5.2": (1.75, 0.175, 14.0),
    "openai/gpt-5.2-codex": (1.75, 0.175, 14.0),
    "google/gemini-2.5-pro-preview": (1.25, 0.125, 10.0),
    "google/gemini-3-pro-preview": (2.0, 0.20, 12.0),
    "x-ai/grok-3-mini": (0.30, 0.03, 0.50),
    "qwen/qwen3.5-plus-02-15": (0.40, 0.04, 2.40),
}

_pricing_fetched = False
_cached_pricing = None
_pricing_lock = threading.Lock()


def _get_pricing() -> dict[str, tuple[float, float, float]]:
    """
    Lazy-load pricing. On first call, attempts to fetch from OpenRouter API.
    Falls back to static pricing if fetch fails.
    Thread-safe via module-level lock.
    """
    global _pricing_fetched, _cached_pricing

    if _pricing_fetched:
        return _cached_pricing or _MODEL_PRICING_STATIC

    with _pricing_lock:
        if _pricing_fetched:
            return _cached_pricing or _MODEL_PRICING_STATIC

        _pricing_fetched = True
        _cached_pricing = dict(_MODEL_PRICING_STATIC)

        try:
            from ouroboros.llm import fetch_openrouter_pricing

            _live = fetch_openrouter_pricing()
            if _live and len(_live) > 5:
                _cached_pricing.update(_live)
        except Exception as e:
            import logging as _log

            _log.getLogger(__name__).warning(
                "Failed to sync pricing from OpenRouter: %s", e
            )
            _pricing_fetched = False

        return _cached_pricing


def _estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimate cost from token counts using known pricing. Returns 0 if model unknown."""
    model_pricing = _get_pricing()
    pricing = model_pricing.get(model)
    if not pricing:
        best_match = None
        best_length = 0
        for key, val in model_pricing.items():
            if model and model.startswith(key):
                if len(key) > best_length:
                    best_match = val
                    best_length = len(key)
        pricing = best_match
    if not pricing:
        return 0.0
    input_price, cached_price, output_price = pricing
    regular_input = max(0, prompt_tokens - cached_tokens)
    cost = (
        regular_input * input_price / 1_000_000
        + cached_tokens * cached_price / 1_000_000
        + completion_tokens * output_price / 1_000_000
    )
    return round(cost, 6)


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PRIVATE_KEY))\s*[:=]\s*)(['\"]?)([^\s,'\"]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._\-+/=]{12,})")
_PSEUDO_TOOLCALL_MARKERS = (
    "<tool_call>",
    "</tool_call>",
    "<function_call>",
    "</function_call>",
    "<arg_key>",
    "<arg_value>",
)
_PSEUDO_TOOLCALL_NAME_RE = re.compile(
    r"(?i)\b(update_workspace_seed|run_workspace_command|mark_subtask_complete|propose_task_plan|revise_remaining_plan)\s*\("
)
_FORBIDDEN_DELEGATION_SEEN: set[str] = set()
_JSON_TYPE_TO_PYTHON: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _infer_workspace_id_from_tools_context(tools: ToolRegistry) -> str:
    """Best-effort workspace id inference from active drive root."""
    try:
        ctx = getattr(tools, "_ctx", None)
        drive_root = pathlib.Path(str(getattr(ctx, "drive_root", "") or "")).resolve()
        parts = list(drive_root.parts)
        for idx, part in enumerate(parts):
            if part == "workspaces" and idx + 1 < len(parts):
                candidate = str(parts[idx + 1]).strip()
                if candidate:
                    return candidate
    except Exception:
        return ""
    return ""


def _redact_sensitive(text: str) -> str:
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[redacted]", text)
    return _BEARER_RE.sub(r"\1[redacted]", redacted)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("text"):
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(f"[{item.get('type') or 'content'}]")
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content)


def _looks_like_pseudo_tool_call_text(content: Any) -> bool:
    text = _content_to_text(content)
    if not text:
        return False
    low = text.lower()
    if any(marker in low for marker in _PSEUDO_TOOLCALL_MARKERS):
        return True
    return _PSEUDO_TOOLCALL_NAME_RE.search(text) is not None


def _message_summary(message: dict[str, Any], index: int) -> dict[str, Any]:
    content = _redact_sensitive(_content_to_text(message.get("content")))
    preview = content[:1200]
    if len(content) > len(preview):
        preview += "\n[truncated]"
    tool_calls = message.get("tool_calls") or []
    return {
        "index": index,
        "role": message.get("role"),
        "chars": len(content),
        "preview": preview,
        "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
    }


def _round_input_snapshot(messages: list[dict[str, Any]]) -> dict[str, Any]:
    role_counts: dict[str, int] = {}
    for message in messages:
        role = str(message.get("role") or "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1
    recent = messages[-12:]
    offset = len(messages) - len(recent)
    return {
        "message_count": len(messages),
        "estimated_tokens": estimate_tokens(messages),
        "role_counts": role_counts,
        "recent_messages": [
            _message_summary(message, offset + idx)
            for idx, message in enumerate(recent)
        ],
    }


def _round_output_snapshot(msg: dict[str, Any]) -> dict[str, Any]:
    content = _redact_sensitive(_content_to_text(msg.get("content")))
    tool_payloads = []
    for tool_call in msg.get("tool_calls") or []:
        fn_name = str(tool_call.get("function", {}).get("name") or "")
        args = _safe_tool_args_json(tool_call)
        tool_payloads.append(
            {
                "name": fn_name,
                "args": sanitize_tool_args_for_log(fn_name, args),
            }
        )
    preview = content[:4000]
    if len(content) > len(preview):
        preview += "\n[truncated]"
    return {
        "content_preview": preview,
        "content_chars": len(content),
        "tool_calls": tool_payloads,
        "finish_reason": msg.get("finish_reason") or msg.get("stop_reason"),
    }


def _append_round_io(
    drive_logs: pathlib.Path,
    *,
    task_id: str,
    round_idx: int,
    round_event: dict[str, Any],
    input_snapshot: dict[str, Any],
    msg: dict[str, Any],
) -> None:
    try:
        append_jsonl(
            drive_logs / "round_io.jsonl",
            {
                "ts": round_event.get("ts") or utc_now_iso(),
                "type": "round_io",
                "task_id": task_id,
                "round": round_idx,
                "phase": round_event.get("phase"),
                "model": round_event.get("model"),
                "input": input_snapshot,
                "output": _round_output_snapshot(msg),
            },
        )
    except Exception:
        log.debug("Failed to append round IO trace", exc_info=True)


def _check_stop_requested(
    drive_root: pathlib.Path | None,
    task_id: str,
    accumulated_usage: dict[str, Any],
    llm_trace: dict[str, Any],
    content: str | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    if drive_root is None:
        return None
    stop_path = pathlib.Path(drive_root) / "state" / "stop_requested.json"
    if not stop_path.exists():
        return None
    try:
        payload = json.loads(stop_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        payload = {}
    if not _stop_request_matches_task(payload, task_id):
        return None
    if content and content.strip():
        llm_trace["assistant_notes"].append(content.strip()[:320])
    reason = (
        str(payload.get("reason") or "manual stop requested")
        if isinstance(payload, dict)
        else "manual stop requested"
    )
    return f"Stop requested by dashboard: {reason}", accumulated_usage, llm_trace


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


READ_ONLY_PARALLEL_TOOLS = frozenset(
    {
        "repo_read",
        "repo_list",
        "drive_read",
        "drive_list",
        "web_search",
        "codebase_digest",
        "chat_history",
        "terminal_view",
    }
)

_REPEATED_READ_THRESHOLD = 4
_REPEATED_READ_REMIND_EVERY = 3


@dataclass
class _RepeatedReadGuardState:
    last_signature: str = ""
    streak: int = 0
    last_reminder_signature: str = ""
    last_reminder_streak: int = 0


_REPEATED_FAIL_THRESHOLD = 3
_REPEATED_FAIL_REMIND_EVERY = 2
_DEFAULT_LLM_LOOP_RETRIES = 3


@dataclass
class _RepeatedFailureGuardState:
    last_signature: str = ""
    streak: int = 0
    last_reminder_signature: str = ""
    last_reminder_streak: int = 0
    last_error_excerpt: str = ""


@dataclass
class _CompletionToolImpasseState:
    counts: dict[tuple[str, str, str, str], int] = field(default_factory=dict)


_COMPLETION_IMPASSE_THRESHOLD = 3


def _safe_tool_args_json(tool_call: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = tool_call.get("function", {}).get("arguments") or "{}"
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_llm_loop_retries() -> int:
    raw = os.environ.get("OUROBOROS_LLM_LOOP_RETRIES")
    if raw is None or raw == "":
        return _DEFAULT_LLM_LOOP_RETRIES
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        log.warning(
            "Invalid OUROBOROS_LLM_LOOP_RETRIES=%r, defaulting to %d",
            raw,
            _DEFAULT_LLM_LOOP_RETRIES,
        )
        return _DEFAULT_LLM_LOOP_RETRIES


def _looks_like_file_view_command(argv: list[str]) -> bool:
    lowered = [str(item).strip().lower() for item in argv if str(item).strip()]
    if not lowered:
        return False
    if lowered[:3] in (
        ["cmd", "/c", "type"],
        ["cmd", "/c", "more"],
        ["cmd", "/c", "cat"],
    ):
        return len(lowered) >= 4
    if lowered[:4] == ["cmd", "/c", "powershell", "-command"]:
        return len(lowered) >= 5 and lowered[4] in {"get-content", "gc", "cat", "type"}
    if lowered[0] in {"cat", "more", "type"}:
        return len(lowered) >= 2
    if lowered[0] in {"findstr", "grep", "rg"}:
        return len(lowered) >= 3
    if lowered[0] in {"powershell", "pwsh"} and "-command" in lowered:
        return any(
            token
            in {"get-content", "gc", "cat", "type", "select-string", "sls", "findstr"}
            for token in lowered
        )
    return False


def _signature_from_repeated_read_tool_call(tool_call: dict[str, Any]) -> str:
    fn_name = tool_call.get("function", {}).get("name", "")
    args = _safe_tool_args_json(tool_call)
    workspace_id = str(args.get("workspace_id") or "").strip()

    if fn_name == "read_workspace_file":
        file_path = str(args.get("file_path") or "").strip()
        if workspace_id and file_path:
            return f"read_workspace_file({workspace_id}:{file_path})"
        return ""

    if fn_name != "run_workspace_command":
        return ""

    raw_cmd = args.get("argv")
    if raw_cmd is None:
        raw_cmd = args.get("command")
    if isinstance(raw_cmd, str):
        argv = [part for part in raw_cmd.split() if part]
    elif isinstance(raw_cmd, list):
        argv = [str(part) for part in raw_cmd if str(part).strip()]
    else:
        return ""
    if not _looks_like_file_view_command(argv):
        return ""
    preview = " ".join(argv[:10])
    if len(argv) > 10:
        preview += " ..."
    return f"run_workspace_command({workspace_id}:{preview})"


def _maybe_inject_repeated_read_guard(
    tool_calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    state: _RepeatedReadGuardState,
) -> None:
    signatures = [
        sig
        for tool_call in tool_calls
        if (sig := _signature_from_repeated_read_tool_call(tool_call))
    ]
    if not signatures:
        state.last_signature = ""
        state.streak = 0
        return

    for sig in signatures:
        if sig == state.last_signature:
            state.streak += 1
        else:
            state.last_signature = sig
            state.streak = 1

    if state.streak < _REPEATED_READ_THRESHOLD:
        return
    if (
        state.last_reminder_signature == state.last_signature
        and state.streak < state.last_reminder_streak + _REPEATED_READ_REMIND_EVERY
    ):
        return

    messages.append(
        {
            "role": "system",
            "content": (
                "[PROGRESS_GUARD] You have repeated the same workspace inspection "
                f"{state.streak} times: `{state.last_signature}`.\n"
                "Do not run the exact same read again unless the file changed. "
                "Use the information you already have and switch to a new action: "
                "inspect a different file, write a patch, verify, check Umbrella memory, "
                "or use research tools if you are blocked. For implementation subtasks, "
                "prefer producing the next runnable vertical slice over more local search."
            ),
        }
    )
    state.last_reminder_signature = state.last_signature
    state.last_reminder_streak = state.streak


_FAILED_TOOL_RESULT_MARKERS = (
    "syntaxerror",
    "traceback (most recent call last)",
    '"status": "blocked"',
    '"status": "invalid_command"',
    '"status": "error"',
    '"timed_out": true',
    "warning:",
    "error:",
    "tool_timeout",
    "modulenotfounderror",
)


def _signature_for_any_tool_call(tool_call: dict[str, Any]) -> str:
    """Stable signature for *any* tool call -- name + canonical args hash.

    Used by the failure-repetition guard. We hash the JSON-canonical form of
    the args so two calls with identical effect collide regardless of key
    order, and a small prefix preview is included for readability in the
    injected system message.
    """
    fn_name = tool_call.get("function", {}).get("name", "?")
    raw_args = tool_call.get("function", {}).get("arguments") or "{}"
    try:
        data = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except Exception:
        data = {"__raw__": str(raw_args)[:500]}
    try:
        canon = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        canon = repr(data)
    import hashlib

    digest = hashlib.sha1(canon.encode("utf-8", errors="replace")).hexdigest()[:10]
    preview = canon if len(canon) <= 160 else canon[:160] + "..."
    return f"{fn_name}#{digest}|{preview}"


def _last_tool_result_payload(messages: list[dict[str, Any]], tool_call_id: str) -> str:
    """Walk back through ``messages`` to find the matching tool result body."""
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        if str(msg.get("tool_call_id") or "") == str(tool_call_id):
            content = msg.get("content")
            return str(content)[:4000] if content else ""
    return ""


def _looks_like_tool_failure(payload: str) -> bool:
    if not payload:
        return False
    low = payload.lower()
    return any(marker in low for marker in _FAILED_TOOL_RESULT_MARKERS)


def _maybe_inject_repeated_failure_guard(
    tool_calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    state: _RepeatedFailureGuardState,
) -> None:
    """If the same tool call fails N times in a row, inject a hint.

    Distinct from ``_maybe_inject_repeated_read_guard`` which only watches
    *successful but redundant* file inspections. This guard fires when the
    model is stuck retrying a clearly-broken call (classic example: the
    Round 26-32 loop where `python -c` with `async def` SyntaxErrored 6
    times in a row and the model just kept resending the same payload).
    """
    if not tool_calls:
        return
    # Per round we look at every tool call the LLM emitted; in practice this
    # is almost always 1 because of the strict-tool-choice setup.
    for tc in tool_calls:
        sig = _signature_for_any_tool_call(tc)
        result_payload = _last_tool_result_payload(messages, tc.get("id", ""))
        if not _looks_like_tool_failure(result_payload):
            # Successful (or at least non-failure-shaped) call -- reset streak.
            state.last_signature = sig
            state.streak = 1
            state.last_error_excerpt = ""
            continue
        if sig == state.last_signature:
            state.streak += 1
        else:
            state.last_signature = sig
            state.streak = 1
        # Keep a short error excerpt for the reminder.
        state.last_error_excerpt = result_payload[:600]

    if state.streak < _REPEATED_FAIL_THRESHOLD:
        return
    if (
        state.last_reminder_signature == state.last_signature
        and state.streak < state.last_reminder_streak + _REPEATED_FAIL_REMIND_EVERY
    ):
        return

    messages.append(
        {
            "role": "system",
            "content": (
                "[FAILURE_LOOP_GUARD] You have called the same tool with the same "
                f"arguments {state.streak} times in a row and it failed every time:\n"
                f"  signature: {state.last_signature}\n"
                f"  last error excerpt: {state.last_error_excerpt[:400]}\n"
                "STOP retrying the same call. Read the error message carefully and "
                "either:\n"
                "  - change the arguments (different file, different command, fixed syntax),\n"
                "  - switch to a different tool (e.g. `run_python_code` instead of `python -c`, "
                "`bg_start` instead of foreground server, `web_fetch` for docs lookup),\n"
                "  - or pause and write a short plan to scratchpad before continuing,\n"
                "  - if the error looks like the harness itself is wrong (a guard "
                "rejects clearly valid input, a tool refuses a supported file format, "
                "an umbrella/ helper has a buggy default), use `sandbox_self_edit` to "
                "patch the offending file under `ouroboros/` or `umbrella/`. The change "
                "persists after the task ends, so use it only for real capability "
                "gaps in the harness itself."
            ),
        }
    )
    state.last_reminder_signature = state.last_signature
    state.last_reminder_streak = state.streak


def _loop_teardown(stateful_executor, tools, drive_root, task_id):
    """Best-effort cleanup of per-loop resources. Called from run_llm_loop's finally."""
    if stateful_executor:
        try:
            stateful_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            log.warning("Failed to shutdown stateful executor", exc_info=True)
    try:
        from ouroboros.tools.terminal_session import shutdown_all_sessions

        shutdown_all_sessions(tools._ctx)
    except Exception:
        log.debug("Failed to shutdown terminal sessions", exc_info=True)
    try:
        from ouroboros.tools import background_jobs as _bg_jobs

        bg_drive_root = getattr(tools._ctx, "drive_root", None) or drive_root
        if bg_drive_root is not None:
            _bg_jobs.shutdown_all(pathlib.Path(bg_drive_root))
    except Exception:
        log.debug("Failed to shutdown background jobs", exc_info=True)
    if drive_root is not None and task_id:
        try:
            from ouroboros.owner_inject import cleanup_task_mailbox

            cleanup_task_mailbox(drive_root, task_id)
        except Exception:
            log.debug("Failed to cleanup task mailbox", exc_info=True)


STATEFUL_BROWSER_TOOLS = frozenset({"browse_page", "browser_action"})


def _truncate_tool_result(result: Any) -> str:
    """
    Hard-cap tool result string to 15000 characters.
    If truncated, append a note with the original length.
    """
    result_str = str(result)
    if len(result_str) <= 15000:
        return result_str
    original_len = len(result_str)
    return result_str[:15000] + f"\n... (truncated from {original_len} chars)"


_FULL_RESULT_TRACE_TOOLS = frozenset(
    {
        "propose_discovery_plan",
        "propose_task_plan",
        "revise_remaining_plan",
        "mark_subtask_complete",
        "mark_remediation_complete",
        "run_workspace_verify",
    }
)


def _trace_result_text(item: dict[str, Any]) -> str:
    raw = item.get("result_full")
    if raw is None:
        raw = item.get("result")
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    return str(raw)


def _phase_return(
    value: Any,
    exit_reason: str = "final",
) -> tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, str]:
    """Normalize all _run_llm_phase exits to (final_or_none, reason)."""

    if value is None:
        return None, exit_reason
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and (value[0] is None or _looks_like_final_payload(value[0]))
        and isinstance(value[1], str)
    ):
        return value
    if _looks_like_final_payload(value):
        return value, exit_reason
    return None, str(value or exit_reason)


def _looks_like_final_payload(value: Any) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 3
        and isinstance(value[1], dict)
        and isinstance(value[2], dict)
    )


def _completion_tool_result_is_success(result_text: str) -> bool:
    """Completion/control tools are accepted only when their handler says OK."""

    return str(result_text or "").lstrip().startswith("OK:")


def _successful_terminating_tools(
    *,
    tool_calls: list[dict[str, Any]],
    trace_tool_calls: list[dict[str, Any]],
    terminating_tools: frozenset,
) -> tuple[set[str], dict[str, str]]:
    """Return accepted and rejected terminating tools for the latest round.

    A completion tool call that returns a warning/error is feedback, not a
    phase boundary. This distinction is critical for gates like
    ``mark_subtask_complete``: a failed verify-evidence gate must keep the
    agent in the same subtask so it can fix/rerun verify.
    """

    if not tool_calls or not terminating_tools:
        return set(), {}
    invoked = [
        str(tc.get("function", {}).get("name") or "").strip()
        for tc in tool_calls
        if str(tc.get("function", {}).get("name") or "").strip() in terminating_tools
    ]
    if not invoked:
        return set(), {}

    tail = trace_tool_calls[-len(tool_calls) :] if trace_tool_calls else []
    successes: set[str] = set()
    rejected: dict[str, str] = {}
    for item in tail:
        tool_name = str(item.get("tool") or "").strip()
        if tool_name not in terminating_tools:
            continue
        result_text = _trace_result_text(item)
        if _completion_tool_result_is_success(result_text):
            successes.add(tool_name)
        else:
            rejected[tool_name] = result_text[:1000] or "<empty tool result>"

    # If the trace is missing for an invoked terminator, do not terminate
    # optimistically. The safe move is to let the loop continue with a nudge.
    for tool_name in invoked:
        if tool_name not in successes and tool_name not in rejected:
            rejected[tool_name] = "<missing tool result>"
    return successes, rejected


def _format_rejected_termination_nudge(rejected: dict[str, str]) -> str:
    rendered = []
    for name, result in sorted(rejected.items()):
        one_line = " ".join(str(result or "").split())
        rendered.append(f"- `{name}` returned: {one_line[:700]}")
    body = "\n".join(rendered)
    return (
        "[COMPLETION_TOOL_REJECTED]\n"
        "A phase-completion tool was called, but the tool did not accept "
        "the completion. This phase is still active. Read the tool result, "
        "fix the requested issue, rerun verification when required, and "
        "only retry the completion tool after it can return `OK:`.\n"
        f"{body}"
    )


_FORBIDDEN_TOOL_REPEAT_LIMIT = 3


def _allowed_tool_names_from_schemas(
    tool_schemas: list[dict[str, Any]] | None,
) -> frozenset | None:
    """Return the set of tool names exposed by ``tool_schemas``.

    ``None`` means "no restriction" (let the registry execute anything that
    is registered). Returning an empty frozenset would mean "no tools at
    all", which is never what we want — collapse that case to ``None``.
    """
    if not tool_schemas:
        return None
    names = {
        str(((s or {}).get("function") or {}).get("name") or "").strip()
        for s in tool_schemas
    }
    names.discard("")
    return frozenset(names) if names else None


def _format_forbidden_tool_error(
    fn_name: str,
    allowed: frozenset,
    phase_label: str,
) -> str:
    """User-facing message returned to the LLM when it calls a tool that is
    not part of the active schema for the current phase."""
    sample = ", ".join(sorted(allowed)[:8]) or "<no tools>"
    return (
        f"\u26a0\ufe0f TOOL_FORBIDDEN_IN_PHASE: '{fn_name}' is not exposed "
        f"in the current phase ({phase_label}). The harness only routes "
        f"calls for the schema you were given. Allowed tool(s) right now: "
        f"{sample}. Re-emit your last action using one of the allowed "
        "tools, do not repeat this forbidden call."
    )


def _rewrite_forbidden_tool_call_if_safe(
    tc: dict[str, Any],
    *,
    allowed_tool_names: frozenset | None,
    phase_label: str,
) -> tuple[dict[str, Any], str | None]:
    """Return a forbidden call unchanged.

    Review-phase writes used to be rewritten to ``revise_remaining_plan``
    with ``steps=[]``. That looked like a harmless no-op, but the planner
    interprets an empty tail as "clear all remaining work", so a mistaken
    write call could prematurely finish a run. Keep forbidden tools hard
    failures and let the existing strike counter move the phase forward.
    """
    del allowed_tool_names, phase_label
    return tc, None


def _maybe_delegate_forbidden_tool_call(
    *,
    tools: ToolRegistry,
    fn_name: str,
    tc: dict[str, Any],
    drive_logs: pathlib.Path,
    task_id: str,
    phase_label: str,
    allowed_tool_names: frozenset | None,
) -> str | None:
    """Queue a delegated follow-up only for subtask-phase forbidden calls."""
    if not (
        phase_label.startswith("subtask_") or phase_label.startswith("remediation_")
    ):
        return None
    if "schedule_task" in (allowed_tool_names or frozenset()):
        return None
    if "schedule_task" not in set(tools.available_tools()):
        return None

    args = _safe_tool_args_json(tc)
    delegated_args = sanitize_tool_args_for_log(
        fn_name,
        args if isinstance(args, dict) else {},
    )
    signature = json.dumps(
        {
            "task_id": task_id,
            "phase": phase_label,
            "tool": fn_name,
            "args": delegated_args,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    if signature in _FORBIDDEN_DELEGATION_SEEN:
        return (
            f"⚠️ TOOL_FORBIDDEN_IN_PHASE: '{fn_name}' is not exposed in {phase_label}. "
            "Equivalent delegation already queued; continue with currently allowed tools."
        )
    _FORBIDDEN_DELEGATION_SEEN.add(signature)

    context_payload = {
        "origin": "auto_forbidden_tool_delegate",
        "task_id": task_id,
        "phase": phase_label,
        "forbidden_tool": fn_name,
        "forbidden_args": delegated_args,
        "allowed_tools": sorted(allowed_tool_names or frozenset()),
    }
    delegated = tools.execute(
        "schedule_task",
        {
            "description": (
                f"Handle forbidden tool call `{fn_name}` from phase `{phase_label}` "
                "using tools allowed in that phase."
            ),
            "context": json.dumps(context_payload, ensure_ascii=False),
            "parent_task_id": task_id,
        },
    )
    append_jsonl(
        drive_logs / "events.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "tool_forbidden_delegated",
            "task_id": task_id,
            "phase": phase_label,
            "tool": fn_name,
            "allowed": sorted(allowed_tool_names or frozenset()),
            "delegate_result": truncate_for_log(str(delegated), 500),
        },
    )
    return (
        f"⚠️ TOOL_FORBIDDEN_IN_PHASE: '{fn_name}' is not exposed in {phase_label}. "
        f"Auto-delegated via schedule_task: {delegated}. Continue current phase with allowed tools."
    )


def _blocked_tool_result_if_stop_requested(
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    drive_logs: pathlib.Path,
    task_id: str,
    phase_label: str,
) -> dict[str, Any] | None:
    stop_path = pathlib.Path(drive_logs).parent / "state" / "stop_requested.json"
    if not stop_path.exists():
        return None
    try:
        stop_payload = json.loads(
            stop_path.read_text(encoding="utf-8", errors="replace")
        )
    except Exception:
        stop_payload = {}
    if not _stop_request_matches_task(stop_payload, task_id):
        return None
    reason = (
        str(stop_payload.get("reason") or "manual stop requested")
        if isinstance(stop_payload, dict)
        else "manual stop requested"
    )
    result = f"STOP_REQUESTED: refusing to start tool `{fn_name}` after dashboard cancel: {reason}"
    append_jsonl(
        drive_logs / "events.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "tool_blocked_stop_requested",
            "task_id": task_id,
            "tool": fn_name,
            "phase": phase_label,
            "reason": reason,
        },
    )
    append_jsonl(
        drive_logs / "tools.jsonl",
        {
            "ts": utc_now_iso(),
            "tool": fn_name,
            "task_id": task_id,
            "args": {},
            "result_preview": result,
        },
    )
    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": True,
        "args_for_log": {},
        "is_code_tool": is_code_tool,
    }


def _tool_execution_result(
    *,
    tool_call_id: str,
    fn_name: str,
    result: str,
    is_error: bool,
    args_for_log: dict[str, Any],
    is_code_tool: bool,
) -> dict[str, Any]:
    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": is_error,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


def _recover_pseudo_xml_tool_name(
    tc: dict[str, Any],
    *,
    drive_logs: pathlib.Path,
    task_id: str,
    phase_label: str,
) -> str:
    fn_name = tc["function"]["name"]
    raw_arguments_before = tc.get("function", {}).get("arguments")
    clean_fn_name, salvaged_args = extract_pseudo_xml_args(
        fn_name,
        raw_arguments_before,
    )
    if not clean_fn_name or clean_fn_name == fn_name:
        return fn_name

    try:
        existing_args, _note = repair_tool_arguments(
            clean_fn_name,
            raw_arguments_before or "{}",
        )
    except Exception:
        existing_args = {}
    if not isinstance(existing_args, dict):
        existing_args = {}

    merged = {**salvaged_args, **existing_args}
    tc.setdefault("function", {})["name"] = clean_fn_name
    try:
        tc["function"]["arguments"] = json.dumps(merged, ensure_ascii=False)
    except (TypeError, ValueError):
        pass
    append_jsonl(
        drive_logs / "events.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "tool_pseudo_xml_recovered",
            "task_id": task_id,
            "tool": clean_fn_name,
            "phase": phase_label,
            "salvaged_keys": sorted(salvaged_args.keys()),
        },
    )
    return clean_fn_name


def _handle_forbidden_tool_call(
    *,
    tools: ToolRegistry,
    tc: dict[str, Any],
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    drive_logs: pathlib.Path,
    task_id: str,
    allowed_tool_names: frozenset | None,
    phase_label: str,
) -> dict[str, Any] | None:
    tc, rewrite_note = _rewrite_forbidden_tool_call_if_safe(
        tc,
        allowed_tool_names=allowed_tool_names,
        phase_label=phase_label,
    )
    if rewrite_note:
        fn_name = tc["function"]["name"]
        append_jsonl(
            drive_logs / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "tool_rewrite",
                "task_id": task_id,
                "phase": phase_label,
                "note": rewrite_note,
            },
        )

    if allowed_tool_names is None or fn_name in allowed_tool_names:
        return None

    delegated_result = _maybe_delegate_forbidden_tool_call(
        tools=tools,
        fn_name=fn_name,
        tc=tc,
        drive_logs=drive_logs,
        task_id=task_id,
        phase_label=phase_label,
        allowed_tool_names=allowed_tool_names,
    )
    if delegated_result is not None:
        append_jsonl(
            drive_logs / "tools.jsonl",
            {
                "ts": utc_now_iso(),
                "tool": fn_name,
                "task_id": task_id,
                "args": {},
                "result_preview": sanitize_tool_result_for_log(
                    truncate_for_log(delegated_result, 2000)
                ),
            },
        )
        return _tool_execution_result(
            tool_call_id=tool_call_id,
            fn_name=fn_name,
            result=delegated_result,
            is_error=False,
            args_for_log={},
            is_code_tool=is_code_tool,
        )

    result = _format_forbidden_tool_error(fn_name, allowed_tool_names, phase_label)
    log.warning(
        "[TOOLS] Refusing tool '%s' — not in active schema for phase '%s' (allowed=%s)",
        fn_name,
        phase_label,
        ",".join(sorted(allowed_tool_names))[:200],
    )
    append_jsonl(
        drive_logs / "events.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "tool_forbidden",
            "task_id": task_id,
            "tool": fn_name,
            "phase": phase_label,
            "allowed": sorted(allowed_tool_names),
        },
    )
    return _tool_execution_result(
        tool_call_id=tool_call_id,
        fn_name=fn_name,
        result=result,
        is_error=True,
        args_for_log={},
        is_code_tool=is_code_tool,
    )


def _handle_tool_preflight_error(
    *,
    fn_name: str,
    tc: dict[str, Any],
    tools: ToolRegistry,
    tool_call_id: str,
    is_code_tool: bool,
    drive_logs: pathlib.Path,
    task_id: str,
    phase_label: str,
) -> dict[str, Any] | None:
    preflight_error = _tool_call_preflight_error(fn_name, tc, tools)
    if not preflight_error:
        return None

    schema_hint = _tool_schema_hint(fn_name, tools)
    result = f"⚠️ TOOL_PREFLIGHT_ERROR ({fn_name}): {preflight_error}"
    if schema_hint:
        result = f"{result}\n{schema_hint}"
    try:
        raw_args_str = tc.get("function", {}).get("arguments")
        offending_args, _repair_note = repair_tool_arguments(
            fn_name, raw_args_str or "{}"
        )
    except Exception:
        offending_args, raw_args_str = {}, None
    offending_keys = (
        sorted(list(offending_args.keys())) if isinstance(offending_args, dict) else []
    )
    try:
        raw_preview = str(raw_args_str)[:600] if raw_args_str is not None else ""
    except Exception:
        raw_preview = ""

    consecutive = _PREFLIGHT_TRACKER.bump(task_id, fn_name, phase_label)
    examples_text = ""
    if consecutive >= 3:
        try:
            examples = recent_successful_args(drive_logs, fn_name, n=2, task_id=task_id)
        except Exception:
            examples = []
        examples_text = format_examples_for_prompt(examples)
        if examples_text:
            result = (
                f"{result}\n\n"
                f"You have failed `{fn_name}` preflight {consecutive} times in a row. "
                "Stop guessing the field names — copy the shape of these recent successful "
                f"calls from THIS run and adapt them:\n{examples_text}"
            )
        else:
            result = (
                f"{result}\n\n"
                f"You have failed `{fn_name}` preflight {consecutive} times in a row "
                "without ever succeeding in this run. Re-read the schema, then EITHER "
                "emit the call with the exact required fields OR pick a different "
                "approach (read_workspace_file to inspect, then write)."
            )
    if consecutive >= 6:
        result = (
            f"{result}\n\n"
            f"⛔ CIRCUIT_BREAKER: This is your {consecutive}th failed "
            f"`{fn_name}` call in {phase_label}. Subsequent identical calls "
            "in this phase will keep failing without re-trying the LLM "
            f"prompt. Stop calling `{fn_name}` with the same shape and try "
            "a different approach right now (e.g. read the file first, "
            "then construct the write call from the read result). "
            "If you are stuck, mark this subtask failed via "
            "`mark_subtask_complete` and let the next phase reassess."
        )

    append_jsonl(
        drive_logs / "events.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "tool_preflight_error",
            "task_id": task_id,
            "tool": fn_name,
            "phase": phase_label,
            "error": preflight_error,
            "schema_hint": schema_hint,
            "received_keys": offending_keys,
            "received_preview": raw_preview,
            "consecutive": consecutive,
            "examples_injected": bool(examples_text),
        },
    )
    return _tool_execution_result(
        tool_call_id=tool_call_id,
        fn_name=fn_name,
        result=result,
        is_error=True,
        args_for_log={},
        is_code_tool=is_code_tool,
    )


def _execute_tool_after_preflight(
    *,
    tools: ToolRegistry,
    tc: dict[str, Any],
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    drive_logs: pathlib.Path,
    task_id: str,
    phase_label: str,
) -> dict[str, Any]:
    try:
        args, repair_note = repair_tool_arguments(
            fn_name, tc["function"]["arguments"] or "{}"
        )
        if repair_note.startswith("unrepairable:"):
            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "tool_args_repair",
                    "task_id": task_id,
                    "tool": fn_name,
                    "phase": phase_label,
                    "note": repair_note,
                    "repaired": False,
                },
            )
            return _tool_execution_result(
                tool_call_id=tool_call_id,
                fn_name=fn_name,
                result=(
                    f"⚠️ TOOL_ARG_ERROR ({fn_name}): {repair_note}. "
                    "Re-emit this call with valid JSON arguments."
                ),
                is_error=True,
                args_for_log={},
                is_code_tool=is_code_tool,
            )
        if repair_note and repair_note not in ("ok", "already_dict", "empty"):
            log.info("[loop] Repaired malformed args for %s (%s)", fn_name, repair_note)
            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "tool_args_repair",
                    "task_id": task_id,
                    "tool": fn_name,
                    "phase": phase_label,
                    "note": repair_note,
                    "repaired": True,
                },
            )
            try:
                tc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
            except (TypeError, ValueError):
                pass
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        append_jsonl(
            drive_logs / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "tool_args_repair",
                "task_id": task_id,
                "tool": fn_name,
                "phase": phase_label,
                "note": repr(e),
                "repaired": False,
            },
        )
        return _tool_execution_result(
            tool_call_id=tool_call_id,
            fn_name=fn_name,
            result=f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}",
            is_error=True,
            args_for_log={},
            is_code_tool=is_code_tool,
        )

    args_for_log = sanitize_tool_args_for_log(
        fn_name, args if isinstance(args, dict) else {}
    )
    tool_ok = True
    try:
        result = tools.execute(fn_name, args)
    except Exception as e:
        tool_ok = False
        result = f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}"
        append_jsonl(
            drive_logs / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "tool_error",
                "task_id": task_id,
                "tool": fn_name,
                "args": args_for_log,
                "error": repr(e),
            },
        )

    append_jsonl(
        drive_logs / "tools.jsonl",
        {
            "ts": utc_now_iso(),
            "tool": fn_name,
            "task_id": task_id,
            "args": args_for_log,
            "result_preview": sanitize_tool_result_for_log(
                truncate_for_log(result, 2000)
            ),
        },
    )

    is_error = (not tool_ok) or str(result).startswith("⚠️")
    if not is_error:
        try:
            _PREFLIGHT_TRACKER.record_success(task_id)
        except Exception:
            pass

    return _tool_execution_result(
        tool_call_id=tool_call_id,
        fn_name=fn_name,
        result=result,
        is_error=is_error,
        args_for_log=args_for_log,
        is_code_tool=is_code_tool,
    )


def _execute_single_tool(
    tools: ToolRegistry,
    tc: dict[str, Any],
    drive_logs: pathlib.Path,
    task_id: str = "",
    allowed_tool_names: frozenset | None = None,
    phase_label: str = "",
) -> dict[str, Any]:
    """
    Execute a single tool call and return all needed info.

    Returns dict with: tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]

    # Pre-step: salvage pseudo-XML mess that GLM-4.7 occasionally bakes
    # into ``fn_name`` itself, e.g.
    # ``update_workspace_seed</arg_value>flag</arg_key><arg_value>false</arg_value>``.
    # Without recovery the call is rejected with ``unknown tool``,
    # ``schedule_task`` gets spuriously delegated, and the round is
    # wasted. ``extract_pseudo_xml_args`` returns the clean fn_name AND
    # any embedded ``<arg_key>/<arg_value>`` pairs we can still rescue;
    # we merge them into the existing ``arguments`` JSON before
    # preflight runs.
    fn_name = _recover_pseudo_xml_tool_name(
        tc,
        drive_logs=drive_logs,
        task_id=task_id,
        phase_label=phase_label,
    )

    is_code_tool = fn_name in tools.CODE_TOOLS
    stop_result = _blocked_tool_result_if_stop_requested(
        fn_name, tool_call_id, is_code_tool, drive_logs, task_id, phase_label
    )
    if stop_result is not None:
        return stop_result
    forbidden_result = _handle_forbidden_tool_call(
        tools=tools,
        tc=tc,
        fn_name=fn_name,
        tool_call_id=tool_call_id,
        is_code_tool=is_code_tool,
        drive_logs=drive_logs,
        task_id=task_id,
        allowed_tool_names=allowed_tool_names,
        phase_label=phase_label,
    )
    if forbidden_result is not None:
        return forbidden_result
    fn_name = tc["function"]["name"]

    preflight_result = _handle_tool_preflight_error(
        fn_name=fn_name,
        tc=tc,
        tools=tools,
        tool_call_id=tool_call_id,
        is_code_tool=is_code_tool,
        drive_logs=drive_logs,
        task_id=task_id,
        phase_label=phase_label,
    )
    if preflight_result is not None:
        return preflight_result

    return _execute_tool_after_preflight(
        tools=tools,
        tc=tc,
        fn_name=fn_name,
        tool_call_id=tool_call_id,
        is_code_tool=is_code_tool,
        drive_logs=drive_logs,
        task_id=task_id,
        phase_label=phase_label,
    )


def _tool_call_preflight_error(
    fn_name: str,
    tc: dict[str, Any],
    tools: ToolRegistry,
) -> str:
    """Validate required fields and primitive types before execution."""
    try:
        args, _note = repair_tool_arguments(
            fn_name, tc.get("function", {}).get("arguments") or "{}"
        )
    except Exception as exc:  # noqa: BLE001
        return f"arguments JSON is invalid: {exc}"
    if not isinstance(args, dict):
        return "arguments must decode to an object"
    get_schema = getattr(tools, "get_schema_by_name", None)
    if not callable(get_schema):
        return ""
    schema = get_schema(fn_name) or {}
    fn_schema = schema.get("function") if isinstance(schema, dict) else {}
    params = fn_schema.get("parameters") if isinstance(fn_schema, dict) else {}
    if not isinstance(params, dict):
        return ""
    required = params.get("required") or []
    if "workspace_id" in required and "workspace_id" not in args:
        inferred_workspace = _infer_workspace_id_from_tools_context(tools)
        if inferred_workspace:
            args["workspace_id"] = inferred_workspace
            try:
                tc["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
            except Exception:
                pass
    missing = [key for key in required if key not in args]
    if missing:
        return f"missing required field(s): {', '.join(missing)}"
    properties = params.get("properties") or {}
    if not isinstance(properties, dict):
        return ""
    for key, value in args.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        expected_type = str(spec.get("type") or "").strip()
        if not expected_type:
            continue
        py_types = _JSON_TYPE_TO_PYTHON.get(expected_type)
        if not py_types:
            continue
        if not isinstance(value, py_types):
            return f"field `{key}` expects {expected_type}, got {type(value).__name__}"
    return ""


def _tool_schema_hint(fn_name: str, tools: ToolRegistry) -> str:
    get_schema = getattr(tools, "get_schema_by_name", None)
    if not callable(get_schema):
        return ""
    schema = get_schema(fn_name) or {}
    fn_schema = schema.get("function") if isinstance(schema, dict) else {}
    params = fn_schema.get("parameters") if isinstance(fn_schema, dict) else {}
    if not isinstance(params, dict):
        return ""
    required = params.get("required") or []
    properties = params.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    example: dict[str, Any] = {}
    for key in required:
        spec = properties.get(key) if isinstance(properties.get(key), dict) else {}
        typ = str(spec.get("type") or "string")
        if typ == "array":
            example[key] = []
        elif typ == "object":
            example[key] = {}
        elif typ in {"integer", "number"}:
            example[key] = 0
        elif typ == "boolean":
            example[key] = False
        else:
            example[key] = f"<{key}>"
    return (
        f"Required args for `{fn_name}`: {', '.join(required) or '(none)'}. "
        f"Valid JSON example: {json.dumps(example, ensure_ascii=False)}"
    )


class _StatefulToolExecutor:
    """
    Thread-sticky executor for stateful tools (browser, etc).

    Playwright sync API uses greenlet internally which has strict thread-affinity:
    once a greenlet starts in a thread, all subsequent calls must happen in the same thread.
    This executor ensures browse_page/browser_action always run in the same thread.

    On timeout: we shutdown the executor and create a fresh one to reset state.
    """

    def __init__(self):
        self._executor: ThreadPoolExecutor | None = None

    def submit(self, fn, *args, **kwargs):
        """Submit work to the sticky thread. Creates executor on first call."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="stateful_tool"
            )
        return self._executor.submit(fn, *args, **kwargs)

    def reset(self):
        """Shutdown current executor and create a fresh one. Used after timeout/error."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def shutdown(self, wait=True, cancel_futures=False):
        """Final cleanup."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._executor = None


def _make_timeout_result(
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    tc: dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    reset_msg: str = "",
) -> dict[str, Any]:
    """
    Create a timeout error result dictionary and log the timeout event.

    Args:
        reset_msg: Optional additional message (e.g., "Browser state has been reset. ")

    Returns: Dict with tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    args_for_log = {}
    try:
        args, _note = repair_tool_arguments(
            fn_name, tc["function"]["arguments"] or "{}"
        )
        args_for_log = sanitize_tool_args_for_log(
            fn_name, args if isinstance(args, dict) else {}
        )
    except Exception:
        pass

    result = (
        f"⚠️ TOOL_TIMEOUT ({fn_name}): exceeded {timeout_sec}s limit. "
        f"The tool is still running in background but control is returned to you. "
        f"{reset_msg}Try a different approach or inform the owner{' about the issue' if not reset_msg else ''}."
    )

    append_jsonl(
        drive_logs / "events.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "tool_timeout",
            "tool": fn_name,
            "args": args_for_log,
            "timeout_sec": timeout_sec,
        },
    )
    append_jsonl(
        drive_logs / "tools.jsonl",
        {
            "ts": utc_now_iso(),
            "tool": fn_name,
            "args": args_for_log,
            "result_preview": result,
        },
    )

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": True,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


def _execute_with_timeout(
    tools: ToolRegistry,
    tc: dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    stateful_executor: _StatefulToolExecutor | None = None,
    allowed_tool_names: frozenset | None = None,
    phase_label: str = "",
) -> dict[str, Any]:
    """
    Execute a tool call with a hard timeout.

    On timeout: returns TOOL_TIMEOUT error so the LLM regains control.
    For stateful tools (browser): resets the sticky executor to recover state.
    For regular tools: the hung worker thread leaks as daemon — watchdog handles recovery.
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS

    # Cheap pre-check: if the tool is not exposed by the active schema we
    # never even spawn an executor — just synthesize the forbidden error.
    if allowed_tool_names is not None and fn_name not in allowed_tool_names:
        return _execute_single_tool(
            tools,
            tc,
            drive_logs,
            task_id,
            allowed_tool_names=allowed_tool_names,
            phase_label=phase_label,
        )

    use_stateful = stateful_executor and fn_name in STATEFUL_BROWSER_TOOLS

    if use_stateful:
        future = stateful_executor.submit(
            _execute_single_tool,
            tools,
            tc,
            drive_logs,
            task_id,
            allowed_tool_names,
            phase_label,
        )
        try:
            return future.result(timeout=timeout_sec)
        except TimeoutError:
            stateful_executor.reset()
            reset_msg = "Browser state has been reset. "
            return _make_timeout_result(
                fn_name,
                tool_call_id,
                is_code_tool,
                tc,
                drive_logs,
                timeout_sec,
                task_id,
                reset_msg,
            )
    else:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                _execute_single_tool,
                tools,
                tc,
                drive_logs,
                task_id,
                allowed_tool_names,
                phase_label,
            )
            try:
                return future.result(timeout=timeout_sec)
            except TimeoutError:
                return _make_timeout_result(
                    fn_name,
                    tool_call_id,
                    is_code_tool,
                    tc,
                    drive_logs,
                    timeout_sec,
                    task_id,
                    reset_msg="",
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def _handle_tool_calls(
    tool_calls: list[dict[str, Any]],
    tools: ToolRegistry,
    drive_logs: pathlib.Path,
    task_id: str,
    stateful_executor: _StatefulToolExecutor,
    messages: list[dict[str, Any]],
    llm_trace: dict[str, Any],
    emit_progress: Callable[[str], None],
    allowed_tool_names: frozenset | None = None,
    phase_label: str = "",
) -> int:
    """
    Execute tool calls and append results to messages.

    Returns: Number of errors encountered

    ``allowed_tool_names`` enforces the active phase's tool schema. Any
    call whose function name is missing from that set is short-circuited
    with a ``TOOL_FORBIDDEN_IN_PHASE`` error message instead of being
    executed. ``None`` disables the check (full registry exposed).
    """
    _tool_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
    log.info(
        "[TOOLS] Executing %d tool(s): %s", len(tool_calls), ", ".join(_tool_names)
    )

    can_parallel = len(tool_calls) > 1 and all(
        tc.get("function", {}).get("name") in READ_ONLY_PARALLEL_TOOLS
        for tc in tool_calls
    )

    if not can_parallel:
        results = [
            _execute_with_timeout(
                tools,
                tc,
                drive_logs,
                tools.get_timeout(tc["function"]["name"]),
                task_id,
                stateful_executor,
                allowed_tool_names=allowed_tool_names,
                phase_label=phase_label,
            )
            for tc in tool_calls
        ]
    else:
        max_workers = min(len(tool_calls), 8)
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_to_index = {
                executor.submit(
                    _execute_with_timeout,
                    tools,
                    tc,
                    drive_logs,
                    tools.get_timeout(tc["function"]["name"]),
                    task_id,
                    stateful_executor,
                    allowed_tool_names,
                    phase_label,
                ): idx
                for idx, tc in enumerate(tool_calls)
            }
            results = [None] * len(tool_calls)
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                results[idx] = future.result()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    return _process_tool_results(results, messages, llm_trace, emit_progress)


def _handle_text_response(
    content: str | None,
    llm_trace: dict[str, Any],
    accumulated_usage: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """
    Handle LLM response without tool calls (final response).

    Returns: (final_text, accumulated_usage, llm_trace)
    """
    if content and content.strip():
        llm_trace["assistant_notes"].append(content.strip()[:320])
    return (content or ""), accumulated_usage, llm_trace


def _check_budget_limits(
    budget_remaining_usd: float | None,
    accumulated_usage: dict[str, Any],
    round_idx: int,
    messages: list[dict[str, Any]],
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    active_max_tokens: int,
    active_temperature: float | None,
    active_tool_choice: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: queue.Queue | None,
    llm_trace: dict[str, Any],
    task_type: str = "task",
) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """
    Check budget limits and handle budget overrun.

    Returns:
        None if budget is OK (continue loop)
        (final_text, accumulated_usage, llm_trace) if budget exceeded (stop loop)
    """
    if budget_remaining_usd is None:
        return None

    task_cost = accumulated_usage.get("cost", 0)
    budget_pct = task_cost / budget_remaining_usd if budget_remaining_usd > 0 else 1.0

    if budget_pct > 0.5:
        # Hard stop — protect the budget
        finish_reason = f"Task spent ${task_cost:.3f} (>50% of remaining ${budget_remaining_usd:.2f}). Budget exhausted."
        messages.append(
            {
                "role": "system",
                "content": f"[BUDGET LIMIT] {finish_reason} Give your final response now.",
            }
        )
        try:
            final_msg, final_cost = _call_llm_with_retry(
                llm,
                messages,
                active_model,
                None,
                active_effort,
                active_max_tokens,
                active_temperature,
                active_tool_choice,
                max_retries,
                drive_logs,
                task_id,
                round_idx,
                event_queue,
                accumulated_usage,
                task_type,
            )
            if final_msg:
                return (
                    (final_msg.get("content") or finish_reason),
                    accumulated_usage,
                    llm_trace,
                )
            return finish_reason, accumulated_usage, llm_trace
        except Exception:
            log.warning(
                "Failed to get final response after budget limit", exc_info=True
            )
            return finish_reason, accumulated_usage, llm_trace
    elif budget_pct > 0.3 and round_idx % 10 == 0:
        messages.append(
            {
                "role": "system",
                "content": f"[INFO] Task spent ${task_cost:.3f} of ${budget_remaining_usd:.2f}. Wrap up if possible.",
            }
        )

    return None


# Re-export the small helpers from memory_hooks so existing tests keep
# importing them from ouroboros.loop. They're trivial pure functions but
# centralised in memory_hooks to avoid duplication.
from ouroboros.memory_hooks import (
    _extract_task_brief,
)


def _summarize_recent_actions(llm_trace: dict[str, Any]) -> str:
    """Short summary of recent assistant activity used as the periodic-
    recall search query. We prefer the last few assistant notes (which
    reflect the agent's current plan) over raw tool names.
    """
    notes = llm_trace.get("assistant_notes") or []
    if not notes:
        return ""
    return " | ".join(str(n) for n in notes[-3:])[:1000]


def _tool_schemas_for_names(
    tool_schemas: list[dict[str, Any]],
    names: set[str],
) -> list[dict[str, Any]]:
    selected = [
        schema
        for schema in tool_schemas
        if ((schema.get("function") or {}).get("name") in names)
    ]
    if not selected:
        log.warning("Requested tool schema(s) missing: %s", ",".join(sorted(names)))
    return selected or tool_schemas


_PLANNER_DISCOVERY_TOOL_NAMES = {
    "propose_discovery_plan",
    "propose_task_plan",
    "list_workspace_files",
    "read_workspace_file",
    "web_fetch",
    "deep_search",
    "github_project_search",
    "github_extract_snippets",
    "mcp_discover",
    "mcp_install",
    "search_gmas_knowledge",
    "get_gmas_context",
    "get_umbrella_memory",
    "get_workspace_metrics",
    "get_workspace_logs",
    "load_skill",
    "knowledge_read",
}

_SUBTASK_TOOL_NAMES = {
    "list_workspace_files",
    "read_workspace_file",
    "run_workspace_command",
    "run_python_code",
    "update_workspace_seed",
    "apply_workspace_patch",
    "update_workspace_from_instance",
    "delete_workspace_file",
    "commit_workspace_changes",
    "run_workspace_verify",
    "load_skill",
    "search_gmas_knowledge",
    "get_gmas_context",
    "get_umbrella_memory",
    "save_umbrella_memory",
    "save_umbrella_lesson",
    "record_workspace_event",
    "record_idea",
    "deep_search",
    "github_project_search",
    "github_extract_snippets",
    "mcp_discover",
    "mcp_install",
    "probe_input_file",
    "propose_discovery_plan",
    "mark_subtask_complete",
}

_REMEDIATION_TOOL_NAMES = _SUBTASK_TOOL_NAMES | {
    "get_current_plan",
    "mark_remediation_complete",
    "revise_remaining_plan",
    "get_umbrella_memory",
    "get_workspace_logs",
    "get_workspace_metrics",
    "search_gmas_knowledge",
    "get_gmas_context",
    "deep_search",
}

_REVIEW_TOOL_NAMES = {
    "revise_remaining_plan",
}


_PERIODIC_RECALL_DEFAULT_PHASES: frozenset[str] = frozenset({"planner", "remediation"})


def _periodic_recall_enabled_for_phase(phase_label: str) -> bool:
    """Decide whether periodic memory recall fires inside this phase.

    Tier 1.4 policy:
    - ``OUROBOROS_ENABLE_PERIODIC_RECALL=1/0`` is an absolute override
      for every phase. Otherwise periodic recall is off; the agent should
      call ``get_umbrella_memory`` explicitly when the active prompt/gate
      says prior workspace memory matters.
    """

    raw = str(os.environ.get("OUROBOROS_ENABLE_PERIODIC_RECALL", "")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return False


def _consume_ctx_overrides(
    ctx: Any,
    active_model: str,
    active_effort: str,
    active_max_tokens: int,
    active_temperature: float | None,
    active_tool_choice: str,
) -> tuple[str, str, int, float | None, str]:
    """Apply LLM-control overrides set by tools (switch_model etc.) and
    clear them. Extracted from run_llm_loop to keep that function under
    the 200-line cap enforced by the smoke tests.
    """
    if ctx.active_model_override:
        active_model = ctx.active_model_override
        ctx.active_model_override = None
    if ctx.active_effort_override:
        active_effort = normalize_reasoning_effort(
            ctx.active_effort_override, default=active_effort
        )
        ctx.active_effort_override = None
    if ctx.active_max_tokens_override:
        active_max_tokens = int(ctx.active_max_tokens_override)
        ctx.active_max_tokens_override = None
    if ctx.active_temperature_override is not None:
        active_temperature = float(ctx.active_temperature_override)
        ctx.active_temperature_override = None
    if ctx.active_tool_choice_override:
        active_tool_choice = str(ctx.active_tool_choice_override)
        ctx.active_tool_choice_override = None
    return (
        active_model,
        active_effort,
        active_max_tokens,
        active_temperature,
        active_tool_choice,
    )


def _auto_set_pending_compaction_for_overflow(
    messages: list[dict[str, Any]],
    ctx: Any,
) -> None:
    """If message history is huge, request tool-history compaction for this round."""
    try:
        from umbrella.llm_budget import get_model_context_tokens

        def _msg_tokens(m: dict[str, Any]) -> int:
            c = m.get("content", "")
            if isinstance(c, str):
                return estimate_tokens(c)
            if isinstance(c, list):
                return sum(
                    estimate_tokens(str(b.get("text", "")))
                    for b in c
                    if isinstance(b, dict)
                )
            return 0

        ctx_tok = sum(_msg_tokens(m) for m in messages)
        limit = int(get_model_context_tokens())
        if getattr(ctx, "_pending_compaction", None) is not None:
            return
        if limit <= 0:
            return
        if ctx_tok > limit * 0.80:
            ctx._pending_compaction = 3
            log.info(
                "[LOOP] auto-compact (aggressive keep_last=3): ~%s tokens > 0.80 * %s",
                ctx_tok,
                limit,
            )
        elif ctx_tok > limit * 0.65:
            ctx._pending_compaction = 5
            log.info(
                "[LOOP] auto-compact (keep_last=5): ~%s tokens > 0.65 * %s",
                ctx_tok,
                limit,
            )
    except Exception:
        log.debug("[LOOP] auto-compact token estimate skipped", exc_info=True)


def _maybe_compact_history(
    messages: list[dict[str, Any]],
    ctx: Any,
    round_idx: int,
) -> list[dict[str, Any]]:
    """Decide whether to compact the message history this round.

    Honors any explicit ``_pending_compaction`` request set by the
    ``compact_context`` tool first; otherwise applies the rolling
    heuristic (every round past 8, or earlier if messages get long).
    """
    pending = getattr(ctx, "_pending_compaction", None)
    if pending is not None:
        ctx._pending_compaction = None
        return compact_tool_history_llm(messages, keep_recent=pending)
    if round_idx > 4:
        return compact_tool_history(messages, keep_recent=5)
    if round_idx > 2 and len(messages) > 35:
        return compact_tool_history(messages, keep_recent=5)
    return messages


def _resolve_max_rounds() -> tuple[int, str]:
    """Read ``OUROBOROS_MAX_ROUNDS`` and apply unlimited/invalid semantics.

    Returns ``(max_rounds, label)`` where ``max_rounds == 0`` means unlimited
    (no cap) and ``label`` is the human-readable string used in log lines
    (``"∞"`` for unlimited, otherwise ``str(max_rounds)``). Umbrella sets the
    env var to ``0`` when the user passes ``--max-rounds 0`` (or any
    non-positive integer) so that "no limits" mode at the CLI translates
    cleanly into "no round cap" inside this loop.
    """
    try:
        raw = int(os.environ.get("OUROBOROS_MAX_ROUNDS", "200"))
    except (ValueError, TypeError):
        raw = 200
        log.warning("Invalid OUROBOROS_MAX_ROUNDS, defaulting to 200")
    max_rounds = raw if raw > 0 else 0
    if max_rounds == 0:
        log.info("[LOOP] MAX_ROUNDS unlimited (OUROBOROS_MAX_ROUNDS<=0)")
    return max_rounds, "∞" if max_rounds == 0 else str(max_rounds)


def _maybe_inject_self_check(
    round_idx: int,
    max_rounds: int,
    messages: list[dict[str, Any]],
    accumulated_usage: dict[str, Any],
    emit_progress: Callable[[str], None],
) -> None:
    """Inject a soft self-check reminder every REMINDER_INTERVAL rounds.

    This is a cognitive feature (Bible P0: subjectivity) — the agent reflects
    on its own resource usage and strategy, not a hard kill.
    """
    REMINDER_INTERVAL = 50
    if round_idx <= 1 or round_idx % REMINDER_INTERVAL != 0:
        return
    ctx_tokens = sum(
        estimate_tokens(str(m.get("content", "")))
        if isinstance(m.get("content"), str)
        else sum(
            estimate_tokens(str(b.get("text", "")))
            for b in m.get("content", [])
            if isinstance(b, dict)
        )
        for m in messages
    )
    task_cost = accumulated_usage.get("cost", 0)
    checkpoint_num = round_idx // REMINDER_INTERVAL

    # max_rounds == 0 means unlimited; render that without a misleading
    # "Rounds remaining: -<round_idx>" calculation.
    max_label = "∞" if max_rounds <= 0 else str(max_rounds)
    remaining_label = "∞" if max_rounds <= 0 else str(max_rounds - round_idx)
    reminder = (
        f"[CHECKPOINT {checkpoint_num} — round {round_idx}/{max_label}]\n"
        f"📊 Context: ~{ctx_tokens} tokens | Cost so far: ${task_cost:.2f} | "
        f"Rounds remaining: {remaining_label}\n\n"
        f"⏸️ PAUSE AND REFLECT before continuing:\n"
        f"1. Am I making real progress, or repeating the same actions?\n"
        f"2. Is my current strategy working? Should I try something different?\n"
        f"3. Is my context bloated with old tool results I no longer need?\n"
        f"   → If yes, call `compact_context` to summarize them selectively.\n"
        f"4. Have I been stuck on the same sub-problem for many rounds?\n"
        f"   → If yes, consider: simplify the approach, skip the sub-problem, or finish with what I have.\n"
        f"   If you finish with a partial result, first record the concrete blocker and evidence.\n"
        f"5. What exact deliverable, test, report, memory note, or commit remains before the completion contract is true?\n"
        f"Only stop when the task contract is complete or a real blocker is documented.\n\n"
        f"This is not a hard limit — you decide. But be honest with yourself."
    )
    messages.append({"role": "system", "content": reminder})
    emit_progress(
        f"🔄 Checkpoint {checkpoint_num} at round {round_idx}: ~{ctx_tokens} tokens, ${task_cost:.2f} spent"
    )


def _is_llm_auth_error(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    return any(
        marker in lowered for marker in ("authenticationerror", "unauthorized", "401")
    )


def _format_llm_unavailable_message(
    model: str, max_retries: int, last_error: str
) -> str:
    if _is_llm_auth_error(last_error):
        return (
            f"LLM authentication failed for model {model} after {max_retries} attempts. "
            "The run used live LLM mode and did not fall back to mocks. "
            "Check the env credentials/base URL for the configured provider."
        )
    return (
        f"Failed to get a response from model {model} after {max_retries} attempts. "
        "No mock LLM fallback was used."
    )


def _setup_dynamic_tools(tools_registry, tool_schemas, messages):
    """
    Wire tool-discovery handlers onto an existing tool_schemas list.

    Creates closures for list_available_tools / enable_tools, registers them
    as handler overrides, and injects a system message advertising non-core
    tools.  Mutates tool_schemas in-place (via list.append) when tools are
    enabled, so the caller's reference stays live.

    Returns (tool_schemas, enabled_extra_set).
    """
    enabled_extra: set = set()

    def _handle_list_tools(ctx=None, **kwargs):
        non_core = tools_registry.list_non_core_tools()
        if not non_core:
            return "All tools are already in your active set."
        lines = [
            f"**{len(non_core)} additional tools available** (use `enable_tools` to activate):\n"
        ]
        for t in non_core:
            lines.append(f"- **{t['name']}**: {t['description'][:120]}")
        return "\n".join(lines)

    def _handle_enable_tools(ctx=None, tools: str = "", **kwargs):
        names = [n.strip() for n in tools.split(",") if n.strip()]
        enabled, not_found = [], []
        for name in names:
            schema = tools_registry.get_schema_by_name(name)
            if schema and name not in enabled_extra:
                tool_schemas.append(schema)
                enabled_extra.add(name)
                enabled.append(name)
            elif name in enabled_extra:
                enabled.append(f"{name} (already active)")
            else:
                not_found.append(name)
        parts = []
        if enabled:
            parts.append(f"✅ Enabled: {', '.join(enabled)}")
        if not_found:
            parts.append(f"❌ Not found: {', '.join(not_found)}")
        return "\n".join(parts) if parts else "No tools specified."

    tools_registry.override_handler("list_available_tools", _handle_list_tools)
    tools_registry.override_handler("enable_tools", _handle_enable_tools)

    non_core_count = len(tools_registry.list_non_core_tools())
    if non_core_count > 0:
        messages.append(
            {
                "role": "system",
                "content": (
                    f"Note: {len(tool_schemas)} tools are pre-loaded in your active set. "
                    f"Another {non_core_count} specialised tools exist but are not "
                    f"loaded by default — call `list_available_tools` to inspect them "
                    f"and `enable_tools` to activate any you need (their schemas will "
                    f"be added to subsequent rounds). Reach for them whenever the "
                    f"core set is missing a capability for the current subtask."
                ),
            }
        )

    return tool_schemas, enabled_extra


def _drain_incoming_messages(
    messages: list[dict[str, Any]],
    incoming_messages: queue.Queue,
    drive_root: pathlib.Path | None,
    task_id: str,
    event_queue: queue.Queue | None,
    _owner_msg_seen: set,
) -> None:
    """
    Inject owner messages received during task execution.
    Drains both the in-process queue and the Drive mailbox.
    """
    # Inject owner messages received during task execution
    while not incoming_messages.empty():
        try:
            injected = incoming_messages.get_nowait()
            messages.append({"role": "user", "content": injected})
        except queue.Empty:
            break

    # Drain per-task owner messages from Drive mailbox (written by forward_to_worker tool)
    if drive_root is not None and task_id:
        from ouroboros.owner_inject import drain_owner_messages

        drive_msgs = drain_owner_messages(
            drive_root, task_id=task_id, seen_ids=_owner_msg_seen
        )
        for dmsg in drive_msgs:
            messages.append(
                {
                    "role": "user",
                    "content": f"[Owner message during task]: {dmsg}",
                }
            )
            if event_queue is not None:
                try:
                    event_queue.put_nowait(
                        {
                            "type": "owner_message_injected",
                            "task_id": task_id,
                            "text": dmsg[:200],
                        }
                    )
                except Exception:
                    pass


@dataclass
class _LoopState:
    """Mutable per-task loop state shared across phases."""

    active_model: str = ""
    active_effort: str = "medium"
    active_max_tokens: int = 16384
    active_temperature: float | None = None
    active_tool_choice: str = "auto"
    accumulated_usage: dict[str, Any] = field(default_factory=dict)
    llm_trace: dict[str, Any] = field(
        default_factory=lambda: {"assistant_notes": [], "tool_calls": []}
    )
    round_idx: int = 0
    last_periodic_recall_round: int = 0
    active_workspace_id: str = ""
    last_text: str = ""
    # Verification evidence trail (Tier 1.3 / 2.4):
    # ``last_verify_run_id`` is the id assigned to the most recent
    # ``run_workspace_verify`` call (via ``record_verify_outcome``).
    # ``last_verify_round`` is the loop round_idx at which that verify ran.
    # ``last_verify_passed`` / ``last_verify_failed_count`` summarise the
    # outcome so completion gates can refuse stale or red verifies without
    # re-parsing the full report.
    # ``last_write_round`` is the round_idx of the most recent workspace
    # write (any tool in WRITE_TOOL_NAMES). Comparing these tells us
    # whether the verify evidence is fresh w.r.t. current code state.
    # ``last_verify_summary`` is the rendered markdown summary suitable
    # for prompt injection (used in final_aggregation).
    last_verify_run_id: str = ""
    last_verify_round: int = -1
    last_verify_passed: bool = False
    last_verify_failed_count: int = 0
    last_verify_summary: str = ""
    last_write_round: int = -1
    # Discovery tracking (Tier 3.1): how many discovery / recall tool calls
    # the agent made in the *current* subtask. Reset when a new subtask
    # begins. Used by ``mark_subtask_complete`` to block premature
    # completion of ``domain_unknown``-tagged subtasks.
    current_subtask_id: str = ""
    current_subtask_discovery_calls: int = 0
    # Separate counter for *external* discovery (web/github/mcp/fetch) so
    # we can require at least one external lookup when memory recall came
    # back empty. Without this, the agent slides through the gate by
    # calling ``get_umbrella_memory`` once and never consults real sources.
    current_subtask_external_discovery_calls: int = 0
    # ``get_umbrella_memory`` returned zero hits at least once — strong
    # signal that internal memory has nothing on this topic and the
    # agent SHOULD consult external sources before claiming domain
    # knowledge.
    last_memory_recall_empty: bool = False
    # Planner discovery tracking (Tier 3.2): did the planner call any
    # read/discovery tool before ``propose_task_plan``? If not, we nudge.
    planner_discovery_calls: int = 0
    planner_external_discovery_calls: int = 0
    discovery_plan_proposed: bool = False
    discovery_calls_by_tool: dict[str, int] = field(default_factory=dict)


def _try_fallback_llm(
    *,
    state: "_LoopState",
    messages: list[dict[str, Any]],
    llm: LLMClient,
    tool_schemas: list[dict[str, Any]],
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: queue.Queue | None,
    task_type: str,
    emit_progress: Callable[[str], None],
) -> tuple[dict[str, Any] | None, tuple[str, dict[str, Any], dict[str, Any]]]:
    """Pick a fallback model after an empty response and retry once.

    Returns ``(msg_or_None, final_payload)`` — ``msg_or_None`` is the
    fallback response (or ``None`` if no fallback worked / auth failed),
    and ``final_payload`` is the final ``(text, usage, trace)`` to surface
    to the caller when ``msg_or_None`` is ``None``.
    """
    last_llm_error = str(state.accumulated_usage.get("_last_llm_error") or "")
    if _is_llm_auth_error(last_llm_error):
        final = (
            _format_llm_unavailable_message(
                state.active_model, max_retries, last_llm_error
            ),
            state.accumulated_usage,
            state.llm_trace,
        )
        return None, final

    fallback_list_raw = os.environ.get(
        "OUROBOROS_MODEL_FALLBACK_LIST",
        "google/gemini-2.5-pro-preview,openai/o3,anthropic/claude-sonnet-4.6",
    )
    candidates = [m.strip() for m in fallback_list_raw.split(",") if m.strip()]
    fallback_model = next((c for c in candidates if c != state.active_model), None)
    if fallback_model is None:
        final = (
            f"⚠️ Failed to get a response from model {state.active_model} after {max_retries} attempts. "
            f"All fallback models match the active one. Try rephrasing your request.",
            state.accumulated_usage,
            state.llm_trace,
        )
        return None, final

    emit_progress(
        f"⚡ Fallback: {state.active_model} → {fallback_model} after empty response"
    )
    msg, _cost = _call_llm_with_retry(
        llm,
        messages,
        fallback_model,
        tool_schemas,
        state.active_effort,
        state.active_max_tokens,
        state.active_temperature,
        state.active_tool_choice,
        max_retries,
        drive_logs,
        task_id,
        state.round_idx,
        event_queue,
        state.accumulated_usage,
        task_type,
    )
    if msg is None:
        final = (
            f"⚠️ Failed to get a response from the model after {max_retries} attempts. "
            f"Fallback model ({fallback_model}) also returned no response.",
            state.accumulated_usage,
            state.llm_trace,
        )
        return None, final
    return msg, ("", state.accumulated_usage, state.llm_trace)


_NO_WRITE_NUDGE_LIMIT = 2
_NO_WRITE_TOOL_NUDGE_AFTER_ROUNDS = 8
_NO_WRITE_TOOL_NUDGE_INTERVAL = 4
_NO_WRITE_TOOL_NUDGE_LIMIT = 2
_NO_WRITE_TOOL_ABORT_AFTER_NUDGES = 2
# Tier 1.2: how many times we re-prompt in a text-only phase before
# giving up when the assistant insists on emitting pseudo tool-call text.
# Two is enough — by then either the model fixes itself or no amount of
# nudging will help, and we'd rather exit with the last text than spin.
_PSEUDO_TEXT_NUDGE_LIMIT = 2


def _maybe_handle_text_phase_pseudo_tool_call(
    *,
    pseudo_tool_text: bool,
    tool_schemas: list[dict[str, Any]],
    nudges_so_far: int,
    phase_label: str,
    content: Any,
    messages: list[dict[str, Any]],
) -> tuple[bool, int]:
    """Tier 1.2: detect agents writing tool-call XML or ``tool_name(...)`` as
    plain text in a text-only phase (no tool schemas exposed).

    Without this guard the phase silently terminates as if a real
    verification had been done. We give the model up to
    ``_PSEUDO_TEXT_NUDGE_LIMIT`` retries to re-emit clean prose; if it
    can't, the caller still exits (with whatever ``state.last_text`` is)
    but the log makes the failure visible.

    Returns ``(handled, new_nudges_so_far)``. ``handled=True`` means the
    caller should ``continue`` the phase loop instead of exiting.
    """

    if not pseudo_tool_text or tool_schemas:
        return False, nudges_so_far
    if nudges_so_far >= _PSEUDO_TEXT_NUDGE_LIMIT:
        log.warning(
            "[LOOP] Phase '%s' kept emitting pseudo tool-call text after "
            "%d nudges; surrendering and exiting with last text",
            phase_label,
            nudges_so_far,
        )
        return False, nudges_so_far
    new_count = nudges_so_far + 1
    log.warning(
        "[LOOP] Phase '%s' emitted pseudo tool-call text in a text-only "
        "phase (nudge %d/%d)",
        phase_label,
        new_count,
        _PSEUDO_TEXT_NUDGE_LIMIT,
    )
    messages.append({"role": "assistant", "content": content or ""})
    messages.append(
        {
            "role": "system",
            "content": (
                "[NO_TOOLS_IN_PHASE]\n"
                "You wrote tool-call XML or `tool_name(...)` in your reply. "
                "Tools are disabled in this phase — that text did NOT execute "
                "anything. Re-emit your final answer as plain prose, citing "
                "the verification report you were given above. Do not pretend "
                "to call tools."
            ),
        }
    )
    return True, new_count


def _count_workspace_write_tool_calls(tool_calls: list[dict[str, Any]]) -> int:
    return sum(
        1
        for tc in tool_calls
        if str(tc.get("function", {}).get("name") or "") in WRITE_TOOL_NAMES
    )


# Tier 3.1: tools that count as "discovery" — the agent went out and
# fetched evidence (memory, web, github, mcp) instead of writing files
# or running ad-hoc commands. ``read_workspace_file`` / ``list_workspace_files``
# are intentionally *not* here because they're local navigation, not
# external knowledge; we want to push the agent to consult real sources
# for unknown domains.
DISCOVERY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "get_umbrella_memory",
        "deep_search",
        "github_project_search",
        "github_extract_snippets",
        "mcp_discover",
        "mcp_install",
        "web_fetch",
    }
)

# Subset of ``DISCOVERY_TOOL_NAMES`` that constitutes *external* discovery
# (i.e. the agent reached past memory recall to web/github/mcp/raw fetch).
# We track these separately so the discovery gate can require an external
# source when internal memory turned up empty — otherwise the agent
# habitually satisfies the gate with a single ``get_umbrella_memory`` call
# that returned zero hits and never consults real-world references.
EXTERNAL_DISCOVERY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "deep_search",
        "github_project_search",
        "github_extract_snippets",
        "mcp_discover",
        "mcp_install",
        "web_fetch",
    }
)


def _resolve_workspace_root_for_focus(
    tools: ToolRegistry, state: "_LoopState"
) -> pathlib.Path | None:
    """Best-effort resolver for the active workspace root.

    Used by ``focus_block`` to render a [WORKSPACE_INVENTORY] section.
    Returns ``None`` when the path can't be derived; the inventory then
    falls back to "no listing", which is safer than guessing.
    """

    ws_id = getattr(state, "active_workspace_id", "") or ""
    ctx = getattr(tools, "_ctx", None)
    if ctx is not None:
        explicit = getattr(ctx, "active_workspace_path", None)
        if isinstance(explicit, pathlib.Path):
            return explicit
        if isinstance(explicit, str) and explicit.strip():
            return pathlib.Path(explicit)
        repo_root_attr = getattr(ctx, "repo_dir", None) or getattr(
            ctx, "repo_root", None
        )
        if isinstance(repo_root_attr, pathlib.Path) and ws_id:
            candidate = repo_root_attr / "workspaces" / ws_id
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                return None
    return None


def _resolve_noise_paths_for_focus(
    tools: ToolRegistry, state: "_LoopState"
) -> list[str] | None:
    """Best-effort resolver for noise paths previously flagged by sweep.

    The sweep persists its findings on ``ctx`` after each remediation
    pass. When present, we forward only the *block-level* hits to the
    focus block — warn-level noise is auto-cleaned and doesn't need to
    interrupt the agent's attention.
    """

    del state  # unused for now; kept for symmetric signature
    ctx = getattr(tools, "_ctx", None)
    if ctx is None:
        return None
    payload = getattr(ctx, "last_sweep_payload", None)
    if not isinstance(payload, dict):
        return None
    blocking = payload.get("blocking_noise")
    if not isinstance(blocking, list):
        return None
    paths: list[str] = []
    for hit in blocking:
        if isinstance(hit, dict):
            p = hit.get("path")
            if isinstance(p, str) and p.strip():
                paths.append(p.strip())
    return paths or None


def _publish_subtask_tags_to_tool_ctx(tools: ToolRegistry, tags: list[str]) -> None:
    """Pin the current subtask's tags onto ``ctx.loop_state_view`` so that
    the completion gate can detect ``domain_unknown`` and similar
    metadata-driven invariants without re-loading the plan.
    """

    try:
        ctx = getattr(tools, "_ctx", None)
        if ctx is None:
            return
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            view = {}
            try:
                ctx.loop_state_view = view  # type: ignore[attr-defined]
            except Exception:
                return
        view["current_subtask_tags"] = list(tags or [])
    except Exception:
        log.debug("Failed to publish subtask tags to tool ctx", exc_info=True)


def _publish_state_view_to_tool_ctx(
    state: "_LoopState",
    tools: ToolRegistry,
    *,
    phase_label: str = "",
) -> None:
    """Refresh ``ctx.loop_state_view`` on the active tools' context so the
    completion gates inside ``_mark_subtask_complete`` /
    ``_mark_remediation_complete`` can see fresh verify/discovery data
    without us needing to thread ``_LoopState`` through every tool call.
    """

    try:
        ctx = getattr(tools, "_ctx", None)
        if ctx is None:
            return
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            view = {}
            try:
                ctx.loop_state_view = view  # type: ignore[attr-defined]
            except Exception:
                return
        view["round_idx"] = state.round_idx
        view["phase_label"] = phase_label
        view["last_verify_run_id"] = state.last_verify_run_id
        view["last_verify_round"] = state.last_verify_round
        view["last_verify_passed"] = state.last_verify_passed
        view["last_verify_failed_count"] = state.last_verify_failed_count
        view["last_write_round"] = state.last_write_round
        view["current_subtask_id"] = state.current_subtask_id
        view["current_subtask_discovery_calls"] = state.current_subtask_discovery_calls
        view["current_subtask_external_discovery_calls"] = (
            state.current_subtask_external_discovery_calls
        )
        view["planner_discovery_calls"] = state.planner_discovery_calls
        view["planner_external_discovery_calls"] = (
            state.planner_external_discovery_calls
        )
        view["last_memory_recall_empty"] = state.last_memory_recall_empty
        view["discovery_plan_proposed"] = state.discovery_plan_proposed
        view["discovery_calls_by_tool"] = dict(state.discovery_calls_by_tool)
    except Exception:
        log.debug("Failed to publish loop state view to tool ctx", exc_info=True)


def _publish_plan_execution_context(
    tools: ToolRegistry,
    *,
    active_plan_id: str,
    plan_store_root: pathlib.Path | str,
    task_id: str,
    phase: str,
    subtask_id: str = "",
) -> None:
    try:
        from ouroboros.task_planner import PlanExecutionContext

        ctx = getattr(tools, "_ctx", None)
        if ctx is None:
            return
        ctx.plan_execution_context = PlanExecutionContext(
            active_plan_id=str(active_plan_id or task_id),
            plan_store_root=str(plan_store_root or getattr(ctx, "drive_root", "")),
            task_id=str(task_id or ""),
            phase=str(phase or ""),
            subtask_id=str(subtask_id or ""),
        )
        ctx.active_plan_id = str(active_plan_id or task_id)
    except Exception:
        log.debug("Failed to publish plan execution context", exc_info=True)


def _reset_subtask_tool_state(tools: ToolRegistry) -> None:
    try:
        ctx = getattr(tools, "_ctx", None)
        view = getattr(ctx, "loop_state_view", None) if ctx is not None else None
        if isinstance(view, dict):
            view["subtask_diff"] = {}
            view["subtask_discovery_calls_by_tool"] = {}
    except Exception:
        log.debug("Failed to reset subtask tool state", exc_info=True)


def _update_state_from_tool_calls(
    state: "_LoopState", tool_calls: list[dict[str, Any]]
) -> None:
    """Update verify / write / discovery counters in ``state`` from a batch
    of tool calls executed in the current round.

    This runs *after* ``_handle_tool_calls`` so ``state.llm_trace.tool_calls``
    already contains the structured tool results for this batch. We use
    those results (not just the request) to parse ``run_workspace_verify``
    payloads — the agent may have asked, but we only credit it when the
    verifier actually returned a structured outcome.
    """

    if not tool_calls:
        return

    write_seen = False
    discovery_seen = 0
    external_discovery_seen = 0
    memory_recall_in_batch = False
    for tc in tool_calls:
        fn_name = str(tc.get("function", {}).get("name") or "")
        if not fn_name:
            continue
        if fn_name in WRITE_TOOL_NAMES:
            write_seen = True
        if fn_name in DISCOVERY_TOOL_NAMES:
            discovery_seen += 1
        if fn_name in EXTERNAL_DISCOVERY_TOOL_NAMES:
            external_discovery_seen += 1
            state.discovery_calls_by_tool[fn_name] = (
                state.discovery_calls_by_tool.get(fn_name, 0) + 1
            )
        if fn_name == "get_umbrella_memory":
            memory_recall_in_batch = True
        if fn_name == "propose_discovery_plan":
            state.discovery_plan_proposed = True

    if write_seen:
        state.last_write_round = state.round_idx
    if discovery_seen:
        state.current_subtask_discovery_calls += discovery_seen
        state.planner_discovery_calls += discovery_seen
    if external_discovery_seen:
        state.current_subtask_external_discovery_calls += external_discovery_seen
        state.planner_external_discovery_calls += external_discovery_seen

    # If the agent called ``get_umbrella_memory`` in this batch, peek at
    # the structured response to detect "empty recall". A zero-hit
    # recall is the strongest signal that the agent has no prior
    # knowledge and SHOULD consult external sources before claiming
    # domain expertise.
    if memory_recall_in_batch:
        trace_tool_calls_now = state.llm_trace.get("tool_calls") or []
        for item in trace_tool_calls_now[-len(tool_calls) :]:
            if str(item.get("tool") or "") != "get_umbrella_memory":
                continue
            result_text = _trace_result_text(item)
            if not result_text:
                continue
            try:
                payload = json.loads(result_text)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            # Response keys are ``palace_memory`` / ``lesson_memory`` /
            # ``hierarchical_ideas`` (see ``get_umbrella_memory``'s return).
            # Earlier revisions used ``palace_hits`` / ``lesson_hits`` here,
            # which never existed on the payload — so the empty-recall
            # flag silently never updated based on this leg.
            palace_mem = payload.get("palace_memory") or []
            lesson_mem = payload.get("lesson_memory") or []
            hierarchical = payload.get("hierarchical_ideas") or []
            total = (
                (len(palace_mem) if isinstance(palace_mem, list) else 0)
                + (len(lesson_mem) if isinstance(lesson_mem, list) else 0)
                + (len(hierarchical) if isinstance(hierarchical, list) else 0)
            )
            state.last_memory_recall_empty = total == 0
            break

    # Parse ``run_workspace_verify`` outcomes from the trace tail. We look
    # at the last ``len(tool_calls)`` entries; the executor appends in
    # call order, so this slice covers exactly this round.
    trace_tool_calls = state.llm_trace.get("tool_calls") or []
    if not trace_tool_calls:
        return
    tail = trace_tool_calls[-len(tool_calls) :]
    for item in tail:
        if str(item.get("tool") or "") != "run_workspace_verify":
            continue
        result_text = _trace_result_text(item)
        if (
            not result_text
            or result_text.startswith("⚠️")
            or result_text.startswith("WARNING")
        ):
            continue
        try:
            payload = json.loads(result_text)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("skipped"):
            # Verification was declared but produced no steps. We still
            # surface this so the completion gate can warn the agent, but
            # don't credit it as a passing verify.
            state.last_verify_run_id = ""
            state.last_verify_round = state.round_idx
            state.last_verify_passed = False
            state.last_verify_failed_count = 0
            state.last_verify_summary = str(
                payload.get("reason") or "verification skipped"
            )
            continue
        state.last_verify_round = state.round_idx
        state.last_verify_passed = bool(payload.get("passed"))
        explicit_failed = payload.get("failed_step_count")
        if isinstance(explicit_failed, int) and explicit_failed >= 0:
            failed = int(explicit_failed)
        else:
            results = payload.get("results")
            failed = 0
            if isinstance(results, list):
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    status = str(r.get("status") or "").lower()
                    optional = bool(r.get("optional"))
                    if optional:
                        continue
                    if status in {"failed", "error"}:
                        failed += 1
        state.last_verify_failed_count = failed
        summary = payload.get("summary")
        if isinstance(summary, str):
            state.last_verify_summary = summary
        # Prefer the stable id minted by ``record_verify_outcome`` and
        # surfaced through the tool payload (Tier 2.4). Falls back to a
        # round-derived id so the freshness gate still works when the
        # palace is unavailable.
        payload_run_id = payload.get("verify_run_id")
        if isinstance(payload_run_id, str) and payload_run_id.strip():
            state.last_verify_run_id = payload_run_id.strip()
        else:
            state.last_verify_run_id = f"round-{state.round_idx}"


def _resolve_positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        log.warning("Invalid %s=%r, defaulting to %d", name, raw, default)
        return default


def _resolve_tool_round_max_tokens(base_max_tokens: int, has_tools: bool) -> int:
    """Clamp max_tokens for tool-calling rounds to reduce malformed JSON args."""
    if not has_tools:
        return base_max_tokens
    cap = _resolve_positive_int_env("OUROBOROS_TOOL_MAX_TOKENS", 1536)
    return max(256, min(base_max_tokens, cap))


def _classify_llm_error(error: Exception) -> str:
    text = repr(error).lower()
    if any(
        marker in text
        for marker in (
            "context_length",
            "context window",
            "maximum context",
            "too many tokens",
            "token limit",
        )
    ):
        return "context_limit"
    if "404" in text or "not found" in text:
        return "model_not_found"
    if any(marker in text for marker in ("504", "502", "503", "timeout", "gateway")):
        return "server_transient"
    return "unknown"


def _tool_preflight_repair_round_cap() -> int:
    return _resolve_positive_int_env("OUROBOROS_TOOL_PREFLIGHT_REPAIR_ROUNDS", 6)


def _select_forced_progress_tool(
    allowed_tool_names: set[str] | None,
) -> str | None:
    """Pick a concrete write/verify tool for no-write recovery."""
    priority = (
        "run_workspace_verify",
        "update_workspace_seed",
        "update_workspace_from_instance",
        "commit_workspace_changes",
    )
    for name in priority:
        if allowed_tool_names is None or name in allowed_tool_names:
            return name
    return None


def _reject_tool_calls_under_no_write_enforcement(
    *,
    forced_tool: str | None,
    tool_calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    llm_trace: dict[str, Any],
    emit_progress: Callable[[str], None],
    phase_label: str,
) -> bool:
    """Reject a round that ignored a forced no-write progress tool."""
    forced = str(forced_tool or "").strip()
    if not forced or not tool_calls:
        return False
    invoked = [
        str(tc.get("function", {}).get("name") or "").strip()
        for tc in tool_calls
        if str(tc.get("function", {}).get("name") or "").strip()
    ]
    if forced in invoked:
        return False

    invoked_preview = ", ".join(invoked[:6]) or "<unknown tool>"
    result = (
        "TOOL_REJECTED_UNDER_ENFORCEMENT: "
        f"phase `{phase_label}` requires `{forced}` as the next tool call, "
        f"but the model called: {invoked_preview}. Re-emit the next action "
        f"using `{forced}` only."
    )
    for tc in tool_calls:
        fn_name = str(tc.get("function", {}).get("name") or "")
        messages.append(
            {
                "role": "tool",
                "tool_call_id": str(tc.get("id") or ""),
                "content": result,
            }
        )
        llm_trace.setdefault("tool_calls", []).append(
            {
                "tool": fn_name,
                "args": _safe_args(_safe_tool_args_json(tc)),
                "result": result,
                "is_error": True,
            }
        )
    emit_progress(result)
    return True


def _maybe_inject_no_write_nudge(
    *,
    require: bool,
    phase_write_tool_calls: int,
    nudges_so_far: int,
    phase_label: str,
    workspace_id: str,
    content: str | None,
    messages: list[dict[str, Any]],
) -> bool:
    """Block ``text reply -> phase exit`` while the agent has 0 workspace writes.

    Returns ``True`` if a nudge was injected (caller should ``continue``);
    ``False`` if the phase should be allowed to exit normally.
    """
    if not require:
        return False
    if nudges_so_far >= _NO_WRITE_NUDGE_LIMIT:
        return False
    if phase_write_tool_calls > 0:
        return False
    log.warning(
        "[LOOP] Phase '%s' tried to exit via text reply with 0 workspace "
        "writes; injecting nudge #%d/%d instead.",
        phase_label,
        nudges_so_far + 1,
        _NO_WRITE_NUDGE_LIMIT,
    )
    nudge = (
        "[NO_PROGRESS_GUARD]\n"
        f"You just produced a text reply, but this task has not resulted "
        f"in a single workspace-write tool call yet (reads/commands do not "
        f"count). The task brief expects concrete deliverables in "
        f"`workspaces/{workspace_id or '<workspace>'}/`. Decide right now:\n"
        "  - call a write tool (`update_workspace_seed`, "
        "`update_workspace_from_instance`, `commit_workspace_changes`, or "
        "the workspace-aware variant relevant to your subtask), OR\n"
        "  - if the harness itself is blocking you (file format, guard, tool "
        "gap), call `sandbox_self_edit` to patch the blocker, OR\n"
        "  - if the task is truly read-only, say so EXPLICITLY in your next "
        "reply and explain why no files need to change.\n"
        "Silent surrender is not an option — this nudge will be re-issued if "
        "you try to text-exit again with no writes."
    )
    messages.append({"role": "assistant", "content": content or ""})
    messages.append({"role": "system", "content": nudge})
    return True


def _maybe_inject_no_write_tool_nudge(
    *,
    require: bool,
    phase_write_tool_calls: int,
    nudges_so_far: int,
    last_nudge_round: int,
    round_idx: int,
    rounds_in_phase: int,
    phase_label: str,
    workspace_id: str,
    tool_calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
) -> tuple[int, int]:
    """Nudge when a delivery phase keeps calling tools but never writes.

    The text-exit guard above handles "I am done" with zero deliverables.
    Live Ouroboros runs can fail in a subtler way: repeated reads, env
    probes, and diagnostics keep the loop alive forever, so the text-exit
    guard never fires. This soft guard keeps the model's next turn pointed
    at a concrete artifact-producing action without removing tool access.
    """
    if not require or not tool_calls:
        return nudges_so_far, last_nudge_round
    if phase_write_tool_calls > 0:
        return nudges_so_far, last_nudge_round

    limit = _resolve_positive_int_env(
        "OUROBOROS_NO_WRITE_TOOL_NUDGE_LIMIT",
        _NO_WRITE_TOOL_NUDGE_LIMIT,
    )
    if nudges_so_far >= limit:
        return nudges_so_far, last_nudge_round

    threshold = _resolve_positive_int_env(
        "OUROBOROS_NO_WRITE_TOOL_NUDGE_AFTER_ROUNDS",
        _NO_WRITE_TOOL_NUDGE_AFTER_ROUNDS,
    )
    if rounds_in_phase < threshold:
        return nudges_so_far, last_nudge_round

    interval = _resolve_positive_int_env(
        "OUROBOROS_NO_WRITE_TOOL_NUDGE_INTERVAL",
        _NO_WRITE_TOOL_NUDGE_INTERVAL,
    )
    if last_nudge_round and (round_idx - last_nudge_round) < interval:
        return nudges_so_far, last_nudge_round

    invoked = [
        str(tc.get("function", {}).get("name") or "").strip()
        for tc in tool_calls
        if str(tc.get("function", {}).get("name") or "").strip()
    ]
    invoked_preview = ", ".join(invoked[:6]) or "<unknown tool>"
    if len(invoked) > 6:
        invoked_preview += ", ..."

    log.warning(
        "[LOOP] Phase '%s' has %d tool rounds and 0 workspace writes; "
        "injecting no-write tool nudge #%d/%d.",
        phase_label,
        rounds_in_phase,
        nudges_so_far + 1,
        limit,
    )
    messages.append(
        {
            "role": "system",
            "content": (
                "[NO_WRITE_TOOL_GUARD]\n"
                f"You are in phase `{phase_label}` and have spent "
                f"{rounds_in_phase} rounds calling tools, but there has not "
                "been a single workspace-write tool call yet. The last tool "
                f"round invoked: {invoked_preview}.\n"
                f"Stop broad inspection of `workspaces/{workspace_id or '<workspace>'}/`. "
                "On your next turn, choose one concrete progress action:\n"
                "  - call `update_workspace_seed` (or another workspace write "
                "tool) with actual project files,\n"
                "  - call `run_workspace_command` only if it creates or "
                "modifies files as part of a deterministic generation step, "
                "then verify those files immediately,\n"
                "  - call `sandbox_self_edit` if the harness or tool layer is "
                "blocking progress, or\n"
                "  - state a precise blocker if no write is possible.\n"
                "Do not spend the next round on another general inventory or "
                "environment probe unless it is directly required by the chosen "
                "write action."
            ),
        }
    )
    return nudges_so_far + 1, round_idx


def _should_abort_no_write_tool_churn(
    *,
    require: bool,
    phase_write_tool_calls: int,
    nudges_so_far: int,
    nudge_injected_this_round: bool,
    tool_calls: list[dict[str, Any]],
) -> bool:
    if not require or not tool_calls or nudge_injected_this_round:
        return False
    if phase_write_tool_calls > 0:
        return False
    abort_after = _resolve_positive_int_env(
        "OUROBOROS_NO_WRITE_TOOL_ABORT_AFTER_NUDGES",
        _NO_WRITE_TOOL_ABORT_AFTER_NUDGES,
    )
    return nudges_so_far >= abort_after


def _format_no_write_tool_stall_message(
    *,
    phase_label: str,
    workspace_id: str,
    rounds_in_phase: int,
    nudges_so_far: int,
) -> str:
    return (
        "⚠️ Ouroboros stalled before producing deliverables: "
        f"phase `{phase_label}` spent {rounds_in_phase} tool rounds with "
        "zero workspace-write tool calls after "
        f"{nudges_so_far} no-write progress guard(s). "
        f"The workspace `workspaces/{workspace_id or '<workspace>'}/` still "
        "needs a concrete write via `update_workspace_seed`, "
        "`update_workspace_from_instance`, or a recorded blocker. "
        "This attempt is ending as incomplete so the retry loop can inject "
        "this failure instead of burning unlimited rounds."
    )


_READ_ONLY_SUBTASK_RE = re.compile(
    r"(?i)\b(diagnos|inspect|audit|analy[sz]e|investigat|probe|read|extract|discover|map|inventory)\b"
)


def _subtask_allows_read_only_progress(subtask: Any) -> bool:
    """Return True for subtasks whose deliverable may be evidence, not edits."""

    if subtask is None:
        return False
    text = "\n".join(
        str(getattr(subtask, name, "") or "")
        for name in ("title", "description", "success_check")
    )
    if not text.strip():
        return False
    if _READ_ONLY_SUBTASK_RE.search(text):
        return True
    tags = {str(t).strip().lower() for t in (getattr(subtask, "tags", []) or [])}
    return bool(tags & {"research", "diagnostic", "analysis", "read_only"})


def _maybe_inject_planner_phase_nudges(
    *,
    state: _LoopState,
    messages: list[dict[str, Any]],
    phase_label: str,
    rounds_in_phase: int,
    allowed_tool_names: frozenset | None,
    plan_now_nudge_emitted: bool,
    planner_discovery_nudge_emitted: bool,
    planner_external_nudge_emitted: bool,
    forced_progress_tool_choice: str | None,
) -> tuple[bool, bool, bool, str | None]:
    planner_discovery_required = str(
        os.environ.get("OUROBOROS_REQUIRE_PLANNER_DISCOVERY") or ""
    ).strip().lower() not in {"0", "false", "no", "off"}
    if (
        phase_label in {"planner", "planner_rescue"}
        and planner_discovery_required
        and state.planner_discovery_calls <= 0
        and not planner_discovery_nudge_emitted
        and rounds_in_phase >= 2
    ):
        messages.append(
            {
                "role": "system",
                "content": (
                    "[PLANNER_DISCOVERY_REQUIRED]\n"
                    "Before `propose_task_plan`, call `get_umbrella_memory` "
                    "or one external discovery tool (`deep_search`, "
                    "`github_project_search`, `github_extract_snippets`, "
                    "`mcp_discover`, `web_fetch`). Workspace reads alone "
                    "do not satisfy the planner discovery gate. For non-trivial "
                    "coding work, prefer one external prior-art/source lookup "
                    "unless you can state why current workspace memory is enough."
                ),
            }
        )
        planner_discovery_nudge_emitted = True

    if (
        phase_label in {"planner", "planner_rescue"}
        and planner_discovery_required
        and state.planner_discovery_calls > 0
        and state.planner_external_discovery_calls <= 0
        and state.last_memory_recall_empty
        and not planner_external_nudge_emitted
    ):
        messages.append(
            {
                "role": "system",
                "content": (
                    "[PLANNER_EXTERNAL_DISCOVERY_REQUIRED]\n"
                    "`get_umbrella_memory` returned no prior knowledge for this "
                    "task. That means the project genuinely needs outside "
                    "input — you cannot plan blindly here. Call ONE external "
                    "discovery tool BEFORE `propose_task_plan`: "
                    "`deep_search(intent='prior_art', query=...)` for web "
                    "results, `github_project_search(query=...)` for similar "
                    "open-source projects, `github_extract_snippets` for "
                    "specific code, `mcp_discover(query=...)` for MCP servers, "
                    "or `web_fetch(url=...)` for known doc URLs. Planning "
                    "without any external evidence on a domain memory does "
                    "not know is what causes the remediation thrash."
                ),
            }
        )
        planner_external_nudge_emitted = True

    if (
        phase_label in {"planner", "planner_rescue"}
        and "propose_task_plan" in (allowed_tool_names or set())
        and not plan_now_nudge_emitted
        and rounds_in_phase >= 3
        and (not planner_discovery_required or state.planner_discovery_calls > 0)
    ):
        messages.append(
            {
                "role": "system",
                "content": (
                    "[PLANNER_PLAN_NOW]\n"
                    f"You have used {rounds_in_phase} discovery rounds. Stop "
                    "reading the workspace; you have enough context. If you "
                    "have not yet called `propose_discovery_plan`, call it "
                    "first to store your research budget, then call "
                    "`propose_task_plan` with at least one subtask. Further "
                    "read/list/search calls in this phase will be answered "
                    "by the harness with this same nudge until the plan exists."
                ),
            }
        )
        plan_now_nudge_emitted = True
        forced_progress_tool_choice = (
            "propose_discovery_plan"
            if planner_discovery_required and not state.discovery_plan_proposed
            else "propose_task_plan"
        )

    return (
        plan_now_nudge_emitted,
        planner_discovery_nudge_emitted,
        planner_external_nudge_emitted,
        forced_progress_tool_choice,
    )


def _handle_no_tool_response_in_phase(
    *,
    content: Any,
    state: _LoopState,
    messages: list[dict[str, Any]],
    phase_label: str,
    phase_write_tool_calls: int,
    no_write_text_nudges: int,
    pseudo_text_nudges: int,
    terminate_on_text: bool,
    require_writes_before_text_exit: bool,
    tool_schemas: list[dict[str, Any]],
    terminating_tools: frozenset,
) -> tuple[
    tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, str] | None,
    int,
    int,
    str | None,
]:
    final_text, _usage, _trace = _handle_text_response(
        content, state.llm_trace, state.accumulated_usage
    )
    state.last_text = final_text
    pseudo_tool_text = _looks_like_pseudo_tool_call_text(content)
    if terminate_on_text:
        if _maybe_inject_no_write_nudge(
            require=require_writes_before_text_exit,
            phase_write_tool_calls=phase_write_tool_calls,
            nudges_so_far=no_write_text_nudges,
            phase_label=phase_label,
            workspace_id=state.active_workspace_id,
            content=content,
            messages=messages,
        ):
            return None, no_write_text_nudges + 1, pseudo_text_nudges, None
        pseudo_handled, pseudo_text_nudges = _maybe_handle_text_phase_pseudo_tool_call(
            pseudo_tool_text=pseudo_tool_text,
            tool_schemas=tool_schemas,
            nudges_so_far=pseudo_text_nudges,
            phase_label=phase_label,
            content=content,
            messages=messages,
        )
        if pseudo_handled:
            return None, no_write_text_nudges, pseudo_text_nudges, None
        log.info(
            "[LOOP] <<< Phase '%s' exited: text reply (no tool calls)", phase_label
        )
        return (None, "text"), no_write_text_nudges, pseudo_text_nudges, None

    messages.append({"role": "assistant", "content": content or ""})
    forced_progress_tool_choice = None
    if terminating_tools:
        required = ", ".join(f"`{name}`" for name in sorted(terminating_tools))
        log.warning(
            "[LOOP] Phase '%s' received text without required tool(s): %s",
            phase_label,
            ", ".join(sorted(terminating_tools)),
        )
        if pseudo_tool_text:
            log.warning(
                "[LOOP] Phase '%s' emitted pseudo tool-call text without structured tool_calls",
                phase_label,
            )
        messages.append(
            {
                "role": "system",
                "content": (
                    "[REQUIRED_TOOL_MISSING]\n"
                    f"This phase cannot advance on prose. Call {required} "
                    "as a tool now. Do not call unrelated tools and do not "
                    "answer with text only. If the task is partially implemented, "
                    "first write the missing files/logic and rerun tests, then "
                    "call the required tool.\n"
                    + (
                        "\n[TOOL_CALL_FORMAT]\n"
                        "You emitted pseudo tool-call text (for example XML-like tags "
                        "or `tool_name(...)` in plain text). That does NOT execute tools. "
                        "Before your next response, do a tool-call preflight: "
                        "1) choose one tool from the active schema, "
                        "2) ensure arguments exactly match that tool parameters, "
                        "3) emit a native structured tool_call only (no wrappers, no prose)."
                        if pseudo_tool_text
                        else ""
                    )
                ),
            }
        )
        forced_progress_tool_choice = sorted(terminating_tools)[0]
    return None, no_write_text_nudges, pseudo_text_nudges, forced_progress_tool_choice


def _preflight_failed_tool_names(
    *,
    tool_calls: list[dict[str, Any]],
    trace_tool_calls: list[dict[str, Any]],
) -> list[str]:
    if not trace_tool_calls:
        return []
    recent_trace = trace_tool_calls[-len(tool_calls) :] if tool_calls else []
    failed_tools: list[str] = []
    for tc in tool_calls:
        name = str(tc.get("function", {}).get("name") or "")
        if any(
            item.get("tool") == name
            and str(item.get("result") or "").startswith("⚠️ TOOL_PREFLIGHT_ERROR")
            for item in recent_trace
        ):
            failed_tools.append(name)
    return failed_tools


def _normalize_completion_error(text: str) -> str:
    value = str(text or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[0-9a-f]{8,}", "<id>", value)
    return value[:240]


def _active_plan_id_from_tools(tools: ToolRegistry, task_id: str) -> str:
    ctx = getattr(tools, "_ctx", None)
    if ctx is None:
        return task_id
    plan_ctx = getattr(ctx, "plan_execution_context", None)
    if isinstance(plan_ctx, dict):
        return str(plan_ctx.get("active_plan_id") or task_id)
    return str(
        getattr(plan_ctx, "active_plan_id", "")
        or getattr(ctx, "active_plan_id", "")
        or task_id
    )


def _maybe_trip_completion_impasse(
    *,
    state: _CompletionToolImpasseState,
    tool_calls: list[dict[str, Any]],
    trace_tool_calls: list[dict[str, Any]],
    terminating_tools: frozenset,
    phase_label: str,
    task_id: str,
    drive_root: pathlib.Path | None,
    tools: ToolRegistry,
) -> str:
    if not terminating_tools or not tool_calls or not trace_tool_calls:
        return ""
    recent_trace = trace_tool_calls[-len(tool_calls) :]
    for item in recent_trace:
        tool = str(item.get("tool") or "")
        if tool not in terminating_tools:
            continue
        result = str(item.get("result") or "")
        is_error = (
            bool(item.get("is_error"))
            or result.startswith("⚠️")
            or '"status": "control_plane_error"' in result
        )
        if not is_error:
            continue
        normalized = _normalize_completion_error(result)
        active_plan_id = _active_plan_id_from_tools(tools, task_id)
        key = (phase_label, tool, normalized, active_plan_id)
        count = state.counts.get(key, 0) + 1
        state.counts[key] = count
        if count < _COMPLETION_IMPASSE_THRESHOLD:
            continue
        args_preview = ""
        for tc in tool_calls:
            if str(tc.get("function", {}).get("name") or "") == tool:
                args_preview = json.dumps(_safe_tool_args_json(tc), ensure_ascii=False)[
                    :1200
                ]
                break
        payload = {
            "schema_version": 1,
            "ts": utc_now_iso(),
            "status": "phase_impasse",
            "task_id": task_id,
            "plan_id": active_plan_id,
            "drive_root": str(drive_root or ""),
            "phase": phase_label,
            "tool": tool,
            "repeat_count": count,
            "normalized_error": normalized,
            "last_error": result[:2000],
            "tool_args_preview": args_preview,
        }
        if drive_root:
            try:
                state_dir = pathlib.Path(drive_root) / "state"
                state_dir.mkdir(parents=True, exist_ok=True)
                (state_dir / "phase_impasse.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                log.debug("phase_impasse artifact write failed", exc_info=True)
        return (
            "⛔ phase_impasse: completion tool failed repeatedly with the same "
            f"control-plane error in phase `{phase_label}`. See phase_impasse.json."
        )
    return ""


def _process_tool_call_round_after_execution(
    *,
    state: _LoopState,
    tool_calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: ToolRegistry,
    drive_logs: pathlib.Path,
    task_id: str,
    stateful_executor: "_StatefulToolExecutor",
    emit_progress: Callable[[str], None],
    allowed_tool_names: frozenset | None,
    phase_label: str,
    repeated_read_guard: _RepeatedReadGuardState,
    repeated_failure_guard: _RepeatedFailureGuardState,
    completion_impasse_guard: _CompletionToolImpasseState,
    verify_gate: VerifyGate,
    repo_root: Any,
    require_writes_before_text_exit: bool,
    phase_write_tool_calls: int,
    no_write_tool_nudges: int,
    last_no_write_tool_nudge_round: int,
    round_idx: int,
    rounds_in_phase: int,
    forced_progress_tool_choice: str | None,
    preflight_repair_rounds: int,
    forbidden_strike_counts: dict[str, int],
    memory_hooks: Any,
    terminating_tools: frozenset,
    drive_root: pathlib.Path | None,
) -> tuple[
    tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, str] | str | None,
    int,
    int,
    int,
    str | None,
    int,
]:
    if _reject_tool_calls_under_no_write_enforcement(
        forced_tool=forced_progress_tool_choice,
        tool_calls=tool_calls,
        messages=messages,
        llm_trace=state.llm_trace,
        emit_progress=emit_progress,
        phase_label=phase_label,
    ):
        return (
            "continue",
            phase_write_tool_calls,
            no_write_tool_nudges,
            last_no_write_tool_nudge_round,
            forced_progress_tool_choice,
            preflight_repair_rounds,
        )
    if forced_progress_tool_choice and any(
        str(tc.get("function", {}).get("name") or "") == forced_progress_tool_choice
        for tc in tool_calls
    ):
        forced_progress_tool_choice = None

    _publish_state_view_to_tool_ctx(state, tools, phase_label=phase_label)
    _handle_tool_calls(
        tool_calls,
        tools,
        drive_logs,
        task_id,
        stateful_executor,
        messages,
        state.llm_trace,
        emit_progress,
        allowed_tool_names=allowed_tool_names,
        phase_label=phase_label,
    )
    preflight_tools = _preflight_failed_tool_names(
        tool_calls=tool_calls,
        trace_tool_calls=list(state.llm_trace.get("tool_calls") or []),
    )
    if preflight_tools and preflight_repair_rounds < _tool_preflight_repair_round_cap():
        preflight_repair_rounds += 1
        hints = "\n".join(
            _tool_schema_hint(name, tools) for name in preflight_tools if name
        )
        repair_tool = next((name for name in preflight_tools if name), "")
        if repair_tool and (
            allowed_tool_names is None or repair_tool in allowed_tool_names
        ):
            forced_progress_tool_choice = repair_tool
        messages.append(
            {
                "role": "system",
                "content": (
                    "[TOOL_REPAIR_HINT]\n"
                    "The previous native tool call failed preflight before execution. "
                    "Re-emit the same intended tool call with valid JSON arguments only. "
                    "Do not answer in prose and do not switch to final summary.\n"
                    f"{hints}"
                ),
            }
        )
        return (
            None,
            phase_write_tool_calls,
            no_write_tool_nudges,
            last_no_write_tool_nudge_round,
            forced_progress_tool_choice,
            preflight_repair_rounds,
        )

    phase_write_tool_calls += _count_workspace_write_tool_calls(tool_calls)
    _update_state_from_tool_calls(state, tool_calls)
    impasse_message = _maybe_trip_completion_impasse(
        state=completion_impasse_guard,
        tool_calls=tool_calls,
        trace_tool_calls=list(state.llm_trace.get("tool_calls") or []),
        terminating_tools=terminating_tools,
        phase_label=phase_label,
        task_id=task_id,
        drive_root=drive_root,
        tools=tools,
    )
    if impasse_message:
        state.last_text = impasse_message
        append_jsonl(
            drive_logs / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "phase_impasse",
                "task_id": task_id,
                "phase": phase_label,
                "message": impasse_message,
            },
        )
        return (
            (impasse_message, state.accumulated_usage, state.llm_trace),
            phase_write_tool_calls,
            no_write_tool_nudges,
            last_no_write_tool_nudge_round,
            forced_progress_tool_choice,
            preflight_repair_rounds,
        )
    if allowed_tool_names is not None:
        for tc in tool_calls:
            fn = str(tc.get("function", {}).get("name") or "")
            if fn and fn not in allowed_tool_names:
                strikes = forbidden_strike_counts.get(fn, 0) + 1
                forbidden_strike_counts[fn] = strikes
                phase_limit = (
                    1
                    if phase_label.startswith("review_")
                    else _FORBIDDEN_TOOL_REPEAT_LIMIT
                )
                if strikes >= phase_limit:
                    log.warning(
                        "[LOOP] <<< Phase '%s' exited early: model emitted forbidden tool '%s' %d times in a row (allowed=%s).",
                        phase_label,
                        fn,
                        strikes,
                        ",".join(sorted(allowed_tool_names))[:200],
                    )
                    return (
                        (None, "forbidden_tool_loop"),
                        phase_write_tool_calls,
                        no_write_tool_nudges,
                        last_no_write_tool_nudge_round,
                        forced_progress_tool_choice,
                        preflight_repair_rounds,
                    )
            else:
                forbidden_strike_counts.pop(fn, None)
    _maybe_inject_repeated_read_guard(tool_calls, messages, repeated_read_guard)
    _maybe_inject_repeated_failure_guard(tool_calls, messages, repeated_failure_guard)

    state.active_workspace_id = memory_hooks.observe_tool_calls(
        tool_calls=tool_calls,
        recent_tool_results=list(state.llm_trace.get("tool_calls") or [])[
            -len(tool_calls) :
        ],
        write_tool_names=WRITE_TOOL_NAMES,
        verify_gate=verify_gate,
        repo_root=repo_root,
        current_workspace_id=state.active_workspace_id,
    )
    previous_no_write_tool_nudges = no_write_tool_nudges
    no_write_tool_nudges, last_no_write_tool_nudge_round = (
        _maybe_inject_no_write_tool_nudge(
            require=require_writes_before_text_exit,
            phase_write_tool_calls=phase_write_tool_calls,
            nudges_so_far=no_write_tool_nudges,
            last_nudge_round=last_no_write_tool_nudge_round,
            round_idx=round_idx,
            rounds_in_phase=rounds_in_phase,
            phase_label=phase_label,
            workspace_id=state.active_workspace_id,
            tool_calls=tool_calls,
            messages=messages,
        )
    )
    no_write_tool_nudge_injected = no_write_tool_nudges > previous_no_write_tool_nudges
    if any(
        str(tc.get("function", {}).get("name") or "") in WRITE_TOOL_NAMES
        or str(tc.get("function", {}).get("name") or "") == "run_workspace_verify"
        for tc in tool_calls
    ):
        forced_progress_tool_choice = None
    elif no_write_tool_nudge_injected:
        forced_progress_tool_choice = _select_forced_progress_tool(allowed_tool_names)
        if forced_progress_tool_choice:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "[NO_WRITE_TOOL_ENFORCED]\n"
                        f"Your next tool call is forced to `{forced_progress_tool_choice}` to produce concrete progress."
                    ),
                }
            )
    if _should_abort_no_write_tool_churn(
        require=require_writes_before_text_exit,
        phase_write_tool_calls=phase_write_tool_calls,
        nudges_so_far=no_write_tool_nudges,
        nudge_injected_this_round=no_write_tool_nudge_injected,
        tool_calls=tool_calls,
    ):
        stall_message = _format_no_write_tool_stall_message(
            phase_label=phase_label,
            workspace_id=state.active_workspace_id,
            rounds_in_phase=rounds_in_phase,
            nudges_so_far=no_write_tool_nudges,
        )
        log.warning("[LOOP] <<< Phase '%s' aborted: no-write tool churn", phase_label)
        messages.append(
            {"role": "system", "content": f"[NO_WRITE_TOOL_STALL] {stall_message}"}
        )
        state.last_text = stall_message
        return (
            (stall_message, state.accumulated_usage, state.llm_trace),
            phase_write_tool_calls,
            no_write_tool_nudges,
            last_no_write_tool_nudge_round,
            forced_progress_tool_choice,
            preflight_repair_rounds,
        )

    return (
        None,
        phase_write_tool_calls,
        no_write_tool_nudges,
        last_no_write_tool_nudge_round,
        forced_progress_tool_choice,
        preflight_repair_rounds,
    )


def _call_llm_for_phase_round(
    *,
    state: _LoopState,
    messages: list[dict[str, Any]],
    llm: LLMClient,
    tool_schemas: list[dict[str, Any]],
    force_tool_choice: str | None,
    forced_progress_tool_choice: str | None,
    phase_label: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: queue.Queue | None,
    task_type: str,
    emit_progress: Callable[[str], None],
) -> tuple[
    dict[str, Any] | None, tuple[str, dict[str, Any], dict[str, Any]] | None
]:
    effective_tool_choice: Any = state.active_tool_choice
    if force_tool_choice and tool_schemas:
        effective_tool_choice = {
            "type": "function",
            "function": {"name": force_tool_choice},
        }
    elif forced_progress_tool_choice and tool_schemas:
        effective_tool_choice = {
            "type": "function",
            "function": {"name": forced_progress_tool_choice},
        }
    request_max_tokens = _resolve_tool_round_max_tokens(
        state.active_max_tokens,
        bool(tool_schemas),
    )
    request_tool_schemas = tool_schemas
    if (
        phase_label in {"planner", "planner_rescue"}
        and forced_progress_tool_choice == "propose_task_plan"
    ):
        planner_discovery_required = str(
            os.environ.get("OUROBOROS_REQUIRE_PLANNER_DISCOVERY") or ""
        ).strip().lower() not in {"0", "false", "no", "off"}
        names = {"propose_task_plan"}
        if planner_discovery_required:
            names.add("propose_discovery_plan")
        request_tool_schemas = _tool_schemas_for_names(tool_schemas, names)
    msg, _cost = _call_llm_with_retry(
        llm,
        messages,
        state.active_model,
        request_tool_schemas,
        state.active_effort,
        request_max_tokens,
        state.active_temperature,
        effective_tool_choice,
        max_retries,
        drive_logs,
        task_id,
        state.round_idx,
        event_queue,
        state.accumulated_usage,
        task_type,
        phase_label,
    )
    if msg is not None:
        return msg, None
    msg, fallback_final = _try_fallback_llm(
        state=state,
        messages=messages,
        llm=llm,
        tool_schemas=tool_schemas,
        max_retries=max_retries,
        drive_logs=drive_logs,
        task_id=task_id,
        event_queue=event_queue,
        task_type=task_type,
        emit_progress=emit_progress,
    )
    return msg, fallback_final if msg is None else None


def _handle_phase_tail_after_tool_round(
    *,
    state: _LoopState,
    tool_calls: list[dict[str, Any]],
    terminating_tools: frozenset,
    messages: list[dict[str, Any]],
    phase_label: str,
    budget_remaining_usd: float | None,
    llm: LLMClient,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: queue.Queue | None,
    task_type: str,
    drive_root: pathlib.Path | None,
    rounds_in_phase: int,
) -> tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, str] | None:
    if terminating_tools:
        invoked = {tc.get("function", {}).get("name", "") for tc in tool_calls}
        if invoked & set(terminating_tools):
            accepted, rejected = _successful_terminating_tools(
                tool_calls=tool_calls,
                trace_tool_calls=list(state.llm_trace.get("tool_calls") or []),
                terminating_tools=terminating_tools,
            )
            if accepted:
                log.info(
                    "[LOOP] <<< Phase '%s' exited: accepted terminating tool(s) %s",
                    phase_label,
                    ",".join(sorted(accepted)),
                )
                return None, "terminated"
            if rejected:
                log.warning(
                    "[LOOP] Phase '%s' stays active: rejected terminating tool(s) %s",
                    phase_label,
                    ",".join(sorted(rejected)),
                )
                messages.append(
                    {
                        "role": "system",
                        "content": _format_rejected_termination_nudge(rejected),
                    }
                )
                return "continue", "continue"

    budget_result = _check_budget_limits(
        budget_remaining_usd,
        state.accumulated_usage,
        state.round_idx,
        messages,
        llm,
        state.active_model,
        state.active_effort,
        state.active_max_tokens,
        state.active_temperature,
        state.active_tool_choice,
        max_retries,
        drive_logs,
        task_id,
        event_queue,
        state.llm_trace,
        task_type,
    )
    if budget_result is not None:
        return budget_result, "final"

    _maybe_write_run_snapshot(
        drive_root=drive_root,
        task_id=task_id,
        phase_label=phase_label,
        round_idx=state.round_idx,
        phase_round=rounds_in_phase,
        usage=state.accumulated_usage,
        active_model=state.active_model,
        active_workspace_id=state.active_workspace_id,
        messages=messages,
    )
    return None


def _check_phase_round_caps(
    *,
    state: _LoopState,
    messages: list[dict[str, Any]],
    llm: LLMClient,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: queue.Queue | None,
    task_type: str,
    max_retries: int,
    max_global_rounds: int,
    max_phase_rounds: int,
    rounds_in_phase: int,
    phase_label: str,
) -> tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, str] | None:
    if max_global_rounds > 0 and state.round_idx > max_global_rounds:
        finish_reason = (
            f"⚠️ Task exceeded MAX_ROUNDS ({max_global_rounds}). "
            "Consider decomposing into subtasks via schedule_task."
        )
        messages.append({"role": "system", "content": f"[ROUND_LIMIT] {finish_reason}"})
        try:
            final_msg, _final_cost = _call_llm_with_retry(
                llm,
                messages,
                state.active_model,
                None,
                state.active_effort,
                state.active_max_tokens,
                state.active_temperature,
                state.active_tool_choice,
                max_retries,
                drive_logs,
                task_id,
                state.round_idx,
                event_queue,
                state.accumulated_usage,
                task_type,
            )
            if final_msg:
                return (
                    final_msg.get("content") or finish_reason,
                    state.accumulated_usage,
                    state.llm_trace,
                ), "final"
            return (finish_reason, state.accumulated_usage, state.llm_trace), "final"
        except Exception:
            log.warning("Failed to get final response after round limit", exc_info=True)
            return (finish_reason, state.accumulated_usage, state.llm_trace), "final"

    if max_phase_rounds > 0 and rounds_in_phase > max_phase_rounds:
        log.info(
            "[LOOP] <<< Phase '%s' exited: max_phase_rounds (%d) reached",
            phase_label,
            max_phase_rounds,
        )
        return None, "max_phase_rounds"
    return None


def _prepare_llm_phase_round(
    *,
    state: _LoopState,
    messages: list[dict[str, Any]],
    tools: ToolRegistry,
    incoming_messages: queue.Queue,
    drive_root: pathlib.Path | None,
    task_id: str,
    event_queue: queue.Queue | None,
    owner_msg_seen: set,
    verify_gate: VerifyGate,
    repo_root: Any,
    phase_label: str,
    emit_progress: Callable[[str], None],
    max_global_rounds: int,
    memory_hooks: Any,
) -> None:
    _maybe_inject_self_check(
        state.round_idx,
        max_global_rounds,
        messages,
        state.accumulated_usage,
        emit_progress,
    )
    if _periodic_recall_enabled_for_phase(phase_label):
        state.last_periodic_recall_round = memory_hooks.maybe_inject_periodic_recall(
            workspace_id=state.active_workspace_id,
            round_idx=state.round_idx,
            last_recall_round=state.last_periodic_recall_round,
            recent_actions_summary=_summarize_recent_actions(state.llm_trace),
            repo_root=repo_root,
            messages=messages,
            phase=phase_label,
        )
    if verify_gate.should_remind(state.round_idx):
        messages.append(verify_gate.build_reminder(state.round_idx))
    (
        state.active_model,
        state.active_effort,
        state.active_max_tokens,
        state.active_temperature,
        state.active_tool_choice,
    ) = _consume_ctx_overrides(
        tools._ctx,
        state.active_model,
        state.active_effort,
        state.active_max_tokens,
        state.active_temperature,
        state.active_tool_choice,
    )
    _drain_incoming_messages(
        messages, incoming_messages, drive_root, task_id, event_queue, owner_msg_seen
    )
    _auto_set_pending_compaction_for_overflow(messages, tools._ctx)
    messages[:] = _maybe_compact_history(messages, tools._ctx, state.round_idx)


def _start_llm_phase_round(
    *,
    state: _LoopState,
    phase_start_round: int,
    max_rounds_label: str,
    phase_label: str,
    deadline_monotonic: float | None,
    drive_root: pathlib.Path | None,
    task_id: str,
    messages: list[dict[str, Any]],
    llm: LLMClient,
    drive_logs: pathlib.Path,
    event_queue: queue.Queue | None,
    task_type: str,
    max_retries: int,
    max_global_rounds: int,
    max_phase_rounds: int,
) -> tuple[
    int, tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, str] | None
]:
    state.round_idx += 1
    rounds_in_phase = state.round_idx - phase_start_round
    _total_cost = float(state.accumulated_usage.get("cost") or 0)
    log.info(
        "[LOOP] === Round %d/%s | phase=%s | model=%s | effort=%s | cost=$%.4f ===",
        state.round_idx,
        max_rounds_label,
        phase_label,
        state.active_model,
        state.active_effort,
        _total_cost,
    )
    deadline_result = check_runtime_deadline(
        deadline_monotonic,
        state.accumulated_usage,
        state.llm_trace,
        DEADLINE_BEFORE_NEXT_ROUND,
    )
    if deadline_result is not None:
        return rounds_in_phase, (deadline_result, "final")
    stop_result = _check_stop_requested(
        drive_root,
        task_id,
        state.accumulated_usage,
        state.llm_trace,
    )
    if stop_result is not None:
        return rounds_in_phase, (stop_result, "final")
    return rounds_in_phase, _check_phase_round_caps(
        state=state,
        messages=messages,
        llm=llm,
        drive_logs=drive_logs,
        task_id=task_id,
        event_queue=event_queue,
        task_type=task_type,
        max_retries=max_retries,
        max_global_rounds=max_global_rounds,
        max_phase_rounds=max_phase_rounds,
        rounds_in_phase=rounds_in_phase,
        phase_label=phase_label,
    )


def _process_phase_tool_round(
    *,
    state: _LoopState,
    tool_calls: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: ToolRegistry,
    drive_logs: pathlib.Path,
    task_id: str,
    stateful_executor: "_StatefulToolExecutor",
    emit_progress: Callable[[str], None],
    allowed_tool_names: frozenset | None,
    phase_label: str,
    repeated_read_guard: _RepeatedReadGuardState,
    repeated_failure_guard: _RepeatedFailureGuardState,
    completion_impasse_guard: _CompletionToolImpasseState,
    verify_gate: VerifyGate,
    repo_root: Any,
    require_writes_before_text_exit: bool,
    phase_write_tool_calls: int,
    no_write_tool_nudges: int,
    last_no_write_tool_nudge_round: int,
    rounds_in_phase: int,
    forced_progress_tool_choice: str | None,
    preflight_repair_rounds: int,
    forbidden_strike_counts: dict[str, int],
    memory_hooks: Any,
    terminating_tools: frozenset,
    drive_root: pathlib.Path | None,
) -> tuple[
    tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, str] | str | None,
    int,
    int,
    int,
    str | None,
    int,
]:
    return _process_tool_call_round_after_execution(
        state=state,
        tool_calls=tool_calls,
        messages=messages,
        tools=tools,
        drive_logs=drive_logs,
        task_id=task_id,
        stateful_executor=stateful_executor,
        emit_progress=emit_progress,
        allowed_tool_names=allowed_tool_names,
        phase_label=phase_label,
        repeated_read_guard=repeated_read_guard,
        repeated_failure_guard=repeated_failure_guard,
        completion_impasse_guard=completion_impasse_guard,
        verify_gate=verify_gate,
        repo_root=repo_root,
        require_writes_before_text_exit=require_writes_before_text_exit,
        phase_write_tool_calls=phase_write_tool_calls,
        no_write_tool_nudges=no_write_tool_nudges,
        last_no_write_tool_nudge_round=last_no_write_tool_nudge_round,
        round_idx=state.round_idx,
        rounds_in_phase=rounds_in_phase,
        forced_progress_tool_choice=forced_progress_tool_choice,
        preflight_repair_rounds=preflight_repair_rounds,
        forbidden_strike_counts=forbidden_strike_counts,
        memory_hooks=memory_hooks,
        terminating_tools=terminating_tools,
        drive_root=drive_root,
    )


def _run_llm_phase(
    *,
    state: _LoopState,
    messages: list[dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str,
    task_id: str,
    budget_remaining_usd: float | None,
    event_queue: queue.Queue | None,
    drive_root: pathlib.Path | None,
    deadline_monotonic: float | None,
    tool_schemas: list[dict[str, Any]],
    stateful_executor: "_StatefulToolExecutor",
    repo_root: Any,
    verify_gate: VerifyGate,
    owner_msg_seen: set,
    max_retries: int,
    max_global_rounds: int,
    max_rounds_label: str,
    phase_label: str,
    terminating_tools: frozenset = frozenset(),
    max_phase_rounds: int = 0,
    terminate_on_text: bool = True,
    require_writes_before_text_exit: bool = False,
    force_tool_choice: str | None = None,
    memory_hooks: Any = None,
) -> tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, str]:
    """Drive one LLM phase until a terminating condition fires."""
    repeated_read_guard, repeated_failure_guard = (
        _RepeatedReadGuardState(),
        _RepeatedFailureGuardState(),
    )
    completion_impasse_guard = _CompletionToolImpasseState()
    phase_start_round = state.round_idx
    log.info(
        "[LOOP] >>> Phase '%s' starts at global round %d",
        phase_label,
        phase_start_round,
    )
    no_write_text_nudges = no_write_tool_nudges = last_no_write_tool_nudge_round = 0
    phase_write_tool_calls = 0
    forced_progress_tool_choice: str | None = None
    allowed_tool_names = _allowed_tool_names_from_schemas(tool_schemas)
    forbidden_strike_counts: dict[str, int] = {}
    plan_now_nudge_emitted = planner_discovery_nudge_emitted = (
        planner_external_nudge_emitted
    ) = False
    preflight_repair_rounds = pseudo_text_nudges = 0

    while True:
        rounds_in_phase, start_result = _start_llm_phase_round(
            state=state,
            phase_start_round=phase_start_round,
            max_rounds_label=max_rounds_label,
            phase_label=phase_label,
            deadline_monotonic=deadline_monotonic,
            drive_root=drive_root,
            task_id=task_id,
            messages=messages,
            llm=llm,
            drive_logs=drive_logs,
            event_queue=event_queue,
            task_type=task_type,
            max_retries=max_retries,
            max_global_rounds=max_global_rounds,
            max_phase_rounds=max_phase_rounds,
        )
        if start_result is not None:
            return _phase_return(start_result)

        _prepare_llm_phase_round(
            state=state,
            messages=messages,
            tools=tools,
            incoming_messages=incoming_messages,
            drive_root=drive_root,
            task_id=task_id,
            event_queue=event_queue,
            owner_msg_seen=owner_msg_seen,
            verify_gate=verify_gate,
            repo_root=repo_root,
            phase_label=phase_label,
            emit_progress=emit_progress,
            max_global_rounds=max_global_rounds,
            memory_hooks=memory_hooks,
        )

        (
            plan_now_nudge_emitted,
            planner_discovery_nudge_emitted,
            planner_external_nudge_emitted,
            forced_progress_tool_choice,
        ) = _maybe_inject_planner_phase_nudges(
            state=state,
            messages=messages,
            phase_label=phase_label,
            rounds_in_phase=rounds_in_phase,
            allowed_tool_names=allowed_tool_names,
            plan_now_nudge_emitted=plan_now_nudge_emitted,
            planner_discovery_nudge_emitted=planner_discovery_nudge_emitted,
            planner_external_nudge_emitted=planner_external_nudge_emitted,
            forced_progress_tool_choice=forced_progress_tool_choice,
        )

        msg, fallback_final = _call_llm_for_phase_round(
            state=state,
            messages=messages,
            llm=llm,
            tool_schemas=tool_schemas,
            force_tool_choice=force_tool_choice,
            forced_progress_tool_choice=forced_progress_tool_choice,
            phase_label=phase_label,
            max_retries=max_retries,
            drive_logs=drive_logs,
            task_id=task_id,
            event_queue=event_queue,
            task_type=task_type,
            emit_progress=emit_progress,
        )
        if msg is None:
            return fallback_final, "final"

        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content")
        if not tool_calls:
            no_tool_result, no_write_text_nudges, pseudo_text_nudges, forced_choice = (
                _handle_no_tool_response_in_phase(
                    content=content,
                    state=state,
                    messages=messages,
                    phase_label=phase_label,
                    phase_write_tool_calls=phase_write_tool_calls,
                    no_write_text_nudges=no_write_text_nudges,
                    pseudo_text_nudges=pseudo_text_nudges,
                    terminate_on_text=terminate_on_text,
                    require_writes_before_text_exit=require_writes_before_text_exit,
                    tool_schemas=tool_schemas,
                    terminating_tools=terminating_tools,
                )
            )
            if forced_choice:
                forced_progress_tool_choice = forced_choice
            if no_tool_result is not None:
                return _phase_return(no_tool_result)
            continue

        deadline_result = check_runtime_deadline(
            deadline_monotonic,
            state.accumulated_usage,
            state.llm_trace,
            DEADLINE_AFTER_LLM_RESPONSE,
            content,
        )
        if deadline_result is not None:
            return deadline_result, "final"

        stop_result = _check_stop_requested(
            drive_root,
            task_id,
            state.accumulated_usage,
            state.llm_trace,
            content,
        )
        if stop_result is not None:
            return stop_result, "final"

        messages.append(
            {"role": "assistant", "content": content or "", "tool_calls": tool_calls}
        )
        if content and content.strip():
            emit_progress(content.strip())
            state.llm_trace["assistant_notes"].append(content.strip()[:320])

        (
            tool_round_result,
            phase_write_tool_calls,
            no_write_tool_nudges,
            last_no_write_tool_nudge_round,
            forced_progress_tool_choice,
            preflight_repair_rounds,
        ) = _process_phase_tool_round(
            state=state,
            tool_calls=tool_calls,
            messages=messages,
            tools=tools,
            drive_logs=drive_logs,
            task_id=task_id,
            stateful_executor=stateful_executor,
            emit_progress=emit_progress,
            allowed_tool_names=allowed_tool_names,
            phase_label=phase_label,
            repeated_read_guard=repeated_read_guard,
            repeated_failure_guard=repeated_failure_guard,
            completion_impasse_guard=completion_impasse_guard,
            verify_gate=verify_gate,
            repo_root=repo_root,
            require_writes_before_text_exit=require_writes_before_text_exit,
            phase_write_tool_calls=phase_write_tool_calls,
            no_write_tool_nudges=no_write_tool_nudges,
            last_no_write_tool_nudge_round=last_no_write_tool_nudge_round,
            rounds_in_phase=rounds_in_phase,
            forced_progress_tool_choice=forced_progress_tool_choice,
            preflight_repair_rounds=preflight_repair_rounds,
            forbidden_strike_counts=forbidden_strike_counts,
            memory_hooks=memory_hooks,
            terminating_tools=terminating_tools,
            drive_root=drive_root,
        )
        if tool_round_result is not None:
            if tool_round_result == "continue":
                continue
            return _phase_return(tool_round_result)

        tail_result = _handle_phase_tail_after_tool_round(
            state=state,
            tool_calls=tool_calls,
            terminating_tools=terminating_tools,
            messages=messages,
            phase_label=phase_label,
            budget_remaining_usd=budget_remaining_usd,
            llm=llm,
            max_retries=max_retries,
            drive_logs=drive_logs,
            task_id=task_id,
            event_queue=event_queue,
            task_type=task_type,
            drive_root=drive_root,
            rounds_in_phase=rounds_in_phase,
        )
        if tail_result is not None:
            if tail_result == ("continue", "continue"):
                continue
            return _phase_return(tail_result)


_RUN_SNAPSHOT_EVERY_ROUNDS = 25


def _maybe_write_run_snapshot(
    *,
    drive_root: pathlib.Path | None,
    task_id: str,
    phase_label: str,
    round_idx: int,
    phase_round: int,
    usage: dict[str, Any],
    active_model: str,
    active_workspace_id: str,
    messages: list[dict[str, Any]],
) -> None:
    """Write a small JSON snapshot of run state every ``N`` rounds.

    Goal: if the launcher process crashes / hangs / OOMs mid-run, the
    UI and post-mortem tooling can show "the run was at round N in
    phase X with model Y when it died" instead of the very vague
    "task in progress, no recent events".

    The snapshot is intentionally lightweight: only metadata + the
    last ~5 message previews. We do not persist the full message
    buffer (it can be megabytes during a long subtask phase). Resume
    semantics are *not* implemented yet; this is a status beacon for
    operator-side diagnosis. Resuming would require also persisting
    the full ``messages`` list and the incremental tool-call history,
    which we intentionally defer.
    """
    if not drive_root:
        return
    if round_idx <= 0 or round_idx % _RUN_SNAPSHOT_EVERY_ROUNDS != 0:
        return
    try:
        state_dir = pathlib.Path(drive_root) / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "run_snapshot.json"
        recent: list[dict[str, Any]] = []
        for msg in messages[-5:]:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = msg.get("content")
            if isinstance(content, list):
                # tool result blocks etc. — flatten to a stringified preview.
                preview_src = json.dumps(content, ensure_ascii=False, default=str)[:400]
            else:
                preview_src = str(content or "")[:400]
            recent.append(
                {
                    "role": role,
                    "preview": preview_src,
                    "tool_call_count": len(msg.get("tool_calls") or [])
                    if isinstance(msg.get("tool_calls"), list)
                    else 0,
                }
            )
        snapshot = {
            "schema_version": 1,
            "ts": utc_now_iso(),
            "task_id": task_id,
            "phase": phase_label,
            "global_round": round_idx,
            "phase_round": phase_round,
            "active_model": active_model,
            "active_workspace_id": active_workspace_id,
            "usage": {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "cost": float(usage.get("cost") or 0.0),
            },
            "message_count": len(messages),
            "recent_messages": recent,
        }
        # Atomic write so a crash mid-write doesn't leave a half-truncated file.
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            tmp.replace(path)
        except (PermissionError, OSError):
            # Antivirus / indexer holding the file briefly; fall back to direct write.
            try:
                path.write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            except OSError:
                log.debug("run_snapshot write failed", exc_info=True)
    except Exception:
        log.debug("run_snapshot encoding failed", exc_info=True)


def _run_planner_phase_with_rescue(
    *,
    state: "_LoopState",
    messages: list[dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str,
    task_id: str,
    budget_remaining_usd: float | None,
    event_queue: queue.Queue | None,
    drive_root: pathlib.Path | None,
    deadline_monotonic: float | None,
    tool_schemas: list[dict[str, Any]],
    stateful_executor: "_StatefulToolExecutor",
    repo_root: Any,
    verify_gate: "VerifyGate",
    owner_msg_seen: set,
    max_retries: int,
    max_global_rounds: int,
    max_rounds_label: str,
    plan_store: Any,
    task_main_text: str,
    planner: Any,
    memory_hooks: Any,
) -> tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, Any]:
    """Run the planner phase (and one rescue retry if it didn't propose).

    Returns ``(early_return, plan_or_none)``. ``early_return`` is the
    final tuple to bubble out of ``run_llm_loop`` (when the phase
    decided to terminate), or ``None`` to keep going.
    """
    messages.append(
        {
            "role": "system",
            "content": planner.planner_system_prompt(task_main_text),
        }
    )
    planner_round_cap = planner.planner_initial_round_cap()
    # Planner sees ONLY discovery tools + propose_task_plan. If we
    # let it have the full schema it freelances with shell/edits and
    # never gets around to actually proposing a plan.
    planner_tool_schemas = _tool_schemas_for_names(
        tool_schemas, _PLANNER_DISCOVERY_TOOL_NAMES
    )
    final, exit_reason = _run_llm_phase(
        state=state,
        messages=messages,
        tools=tools,
        llm=llm,
        drive_logs=drive_logs,
        emit_progress=emit_progress,
        incoming_messages=incoming_messages,
        task_type=task_type,
        task_id=task_id,
        budget_remaining_usd=budget_remaining_usd,
        event_queue=event_queue,
        drive_root=drive_root,
        deadline_monotonic=deadline_monotonic,
        tool_schemas=planner_tool_schemas,
        stateful_executor=stateful_executor,
        repo_root=repo_root,
        verify_gate=verify_gate,
        owner_msg_seen=owner_msg_seen,
        max_retries=max_retries,
        max_global_rounds=max_global_rounds,
        max_rounds_label=max_rounds_label,
        phase_label="planner",
        terminating_tools=frozenset({"propose_task_plan"}),
        max_phase_rounds=planner_round_cap,
        terminate_on_text=False,
        memory_hooks=memory_hooks,
    )
    if final is not None:
        return final, None
    existing_plan = plan_store.load(task_id) if task_id else None
    if existing_plan is not None:
        return None, existing_plan

    planner_discovery_required = str(
        os.environ.get("OUROBOROS_REQUIRE_PLANNER_DISCOVERY") or ""
    ).strip().lower() not in {"0", "false", "no", "off"}
    rescue_needs_discovery = planner_discovery_required and (
        not state.discovery_plan_proposed or state.planner_discovery_calls <= 0
    )
    planner_rescue_tool_schemas = (
        planner_tool_schemas
        if rescue_needs_discovery
        else _tool_schemas_for_names(
            tool_schemas,
            {"propose_task_plan", "propose_discovery_plan"}
            if planner_discovery_required
            else {"propose_task_plan"},
        )
    )
    log.warning(
        "[LOOP] Planner phase ended with no plan (exit=%s); "
        "issuing one [PLANNER_RESCUE] retry before falling back "
        "to legacy linear path.",
        exit_reason,
    )
    messages.append(
        {
            "role": "system",
            "content": (
                "[PLANNER_RESCUE]\n"
                "Your previous response did not call `propose_task_plan`, "
                "so no plan was created. Without a plan the harness has "
                "to fall back to a degraded single-pass execution path. "
                "Please call `propose_task_plan` NOW with at least one "
                "subtask. The bare minimum is a single subtask describing "
                "the user's intent and at least one verification step "
                "(`run_workspace_command` or a deterministic check). "
                "If `propose_discovery_plan` or the planner discovery gate "
                "has not been satisfied, first store the discovery plan and "
                "call memory or one external lookup, then call `propose_task_plan`. "
                "If you genuinely believe no plan is needed (read-only "
                "exploration etc.), still emit a 1-subtask plan that "
                "marks the task as such — do not exit silently."
            ),
        }
    )
    final, exit_reason = _run_llm_phase(
        state=state,
        messages=messages,
        tools=tools,
        llm=llm,
        drive_logs=drive_logs,
        emit_progress=emit_progress,
        incoming_messages=incoming_messages,
        task_type=task_type,
        task_id=task_id,
        budget_remaining_usd=budget_remaining_usd,
        event_queue=event_queue,
        drive_root=drive_root,
        deadline_monotonic=deadline_monotonic,
        tool_schemas=planner_rescue_tool_schemas,
        stateful_executor=stateful_executor,
        repo_root=repo_root,
        verify_gate=verify_gate,
        owner_msg_seen=owner_msg_seen,
        max_retries=max_retries,
        max_global_rounds=max_global_rounds,
        max_rounds_label=max_rounds_label,
        phase_label="planner_rescue",
        terminating_tools=frozenset({"propose_task_plan"}),
        max_phase_rounds=planner_round_cap,
        terminate_on_text=False,
        force_tool_choice=None if rescue_needs_discovery else "propose_task_plan",
        memory_hooks=memory_hooks,
    )
    if final is not None:
        return final, None
    existing_plan = plan_store.load(task_id) if task_id else None
    if existing_plan is None:
        log.warning(
            "[LOOP] Planner rescue also produced no plan; "
            "degrading to legacy linear path."
        )
    return None, existing_plan


_SELF_REVIEW_TOOL_CALL_MARKERS: tuple[str, ...] = (
    "<tool_call>",
    "<arg_key>",
    "<arg_value>",
    '"function":',
    '"tool_calls":',
)


def _looks_like_pseudo_tool_call(text: str) -> bool:
    """Detect XML/JSON-like tool-call markup in a self-review reply.

    Some open models (notably GLM-4.7 on long, tool-heavy traces)
    respond to a tool-less self-review prompt by emitting their internal
    tool-call template as plain text — e.g. ``<tool_call>name<arg_key>...``.
    The caller treats this as a contract violation and retries with a
    stricter system message.
    """

    if not text:
        return False
    head = text.strip()[:600].lower()
    return any(marker in head for marker in (m.lower() for m in _SELF_REVIEW_TOOL_CALL_MARKERS))


def _self_review_starts_with_verdict(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip().upper()
    return head.startswith("LGTM") or head.startswith("NEEDS_FIX")


def _run_self_review_phase(
    *,
    messages: list[dict[str, Any]],
    llm: LLMClient,
    drive_logs: pathlib.Path,
    task_id: str,
    state: _LoopState,
    event_queue: queue.Queue | None,
    max_retries: int,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Self-review runs as a single text-only LLM round.

    Self-review needs *one* deterministic answer: either ``LGTM ...`` or
    ``NEEDS_FIX``. Routing it through the planner / subtask loop with
    full tool access makes the model spend rounds on tool calls (write,
    completion, etc.) and frequently emit malformed pseudo-tool-call
    markup that fails preflight 6+ times in a row, leaving an empty or
    invalid verdict — which the control plane then has to reject as
    ``failed_self_review``.

    This phase removes the foot-gun by calling the LLM with no tools
    at all (so ``tool_choice`` is irrelevant — the model literally
    cannot call anything) and prepending a strict system reminder. If
    the model still violates the contract (empty reply, XML tool-call
    markup, no LGTM/NEEDS_FIX prefix), we retry once with an even
    stricter reminder and append the bad reply as evidence.
    """

    enforcement = (
        "[SELF_REVIEW_PROTOCOL]\n"
        "You are in the FINAL self-review step of the run. The harness "
        "has stripped every tool from your schema for this turn — you "
        "literally cannot call a function. Reply with PLAIN TEXT only.\n"
        "The very FIRST non-whitespace token of your reply MUST be one "
        "of:\n"
        "  - `LGTM` followed by one short sentence accepting the run.\n"
        "  - `NEEDS_FIX` followed on the next lines by a numbered list "
        "of concrete fixes the next remediation cycle should apply.\n"
        "Do NOT emit `<tool_call>`, `<arg_key>`, JSON wrappers, or any "
        "markup. Do NOT prefix the verdict with prose, markdown headers, "
        "or code fences. The parser only inspects the first token."
    )

    review_messages = list(messages)
    review_messages.insert(0, {"role": "system", "content": enforcement})

    def _ask(prompt_messages: list[dict[str, Any]], round_idx: int) -> str:
        msg, _cost = _call_llm_with_retry(
            llm,
            prompt_messages,
            state.active_model,
            None,
            state.active_effort,
            state.active_max_tokens,
            None,
            "none",
            max_retries,
            drive_logs,
            task_id,
            round_idx,
            event_queue,
            state.accumulated_usage,
            task_type="self_review",
            phase_label="self_review",
        )
        if not msg:
            return ""
        return str(msg.get("content") or "").strip()

    text = _ask(review_messages, 0)
    contract_ok = bool(text) and _self_review_starts_with_verdict(text) and not _looks_like_pseudo_tool_call(text)

    if not contract_ok:
        bad_reply_excerpt = (text or "[empty response]")[:600]
        append_jsonl(
            drive_logs / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "self_review_contract_retry",
                "task_id": task_id,
                "phase": "self_review",
                "reason": (
                    "empty_response"
                    if not text
                    else (
                        "pseudo_tool_call"
                        if _looks_like_pseudo_tool_call(text)
                        else "missing_verdict_prefix"
                    )
                ),
                "bad_reply_excerpt": bad_reply_excerpt,
            },
        )
        retry_messages = list(messages)
        retry_messages.insert(
            0,
            {
                "role": "system",
                "content": (
                    enforcement
                    + "\n\n[RETRY] Your previous reply broke the contract. "
                    "It was rejected by the parser. Below is the literal "
                    "text you sent — do NOT repeat its shape. Reply NOW "
                    "with pure text starting with `LGTM` or `NEEDS_FIX`.\n"
                    f"--- previous bad reply ---\n{bad_reply_excerpt}\n--- end ---"
                ),
            },
        )
        text = _ask(retry_messages, 1)

    state.last_text = text or ""
    return state.last_text, state.accumulated_usage, state.llm_trace


def run_llm_loop(
    messages: list[dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str = "",
    task_id: str = "",
    budget_remaining_usd: float | None = None,
    event_queue: queue.Queue | None = None,
    initial_effort: str = "medium",
    drive_root: pathlib.Path | None = None,
    deadline_monotonic: float | None = None,
    remediation_attempt: int = 0,
    prebuilt_plan_id: str = "",
    memory_hooks: Any = None,
    self_review_attempt: int = 0,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Core LLM-with-tools loop.

    Decides between a *linear* phase (legacy behavior) and *adaptive
    planner orchestration* (planner round + sequential subtasks +
    review phases). The planner is gated by ``OUROBOROS_PLANNER_MODE``
    and the size of the task brief, so trivial chats keep the legacy
    path. See ``ouroboros.task_planner`` for the data model.

    Returns ``(final_text, accumulated_usage, llm_trace)``.
    """
    from ouroboros import task_planner as _planner
    from ouroboros.tools import tool_discovery as _td

    if memory_hooks is None:
        from ouroboros import memory_hooks as memory_hooks_module

        memory_hooks = memory_hooks_module
    _td.set_registry(tools)

    state = _LoopState(
        active_model=llm.default_model(),
        active_effort=initial_effort,
        active_max_tokens=_resolve_positive_int_env("OUROBOROS_MAX_TOKENS", 16384),
    )
    max_retries = _resolve_llm_loop_retries()

    if self_review_attempt and self_review_attempt > 0:
        tools._ctx.event_queue = event_queue
        tools._ctx.task_id = task_id
        append_jsonl(
            drive_logs / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "self_review_started",
                "task_id": task_id,
                "phase": "self_review",
                "attempt": int(self_review_attempt),
            },
        )
        return _run_self_review_phase(
            messages=messages,
            llm=llm,
            drive_logs=drive_logs,
            task_id=task_id,
            state=state,
            event_queue=event_queue,
            max_retries=max_retries,
        )

    tool_schemas = tools.schemas(core_only=True)
    tool_schemas, _enabled_extra_tools = _setup_dynamic_tools(
        tools, tool_schemas, messages
    )

    tools._ctx.event_queue = event_queue
    tools._ctx.task_id = task_id
    stateful_executor = _StatefulToolExecutor()
    owner_msg_seen: set = set()
    max_global_rounds, max_rounds_label = _resolve_max_rounds()

    verify_gate = VerifyGate()
    repo_root, initial_ws = memory_hooks.init_loop_memory(messages, tools._ctx)
    state.active_workspace_id = initial_ws

    plan_store = _planner.TaskPlanStore(
        pathlib.Path(drive_root or tools._ctx.drive_root)
    )
    existing_plan = plan_store.load(task_id) if task_id else None
    if existing_plan is None and prebuilt_plan_id:
        existing_plan = plan_store.load(prebuilt_plan_id)
    task_main_text = _extract_task_brief(messages)
    mode = _planner.planner_mode()
    in_external_remediation = bool(remediation_attempt and remediation_attempt > 0)
    use_planner = _planner.should_run_planner(
        mode=mode,
        task_main_text=task_main_text,
        has_existing_plan=existing_plan is not None,
    )

    # Expose the digest to the planner tool handler so propose_task_plan
    # can persist it without re-extracting from the message log.
    setattr(tools._ctx, "task_main_digest", task_main_text)
    setattr(tools._ctx, "active_workspace_id", state.active_workspace_id)
    _publish_plan_execution_context(
        tools,
        active_plan_id=(
            getattr(existing_plan, "task_id", "")
            if existing_plan is not None
            else (prebuilt_plan_id or task_id)
        ),
        plan_store_root=pathlib.Path(drive_root or tools._ctx.drive_root),
        task_id=task_id,
        phase="init",
    )

    try:
        if existing_plan is not None:
            messages.append(
                {
                    "role": "system",
                    "content": _planner.plan_progress_block(existing_plan),
                }
            )

        # Snapshot for phase isolation; see ``_phase_isolation_enabled``.
        base_messages: list[dict[str, Any]] = [dict(m) for m in messages]

        if use_planner and existing_plan is None:
            _publish_plan_execution_context(
                tools,
                active_plan_id=task_id,
                plan_store_root=pathlib.Path(drive_root or tools._ctx.drive_root),
                task_id=task_id,
                phase="planner",
            )
            final, existing_plan = _run_planner_phase_with_rescue(
                state=state,
                messages=messages,
                tools=tools,
                llm=llm,
                drive_logs=drive_logs,
                emit_progress=emit_progress,
                incoming_messages=incoming_messages,
                task_type=task_type,
                task_id=task_id,
                budget_remaining_usd=budget_remaining_usd,
                event_queue=event_queue,
                drive_root=drive_root,
                deadline_monotonic=deadline_monotonic,
                tool_schemas=tool_schemas,
                stateful_executor=stateful_executor,
                repo_root=repo_root,
                verify_gate=verify_gate,
                owner_msg_seen=owner_msg_seen,
                max_retries=max_retries,
                max_global_rounds=max_global_rounds,
                max_rounds_label=max_rounds_label,
                plan_store=plan_store,
                task_main_text=task_main_text,
                planner=_planner,
                memory_hooks=memory_hooks,
            )
            if final is not None:
                return final

        if existing_plan is not None:
            _publish_plan_execution_context(
                tools,
                active_plan_id=getattr(existing_plan, "task_id", "") or task_id,
                plan_store_root=pathlib.Path(drive_root or tools._ctx.drive_root),
                task_id=task_id,
                phase="subtasks",
            )
            return _drive_subtask_loop(
                state=state,
                messages=messages,
                tools=tools,
                llm=llm,
                drive_logs=drive_logs,
                emit_progress=emit_progress,
                incoming_messages=incoming_messages,
                task_type=task_type,
                task_id=task_id,
                budget_remaining_usd=budget_remaining_usd,
                event_queue=event_queue,
                drive_root=drive_root,
                deadline_monotonic=deadline_monotonic,
                tool_schemas=tool_schemas,
                stateful_executor=stateful_executor,
                repo_root=repo_root,
                verify_gate=verify_gate,
                owner_msg_seen=owner_msg_seen,
                max_retries=max_retries,
                max_global_rounds=max_global_rounds,
                max_rounds_label=max_rounds_label,
                plan_store=plan_store,
                base_messages=base_messages,
                in_external_remediation=in_external_remediation,
                external_remediation_attempt=int(remediation_attempt or 0),
                memory_hooks=memory_hooks,
            )

        # Legacy linear path — no plan, no planner. We require at least one
        # workspace-write tool call before allowing a text-only exit, so the
        # model can no longer silently surrender on a task that produced
        # zero deliverables.
        final, exit_reason = _run_llm_phase(
            state=state,
            messages=messages,
            tools=tools,
            llm=llm,
            drive_logs=drive_logs,
            emit_progress=emit_progress,
            incoming_messages=incoming_messages,
            task_type=task_type,
            task_id=task_id,
            budget_remaining_usd=budget_remaining_usd,
            event_queue=event_queue,
            drive_root=drive_root,
            deadline_monotonic=deadline_monotonic,
            tool_schemas=tool_schemas,
            stateful_executor=stateful_executor,
            repo_root=repo_root,
            verify_gate=verify_gate,
            owner_msg_seen=owner_msg_seen,
            max_retries=max_retries,
            max_global_rounds=max_global_rounds,
            max_rounds_label=max_rounds_label,
            phase_label="linear",
            terminating_tools=frozenset(),
            max_phase_rounds=0,
            terminate_on_text=True,
            require_writes_before_text_exit=True,
            memory_hooks=memory_hooks,
        )
        if final is not None:
            return final
        return state.last_text, state.accumulated_usage, state.llm_trace
    finally:
        _loop_teardown(stateful_executor, tools, drive_root, task_id)


def _phase_isolation_enabled() -> bool:
    """Default-on. Set ``OUROBOROS_ISOLATE_PHASES=0`` to keep the shared buffer."""
    raw = str(os.environ.get("OUROBOROS_ISOLATE_PHASES", "1")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _build_phase_messages(
    base_messages: list[dict[str, Any]],
    extra_blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fresh: list[dict[str, Any]] = [dict(m) for m in base_messages]
    fresh.extend(extra_blocks)
    return fresh


def _plan_needs_remediation(plan: Any, planner: Any) -> bool:
    return plan is not None and any(
        str(getattr(subtask, "status", ""))
        in {
            planner.SUBTASK_STATUS_SKIPPED,
            planner.SUBTASK_STATUS_FAILED,
        }
        for subtask in plan.subtasks
    )


def _final_aggregation_block(
    plan: Any, state: _LoopState, planner: Any
) -> dict[str, Any]:
    final_summary = planner.plan_progress_block(plan)
    verify_block = ""
    if state.last_verify_summary:
        trimmed_summary = state.last_verify_summary
        if len(trimmed_summary) > 3000:
            trimmed_summary = trimmed_summary[:3000].rstrip() + "\n[summary truncated]"
        verify_block = (
            "\n\n[VERIFICATION_REPORT]\n"
            f"run_id={state.last_verify_run_id or 'unknown'} "
            f"passed={state.last_verify_passed} "
            f"failed_required_steps={state.last_verify_failed_count}\n"
            f"{trimmed_summary}"
        )
    return {
        "role": "system",
        "content": (
            "[FINAL_AGGREGATION]\n"
            "All planned subtasks are complete. Compose the final answer for the "
            "task originator. Synthesise findings across subtasks. Do NOT call any "
            "tool — emit a single text reply. The verification report below is the "
            "ground truth; cite it directly instead of attempting to re-verify.\n\n"
            f"{final_summary}{verify_block}"
        ),
    }


def _subtask_focus_content(
    *,
    plan: Any,
    planner: Any,
    tools: ToolRegistry,
    state: _LoopState,
    rescue_count: int,
) -> str:
    focus_content = planner.focus_block(
        plan,
        workspace_root=_resolve_workspace_root_for_focus(tools, state),
        noise_paths=_resolve_noise_paths_for_focus(tools, state),
    )
    if rescue_count:
        focus_content = (
            f"{focus_content}\n\n"
            "[SUBTASK_RESCUE_CONTINUATION]\n"
            "The previous attempt hit a phase/tool cap before this subtask was completed. "
            "Do not skip it automatically. Read the latest tool error or partial output, "
            "correct the tool call/implementation, and keep working on this same subtask "
            "until its success_check is satisfied or you can explicitly report a real failure."
        )
    return focus_content


def _prepare_current_subtask_state(
    *,
    plan: Any,
    state: _LoopState,
    tools: ToolRegistry,
    cursor_at_start: int,
) -> Any:
    current_subtask = plan.current() if plan is not None else None
    if current_subtask is not None:
        new_subtask_id = (
            getattr(current_subtask, "id", "") or f"subtask_{cursor_at_start + 1}"
        )
        if state.current_subtask_id != new_subtask_id:
            state.current_subtask_id = new_subtask_id
            state.current_subtask_discovery_calls = 0
            state.current_subtask_external_discovery_calls = 0
            state.last_memory_recall_empty = False
        tags = list(getattr(current_subtask, "tags", []) or [])
        _publish_subtask_tags_to_tool_ctx(tools, tags)
    return current_subtask


def _subtask_phase_tool_schemas(
    *,
    tool_schemas: list[dict[str, Any]],
    in_external_remediation: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    subtask_tool_name_set = (
        _REMEDIATION_TOOL_NAMES if in_external_remediation else _SUBTASK_TOOL_NAMES
    )
    return (
        _tool_schemas_for_names(tool_schemas, subtask_tool_name_set),
        _tool_schemas_for_names(tool_schemas, _REMEDIATION_TOOL_NAMES),
        _tool_schemas_for_names(tool_schemas, _REVIEW_TOOL_NAMES),
    )


def _subtask_phase_label(
    *, in_external_remediation: bool, attempt: int, cursor: int
) -> str:
    if in_external_remediation and attempt > 0:
        return f"remediation_{attempt}_subtask_{cursor + 1}"
    return f"subtask_{cursor + 1}"


def _drive_subtask_loop(
    *,
    state: _LoopState,
    messages: list[dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str,
    task_id: str,
    budget_remaining_usd: float | None,
    event_queue: queue.Queue | None,
    drive_root: pathlib.Path | None,
    deadline_monotonic: float | None,
    tool_schemas: list[dict[str, Any]],
    stateful_executor: "_StatefulToolExecutor",
    repo_root: Any,
    verify_gate: VerifyGate,
    owner_msg_seen: set,
    max_retries: int,
    max_global_rounds: int,
    max_rounds_label: str,
    plan_store,
    base_messages: list[dict[str, Any]] | None = None,
    in_external_remediation: bool = False,
    external_remediation_attempt: int = 0,
    memory_hooks: Any = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    from ouroboros import task_planner as _planner

    subtask_tool_schemas, remediation_tool_schemas, review_tool_schemas = (
        _subtask_phase_tool_schemas(
            tool_schemas=tool_schemas, in_external_remediation=in_external_remediation
        )
    )
    isolate_phases = bool(base_messages) and _phase_isolation_enabled()

    def _phase_messages(extra_block: dict[str, Any]) -> list[dict[str, Any]]:
        if isolate_phases:
            return _build_phase_messages(base_messages or [], [extra_block])
        messages.append(extra_block)
        return messages

    plan = plan_store.load(task_id)
    safety = 0
    subtask_rescue_counts: dict[int, int] = {}
    while plan is not None and not plan.is_complete():
        safety += 1
        if safety > max(8 * len(plan.subtasks), 64):
            log.warning("Subtask loop safety break (safety=%d).", safety)
            break

        cursor_at_start = plan.cursor
        plan_store.start_current(plan)
        plan = plan_store.load(task_id)
        rescue_count = subtask_rescue_counts.get(cursor_at_start, 0)
        current_subtask = _prepare_current_subtask_state(
            plan=plan,
            state=state,
            tools=tools,
            cursor_at_start=cursor_at_start,
        )
        subtask_phase_label = _subtask_phase_label(
            in_external_remediation=in_external_remediation,
            attempt=external_remediation_attempt,
            cursor=cursor_at_start,
        )
        _publish_plan_execution_context(
            tools,
            active_plan_id=getattr(plan, "task_id", "") or task_id,
            plan_store_root=pathlib.Path(
                drive_root or getattr(tools._ctx, "drive_root", "")
            ),
            task_id=task_id,
            phase=subtask_phase_label,
            subtask_id=str(getattr(current_subtask, "id", "") or ""),
        )
        _reset_subtask_tool_state(tools)
        read_only_progress_allowed = _subtask_allows_read_only_progress(current_subtask)
        subtask_messages = _phase_messages(
            {
                "role": "system",
                "content": _subtask_focus_content(
                    plan=plan,
                    planner=_planner,
                    tools=tools,
                    state=state,
                    rescue_count=rescue_count,
                ),
            }
        )

        phase_cap = 0 if rescue_count else _planner.planner_phase_round_cap()
        final, exit_reason = _run_llm_phase(
            state=state,
            messages=subtask_messages,
            tools=tools,
            llm=llm,
            drive_logs=drive_logs,
            emit_progress=emit_progress,
            incoming_messages=incoming_messages,
            task_type=task_type,
            task_id=task_id,
            budget_remaining_usd=budget_remaining_usd,
            event_queue=event_queue,
            drive_root=drive_root,
            deadline_monotonic=deadline_monotonic,
            tool_schemas=subtask_tool_schemas,
            stateful_executor=stateful_executor,
            repo_root=repo_root,
            verify_gate=verify_gate,
            owner_msg_seen=owner_msg_seen,
            max_retries=max_retries,
            max_global_rounds=max_global_rounds,
            max_rounds_label=max_rounds_label,
            phase_label=subtask_phase_label,
            terminating_tools=frozenset({"mark_subtask_complete"}),
            max_phase_rounds=phase_cap,
            terminate_on_text=False,
            require_writes_before_text_exit=not read_only_progress_allowed,
            memory_hooks=memory_hooks,
        )
        if final is not None:
            return final

        plan = plan_store.load(task_id)
        if plan is None:
            break
        if plan.cursor == cursor_at_start:
            subtask_rescue_counts[cursor_at_start] = rescue_count + 1
            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "subtask_rescue_continuation",
                    "task_id": task_id,
                    "phase": subtask_phase_label,
                    "reason": exit_reason,
                    "rescue_count": subtask_rescue_counts[cursor_at_start],
                },
            )
            continue

        last_done = (
            plan.subtasks[cursor_at_start]
            if cursor_at_start < len(plan.subtasks)
            else None
        )
        try:
            memory_hooks.mirror_subtask_to_memory(
                plan=plan,
                subtask=last_done,
                repo_root=repo_root,
                workspace_id=state.active_workspace_id,
            )
        except Exception:
            log.debug("mirror_subtask_to_memory failed", exc_info=True)

        if plan.is_complete():
            break

        review_messages = _phase_messages(
            {"role": "system", "content": _planner.review_block(plan, last_done)}
        )
        _publish_plan_execution_context(
            tools,
            active_plan_id=getattr(plan, "task_id", "") or task_id,
            plan_store_root=pathlib.Path(
                drive_root or getattr(tools._ctx, "drive_root", "")
            ),
            task_id=task_id,
            phase=f"review_{cursor_at_start + 1}",
            subtask_id=str(getattr(last_done, "id", "") or ""),
        )
        review_cap = _planner.planner_review_round_cap()
        final, exit_reason = _run_llm_phase(
            state=state,
            messages=review_messages,
            tools=tools,
            llm=llm,
            drive_logs=drive_logs,
            emit_progress=emit_progress,
            incoming_messages=incoming_messages,
            task_type=task_type,
            task_id=task_id,
            budget_remaining_usd=budget_remaining_usd,
            event_queue=event_queue,
            drive_root=drive_root,
            deadline_monotonic=deadline_monotonic,
            tool_schemas=review_tool_schemas,
            stateful_executor=stateful_executor,
            repo_root=repo_root,
            verify_gate=verify_gate,
            owner_msg_seen=owner_msg_seen,
            max_retries=max_retries,
            max_global_rounds=max_global_rounds,
            max_rounds_label=max_rounds_label,
            phase_label=f"review_{cursor_at_start + 1}",
            terminating_tools=frozenset({"revise_remaining_plan"}),
            max_phase_rounds=review_cap,
            terminate_on_text=True,
            memory_hooks=memory_hooks,
        )
        if final is not None:
            return final
        plan = plan_store.load(task_id)

    if _plan_needs_remediation(plan, _planner):
        _publish_plan_execution_context(
            tools,
            active_plan_id=getattr(plan, "task_id", "") or task_id,
            plan_store_root=pathlib.Path(
                drive_root or getattr(tools._ctx, "drive_root", "")
            ),
            task_id=task_id,
            phase="remediation",
        )
        remediation_messages = _phase_messages(
            {"role": "system", "content": _planner.remediation_block(plan)}
        )
        final, exit_reason = _run_llm_phase(
            state=state,
            messages=remediation_messages,
            tools=tools,
            llm=llm,
            drive_logs=drive_logs,
            emit_progress=emit_progress,
            incoming_messages=incoming_messages,
            task_type=task_type,
            task_id=task_id,
            budget_remaining_usd=budget_remaining_usd,
            event_queue=event_queue,
            drive_root=drive_root,
            deadline_monotonic=deadline_monotonic,
            tool_schemas=remediation_tool_schemas,
            stateful_executor=stateful_executor,
            repo_root=repo_root,
            verify_gate=verify_gate,
            owner_msg_seen=owner_msg_seen,
            max_retries=max_retries,
            max_global_rounds=max_global_rounds,
            max_rounds_label=max_rounds_label,
            phase_label="remediation",
            terminating_tools=frozenset({"mark_remediation_complete"}),
            max_phase_rounds=_planner.planner_remediation_round_cap(),
            terminate_on_text=False,
            require_writes_before_text_exit=True,
            memory_hooks=memory_hooks,
        )
        if final is not None:
            return final
        plan = plan_store.load(task_id)

    if plan is not None:
        final_messages = _phase_messages(
            _final_aggregation_block(plan, state, _planner)
        )
    else:
        final_messages = messages
    final, exit_reason = _run_llm_phase(
        state=state,
        messages=final_messages,
        tools=tools,
        llm=llm,
        drive_logs=drive_logs,
        emit_progress=emit_progress,
        incoming_messages=incoming_messages,
        task_type=task_type,
        task_id=task_id,
        budget_remaining_usd=budget_remaining_usd,
        event_queue=event_queue,
        drive_root=drive_root,
        deadline_monotonic=deadline_monotonic,
        tool_schemas=[],
        stateful_executor=stateful_executor,
        repo_root=repo_root,
        verify_gate=verify_gate,
        owner_msg_seen=owner_msg_seen,
        max_retries=max_retries,
        max_global_rounds=max_global_rounds,
        max_rounds_label=max_rounds_label,
        phase_label="final_aggregation",
        terminating_tools=frozenset(),
        # Tier 1.2: bumped from 2 -> 3 to give the pseudo-text-nudge a
        # round to recover after a single botched attempt without
        # blowing budget on stubborn models.
        max_phase_rounds=3,
        terminate_on_text=True,
        memory_hooks=memory_hooks,
    )
    if final is not None:
        return final
    return state.last_text, state.accumulated_usage, state.llm_trace


def _emit_llm_usage_event(
    event_queue: queue.Queue | None,
    task_id: str,
    model: str,
    usage: dict[str, Any],
    cost: float,
    category: str = "task",
) -> None:
    """
    Emit llm_usage event to the event queue.

    Args:
        event_queue: Queue to emit events to (may be None)
        task_id: Task ID for the event
        model: Model name used for the LLM call
        usage: Usage dict from LLM response
        cost: Calculated cost for this call
        category: Budget category (task, evolution, consciousness, review, summarize, other)
    """
    if not event_queue:
        return
    try:
        event_queue.put_nowait(
            {
                "type": "llm_usage",
                "ts": utc_now_iso(),
                "task_id": task_id,
                "model": model,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "cached_tokens": int(usage.get("cached_tokens") or 0),
                "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
                "cost": cost,
                "cost_estimated": not bool(usage.get("cost")),
                "usage": usage,
                "category": category,
            }
        )
    except Exception:
        log.debug("Failed to put llm_usage event to queue", exc_info=True)


def _call_llm_with_retry(
    llm: LLMClient,
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None,
    effort: str,
    max_tokens: int,
    temperature: float | None,
    tool_choice: Any,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    round_idx: int,
    event_queue: queue.Queue | None,
    accumulated_usage: dict[str, Any],
    task_type: str = "",
    phase_label: str = "",
) -> tuple[dict[str, Any] | None, float]:
    """
    Call LLM with retry logic, usage tracking, and event emission.

    Returns:
        (response_message, cost) on success
        (None, 0.0) on failure after max_retries
    """
    msg = None
    last_error: Exception | None = None

    for attempt in range(max_retries):
        try:
            input_snapshot = _round_input_snapshot(messages)
            kwargs = {
                "messages": messages,
                "model": model,
                "reasoning_effort": effort,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = tool_choice
            resp_msg, usage = llm.chat(**kwargs)
            msg = resp_msg
            add_usage(accumulated_usage, usage)

            # Calculate cost and emit event for EVERY attempt (including retries)
            cost = float(usage.get("cost") or 0)
            if not cost:
                cost = _estimate_cost(
                    model,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    int(usage.get("cached_tokens") or 0),
                    int(usage.get("cache_write_tokens") or 0),
                )

            # Emit real-time usage event with category based on task_type
            category = (
                task_type
                if task_type in ("evolution", "consciousness", "review", "summarize")
                else "task"
            )
            _emit_llm_usage_event(event_queue, task_id, model, usage, cost, category)

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")
            if not tool_calls and (not content or not content.strip()):
                log.warning(
                    "LLM returned empty response (no content, no tool_calls), attempt %d/%d",
                    attempt + 1,
                    max_retries,
                )

                append_jsonl(
                    drive_logs / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "llm_empty_response",
                        "task_id": task_id,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "raw_content": repr(content)[:500] if content else None,
                        "raw_tool_calls": repr(tool_calls)[:500]
                        if tool_calls
                        else None,
                        "finish_reason": msg.get("finish_reason")
                        or msg.get("stop_reason"),
                    },
                )

                if attempt < max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                return None, cost

            accumulated_usage["rounds"] = accumulated_usage.get("rounds", 0) + 1

            _round_event = {
                "ts": utc_now_iso(),
                "type": "llm_round",
                "task_id": task_id,
                "round": round_idx,
                "model": model,
                "phase": phase_label,
                "reasoning_effort": effort,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "tool_choice": tool_choice if tools else "none",
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "cached_tokens": int(usage.get("cached_tokens") or 0),
                "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
                "cost_usd": cost,
            }
            append_jsonl(drive_logs / "events.jsonl", _round_event)
            _append_round_io(
                drive_logs,
                task_id=task_id,
                round_idx=round_idx,
                round_event=_round_event,
                input_snapshot=input_snapshot,
                msg=msg,
            )
            return msg, cost

        except Exception as e:
            last_error = e
            error_kind = _classify_llm_error(e)
            if error_kind in {"context_limit", "model_not_found"}:
                accumulated_usage["_last_llm_error_kind"] = error_kind
                append_jsonl(
                    drive_logs / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "llm_api_error",
                        "task_id": task_id,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "error": repr(e),
                        "error_kind": error_kind,
                        "backoff_sec": 0,
                        "retryable": False,
                    },
                )
                log.warning(
                    "[LLM] Non-retryable API error on attempt %d/%d (round %d): %s",
                    attempt + 1,
                    max_retries,
                    round_idx,
                    e,
                )
                break
            is_server_error = error_kind == "server_transient"
            backoff_cap = 90 if is_server_error else 30
            sleep_sec = min(2**attempt * 2, backoff_cap)
            log.warning(
                "[LLM] API error on attempt %d/%d (round %d): %s. %s",
                attempt + 1,
                max_retries,
                round_idx,
                e,
                f"Retrying in {sleep_sec}s..."
                if attempt < max_retries - 1
                else "No more retries.",
            )
            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "llm_api_error",
                    "task_id": task_id,
                    "round": round_idx,
                    "attempt": attempt + 1,
                    "model": model,
                    "error": repr(e),
                    "backoff_sec": sleep_sec,
                },
            )
            if attempt < max_retries - 1:
                log.warning(
                    "[LLM] Attempt %d/%d failed (%s), retrying in %ds",
                    attempt + 1,
                    max_retries,
                    type(e).__name__,
                    sleep_sec,
                )
                time.sleep(sleep_sec)

    if last_error is not None:
        accumulated_usage["_last_llm_error"] = repr(last_error)
    return None, 0.0


def _process_tool_results(
    results: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    llm_trace: dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """
    Process tool execution results and append to messages/trace.

    Args:
        results: List of tool execution result dicts
        messages: Message list to append tool results to
        llm_trace: Trace dict to append tool call info to
        emit_progress: Callback for progress updates

    Returns:
        Number of errors encountered
    """
    error_count = 0

    for exec_result in results:
        fn_name = exec_result["fn_name"]
        is_error = exec_result["is_error"]

        if is_error:
            error_count += 1

        truncated_result = _truncate_tool_result(exec_result["result"])

        messages.append(
            {
                "role": "tool",
                "tool_call_id": exec_result["tool_call_id"],
                "content": truncated_result,
            }
        )

        trace_entry = {
            "tool": fn_name,
            "args": _safe_args(exec_result["args_for_log"]),
            "result": truncate_for_log(exec_result["result"], 700),
            "is_error": is_error,
        }
        if fn_name in _FULL_RESULT_TRACE_TOOLS:
            trace_entry["result_full"] = str(exec_result["result"])
        llm_trace["tool_calls"].append(trace_entry)

    return error_count


def _safe_args(v: Any) -> Any:
    """Ensure args are JSON-serializable for trace logging."""
    try:
        return json.loads(json.dumps(v, ensure_ascii=False, default=str))
    except Exception:
        log.debug("Failed to serialize args for trace logging", exc_info=True)
        return {"_repr": repr(v)}
