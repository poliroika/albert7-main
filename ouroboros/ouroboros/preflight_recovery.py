"""Preflight error recovery + auto-repair for shaky tool callers.

Some models (notably GLM-4.7) intermittently emit tool calls in two
broken shapes that our regular ``repair_tool_arguments`` can't fix:

1.  ``fn_name`` is polluted with pseudo-XML, e.g.::

        update_workspace_seed</arg_value>allow_large_overwrite</arg_key><arg_value>false</arg_value>

    Here the model meant to call ``update_workspace_seed`` and embed
    a boolean argument in pseudo-XML form. Without recovery the call
    is rejected with ``unknown tool`` and the round is wasted.

2.  Repeated preflight errors on the same ``(tool, phase)`` pair
    where the model keeps omitting the same required field
    (``file_path``, ``new_content``, …). The generic
    ``schema_hint`` is too abstract to unstick the model — a *real*
    successful example from earlier in the SAME run is far more
    actionable.

This module is intentionally small and pure so it can be unit-tested
without spinning up the LLM loop. It is consumed from ``loop.py``
inside ``_execute_single_tool``.
"""

import json
import pathlib
import re
import threading
from typing import Any

from collections.abc import Iterable

__all__ = [
    "extract_pseudo_xml_args",
    "recent_successful_args",
    "format_examples_for_prompt",
    "PreflightErrorTracker",
]

