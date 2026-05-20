"""Robust JSON repair for LLM-generated tool-call arguments.

Some upstream LLM proxies return ``function.arguments`` strings that are
*almost* JSON but contain raw control characters, trailing commas, single
quotes, or even Python-literal-style payloads.  Strict ``json.loads`` rejects
all of these and we lose the entire tool call.  This module attempts a
sequence of progressively more lenient parses and reports which strategy
succeeded.

The contract is intentionally narrow: we always return a ``dict`` (the tool
argument mapping) plus a short note.  Callers can rely on the note to
surface telemetry / logs without having to mirror the parsing fallbacks.
"""

import ast
import json
import re
import shlex
from typing import Any


def _fix_control_chars_inside_json_strings(s: str) -> str:
    """Escape raw \\n / \\r / \\t inside JSON string literals.

    LLMs sometimes emit literal newlines inside string values (``"a\\n"`` is
    parsed as a 2-char string by Python before it even reaches us). This
    walker preserves structural whitespace outside of strings and only
    rewrites bytes that fall inside a ``"..."`` literal.
    """

    out: list[str] = []
    in_str = False
    escape = False
    for ch in s:
        if escape:
            out.append(ch)
            escape = False
            continue
        if ch == "\\" and in_str:
            out.append(ch)
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str and ch == "\n":
            out.append("\\n")
            continue
        if in_str and ch == "\r":
            out.append("\\r")
            continue
        if in_str and ch == "\t":
            out.append("\\t")
            continue
        out.append(ch)
    return "".join(out)


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _strip_trailing_commas(s: str) -> str:
    return _TRAILING_COMMA.sub(r"\1", s)


def _try_python_literal(s: str) -> dict[str, Any] | None:
    """Last resort: parse as a Python literal (handles single quotes, ``True``)."""

    try:
        value = ast.literal_eval(s)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    if isinstance(value, dict):
        # Guarantee str keys for downstream JSON-schema validators.
        return {str(k): v for k, v in value.items()}
    return None


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


def _repair_interpreter_payload_quotes(argv: list[str]) -> tuple[list[str], str | None]:
    normalized = [str(part) for part in argv]
    changed = False

    def _unwrap_at(index: int) -> None:
        nonlocal changed
        if index >= len(normalized):
            return
        repaired = _strip_balanced_outer_quotes(normalized[index])
        if repaired != normalized[index]:
            normalized[index] = repaired
            changed = True

    head = normalized[:4]
    lowered = [part.lower() for part in head]

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

    if changed:
        return normalized, "unwrapped_interpreter_payload_quotes"
    return normalized, None