# Canonical form: ``<arg_key>NAME</arg_key><arg_value>VALUE</arg_value>``.
_ARG_PAIR_CANONICAL_RE = re.compile(
    r"<arg_key>\s*([^<]+?)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*</arg_value>",
    re.DOTALL | re.IGNORECASE,
)
# Lenient form: GLM regularly forgets the opening ``<arg_key>`` and emits
# ``KEY</arg_key><arg_value>VALUE</arg_value>``. We accept any non-XML
# token as the key as long as it is followed by ``</arg_key>``.
_ARG_PAIR_LENIENT_RE = re.compile(
    r"(?:^|[\s>])\s*([A-Za-z_][A-Za-z0-9_]*)\s*</arg_key>\s*<arg_value>\s*(.*?)\s*</arg_value>",
    re.DOTALL | re.IGNORECASE,
)
# Detect any pseudo-XML noise so we know to clean it from fn_name
_PSEUDO_XML_NOISE_RE = re.compile(
    r"</?(?:arg_key|arg_value|tool_call|function_call)>", re.IGNORECASE
)
_PSEUDO_TOOL_NAME_RE = re.compile(
    r"<(?:tool_call|function_call)>\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _coerce_arg_value(raw: str) -> Any:
    """Best-effort scalar coercion for values pulled out of pseudo-XML.

    Most models emit JSON-ish content in these slots. We try strict JSON
    first (so ``true``/``false``/numbers/objects/arrays decode
    correctly) and fall back to the raw stripped string.
    """
    text = (raw or "").strip()
    if text == "":
        return ""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        # Common case: bare boolean/integer literals without JSON quotes.
        low = text.lower()
        if low in ("true", "false"):
            return low == "true"
        if low in ("null", "none"):
            return None
        try:
            if "." in text:
                return float(text)
            return int(text)
        except (TypeError, ValueError):
            return text


def extract_pseudo_xml_args(
    raw_fn_name: str | None,
    raw_args_str: str | None,
) -> tuple[str, dict[str, Any]]:
    """Strip pseudo-XML from ``fn_name`` and recover any embedded args.

    Returns ``(clean_fn_name, recovered_args_dict)``. ``clean_fn_name``
    is the bit before any pseudo-XML marker; ``recovered_args_dict`` is
    the union of pairs found in BOTH ``raw_fn_name`` and
    ``raw_args_str`` (since GLM splits the mess across the two fields).

    If neither source contains pseudo-XML, returns
    ``(raw_fn_name, {})`` and the caller can proceed normally.

    Examples
    --------
    >>> extract_pseudo_xml_args(
    ...     "update_workspace_seed</arg_value>flag</arg_key><arg_value>false</arg_value>",
    ...     "{}",
    ... )
    ('update_workspace_seed', {'flag': False})
    """
    name = (raw_fn_name or "").strip()
    args: dict[str, Any] = {}

    # Sources to scan: the polluted name itself + the raw args string.
    sources: list[str] = []
    if name and ("<" in name or ">" in name):
        sources.append(name)
    raw_args_text = raw_args_str or ""
    if raw_args_text and (
        "<arg_key>" in raw_args_text.lower() or "</arg_key>" in raw_args_text.lower()
    ):
        sources.append(raw_args_text)

    explicit_tool_name = ""
    for src in sources:
        if not explicit_tool_name:
            tool_match = _PSEUDO_TOOL_NAME_RE.search(src)
            if tool_match:
                explicit_tool_name = (tool_match.group(1) or "").strip()
        # Try the canonical shape first so we don't double-match the
        # same pair through the lenient pattern.
        canonical_spans: set[tuple[int, int]] = set()
        for match in _ARG_PAIR_CANONICAL_RE.finditer(src):
            key = (match.group(1) or "").strip()
            if not key:
                continue
            value = _coerce_arg_value(match.group(2) or "")
            args.setdefault(key, value)
            canonical_spans.add(match.span())
        # Lenient pass picks up ``KEY</arg_key><arg_value>VAL</arg_value>``
        # variants where the model dropped the opening ``<arg_key>``.
        for match in _ARG_PAIR_LENIENT_RE.finditer(src):
            if any(start <= match.start() < end for start, end in canonical_spans):
                continue
            key = (match.group(1) or "").strip()
            if not key:
                continue
            value = _coerce_arg_value(match.group(2) or "")
            args.setdefault(key, value)

    if explicit_tool_name:
        name = explicit_tool_name
    else:
        # Strip everything from the first ``<`` or ``(`` onward — that's
        # where the pseudo-XML / call-form pollution starts.
        if "<" in name or "(" in name:
            name = re.split(r"[<(]", name, maxsplit=1)[0].strip()
        name = _PSEUDO_XML_NOISE_RE.sub("", name).strip()

    return name, args


def recent_successful_args(
    drive_logs: pathlib.Path,
    fn_name: str,
    *,
    n: int = 2,
    task_id: str | None = None,
    max_lines: int = 800,
) -> list[dict[str, Any]]:
    """Return up to ``n`` recent successful arg dicts for ``fn_name``.

    Reads the tail of ``tools.jsonl`` (last ``max_lines`` entries) and
    keeps the most recent successful invocations of the same tool from
    the same ``task_id`` (when provided). "Successful" here is defined
    as: ``args`` is a non-empty dict and ``result_preview`` does NOT
    look like an error.

    The returned list is in reverse chronological order — newest first.
    The caller can pick the top-1 to embed in the error message.
    """
    path = pathlib.Path(drive_logs) / "tools.jsonl"
    if not path.exists() or n <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if not lines:
        return []
    # Walk the tail backwards so we naturally get the newest entries.
    examples: list[dict[str, Any]] = []
    for raw in reversed(lines[-max_lines:]):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if str(entry.get("tool") or "") != fn_name:
            continue
        if task_id and str(entry.get("task_id") or "") != task_id:
            continue
        args = entry.get("args")
        if not isinstance(args, dict) or not args:
            continue
        preview = str(entry.get("result_preview") or "")
        # Heuristic: skip entries whose result clearly indicates an error.
        # We cannot trust exit codes here (no schema), so look for the
        # canonical warning prefixes the loop emits.
        if preview.startswith(("WARNING:", "⚠️", "ERROR:", "STOP_REQUESTED")):
            continue
        examples.append(args)
        if len(examples) >= n:
            break
    return examples


def format_examples_for_prompt(
    examples: Iterable[dict[str, Any]],
    *,
    max_per_field_chars: int = 200,
) -> str:
    """Render successful examples as a compact JSON snippet for the
    error message.

    Long string fields (``new_content`` of a 50KB file, etc.) are
    truncated so we don't blow the model's context. The point is to
    show the *shape* of a working call, not to dump real content.
    """
    rendered: list[str] = []
    for idx, args in enumerate(examples, start=1):
        if not isinstance(args, dict):
            continue
        compact: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(value, str) and len(value) > max_per_field_chars:
                tail = " …[+%d chars]" % (len(value) - max_per_field_chars)
                compact[key] = value[:max_per_field_chars] + tail
            else:
                compact[key] = value
        try:
            rendered.append(
                f"Example #{idx} (this run): {json.dumps(compact, ensure_ascii=False)}"
            )
        except (TypeError, ValueError):
            continue
    if not rendered:
        return ""
    return "\n".join(rendered)


class PreflightErrorTracker:
    """Process-local counter of consecutive preflight errors per (task, tool, phase).

    Keys used by the loop are ``(task_id, fn_name, phase_label)``. The
    counter resets to 0 the moment ANY successful tool call lands for
    that task — that signal comes through ``record_success(task_id)``.

    Thread-safe so the small chance of parallel candidate workers in
    the harness doesn't corrupt the dict.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, str], int] = {}
        self._lock = threading.Lock()

    def bump(self, task_id: str, fn_name: str, phase: str) -> int:
        key = (task_id or "", fn_name or "", phase or "")
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            return self._counts[key]

    def reset(self, task_id: str, fn_name: str, phase: str) -> None:
        key = (task_id or "", fn_name or "", phase or "")
        with self._lock:
            self._counts.pop(key, None)

    def record_success(self, task_id: str) -> None:
        """Drop every counter associated with ``task_id`` — any successful
        call means whatever was stuck has unstuck."""
        if not task_id:
            return
        with self._lock:
            stale = [key for key in self._counts if key[0] == task_id]
            for key in stale:
                self._counts.pop(key, None)

    def current(self, task_id: str, fn_name: str, phase: str) -> int:
        key = (task_id or "", fn_name or "", phase or "")
        with self._lock:
            return self._counts.get(key, 0)

    def reset_all(self) -> None:
        """For tests."""
        with self._lock:
            self._counts.clear()