def _normalize_run_workspace_command_args(
    args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Normalize legacy / weakly-typed run_workspace_command payloads.

    Preferred schema is:
        {"workspace_id": "...", "argv": ["python", "-m", "pytest"], ...}

    Older models frequently emit `command` instead of `argv`, and some emit the
    argv payload as a single shell string. We coerce those shapes into the
    canonical `argv` form so downstream execution and later strict-proxy passes
    see a stable JSON object.
    """
    if not isinstance(args, dict):
        return args, None

    if "argv" in args:
        argv = args.get("argv")
        if isinstance(argv, list):
            coerced = [str(part) for part in argv]
            type_changed = any(not isinstance(orig, str) for orig in argv)
            args["argv"] = coerced
            args["argv"], extra = _repair_interpreter_payload_quotes(args["argv"])
            if type_changed and extra:
                return args, f"normalized_argv_list+{extra}"
            if type_changed:
                return args, "normalized_argv_list"
            if extra:
                return args, extra
            return args, None
        if isinstance(argv, str):
            stripped = argv.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    args["argv"] = [str(part) for part in parsed]
                    note = "parsed_argv_json_string"
                    args["argv"], extra = _repair_interpreter_payload_quotes(
                        args["argv"]
                    )
                    if extra:
                        note = f"{note}+{extra}"
                    return args, note
            try:
                args["argv"] = shlex.split(argv)
            except ValueError:
                args["argv"] = argv.split()
            note = "split_argv_string"
            args["argv"], extra = _repair_interpreter_payload_quotes(args["argv"])
            if extra:
                note = f"{note}+{extra}"
            return args, note

    if "args" in args and "argv" not in args and "command" not in args:
        raw_args = args.pop("args")
        if isinstance(raw_args, list):
            args["argv"] = [str(part) for part in raw_args]
            note = "mapped_args_list_to_argv"
        elif isinstance(raw_args, str):
            try:
                args["argv"] = shlex.split(raw_args)
            except ValueError:
                args["argv"] = raw_args.split()
            note = "mapped_args_string_to_argv"
        else:
            args["argv"] = [str(raw_args)]
            note = "mapped_args_value_to_argv"
        args["argv"], extra = _repair_interpreter_payload_quotes(args["argv"])
        if extra:
            note = f"{note}+{extra}"
        return args, note

    if "command" in args and "argv" not in args:
        command = args.pop("command")
        if isinstance(command, list):
            args["argv"] = [str(part) for part in command]
            note = "mapped_command_list_to_argv"
            args["argv"], extra = _repair_interpreter_payload_quotes(args["argv"])
            if extra:
                note = f"{note}+{extra}"
            return args, note
        if isinstance(command, str):
            stripped = command.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    args["argv"] = [str(part) for part in parsed]
                    note = "mapped_command_json_string_to_argv"
                    args["argv"], extra = _repair_interpreter_payload_quotes(
                        args["argv"]
                    )
                    if extra:
                        note = f"{note}+{extra}"
                    return args, note
            try:
                args["argv"] = shlex.split(command)
            except ValueError:
                args["argv"] = command.split()
            note = "mapped_command_string_to_argv"
            args["argv"], extra = _repair_interpreter_payload_quotes(args["argv"])
            if extra:
                note = f"{note}+{extra}"
            return args, note

    return args, None


_STEP_BODY_KEYS: frozenset[str] = frozenset(
    {"title", "description", "success_check", "summary", "step", "name", "body"}
)


def _coerce_step_list(value: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Best-effort coercion of an arbitrary ``steps`` payload into a list of dicts.

    Handles the most common shape mistakes models make for
    ``propose_task_plan`` / ``revise_remaining_plan``:

    - ``[{...}, {...}]``                                        -> as-is
    - ``{...}`` (single subtask object)                         -> ``[{...}]``
    - ``{"steps": [...]}`` / ``{"subtasks": [...]}``            -> inner list
    - ``["a", "b"]`` (bare titles)                              -> wrap each
    - JSON-string of any of the above                           -> parse first
    """
    if value is None:
        return None, None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None, None
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            return [
                {"title": stripped[:240], "description": stripped}
            ], "wrapped_string_step"

    if isinstance(value, dict):
        for inner_key in ("steps", "subtasks", "plan", "tasks", "items"):
            inner = value.get(inner_key)
            if isinstance(inner, list):
                coerced, _note = _coerce_step_list(inner)
                if coerced is not None:
                    return coerced, f"unwrapped_{inner_key}_envelope"
        if any(key in value for key in _STEP_BODY_KEYS):
            return [{str(k): v for k, v in value.items()}], "wrapped_single_step_dict"
        return None, None

    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append({str(k): v for k, v in item.items()})
            elif isinstance(item, str):
                title = item.strip()
                if title:
                    out.append({"title": title[:240], "description": title})
            elif item is None:
                continue
            else:
                text = str(item).strip()
                if text:
                    out.append({"title": text[:240], "description": text})
        if not out:
            return None, None
        return out, None

    return None, None


def _ensure_step_body_keys(
    steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Map common per-step alias keys onto the expected schema.

    Required: ``title`` + ``description``. ``success_check`` is optional but
    we still normalise common aliases so the planner stores a proper value
    when the LLM emits ``check`` / ``acceptance_criteria`` etc.
    """
    notes: list[str] = []
    repaired: list[dict[str, Any]] = []
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        item: dict[str, Any] = {}
        for key, value in raw.items():
            item[str(key)] = value

        if "title" not in item:
            for alias in ("name", "step", "summary", "heading"):
                if isinstance(item.get(alias), str) and item[alias].strip():
                    item["title"] = item[alias].strip()[:240]
                    notes.append(f"step_alias_{alias}_to_title")
                    break

        if "description" not in item:
            for alias in (
                "body",
                "details",
                "detail",
                "instructions",
                "task",
                "content",
            ):
                if isinstance(item.get(alias), str) and item[alias].strip():
                    item["description"] = item[alias].strip()
                    notes.append(f"step_alias_{alias}_to_description")
                    break

        if "title" in item and "description" not in item:
            item["description"] = str(item["title"])
            notes.append("step_description_from_title")
        elif "description" in item and "title" not in item:
            text = str(item["description"]).strip()
            if text:
                item["title"] = text.splitlines()[0][:240]
                notes.append("step_title_from_description")

        if "success_check" not in item:
            for alias in (
                "check",
                "acceptance",
                "acceptance_criteria",
                "verification",
                "verify",
            ):
                if isinstance(item.get(alias), str) and item[alias].strip():
                    item["success_check"] = item[alias].strip()
                    notes.append(f"step_alias_{alias}_to_success_check")
                    break

        if "title" in item or "description" in item:
            repaired.append(item)
    # de-dupe note tags so the log line stays compact
    seen: set[str] = set()
    unique_notes = [note for note in notes if not (note in seen or seen.add(note))]
    return repaired, unique_notes


def _normalize_propose_task_plan_args(
    args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Normalise common malformed shapes for ``propose_task_plan``.

    Models very often emit ``subtasks``/``plan``/``tasks``/``items`` instead
    of the canonical ``steps`` key, or wrap a single subtask as a dict
    rather than a one-element list. Without this normalisation the strict
    preflight check rejects the call with
    ``missing required field(s): steps`` and the agent is forced into an
    expensive planner_rescue retry.
    """
    if not isinstance(args, dict):
        return args, None
    notes: list[str] = []

    if "steps" not in args:
        for alias in ("subtasks", "plan", "tasks", "items", "step", "subtask"):
            if alias in args:
                value = args.pop(alias)
                args["steps"] = value
                notes.append(f"alias_{alias}_to_steps")
                break

    if "steps" in args:
        coerced, coerce_note = _coerce_step_list(args["steps"])
        if coerced is not None:
            args["steps"] = coerced
            if coerce_note:
                notes.append(coerce_note)
        elif args.get("steps") is None:
            args.pop("steps", None)

    # If the outer envelope itself looks like a single subtask
    # (``{"title": "...", "description": "..."}``) wrap it as steps=[{...}].
    if "steps" not in args and any(key in args for key in _STEP_BODY_KEYS):
        args = {"steps": [{str(k): v for k, v in args.items()}]}
        notes.append("wrapped_outer_args_as_single_step")

    if isinstance(args.get("steps"), list):
        repaired, body_notes = _ensure_step_body_keys(args["steps"])
        args["steps"] = repaired
        notes.extend(body_notes)

    if notes:
        return args, "+".join(notes)
    return args, None


def _normalize_revise_remaining_plan_args(
    args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Normalise common malformed shapes for ``revise_remaining_plan``.

    The canonical tool schema is ``{"steps": [...], "reason": "..."}`` —
    the same ``steps`` key as ``propose_task_plan``. Models occasionally
    invent alternative names (``tail``, ``remaining``, ``subtasks``,
    ``replacement_steps_for_remaining`` etc.) so we collapse them.
    """
    if not isinstance(args, dict):
        return args, None
    notes: list[str] = []

    if "steps" not in args:
        for alias in (
            "subtasks",
            "tail",
            "remaining",
            "remaining_steps",
            "replacement",
            "replacements",
            "replacement_steps_for_remaining",
            "plan",
            "items",
            "tasks",
            "step",
        ):
            if alias in args:
                args["steps"] = args.pop(alias)
                notes.append(f"alias_{alias}_to_steps")
                break

    if "steps" in args:
        coerced, coerce_note = _coerce_step_list(args["steps"])
        if coerced is not None:
            args["steps"] = coerced
            if coerce_note:
                notes.append(coerce_note)

    if isinstance(args.get("steps"), list):
        repaired, body_notes = _ensure_step_body_keys(args["steps"])
        args["steps"] = repaired
        notes.extend(body_notes)

    if "reason" not in args:
        for alias in ("why", "rationale", "explanation", "note"):
            if isinstance(args.get(alias), str) and args[alias].strip():
                args["reason"] = args.pop(alias).strip()
                notes.append(f"alias_{alias}_to_reason")
                break

    if notes:
        return args, "+".join(notes)
    return args, None


def _normalize_propose_phase_plan_args(
    args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Collapse common artifact-envelope aliases for ``propose_phase_plan``.

    Some models use the generic artifact key ``content`` even when the tool's
    canonical field is ``plan``. Normalize it before registry dispatch so
    valid plan objects do not turn into ``unexpected keyword`` failures.
    """
    if not isinstance(args, dict):
        return args, None
    notes: list[str] = []

    if "plan" not in args:
        for alias in ("content", "phase_plan", "proposal", "payload", "data"):
            if alias in args:
                args["plan"] = args.pop(alias)
                notes.append(f"alias_{alias}_to_plan")
                break

    if "notes" not in args:
        for alias in ("note", "rationale", "explanation", "summary"):
            if isinstance(args.get(alias), str) and args[alias].strip():
                args["notes"] = args.pop(alias).strip()
                notes.append(f"alias_{alias}_to_notes")
                break

    if notes:
        return args, "+".join(notes)
    return args, None


_FILE_PATH_ALIASES: tuple[str, ...] = (
    "path",
    "file",
    "filepath",
    "filename",
    "file_name",
    "target",
    "target_path",
    "target_file",
    "relative_path",
    "rel_path",
    "name",
    "destination",
    "dest",
    "dest_path",
)

_NEW_CONTENT_ALIASES: tuple[str, ...] = (
    "content",
    "contents",
    "text",
    "body",
    "data",
    "source",
    "source_code",
    "code",
    "value",
    "payload",
    "file_content",
    "file_contents",
    "new_text",
    "updated_content",
    "full_content",
)

_WORKSPACE_ID_ALIASES: tuple[str, ...] = (
    "workspace",
    "workspaceId",
    "ws",
    "ws_id",
    "workspace_name",
    "workspaceName",
)


def _alias_into(
    args: dict[str, Any],
    canonical: str,
    aliases: tuple[str, ...],
    notes: list[str],
) -> None:
    """Move the first matching alias key into ``canonical`` (in place).

    Picks the alias whose value looks like a usable string. ``canonical``
    that is already present and non-empty is left untouched.
    """
    if canonical in args and args.get(canonical) not in (None, ""):
        return
    for alias in aliases:
        if alias not in args:
            continue
        value = args.pop(alias)
        if value is None:
            continue
        args[canonical] = value
        notes.append(f"mapped_{alias}_to_{canonical}")
        return


def _normalize_update_workspace_seed_args(
    args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Normalize common malformed shapes for ``update_workspace_seed``.

    Models call this tool a lot and frequently invent alternative arg
    names (``path``/``filepath``/``filename``/``target`` for ``file_path``;
    ``content``/``contents``/``body``/``code``/``data`` for
    ``new_content``). Without this the strict preflight rejects the call
    with ``missing required field(s): file_path, new_content`` and the
    agent burns a recovery round per mistake.
    """
    if not isinstance(args, dict):
        return args, None
    notes: list[str] = []

    _alias_into(args, "workspace_id", _WORKSPACE_ID_ALIASES, notes)
    _alias_into(args, "file_path", _FILE_PATH_ALIASES, notes)
    _alias_into(args, "new_content", _NEW_CONTENT_ALIASES, notes)

    # Some models wrap a single edit in a sub-object: {"edit": {file_path:..., content:...}}.
    for envelope_key in ("edit", "patch", "file", "update"):
        envelope = args.get(envelope_key)
        if isinstance(envelope, dict) and (
            "file_path" not in args or "new_content" not in args
        ):
            inner_path = (
                envelope.get("file_path")
                or envelope.get("path")
                or envelope.get("file")
                or envelope.get("filename")
            )
            inner_content = (
                envelope.get("new_content")
                or envelope.get("content")
                or envelope.get("text")
                or envelope.get("body")
            )
            if isinstance(inner_path, str) and "file_path" not in args:
                args["file_path"] = inner_path
                notes.append(f"unwrapped_{envelope_key}_envelope_path")
            if isinstance(inner_content, str) and "new_content" not in args:
                args["new_content"] = inner_content
                notes.append(f"unwrapped_{envelope_key}_envelope_content")

    new_content = args.get("new_content")
    if isinstance(new_content, dict):
        if isinstance(new_content.get("new_content"), str):
            args["new_content"] = new_content["new_content"]
            notes.append("unwrapped_new_content_dict")
        elif isinstance(new_content.get("content"), str):
            args["new_content"] = new_content["content"]
            notes.append("unwrapped_content_dict")
        elif isinstance(new_content.get("text"), str):
            args["new_content"] = new_content["text"]
            notes.append("unwrapped_text_dict")

    # Some models emit ``new_content`` as a list of string lines.
    if isinstance(args.get("new_content"), list):
        try:
            joined = "\n".join(str(part) for part in args["new_content"])
            args["new_content"] = joined
            notes.append("joined_new_content_list")
        except Exception:
            pass

    for key in ("workspace_id", "file_path"):
        if key in args and not isinstance(args[key], str):
            args[key] = str(args[key])
            notes.append(f"coerced_{key}_to_str")

    if notes:
        return args, "+".join(notes)
    return args, None


def _normalize_read_workspace_file_args(
    args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Map common alias keys onto ``workspace_id`` / ``file_path`` so the
    strict preflight does not reject ``read_workspace_file`` calls that
    use ``path``/``file``/``filename`` instead of ``file_path``.
    """
    if not isinstance(args, dict):
        return args, None
    notes: list[str] = []
    _alias_into(args, "workspace_id", _WORKSPACE_ID_ALIASES, notes)
    _alias_into(args, "file_path", _FILE_PATH_ALIASES, notes)
    for key in ("workspace_id", "file_path"):
        if key in args and not isinstance(args[key], str):
            args[key] = str(args[key])
            notes.append(f"coerced_{key}_to_str")
    if notes:
        return args, "+".join(notes)
    return args, None


def _normalize_list_workspace_files_args(
    args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Map ``ws/workspace`` aliases onto ``workspace_id`` for
    ``list_workspace_files``. ``subdir`` defaults to '' so we do not
    aggressively rename it.
    """
    if not isinstance(args, dict):
        return args, None
    notes: list[str] = []
    _alias_into(args, "workspace_id", _WORKSPACE_ID_ALIASES, notes)
    if "subdir" not in args:
        for alias in ("dir", "folder", "directory", "path", "subpath", "sub_dir"):
            if alias in args:
                args["subdir"] = args.pop(alias)
                notes.append(f"mapped_{alias}_to_subdir")
                break
    if notes:
        return args, "+".join(notes)
    return args, None


_DOUBLE_WRAP_DEPTH_LIMIT = 4


def _unwrap_arguments_envelope(
    args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Strip an ``{"arguments": "<json>"}`` envelope produced by some LLMs.

    DeepSeek-V*, several Qwen variants, and a few open-source proxies
    occasionally wrap the real tool-call payload inside a single
    ``arguments`` key whose value is a JSON string. This causes the
    Python tool to fail with ``unexpected keyword argument 'arguments'``
    even though the model intended a perfectly normal call. Empirically
    we have seen up to 2 levels of nesting in the same payload (the
    model wraps the wrap), so we unwrap iteratively up to
    ``_DOUBLE_WRAP_DEPTH_LIMIT``.

    Returns ``(unwrapped, note)`` where ``note`` is ``None`` when nothing
    needed unwrapping.
    """
    depth = 0
    note: str | None = None
    while (
        depth < _DOUBLE_WRAP_DEPTH_LIMIT
        and isinstance(args, dict)
        and len(args) == 1
        and "arguments" in args
    ):
        inner = args["arguments"]
        if isinstance(inner, dict):
            args = {str(k): v for k, v in inner.items()}
            depth += 1
            note = "unwrapped_arguments_envelope"
            continue
        if isinstance(inner, str):
            stripped = inner.strip()
            if not stripped:
                break
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                relaxed = _strip_trailing_commas(
                    _fix_control_chars_inside_json_strings(stripped)
                )
                try:
                    parsed = json.loads(relaxed)
                except json.JSONDecodeError:
                    break
            if not isinstance(parsed, dict):
                break
            args = {str(k): v for k, v in parsed.items()}
            depth += 1
            note = "unwrapped_arguments_envelope"
            continue
        break
    return args, note


def _postprocess_tool_args(
    fn_name: str, args: dict[str, Any], note: str
) -> tuple[dict[str, Any], str]:
    args, envelope_note = _unwrap_arguments_envelope(args)
    if envelope_note:
        if note in {"ok", "already_dict"}:
            note = envelope_note
        else:
            note = f"{note}+{envelope_note}"
    if fn_name == "run_workspace_command":
        args, extra = _normalize_run_workspace_command_args(args)
        if extra:
            if note in {"ok", "already_dict"}:
                return args, extra
            return args, f"{note}+{extra}"
    if fn_name == "update_workspace_seed":
        args, extra = _normalize_update_workspace_seed_args(args)
        if extra:
            if note in {"ok", "already_dict"}:
                return args, extra
            return args, f"{note}+{extra}"
    if fn_name == "read_workspace_file":
        args, extra = _normalize_read_workspace_file_args(args)
        if extra:
            if note in {"ok", "already_dict"}:
                return args, extra
            return args, f"{note}+{extra}"
    if fn_name == "list_workspace_files":
        args, extra = _normalize_list_workspace_files_args(args)
        if extra:
            if note in {"ok", "already_dict"}:
                return args, extra
            return args, f"{note}+{extra}"
    if fn_name == "propose_task_plan":
        args, extra = _normalize_propose_task_plan_args(args)
        if extra:
            if note in {"ok", "already_dict"}:
                return args, extra
            return args, f"{note}+{extra}"
    if fn_name == "revise_remaining_plan":
        args, extra = _normalize_revise_remaining_plan_args(args)
        if extra:
            if note in {"ok", "already_dict"}:
                return args, extra
            return args, f"{note}+{extra}"
    if fn_name == "propose_phase_plan":
        args, extra = _normalize_propose_phase_plan_args(args)
        if extra:
            if note in {"ok", "already_dict"}:
                return args, extra
            return args, f"{note}+{extra}"
    return args, note


def repair_tool_arguments(fn_name: str, raw: Any) -> tuple[dict[str, Any], str]:
    """Parse ``raw`` into a tool-arguments dict, applying recovery as needed.

    Returns ``(args_dict, note)`` where ``note`` is one of:

    - ``"ok"``                              — direct ``json.loads`` worked
    - ``"already_dict"``                    — caller already passed a dict
    - ``"empty"``                           — ``raw`` was empty / whitespace
    - ``"fixed_control_chars_in_json_strings"``
    - ``"removed_trailing_commas"``
    - ``"python_literal_fallback"``
    - ``"unrepairable: <reason>"``          — falls back to ``{}`` to keep
      the agent loop alive; caller should log loudly.
    """

    fn_name = fn_name or "<unknown>"

    if isinstance(raw, dict):
        return _postprocess_tool_args(
            fn_name,
            {str(k): v for k, v in raw.items()},
            "already_dict",
        )

    if raw is None:
        return ({}, "empty")

    s = str(raw).strip()
    if not s:
        return ({}, "empty")

    # Strict path first — most calls succeed here without any repair.
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError as e:
        first_error = f"{e.msg} at line {e.lineno} col {e.colno}"
    else:
        if isinstance(parsed, dict):
            return _postprocess_tool_args(
                fn_name,
                {str(k): v for k, v in parsed.items()},
                "ok",
            )
        return ({}, f"unrepairable: top-level not an object ({type(parsed).__name__})")

    fixed = _fix_control_chars_inside_json_strings(s)
    if fixed != s:
        try:
            parsed = json.loads(fixed)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return _postprocess_tool_args(
                    fn_name,
                    {str(k): v for k, v in parsed.items()},
                    "fixed_control_chars_in_json_strings",
                )

    relaxed = _strip_trailing_commas(fixed)
    if relaxed != fixed:
        try:
            parsed = json.loads(relaxed)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return _postprocess_tool_args(
                    fn_name,
                    {str(k): v for k, v in parsed.items()},
                    "removed_trailing_commas",
                )

    # Combined heuristic: control-char-fix + trailing-comma fix together.
    combined = _strip_trailing_commas(_fix_control_chars_inside_json_strings(s))
    if combined not in (s, fixed, relaxed):
        try:
            parsed = json.loads(combined)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return _postprocess_tool_args(
                    fn_name,
                    {str(k): v for k, v in parsed.items()},
                    "removed_trailing_commas",
                )

    # Final fallback: Python literal eval. Catches single-quoted dicts and
    # bare ``True/False/None`` that LLMs occasionally emit in response to
    # confusing tool schemas.
    py = _try_python_literal(s)
    if py is not None:
        return _postprocess_tool_args(fn_name, py, "python_literal_fallback")

    return ({}, f"unrepairable: {first_error}")


__all__ = ["repair_tool_arguments"]
