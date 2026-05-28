"""Retry watcher state, escalation blocks, and structured review payloads."""

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.contracts import hash_value, json_ready

_SUCCESS_TEST_TOOL_NAMES = (
    "harness_run",
    "run_workspace_verify",
    "run_unit_tests",
    "run_real_e2e",
    "web_search",
    "deep_search",
    "github_project_search",
    "mcp_discover",
)
_SUCCESS_TEST_TRAILING_OUTCOME_RE = (
    re.compile(
        r"(?is)\s+"
        r"(?:must\s+)?(?:exit|exits|return|returns)"
        r"(?:\s+successfully)?(?:\s+with)?(?:\s+exit)?(?:\s+code)?\s+0\b.*$"
    ),
    re.compile(r"(?is)\s+(?:has|with)\s+exit\s+code\s+0\b.*$"),
    re.compile(r"(?is)\s+(?:passes|succeeds|is\s+successful)\b.*$"),
    re.compile(r"(?is)\s+prints?\s+[`'\"]?[A-Za-z0-9_.:/ -]{1,120}[`'\"]?\s*$"),
)
_SUCCESS_TEST_LEADING_COMMAND_LABEL_RE = re.compile(
    r"(?is)^\s*(?:command|cmd|shell|terminal|run|success[_\s-]*test)\s*:\s*"
)

from umbrella.deep_agent_tools.phase_control_base import *

def _is_final_review_context(ctx: ToolContext) -> bool:
    view = _loop_state_view(ctx)
    phase_label = str(view.get("phase_label") or "").lower()
    if "final_review" in phase_label:
        return True
    overlays = _context_overlays(ctx)
    for key in ("phase_node", "phase_manifest"):
        node = overlays.get(key)
        if not isinstance(node, dict):
            continue
        for field in ("id", "manifest_id"):
            if str(node.get(field) or "").lower() == "final_review":
                return True
    return False


def _final_review_e2e_gate(ctx: ToolContext) -> str:
    if not (_is_phase_run_context(ctx) and _is_final_review_context(ctx)):
        return ""
    view = _loop_state_view(ctx)
    phase_label = str(view.get("phase_label") or "")
    verify_gate = _final_review_workspace_verify_gate(ctx)
    if verify_gate:
        return verify_gate
    logged = _latest_logged_e2e_result(ctx)
    if (
        not logged
        and str(view.get("last_e2e_phase_label") or "") != phase_label
    ):
        return (
            "ERROR: submit_final_review(outcome='ok') requires a fresh "
            "`run_real_e2e` result from this final_review phase. Prior verify "
            "or e2e evidence is not enough."
        )
    if logged:
        passed = bool(logged.get("passed"))
        failed_count = int(logged.get("failed_step_count") or 0)
        run_id = str(logged.get("verify_run_id") or "").strip()
    else:
        passed = bool(view.get("last_e2e_passed"))
        failed_count = int(view.get("last_e2e_failed_count") or 0)
        run_id = str(view.get("last_e2e_run_id") or "").strip()
    if not passed or failed_count > 0 or not run_id:
        return (
            "ERROR: submit_final_review(outcome='ok') requires passing "
            "`run_real_e2e` evidence with zero failed required steps. Fix the "
            "reported e2e gaps or call loop_back_to."
        )
    return ""


def _final_review_workspace_verify_gate(ctx: ToolContext) -> str:
    view = _loop_state_view(ctx)
    logged = _latest_logged_tool_result(ctx, "run_workspace_verify")
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if task_id and not logged:
        return (
            "ERROR: submit_final_review(outcome='ok') requires a fresh "
            "`run_workspace_verify` result from this final_review phase. "
            "Prior verify evidence is not enough."
        )
    if logged:
        passed = bool(logged.get("passed"))
        failed_count = int(logged.get("failed_step_count") or 0)
        run_id = str(logged.get("verify_run_id") or "").strip()
    else:
        passed = bool(view.get("last_verify_passed"))
        failed_count = int(view.get("last_verify_failed_count") or 0)
        run_id = str(view.get("last_verify_run_id") or "").strip()
    if not passed or failed_count > 0 or not run_id:
        return (
            "ERROR: submit_final_review(outcome='ok') requires passing "
            "`run_workspace_verify` evidence with zero failed required steps "
            "from the final_review gate. Fix the reported verification gaps "
            "or call loop_back_to."
        )
    return ""


def _latest_logged_e2e_result(ctx: ToolContext) -> dict[str, Any] | None:
    """Return the latest same-task ``run_real_e2e`` payload from tools.jsonl.

    ``loop_state_view`` is captured at the start of an LLM round. When the
    model calls ``run_real_e2e`` and ``submit_final_review`` in the same tool
    batch, the state view has not been refreshed yet, but the tool log already
    contains the fresh e2e result. Reading that append-only log keeps the gate
    strict without forcing an unnecessary extra round.
    """

    return _latest_logged_tool_result(ctx, "run_real_e2e")


def _latest_logged_tool_result(ctx: ToolContext, tool_name: str) -> dict[str, Any] | None:
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if not task_id:
        return None
    logs_path = pathlib.Path(ctx.drive_root) / "logs" / "tools.jsonl"
    try:
        lines = logs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    for line in reversed(lines[-250:]):
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("task_id") or "") != task_id:
            continue
        if str(record.get("tool") or "") != tool_name:
            continue
        raw = record.get("result_preview")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _current_phase_node(ctx: ToolContext, plan: dict[str, Any]) -> dict[str, Any] | None:
    nodes = [node for node in plan.get("nodes", []) if isinstance(node, dict)]
    if not nodes:
        return None

    candidates: list[str] = []
    overlays = _context_overlays(ctx)
    phase_node = overlays.get("phase_node")
    if isinstance(phase_node, dict):
        for key in ("id", "manifest_id"):
            val = str(phase_node.get(key) or "").strip()
            if val:
                candidates.append(val)

    view = getattr(ctx, "loop_state_view", None)
    if isinstance(view, dict):
        for key in ("umbrella_phase_id", "phase_id", "current_phase_id", "phase_label"):
            val = str(view.get(key) or "").strip()
            if val:
                candidates.append(val)

    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if task_id:
        candidates.append(task_id)
        if ":" in task_id:
            candidates.append(task_id.rsplit(":", 1)[-1])

    for candidate in candidates:
        for node in nodes:
            if str(node.get("id") or "") == candidate:
                return node

    running = [node for node in nodes if str(node.get("status") or "") == "running"]
    if len(running) == 1:
        return running[0]
    return None


def _phase_subtasks(node: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = (node or {}).get("subtasks")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _subtask_status(subtask: dict[str, Any]) -> str:
    return str(subtask.get("status") or "").strip().lower()


def _first_incomplete_subtask(
    subtasks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for subtask in subtasks:
        if _subtask_status(subtask) != "done":
            return subtask
    return None


def _required_tool_from_proof_command(proof_command: str) -> str:
    text = str(proof_command or "")
    for tool_name in _SUCCESS_TEST_TOOL_NAMES:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(tool_name)}(?![A-Za-z0-9_])", text):
            return tool_name
    return ""


def _normalise_command_text(value: Any) -> str:
    if isinstance(value, dict):
        raw = value.get("command") or value.get("argv") or value.get("cmd") or ""
    else:
        raw = value
    if isinstance(raw, (list, tuple)):
        raw = " ".join(_portable_command_vector(raw))
    return re.sub(r"[^a-z0-9]+", "", str(raw or "").lower())


def _portable_command_vector(value: Any) -> list[str]:
    parts = [str(part) for part in (value or [])]
    if not parts:
        return parts
    executable = parts[0].strip().strip("`'\"")
    names = [
        name
        for name in (
            pathlib.PureWindowsPath(executable).name,
            pathlib.PurePosixPath(executable).name,
        )
        if name
    ]
    if names:
        name = min(names, key=len)
        lowered = name.lower()
        for suffix in (".exe", ".cmd", ".bat", ".ps1"):
            if lowered.endswith(suffix):
                name = name[: -len(suffix)]
                break
        parts[0] = name
    return parts


def _tool_row_command_candidates(value: Any) -> list[Any]:
    if not isinstance(value, dict):
        return [value]
    candidates: list[Any] = []
    argv = value.get("argv")
    command = value.get("command")
    cmd = value.get("cmd")
    if command and isinstance(argv, (list, tuple)) and (
        not argv or str(argv[0]).strip().startswith("-")
    ):
        candidates.append([command, *argv])
    for raw in (argv, command, cmd, value):
        if raw is not None:
            candidates.append(raw)
    return candidates


def _strip_proof_command_label(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    previous = None
    while previous != text:
        previous = text
        text = _SUCCESS_TEST_LEADING_COMMAND_LABEL_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"`", "'", '"'}:
        text = text[1:-1].strip()
    return text


def _proof_command_candidates(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    candidates = [text]
    labelled = _strip_proof_command_label(text)
    if labelled and labelled != text:
        candidates.append(labelled)
    for pattern in _SUCCESS_TEST_TRAILING_OUTCOME_RE:
        for candidate in list(candidates):
            stripped = pattern.sub("", candidate).strip()
            if stripped and stripped != candidate:
                candidates.append(stripped)
    return list(dict.fromkeys(candidates))


def _command_alternatives(segment: str) -> list[str]:
    raw = str(segment or "").strip()
    if not raw:
        return []
    if re.fullmatch(r"(?i)cd\s+[^\s;&|]+", raw):
        return []
    alternatives: list[str] = []
    for candidate in _proof_command_candidates(raw):
        alternatives.append(_normalise_command_text(candidate))
        py_c = re.search(r"(?i)(?:^|\s)-c\s+(.+)$", candidate)
        if py_c:
            alternatives.append(
                _normalise_command_text(py_c.group(1).strip().strip("`'\""))
            )
    return [
        item
        for item in dict.fromkeys(alternatives)
        if len(item) >= 12
    ]


def _split_command_tokens_for_retry(value: str) -> list[str]:
    try:
        import shlex

        return shlex.split(str(value or ""), posix=True)
    except Exception:
        return re.findall(r"\S+", str(value or ""))


def _looks_like_pytest_target_token(token: str) -> bool:
    raw = str(token or "").strip().strip("`'\"")
    if not raw or raw.startswith("-"):
        return False
    norm = raw.replace("\\", "/").lower()
    if norm.startswith("./"):
        norm = norm[2:]
    if norm.startswith(("tests/", "test/")):
        return True
    if "::" in norm:
        return True
    return norm.endswith(".py") or ".py::" in norm


def _pytest_target_retry_alternatives(command_text: str) -> list[str]:
    """Return pytest target fragments for retry detection only.

    Completion still requires the exact declared typed proof command. Retry
    escalation is looser: when one combined pytest proof names several files,
    repeated reruns of one failing file are still repeated failures of the
    active subtask's declared proof path and should trigger watcher review.
    """

    tokens = _split_command_tokens_for_retry(command_text)
    if not tokens:
        return []
    pytest_idx = -1
    for idx, token in enumerate(tokens):
        name = pathlib.PurePath(str(token).strip("`'\"")).name.lower()
        if name in {"pytest", "pytest.exe"}:
            pytest_idx = idx
            break
    if pytest_idx < 0:
        return []

    alternatives: list[str] = []
    for token in tokens[pytest_idx + 1 :]:
        if not _looks_like_pytest_target_token(token):
            continue
        target = str(token).strip().strip("`'\"")
        alternatives.append(_normalise_command_text(f"pytest {target}"))
        alternatives.append(_normalise_command_text(f"python -m pytest {target}"))
    return [item for item in dict.fromkeys(alternatives) if len(item) >= 12]


def _retry_command_alternatives(segment: str) -> list[str]:
    raw = str(segment or "").strip()
    if not raw:
        return []
    alternatives = list(_command_alternatives(raw))
    for candidate in _proof_command_candidates(raw):
        alternatives.extend(_pytest_target_retry_alternatives(candidate))
    return [item for item in dict.fromkeys(alternatives) if len(item) >= 12]


def _proof_retry_command_groups(proof_command: str) -> list[list[str]]:
    text = str(proof_command or "").strip()
    if not text:
        return []
    groups: list[list[str]] = []
    for segment in re.split(r"\s*(?:&&|;)\s*", text):
        alternatives = _retry_command_alternatives(segment)
        if alternatives:
            groups.append(alternatives)
    if not groups:
        alternatives = _retry_command_alternatives(text)
        if alternatives:
            groups.append(alternatives)
    return groups


def _proof_command_groups(proof_command: str) -> list[list[str]]:
    text = str(proof_command or "").strip()
    if not text:
        return []
    groups: list[list[str]] = []
    # Success tests are required to be simple executable proof commands. This
    # split intentionally handles the common portable composition style while
    # leaving quoted command bodies as fuzzy command fragments.
    for segment in re.split(r"\s*(?:&&|;)\s*", text):
        alternatives = _command_alternatives(segment)
        if alternatives:
            groups.append(alternatives)
    if not groups:
        alternatives = _command_alternatives(text)
        if alternatives:
            groups.append(alternatives)
    return groups


def _subtask_typed_proof_command_text(subtask: dict[str, Any]) -> str:
    proof = subtask.get("proof")
    if not isinstance(proof, dict):
        return ""
    execution = proof.get("execution")
    if not isinstance(execution, dict):
        return ""
    command = execution.get("command")
    if isinstance(command, (list, tuple)):
        return " ".join(_portable_command_vector(command))
    if isinstance(command, str):
        return command.strip()
    return ""


def _pytest_output_is_skip_only(output: str) -> bool:
    text = str(output or "")
    if not text:
        return False
    if _PYTEST_PASS_RE.search(text) or _PYTEST_FAILURE_RE.search(text):
        return False
    match = _PYTEST_SKIP_ONLY_RE.search(text)
    return bool(match and int(match.group("skipped") or 0) > 0)


def _tool_row_result_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("result_preview") or row.get("result") or {}
    payload = _json_obj_from_preview(raw)
    if payload or not isinstance(raw, str):
        return payload
    parsed: dict[str, Any] = {}
    passed_match = re.search(r'"passed"\s*:\s*(true|false)', raw, re.IGNORECASE)
    if passed_match:
        parsed["passed"] = passed_match.group(1).lower() == "true"
    failed_match = re.search(r'"failed_step_count"\s*:\s*(\d+)', raw)
    if failed_match:
        parsed["failed_step_count"] = int(failed_match.group(1))
    run_match = re.search(r'"verify_run_id"\s*:\s*"([^"]+)"', raw)
    if run_match:
        parsed["verify_run_id"] = run_match.group(1)
    return parsed


def _tool_row_output_text(row: dict[str, Any]) -> str:
    payload = _tool_row_result_payload(row)
    if isinstance(payload.get("output"), str):
        return str(payload.get("output") or "")
    raw = row.get("result_preview") or row.get("result") or ""
    return raw if isinstance(raw, str) else str(raw or "")


def _tool_row_is_blocked_before_execution(row: dict[str, Any]) -> bool:
    payload = _tool_row_result_payload(row)
    return str(payload.get("status") or "").lower() == "blocked" and bool(
        str(payload.get("reason") or "").strip()
    )


def _tool_row_command_norms(row: dict[str, Any]) -> list[str]:
    candidates: list[Any] = []
    candidates.extend(_tool_row_command_candidates(row.get("args")))
    payload = _tool_row_result_payload(row)
    if payload:
        candidates.extend(_tool_row_command_candidates(payload))
    norms = [_normalise_command_text(candidate) for candidate in candidates]
    return [item for item in dict.fromkeys(norms) if item]


def _tool_row_success_status(row: dict[str, Any]) -> tuple[bool, str]:
    if row.get("is_error") is True or row.get("error"):
        return False, "tool reported an error"
    text = str(row.get("result_preview") or row.get("result") or "")
    if text.lstrip().startswith("ERROR:"):
        return False, "tool result starts with ERROR"
    payload = _tool_row_result_payload(row)
    if payload.get("passed") is False:
        return False, "passed=false"
    if str(payload.get("status") or "").lower() in {"error", "failed"}:
        return False, f"status={payload.get('status')}"
    if "exit_code" in payload:
        try:
            if int(payload.get("exit_code")) != 0:
                return False, f"exit_code={payload.get('exit_code')}"
        except (TypeError, ValueError):
            return False, "exit_code is not numeric"
        output = str(payload.get("output") or "")
        if _pytest_output_is_skip_only(output):
            return False, "pytest skipped every collected test"
        return True, ""
    if _pytest_output_is_skip_only(_tool_row_output_text(row)):
        return False, "pytest skipped every collected test"
    lowered = text.lower()
    if (
        '"exit_code": 0' in text
        or "'exit_code': 0" in text
        or '"passed": true' in lowered
        or "'passed': true" in lowered
        or text.lstrip().startswith("OK:")
    ):
        return True, ""
    return False, "no successful command result found"


def _row_uses_shell_env_wrapper(
    row: dict[str, Any], *, declared_alternatives: list[str]
) -> bool:
    norms = _tool_row_command_norms(row)
    if not norms:
        return False
    wrapper_markers = (
        "powershell",
        "pwsh",
        "cmd",
        "bash",
        "/bin/sh",
        "pythonioencoding",
        "set +e",
        "|| true",
    )
    for norm in norms:
        if not any(marker in norm for marker in wrapper_markers):
            continue
        if any(norm == alt for alt in declared_alternatives):
            continue
        if any(alt in norm for alt in declared_alternatives):
            return True
    return False


def _completion_command_success_issue(
    ctx: ToolContext, *, proof_command: str = "", success_text: str = "", subtask_id: str
) -> str:
    if not proof_command:
        proof_command = success_text
    groups = _proof_command_groups(proof_command)
    if not groups:
        return ""
    rows = _tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or ""))
    command_rows = [
        (idx, row)
        for idx, row in enumerate(rows)
        if str(row.get("tool") or "") in {"shell", "run_workspace_command", "terminal_session"}
    ]
    missing_groups: list[str] = []
    skip_only_group = ""
    stale_groups: list[str] = []
    for alternatives in groups:
        matching_rows = [
            (idx, row)
            for idx, row in command_rows
            if any(
                alt and any(alt in norm for norm in _tool_row_command_norms(row))
                for alt in alternatives
            )
        ]
        accepted = False
        accepted_idx = -1
        accepted_row: dict[str, Any] | None = None
        group_skip_only = False
        latest_reason = ""
        for idx, row in matching_rows:
            ok, reason = _tool_row_success_status(row)
            accepted = ok
            if ok:
                accepted_idx = idx
                accepted_row = row
            latest_reason = reason
            group_skip_only = "skipped every collected test" in reason
        if accepted and accepted_row is not None:
            if _row_uses_shell_env_wrapper(
                accepted_row, declared_alternatives=alternatives
            ):
                return (
                    "ERROR: mark_subtask_complete rejected: subtask "
                    f"`{subtask_id}` declares proof command `{proof_command}`, but "
                    "the latest matching evidence used a shell/env command "
                    "wrapper instead of the exact declared proof "
                    "command. Rerun the declared command directly (no "
                    "powershell/cmd/bash/env wrapper) before marking the "
                    "subtask done."
                )
        if not accepted:
            if group_skip_only and "skipped every collected test" in latest_reason:
                skip_only_group = alternatives[0]
            missing_groups.append(alternatives[0])
            continue
        for row in rows[accepted_idx + 1 :]:
            if _tool_row_is_successful_repair_write(row):
                stale_groups.append(alternatives[0])
                break
    if skip_only_group:
        return (
            "ERROR: mark_subtask_complete rejected: subtask "
            f"`{subtask_id}` declares proof command `{proof_command}`, but the "
            "matching pytest run skipped every collected test. Skipped-only "
            "pytest output is not proof; remove the skip, configure the real "
            "dependency/env, or provide a verification command with passing "
            "assertions before marking the subtask done."
        )
    if missing_groups:
        return (
            "ERROR: mark_subtask_complete rejected: subtask "
            f"`{subtask_id}` declares proof command `{proof_command}`, but no "
            "matching successful shell/run_workspace_command evidence was "
            "found for every command fragment. Run the declared proof command "
            "and fix failures before marking the subtask done."
        )
    if stale_groups:
        return (
            "ERROR: mark_subtask_complete rejected: subtask "
            f"`{subtask_id}` declares proof command `{proof_command}`, but "
            "workspace files were modified after the latest matching successful "
            "proof evidence. Rerun the declared proof after the "
            "last repair write before marking the subtask done."
        )
    return ""


def _latest_tool_result_for_task(
    ctx: ToolContext, *, tool_name: str, subtask_id: str = ""
) -> dict[str, Any] | None:
    rows = _tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or ""))
    for row in reversed(rows):
        if str(row.get("tool") or "") != tool_name:
            continue
        if subtask_id:
            args = row.get("args")
            if isinstance(args, dict):
                row_subtask = str(args.get("subtask_id") or "").strip()
                if row_subtask and row_subtask != subtask_id:
                    continue
        return row
    return None


def _completion_proof_command_issue(
    ctx: ToolContext, *, subtask: dict[str, Any], subtask_id: str
) -> str:
    proof_command = _subtask_typed_proof_command_text(subtask)
    required_tool = _required_tool_from_proof_command(proof_command)
    if not required_tool:
        command_issue = _completion_command_success_issue(
            ctx,
            proof_command=proof_command,
            subtask_id=subtask_id,
        )
        if command_issue:
            return command_issue
        return ""
    else:
        row = _latest_tool_result_for_task(
            ctx, tool_name=required_tool, subtask_id=subtask_id
        )
        if row is None:
            return (
                "ERROR: mark_subtask_complete rejected: subtask "
                f"`{subtask_id}` declares success test requiring `{required_tool}`, "
                f"but `{required_tool}` was not called for this task. Run the "
                "declared proof command first, then retry completion with its result."
            )
        if required_tool == "run_workspace_verify":
            payload = _tool_row_result_payload(row)
            if (
                not payload
                or payload.get("passed") is not True
                or int(payload.get("failed_step_count") or 0) > 0
            ):
                failed_raw = payload.get("failed_step_count")
                failed_suffix = (
                    f" ({int(failed_raw)} failed required step(s))"
                    if failed_raw is not None and str(failed_raw).strip() != ""
                    else ""
                )
                return (
                    "ERROR: mark_subtask_complete rejected: subtask "
                    f"`{subtask_id}` requires a passing `run_workspace_verify` "
                    "result, but the latest verify result is missing, stale, or "
                    f"failing{failed_suffix}. Fix the reported verification gaps before marking "
                    "the subtask done."
                )
        else:
            ok, reason = _tool_row_success_status(row)
            if not ok:
                return (
                    "ERROR: mark_subtask_complete rejected: subtask "
                    f"`{subtask_id}` declares proof command requiring `{required_tool}`, "
                    f"but the latest `{required_tool}` result is not passing "
                    f"({reason}). Fix the failure and rerun the declared proof "
                    "before marking the subtask done."
                )
    return ""


_VERIFY_DEFERABLE_SKILL_RE = re.compile(
    r"(?i)\b(?:skill_(?:runtime|quality|compliance)|multi[_-]?agent[_-]?gmas|gmas|llm)\b"
)
_SUBTASK_LLM_SURFACE_RE = re.compile(
    r"(?i)\b(?:gmas|llm|multi[-_\s]?agent|agent|bot|ai)\b|(?:^|/)ai(?:/|$)"
)
_PYTEST_NODE_REF_RE = re.compile(
    r"(?i)(?P<target>(?:[A-Za-z0-9_.-]+/)*test[A-Za-z0-9_./-]*\.py"
    r"(?:::[A-Za-z0-9_./<>\[\]-]+)*)"
)


def _normalise_verify_text(value: Any) -> str:
    return str(value or "").replace("\\", "/").lower()


def _verify_failed_texts(payload: dict[str, Any], raw_preview: Any) -> list[str]:
    texts: list[str] = []
    results = payload.get("results") if isinstance(payload, dict) else None
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict) or bool(result.get("optional")):
                continue
            if str(result.get("status") or "").lower() not in {"failed", "error"}:
                continue
            texts.append(
                "\n".join(
                    str(result.get(key) or "")
                    for key in (
                        "name",
                        "kind",
                        "summary",
                        "stdout",
                        "stderr",
                        "stdout_tail",
                        "stderr_tail",
                        "error",
                        "command",
                    )
                )
            )
    if texts:
        return texts

    summary = str(payload.get("summary") or "") if isinstance(payload, dict) else ""
    raw = str(raw_preview or "")
    source = summary or raw
    blocks = re.split(r"\n(?=- \[(?:required|optional)\])", source)
    for block in blocks:
        if re.search(r"(?i)->\s*(?:failed|error)\b", block):
            texts.append(block)
    if texts:
        return texts
    if isinstance(payload, dict) and payload.get("passed") is False:
        return [summary or raw]
    return []


def _subtask_referenced_paths(subtask: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for key in (
        "files_to_create",
        "files_to_change",
        "files_affected",
    ):
        raw = subtask.get(key)
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            values = list(raw)
        else:
            values = []
        for value in values:
            rel = str(value or "").replace("\\", "/").strip().strip("/").lower()
            if rel:
                paths.add(rel)
    proof = subtask.get("proof")
    scope = proof.get("scope") if isinstance(proof, dict) else None
    if isinstance(scope, dict):
        for key in ("files_under_test", "changed_files_expected", "pytest_targets"):
            raw = scope.get(key)
            if isinstance(raw, str):
                values = [raw]
            elif isinstance(raw, (list, tuple, set, frozenset)):
                values = list(raw)
            else:
                values = []
            for value in values:
                rel = str(value or "").replace("\\", "/").strip().strip("/").lower()
                if rel:
                    paths.add(rel)
    for token in _split_command_tokens_for_retry(
        _subtask_typed_proof_command_text(subtask)
    ):
        if not _looks_like_pytest_target_token(token):
            continue
        target = str(token).strip().strip("`'\"").replace("\\", "/").lower()
        if "::" in target:
            target = target.split("::", 1)[0]
        if target.startswith("./"):
            target = target[2:]
        if target:
            paths.add(target)
    return paths


def _normalise_pytest_target_token(value: Any) -> str:
    target = str(value or "").strip().strip("`'\"()[]{}.,;")
    target = target.replace("\\", "/").lower()
    if target.startswith("./"):
        target = target[2:]
    return target


def _pytest_targets_from_command_text(value: str) -> set[str]:
    targets: set[str] = set()
    for candidate in _proof_command_candidates(value):
        for token in _split_command_tokens_for_retry(candidate):
            if not _looks_like_pytest_target_token(token):
                continue
            target = _normalise_pytest_target_token(token)
            if target:
                targets.add(target)
    return targets


def _subtask_pytest_targets(subtask: dict[str, Any]) -> set[str]:
    return set(_pytest_targets_from_command_text(_subtask_typed_proof_command_text(subtask)))


def _pytest_targets_from_failure_text(value: str) -> set[str]:
    targets: set[str] = set()
    for match in _PYTEST_NODE_REF_RE.finditer(str(value or "")):
        target = _normalise_pytest_target_token(match.group("target"))
        if target:
            targets.add(target)
    return targets


def _pytest_target_covers(candidate: str, target: str) -> bool:
    owner = _normalise_pytest_target_token(candidate)
    wanted = _normalise_pytest_target_token(target)
    if not owner or not wanted:
        return False
    return owner == wanted or wanted.startswith(f"{owner}::")


def _future_subtask_owns_pytest_target(
    subtasks: list[dict[str, Any]], *, subtask_id: str, target: str
) -> bool:
    seen_current = False
    for item in subtasks:
        if str(item.get("id") or "").strip() == subtask_id:
            seen_current = True
            continue
        if not seen_current:
            continue
        if _subtask_status(item) == "done":
            continue
        if any(_pytest_target_covers(candidate, target) for candidate in _subtask_pytest_targets(item)):
            return True
    return False


def _retry_proof_target_files(proof_command: str) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for candidate in _proof_command_candidates(proof_command):
        for token in _split_command_tokens_for_retry(candidate):
            if not _looks_like_pytest_target_token(token):
                continue
            target = str(token).strip().strip("`'\"").replace("\\", "/")
            if "::" in target:
                target = target.split("::", 1)[0]
            if target.startswith("./"):
                target = target[2:]
            key = target.lower()
            if target and key not in seen:
                seen.add(key)
                targets.append(target)
    return targets


def _subtask_has_llm_surface(subtask: dict[str, Any]) -> bool:
    parts: list[str] = []
    for key in (
        "id",
        "subtask_id",
        "title",
        "name",
        "goal",
        "description",
        "files_to_create",
        "files_to_change",
        "files_affected",
    ):
        parts.append(str(subtask.get(key) or ""))
    return bool(_SUBTASK_LLM_SURFACE_RE.search("\n".join(parts)))


def _has_future_llm_subtask(
    subtasks: list[dict[str, Any]], *, subtask_id: str
) -> bool:
    seen_current = False
    for subtask in subtasks:
        if str(subtask.get("id") or "").strip() == subtask_id:
            seen_current = True
            continue
        if not seen_current:
            continue
        if _subtask_status(subtask) == "done":
            continue
        if _subtask_has_llm_surface(subtask):
            return True
    return False


def _workspace_verify_relevant_failure_texts(
    payload: dict[str, Any],
    raw_preview: Any,
    *,
    subtask: dict[str, Any],
    subtask_id: str,
    subtasks: list[dict[str, Any]],
) -> list[str]:
    failed = _verify_failed_texts(payload, raw_preview)
    if not failed:
        return ["unknown failed verification step"]
    paths = _subtask_referenced_paths(subtask)
    current_has_llm = _subtask_has_llm_surface(subtask)
    future_has_llm = _has_future_llm_subtask(subtasks, subtask_id=subtask_id)
    relevant: list[str] = []
    for text in failed:
        norm_text = _normalise_verify_text(text)
        failed_pytest_targets = _pytest_targets_from_failure_text(text)
        if failed_pytest_targets:
            current_owns_pytest_target = any(
                _pytest_target_covers(candidate, target)
                for target in failed_pytest_targets
                for candidate in _subtask_pytest_targets(subtask)
            )
            future_owns_pytest_target = any(
                _future_subtask_owns_pytest_target(
                    subtasks,
                    subtask_id=subtask_id,
                    target=target,
                )
                for target in failed_pytest_targets
            )
            if future_owns_pytest_target and not current_owns_pytest_target:
                continue
        if any(path and path in norm_text for path in paths):
            relevant.append(text)
            continue
        if _VERIFY_DEFERABLE_SKILL_RE.search(norm_text):
            if not current_has_llm and future_has_llm:
                continue
            relevant.append(text)
            continue
        relevant.append(text)
    return relevant


def _workspace_verify_completion_issue(
    ctx: ToolContext,
    *,
    subtask_id: str,
    subtask: dict[str, Any] | None = None,
    subtasks: list[dict[str, Any]] | None = None,
) -> str:
    """Block phase subtask closure when workspace verify is red or stale.

    The loop state snapshot can lag behind tool calls made in the current LLM
    turn, so use the append-only tool log as the source of truth here.
    """

    rows = _tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or ""))
    latest_verify_idx = -1
    latest_payload: dict[str, Any] = {}
    latest_row: dict[str, Any] = {}
    for idx, row in enumerate(rows):
        if str(row.get("tool") or "") != "run_workspace_verify":
            continue
        latest_verify_idx = idx
        latest_row = row
        latest_payload = _tool_row_result_payload(row)
    if latest_verify_idx < 0:
        return ""
    if (
        not latest_payload
        or latest_payload.get("passed") is not True
        or int(latest_payload.get("failed_step_count") or 0) > 0
    ):
        if subtask is not None:
            relevant_failures = _workspace_verify_relevant_failure_texts(
                latest_payload,
                latest_row.get("result_preview") or latest_row.get("result") or "",
                subtask=subtask,
                subtask_id=subtask_id,
                subtasks=subtasks or [],
            )
            if not relevant_failures:
                return ""
        failed_raw = latest_payload.get("failed_step_count")
        failed_text = (
            str(int(failed_raw))
            if failed_raw is not None and str(failed_raw).strip() != ""
            else "unknown"
        )
        return (
            "ERROR: mark_subtask_complete rejected: latest "
            f"`run_workspace_verify` for subtask `{subtask_id}` still reports "
            f"{failed_text} failed required step(s) relevant to this subtask or "
            "its touched files. Fix the reported verification gaps and rerun "
            "`run_workspace_verify` so it passes or only has failures owned by "
            "later planned subtasks before closing."
        )
    for row in rows[latest_verify_idx + 1 :]:
        if _tool_row_is_successful_repair_write(row):
            return (
                "ERROR: mark_subtask_complete rejected: workspace was modified "
                f"after the latest passing `run_workspace_verify` for subtask "
                f"`{subtask_id}`. Rerun `run_workspace_verify` so Umbrella "
                "source-policy and integration evidence reflect the current "
                "workspace before closing."
            )
    return ""


_COMPLETION_LLM_MEMORY_TOKEN_RE = re.compile(
    r"(?:\bOPENAI_(?:API_KEY|BASE_URL|MODEL)\b|OPENAI_\*|"
    r"\bOUROBOROS_LLM_MODEL\b|"
    r"\bOUROBOROS_LLM_API_KEY\b|\bOUROBOROS_LLM_BASE_URL\b|"
    r"\bOUROBOROS_MODEL\b|\bapi\.openai\.com\b|"
    r"\bgpt-[A-Za-z0-9_.-]+\b)",
    re.IGNORECASE,
)
_COMPLETION_LLM_MEMORY_PROTECTIVE_RE = re.compile(
    r"\b(?:no|not|without|never|reject(?:ed|s)?|forbid(?:s|den)?|"
    r"block(?:ed|s)?|remove(?:d|s)?|clear(?:ed|s)?|unset|delete(?:d|s)?|"
    r"does\s+not|must\s+not)\b",
    re.IGNORECASE,
)
_COMPLETION_LLM_MEMORY_POSITIVE_RE = re.compile(
    r"\b(?:support(?:s|ed|ing)?|read(?:s|ing)?|load(?:s|ed|ing)?|"
    r"use(?:s|d|ing)?|accept(?:s|ed|ing)?|implemented|implements|"
    r"environment\s+variable(?:s)?|env\s+var(?:s)?)\b",
    re.IGNORECASE,
)
_SUPPORTED_LLM_ALIAS_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Z0-9_])(?:LLM_\*|LLM_API_KEY|LLM_BASE_URL|LLM_MODEL)(?![A-Z0-9_])"
)
_SUPPORTED_LLM_ALIAS_DEPRECATION_RE = re.compile(
    r"(?is)(?:"
    r"\b(?:without|no|not)\s+(?:a\s+)?(?:fallback|fall[-\s]+back)\b"
    r".{0,100}(?<![A-Z0-9_])(?:LLM_\*|LLM_API_KEY|LLM_BASE_URL|LLM_MODEL)(?![A-Z0-9_])|"
    r"(?<![A-Z0-9_])(?:LLM_\*|LLM_API_KEY|LLM_BASE_URL|LLM_MODEL)(?![A-Z0-9_])"
    r".{0,140}\b(?:unsupported|forbidden|deprecat(?:ed|e|ing)|"
    r"remove(?:d|s|ing)?|delete(?:d|s|ing)?|drop(?:ped|s|ping)?|"
    r"needs?\s+to\s+be\s+removed|must\s+be\s+removed|unsupported\s+legacy\s+behavior)\b|"
    r"\b(?:unsupported|forbidden|deprecat(?:ed|e|ing)|"
    r"remove(?:d|s|ing)?|delete(?:d|s|ing)?|drop(?:ped|s|ping)?|"
    r"needs?\s+to\s+be\s+removed|must\s+be\s+removed|unsupported\s+legacy\s+behavior)\b"
    r".{0,140}(?<![A-Z0-9_])(?:LLM_\*|LLM_API_KEY|LLM_BASE_URL|LLM_MODEL)(?![A-Z0-9_])"
    r")"
)
_OUROBOROS_ONLY_LLM_ALIAS_RE = re.compile(
    r"(?is)(?:"
    r"\b(?:support(?:s|ed|ing)?|use(?:s|d|ing)?|read(?:s|ing)?|"
    r"load(?:s|ed|ing)?|resolve(?:s|d|ing)?|prioriti[sz](?:e|es|ed|ing))\b"
    r".{0,100}(?<![A-Z0-9_])(?:OUROBOROS_LLM_\*|OUROBOROS_LLM_API_KEY|"
    r"OUROBOROS_LLM_BASE_URL|OUROBOROS_MODEL|OUROBOROS_\*)(?![A-Z0-9_])"
    r".{0,100}\b(?:exclusively|exclusive|only)\b|"
    r"(?<![A-Z0-9_])(?:OUROBOROS_LLM_\*|OUROBOROS_LLM_API_KEY|"
    r"OUROBOROS_LLM_BASE_URL|OUROBOROS_MODEL|OUROBOROS_\*)(?![A-Z0-9_])"
    r".{0,100}\b(?:exclusively|exclusive|only)\b"
    r")"
)


def _supported_llm_alias_memory_claim_issue(text: str) -> str:
    """Reject memory that narrows Umbrella's supported LLM alias contract."""

    value = str(text or "").strip()
    if not value:
        return ""
    if _SUPPORTED_LLM_ALIAS_DEPRECATION_RE.search(value):
        return (
            "incorrectly treats supported `LLM_*` runtime aliases as legacy, "
            "unsupported, or removable. Generated workspace memory "
            "must preserve `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` as "
            "the public supported aliases."
        )
    if (
        _OUROBOROS_ONLY_LLM_ALIAS_RE.search(value)
        and _SUPPORTED_LLM_ALIAS_TOKEN_RE.search(value) is None
    ):
        return (
            "narrows the runtime contract to `OUROBOROS_*` only. "
            "Generated workspace memory must use the supported public "
            "`LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` aliases when it "
            "describes LLM environment-variable support."
        )
    return ""


def _completion_llm_memory_claim_issue(
    *, subtask_id: str, summary: str = "", notes: str = "", evidence: list[str] | None = None
) -> str:
    """Block false or forbidden LLM-runtime claims from becoming phase memory."""

    items = [summary, notes, *(evidence or [])]
    for item in items:
        text = str(item or "")
        if not text.strip():
            continue
        supported_alias_issue = _supported_llm_alias_memory_claim_issue(text)
        if supported_alias_issue:
            return (
                "ERROR: mark_subtask_complete rejected: completion memory for "
                f"`{subtask_id}` {supported_alias_issue}"
            )
        match = _COMPLETION_LLM_MEMORY_TOKEN_RE.search(text)
        if not match:
            continue
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 80)
        window = text[start:end]
        protective = bool(_COMPLETION_LLM_MEMORY_PROTECTIVE_RE.search(window))
        positive = bool(_COMPLETION_LLM_MEMORY_POSITIVE_RE.search(window))
        if protective and not positive:
            continue
        return (
            "ERROR: mark_subtask_complete rejected: completion memory for "
            f"`{subtask_id}` claims unsupported or forbidden LLM runtime "
            f"contract `{match.group(0)}`. Generated workspace code/tests/docs "
            "must use the public aliases LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL "
            "only. Umbrella maps host control-plane launch env into those public "
            "aliases before workspace commands run. Remove the false evidence or repair the implementation/tests "
            "before marking the subtask done."
        )
    return ""


def _row_position_after(
    candidate_time: float | None,
    candidate_pos: int,
    baseline_time: float | None,
    baseline_pos: int,
) -> bool:
    if candidate_pos < 0 or baseline_pos < 0:
        return False
    if candidate_time is not None and baseline_time is not None:
        return candidate_time > baseline_time
    return candidate_pos > baseline_pos


def _tool_row_is_successful_repair_write(row: dict[str, Any]) -> bool:
    row_tool = str(row.get("tool") or "")
    if row_tool not in _PHASE_SUBTASK_REPAIR_WRITE_TOOLS:
        return False
    payload = _tool_row_result_payload(row)
    if row_tool == "apply_workspace_patch":
        return (
            str(payload.get("status") or "").lower() == "applied"
            and bool(payload.get("applied"))
        )
    if row_tool == "replace_workspace_file":
        return (
            str(payload.get("status") or "").lower() == "ok"
            and bool(str(payload.get("path") or "").strip())
        )
    if row_tool == "update_workspace_seed":
        text = str(row.get("result_preview") or row.get("result") or "").strip()
        return (
            text.startswith("Updated ")
            and "WARNING:" not in text
            and "ERROR:" not in text
        )
    return False


def _valid_retry_watcher_payload(
    payload: dict[str, Any], *, subtask_id: str, proof_command: str
) -> dict[str, Any] | None:
    if not payload:
        return None
    if str(payload.get("status") or "") != "review_recorded":
        return None
    if str(payload.get("reviewer") or "") != "umbrella":
        return None
    if str(payload.get("review_kind") or "") != "retry_watcher":
        return None
    if str(payload.get("subtask_id") or "").strip() != str(subtask_id or "").strip():
        return None
    recorded_command = str(
        payload.get("proof_command") or payload.get("success_test") or ""
    ).strip()
    if recorded_command != str(proof_command or "").strip():
        return None
    try:
        failed_attempts = int(payload.get("failed_attempts") or 0)
    except (TypeError, ValueError):
        return None
    if failed_attempts < 1:
        return None
    return payload


def _tool_row_retry_watcher_payload(
    row: dict[str, Any], *, subtask_id: str, proof_command: str
) -> dict[str, Any] | None:
    if str(row.get("tool") or "") != "request_watcher_review":
        return None
    payload = _tool_row_result_payload(row)
    return _valid_retry_watcher_payload(
        payload,
        subtask_id=subtask_id,
        proof_command=proof_command,
    )


def _phase_subtask_retry_context(ctx: ToolContext) -> dict[str, Any] | None:
    if not _is_phase_run_context(ctx):
        return None
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if not task_id:
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
    subtask_id = str(first.get("id") or "").strip()
    proof_command = _subtask_typed_proof_command_text(first)
    groups = _proof_retry_command_groups(proof_command)
    if not subtask_id or not groups:
        return None
    return {
        "task_id": task_id,
        "subtask_id": subtask_id,
        "proof_command": proof_command,
        "active_subtask": json_ready(first),
        "groups": groups,
        "required_context_reads": sorted(_subtask_referenced_paths(first)),
    }


def _phase_subtask_retry_state(ctx: ToolContext) -> dict[str, Any] | None:
    context = _phase_subtask_retry_context(ctx)
    if not context:
        return None
    task_id = str(context["task_id"])
    subtask_id = str(context["subtask_id"])
    proof_command = str(context["proof_command"])
    groups = context["groups"]
    exact_groups = _proof_command_groups(proof_command)

    rows = _tool_log_rows_for_task(ctx, task_id)
    mutate_cutoff: float | None = None
    for row in rows:
        if str(row.get("tool") or "") not in {
            "mutate_phase_plan",
            "apply_plan_revision_patch",
        }:
            continue
        ok, _reason = _tool_row_success_status(row)
        if not ok:
            continue
        ts = _tool_row_time(row)
        if ts is not None and (mutate_cutoff is None or ts > mutate_cutoff):
            mutate_cutoff = ts
    failures = 0
    latest_failure_time: float | None = None
    latest_failure_pos = -1
    latest_failure_row: dict[str, Any] | None = None
    latest_failure_reason = ""
    latest_failure_evidence_row: dict[str, Any] | None = None
    latest_failure_evidence_reason = ""
    failure_events: list[tuple[float | None, int]] = []
    latest_declared_success_time: float | None = None
    latest_declared_success_pos = -1
    latest_watcher_time: float | None = None
    latest_watcher_pos = -1
    watcher_review_keys: set[str] = set()
    latest_repair_time: float | None = None
    latest_repair_pos = -1

    for idx, row in enumerate(rows):
        row_tool = str(row.get("tool") or "")
        row_time = _tool_row_time(row)
        watcher_payload = _tool_row_retry_watcher_payload(
            row,
            subtask_id=subtask_id,
            proof_command=proof_command,
        )
        if watcher_payload:
            watcher_key = str(watcher_payload.get("signal_id") or f"tool:{idx}")
            watcher_review_keys.add(watcher_key)
            if row_time is not None:
                latest_watcher_time = row_time
            latest_watcher_pos = idx
            continue
        if _tool_row_is_successful_repair_write(row):
            if row_time is not None:
                latest_repair_time = row_time
            latest_repair_pos = idx
            continue
        if row_tool not in _PHASE_SUBTASK_COMMAND_TOOLS:
            if latest_declared_success_pos >= 0 and _row_position_after(
                row_time,
                idx,
                latest_declared_success_time,
                latest_declared_success_pos,
            ):
                payload = _tool_row_result_payload(row)
                preview = str(row.get("result_preview") or row.get("result") or "")
                try:
                    failed_step_count = int(payload.get("failed_step_count") or 0)
                except (TypeError, ValueError):
                    failed_step_count = 0
                is_verify_failure = (
                    row_tool == "run_workspace_verify"
                    and (
                        payload.get("passed") is False
                        or failed_step_count > 0
                    )
                )
                is_completion_verify_failure = (
                    row_tool == "mark_subtask_complete"
                    and preview.lstrip().startswith("ERROR:")
                    and "verify" in preview.lower()
                    and "fail" in preview.lower()
                )
                if is_verify_failure or is_completion_verify_failure:
                    ok, reason = _tool_row_success_status(row)
                    if ok:
                        continue
                    failures += 1
                    if row_time is not None:
                        latest_failure_time = row_time
                    latest_failure_pos = idx
                    latest_failure_row = row
                    latest_failure_reason = reason
                    latest_failure_evidence_row = row
                    latest_failure_evidence_reason = reason
                    failure_events.append((row_time, idx))
            continue
        if (
            mutate_cutoff is not None
            and row_time is not None
            and row_time < mutate_cutoff
        ):
            continue
        norms = _tool_row_command_norms(row)
        if not any(
            alt and any(alt in norm for norm in norms)
            for alternatives in groups
            for alt in alternatives
        ):
            continue
        exact_declared_match = any(
            alt and any(alt == norm for norm in norms)
            for alternatives in exact_groups
            for alt in alternatives
        )
        if _tool_row_is_blocked_before_execution(row):
            continue
        ok, reason = _tool_row_success_status(row)
        if ok:
            failures = 0
            latest_failure_time = None
            latest_failure_pos = -1
            latest_failure_row = None
            latest_failure_reason = ""
            latest_failure_evidence_row = None
            latest_failure_evidence_reason = ""
            latest_declared_success_time = row_time
            latest_declared_success_pos = idx
            continue
        failures += 1
        if row_time is not None:
            latest_failure_time = row_time
        latest_failure_pos = idx
        latest_failure_row = row
        latest_failure_reason = reason
        if exact_declared_match or latest_failure_evidence_row is None:
            latest_failure_evidence_row = row
            latest_failure_evidence_reason = reason
        failure_events.append((row_time, idx))

    post_watcher_failures = 0
    if latest_watcher_pos >= 0:
        for event_time, event_pos in failure_events:
            if _row_position_after(
                event_time,
                event_pos,
                latest_watcher_time,
                latest_watcher_pos,
            ):
                post_watcher_failures += 1

    return {
        **context,
        "failures": failures,
        "post_watcher_failures": post_watcher_failures,
        "latest_failure_time": latest_failure_time,
        "latest_failure_pos": latest_failure_pos,
        "latest_failure_row": latest_failure_row,
        "latest_failure_reason": latest_failure_reason,
        "latest_failure_evidence_row": latest_failure_evidence_row,
        "latest_failure_evidence_reason": latest_failure_evidence_reason,
        "latest_watcher_time": latest_watcher_time,
        "latest_watcher_pos": latest_watcher_pos,
        "watcher_reviews": len(watcher_review_keys),
        "latest_repair_time": latest_repair_time,
        "latest_repair_pos": latest_repair_pos,
    }


def _short_retry_excerpt(value: Any, limit: int = 1200) -> str:
    text = str(value or "").replace("\r", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 4)].rstrip() + " ..."


def _phase_subtask_retry_recommendation(
    *, failed_attempts: int, watcher_reviews: int, patch_guidance: str = ""
) -> str:
    patch_guidance = str(patch_guidance or "").strip()
    if (
        failed_attempts >= _PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD + 2
        or watcher_reviews >= 2
    ):
        broad_guidance = (
            "Stop chasing only the latest single error. Before the next repair, "
            "read the full declared proof target file and every source file "
            "named by recent failures, compare the schema/API/field contract "
            "end-to-end, record the concise audit in memory or notes, then "
            "apply one comprehensive repair with `apply_workspace_patch` and "
            "rerun the exact declared proof."
        )
        return f"{patch_guidance} {broad_guidance}" if patch_guidance else broad_guidance
    if patch_guidance:
        return patch_guidance
    return (
        "Apply one focused implementation repair based on the latest declared "
        "proof failure, then rerun that exact proof."
    )


def _retry_path_is_test_path(path: Any) -> bool:
    text = str(path or "").replace("\\", "/").strip().lower()
    if not text:
        return False
    leaf = text.rsplit("/", 1)[-1]
    return (
        text.startswith("tests/")
        or "/tests/" in text
        or leaf.startswith("test_")
        or leaf.endswith("_test.py")
        or ".spec." in leaf
        or ".test." in leaf
    )


def _implementation_retry_recommendation(paths: list[str]) -> str:
    source_paths = [
        str(path)
        for path in paths
        if str(path).strip() and not _retry_path_is_test_path(path)
    ]
    source_part = (
        " Source focus: " + ", ".join(source_paths[:8]) + "."
        if source_paths
        else ""
    )
    return (
        "Watcher classified this as an implementation repair. Do not edit the "
        "declared test/proof oracle and do not loop back only to escape a write "
        "guard. Read the source/test context if needed, apply one focused "
        "implementation patch in the active subtask scope, then rerun the exact "
        f"declared proof.{source_part}"
    )


_BAD_GENERATED_SUCCESS_TEST_TEXT_LINT_RE = re.compile(
    r"\b("
    r"bad\s+generated\s+(?:success[-_\s]?test|test|test\s+contract)|"
    r"generated\s+(?:success[-_\s]?test|test)\s+(?:contract\s+)?"
    r"(?:is\s+)?(?:wrong|invalid|contradictory|inconsistent|impossible)|"
    r"(?:generated\s+)?(?:test|proof|oracle)\s+contract\s+"
    r"(?:itself\s+)?(?:has|contains|is)\s+"
    r"(?:bugs?|errors?|wrong|invalid|contradictory|inconsistent|impossible|unfixable)|"
    r"(?:test|proof|oracle)\s+contract\b.{0,160}\b"
    r"(?:bugs?|errors?|wrong|invalid|contradictory|inconsistent|impossible|unfixable)|"
    r"generated\s+test\s+file\b.{0,160}\b"
    r"(?:bugs?|errors?|wrong|invalid|contradictory|inconsistent|impossible|unfixable)|"
    r"mathematically\s+(?:wrong|impossible)|"
    r"expected\s+value\s+is\s+mathematically\s+wrong|"
    r"internally\s+(?:inconsistent|contradictory)|"
    r"contradicts?\s+(?:itself|the\s+accepted\s+plan)|"
    r"test\s+(?:needs|should|must)\s+(?:be\s+)?(?:changed|updated|repaired|"
    r"migrated|fixed|adjusted)|"
    r"proposed\s+fix\s*:\s*change\s+line|"
    r"only\s+(?:sets|provides|supplies)\b"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContractDelta:
    """Typed contract change required before a recovery mutation can be accepted."""

    op: Literal["remove", "replace", "add"]
    path: str
    values: tuple[str, ...] = ()
    replacement: Any | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "path": self.path}
        if self.values:
            payload["values"] = list(self.values)
        if self.replacement is not None:
            payload["replacement"] = json_ready(self.replacement)
        return payload


@dataclass(frozen=True)
class RetryContractIssue:
    """Typed recovery issue; prose can annotate it but cannot create it."""

    code: Literal[
        "bad_generated_oracle",
        "plan_contract_issue",
        "proof_scope_mismatch",
        "proof_execution_infra",
        "need_more_context",
    ]
    severity: Literal["info", "warning", "blocking"]
    target_subtask_id: str
    target_path: str = ""
    contract_path: str = ""
    invalid_values: tuple[str, ...] = ()
    required_removals: tuple[ContractDelta, ...] = ()
    required_replacements: tuple[ContractDelta, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    failure_hash: str = ""
    evidence: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "target_subtask_id": self.target_subtask_id,
        }
        if self.target_path:
            payload["target_path"] = self.target_path
        if self.contract_path:
            payload["contract_path"] = self.contract_path
        if self.invalid_values:
            payload["invalid_values"] = list(self.invalid_values)
        deltas = [*self.required_removals, *self.required_replacements]
        if deltas:
            payload["required_deltas"] = [delta.to_payload() for delta in deltas]
        if self.evidence_refs:
            payload["evidence_refs"] = list(self.evidence_refs)
        if self.failure_hash:
            payload["failure_hash"] = self.failure_hash
        if self.evidence:
            payload["evidence"] = self.evidence
        return payload


_PLAN_REVISION_DELTA_PATH_PREFIXES = (
    "proof.",
    "proof:",
    "subtask:",
)
_PLAN_REVISION_DELTA_PATHS = {
    "files_to_create",
    "files_to_change",
    "files_affected",
    "proof",
    "harness_profile",
    "harness_options",
    "required_capabilities",
}


def _is_plan_revision_delta_path(path: str) -> bool:
    text = str(path or "").strip()
    if not text:
        return False
    if text in _PLAN_REVISION_DELTA_PATHS:
        return True
    if any(text.startswith(prefix) for prefix in _PLAN_REVISION_DELTA_PATH_PREFIXES):
        return True
    return False


def _invalid_values_target_required_properties(
    raw: dict[str, Any],
    *,
    contract_path: str,
) -> bool:
    return (
        isinstance(raw.get("invalid_required_properties"), list)
        or "required_properties" in str(contract_path or "")
    )


def _contract_delta_from_payload(raw: Any) -> ContractDelta | None:
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    if not _is_plan_revision_delta_path(path):
        return None
    op = str(raw.get("op") or "remove").strip()
    if op not in {"remove", "replace", "add"}:
        return None
    values = tuple(
        str(item).strip()
        for item in (raw.get("values") or [])
        if str(item).strip()
    ) if isinstance(raw.get("values"), list) else ()
    replacement = raw.get("replacement") if "replacement" in raw else None
    return ContractDelta(
        op=op, path=path, values=values, replacement=replacement
    )


def _typed_contract_issues_from_latest_failure(
    *,
    subtask_id: str,
    latest_failure: dict[str, Any],
) -> list[RetryContractIssue]:
    issues: list[RetryContractIssue] = []
    failure_hash = _retry_failure_hash(latest_failure)
    raw_issues = latest_failure.get("contract_issues")
    default_evidence_refs = tuple(
        str(item).strip()
        for item in (latest_failure.get("evidence_refs") or [])
        if str(item).strip()
    ) if isinstance(latest_failure.get("evidence_refs"), list) else ()
    if isinstance(raw_issues, list):
        for raw in raw_issues:
            if not isinstance(raw, dict):
                continue
            code = str(raw.get("code") or "").strip()
            if code not in {"bad_generated_oracle", "plan_contract_issue"}:
                continue
            target = str(raw.get("target_subtask_id") or subtask_id).strip()
            if not target:
                continue
            raw_deltas = raw.get("required_deltas")
            if not isinstance(raw_deltas, list):
                raw_deltas = raw.get("required_removals")
            removals = tuple(
                delta
                for delta in (
                    _contract_delta_from_payload(item)
                    for item in (raw_deltas or [])
                )
                if delta is not None
            ) if isinstance(raw_deltas, list) else ()
            invalid_values = tuple(
                str(item).strip()
                for item in (
                    raw.get("invalid_values")
                    or raw.get("invalid_required_properties")
                    or []
                )
                if str(item).strip()
            ) if isinstance(
                raw.get("invalid_values") or raw.get("invalid_required_properties"),
                list,
            ) else ()
            contract_path = str(raw.get("contract_path") or "proof.required_properties")
            if (
                invalid_values
                and not removals
                and _invalid_values_target_required_properties(
                    raw,
                    contract_path=contract_path,
                )
            ):
                removals = (
                    ContractDelta(
                        op="remove",
                        path=contract_path,
                        values=invalid_values,
                    ),
                )
            if not removals and not invalid_values:
                continue
            issues.append(
                RetryContractIssue(
                    code="bad_generated_oracle",
                    severity="blocking",
                    target_subtask_id=target,
                    target_path=str(raw.get("target_path") or "").strip(),
                    contract_path=str(
                        raw.get("contract_path")
                        or (removals[0].path if removals else "")
                    ),
                    invalid_values=invalid_values,
                    required_removals=removals,
                    evidence_refs=tuple(
                        str(item).strip()
                        for item in (raw.get("evidence_refs") or [])
                        if str(item).strip()
                    ) if isinstance(raw.get("evidence_refs"), list) else default_evidence_refs,
                    failure_hash=str(raw.get("failure_hash") or failure_hash),
                    evidence=str(raw.get("evidence") or "").strip(),
                )
            )

    invalid_required_properties = tuple(
        str(item).strip()
        for item in (latest_failure.get("invalid_required_properties") or [])
        if str(item).strip()
    ) if isinstance(latest_failure.get("invalid_required_properties"), list) else ()
    raw_deltas = latest_failure.get("required_deltas")
    if not isinstance(raw_deltas, list):
        raw_deltas = latest_failure.get("required_removals")
    required_removals = tuple(
        delta
        for delta in (
            _contract_delta_from_payload(item)
            for item in (raw_deltas or [])
        )
        if delta is not None
    ) if isinstance(raw_deltas, list) else ()
    if invalid_required_properties or required_removals:
        if invalid_required_properties and not required_removals:
            required_removals = (
                ContractDelta(
                    op="remove",
                    path="proof.required_properties",
                    values=invalid_required_properties,
                ),
            )
        issues.append(
            RetryContractIssue(
                code="bad_generated_oracle",
                severity="blocking",
                target_subtask_id=subtask_id,
                contract_path=required_removals[0].path if required_removals else "",
                invalid_values=invalid_required_properties,
                required_removals=required_removals,
                failure_hash=failure_hash,
            )
        )
    return issues


def _normalise_requested_contract_issues(
    raw_issues: Any,
    *,
    subtask_id: str,
    latest_failure: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(raw_issues, list):
        return []
    default_evidence_refs = [
        str(item).strip()
        for item in (latest_failure.get("evidence_refs") or [])
        if str(item).strip()
    ] if isinstance(latest_failure.get("evidence_refs"), list) else []
    normalised: list[dict[str, Any]] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip()
        if code not in {"bad_generated_oracle", "plan_contract_issue"}:
            continue
        target = str(raw.get("target_subtask_id") or subtask_id).strip()
        contract_path = str(raw.get("contract_path") or "").strip()
        invalid_values = [
            str(item).strip()
            for item in (raw.get("invalid_values") or [])
            if str(item).strip()
        ] if isinstance(raw.get("invalid_values"), list) else []
        deltas = [
            delta.to_payload()
            for delta in (
                _contract_delta_from_payload(item)
                for item in (raw.get("required_deltas") or [])
            )
            if delta is not None
        ] if isinstance(raw.get("required_deltas"), list) else []
        if not contract_path and deltas:
            contract_path = str(deltas[0].get("path") or "").strip()
        if not contract_path:
            continue
        if (
            not deltas
            and invalid_values
            and _invalid_values_target_required_properties(
                raw,
                contract_path=contract_path,
            )
        ):
            deltas = [
                {
                    "op": "remove",
                    "path": contract_path,
                    "values": invalid_values,
                }
            ]
        if not deltas and invalid_values:
            continue
        if not deltas and not invalid_values:
            continue
        evidence_refs = [
            str(item).strip()
            for item in (raw.get("evidence_refs") or [])
            if str(item).strip()
        ] if isinstance(raw.get("evidence_refs"), list) else default_evidence_refs
        item = {
            "code": code,
            "target_subtask_id": target,
            "contract_path": contract_path,
            "invalid_values": invalid_values,
            "required_deltas": deltas,
            "evidence_refs": evidence_refs,
        }
        evidence = str(raw.get("evidence") or "").strip()
        if evidence:
            item["evidence"] = evidence
        normalised.append(item)
    return normalised


def _bad_generated_success_test_text_lints(
    *,
    reason: str,
    latest_failure: dict[str, Any],
) -> list[dict[str, Any]]:
    failure_text = ""
    for key in ("reason", "output_excerpt", "stderr", "stdout"):
        value = latest_failure.get(key)
        if isinstance(value, str) and value.strip():
            failure_text += "\n" + value
    evidence_text = f"{reason}\n{failure_text}".strip()
    if not evidence_text:
        return []
    if not _BAD_GENERATED_SUCCESS_TEST_TEXT_LINT_RE.search(evidence_text):
        return []
    return [
        {
                "code": "possible_bad_generated_oracle_text",
                "confidence": "low",
                "source": "text_lint",
                "message": (
                    "Free-text evidence resembles a bad generated oracle, but route "
                    "requires a typed contract issue with required_deltas."
                ),
            }
    ]


def _plan_revision_patch_from_typed_contract_issues(
    *,
    proof_command: str,
    subtask_id: str,
    latest_failure: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a plan-contract recovery payload from typed oracle issues only."""

    issues = _typed_contract_issues_from_latest_failure(
        subtask_id=subtask_id,
        latest_failure=latest_failure,
    )
    if not issues:
        return None
    primary = issues[0]
    required_deltas: list[dict[str, Any]] = []
    for issue in issues:
        for delta in issue.required_removals:
            delta_payload = delta.to_payload()
            if delta_payload not in required_deltas:
                required_deltas.append(delta_payload)
    if not required_deltas:
        return None
    evidence_refs: list[str] = []
    for issue in issues:
        for ref in issue.evidence_refs:
            if ref and ref not in evidence_refs:
                evidence_refs.append(ref)
    files = _retry_proof_target_files(proof_command)
    payload: dict[str, Any] = {
        "target_subtask_id": primary.target_subtask_id or subtask_id,
        "reason_code": primary.code,
        "contract_path": primary.contract_path,
        "invalid_values": list(primary.invalid_values),
        "required_deltas": required_deltas,
        "evidence_refs": evidence_refs,
        "evidence": primary.evidence or _short_retry_excerpt(
            latest_failure.get("output_excerpt") or latest_failure.get("reason") or ""
        ),
        "contract_issues": [issue.to_payload() for issue in issues],
        "proof_command": proof_command,
        "failure_hash": _retry_failure_hash(latest_failure),
    }
    if files:
        payload["target_files"] = files
    return payload


def _plan_revision_retry_recommendation(revision_patch: dict[str, Any]) -> str:
    files = revision_patch.get("target_files") or []
    if isinstance(files, str):
        files = [files]
    file_list = ", ".join(str(file_path) for file_path in files if str(file_path))
    target = f" for {file_list}" if file_list else ""
    subtask_id = str(revision_patch.get("target_subtask_id") or "").strip()
    selector = (
        f'target_subtask_id="{subtask_id}", '
        if subtask_id
        else ""
    )
    return (
        "Watcher recorded a typed ContractIssue for the generated proof oracle"
        f"{target}. Route to `plan` and revise the subtask proof contract with "
        "a semantic proof patch that satisfies every required_delta. Use "
        f"`apply_plan_revision_patch({selector}deltas=[...], patch={{"
        "\"required_deltas\": [...], "
        "\"proof\": { ... revised typed proof contract ... }"
        "})`; metadata or notes alone will be rejected."
    )


@dataclass(frozen=True)
class RecoveryDecision:
    """Single typed source for retry-watcher routing decisions."""

    decision_id: str
    kind: Literal[
        "implementation_repair",
        "plan_contract_revision",
        "proof_execution_infra",
        "need_more_context",
        "blocked_no_valid_next_action",
    ]
    trigger_code: str
    active_subtask_id: str
    failure_hash: str = ""
    blocker_fingerprint: str = ""
    loop_back_target: Literal[
        "execute", "plan", "subtask_review", "research", "none"
    ] = "execute"
    issues: list[dict[str, Any]] = field(default_factory=list)
    required_plan_changes: list[dict[str, Any]] = field(default_factory=list)
    plan_revision_patch: dict[str, Any] | None = None
    allowed_next_actions: list[str] = field(default_factory=list)
    forbidden_next_actions: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    freshness_refs: list[str] = field(default_factory=list)
    evidence: str = ""


def _recovery_decision_payload(decision: RecoveryDecision) -> dict[str, Any]:
    payload = {
        "decision_id": decision.decision_id,
        "kind": decision.kind,
        "trigger_code": decision.trigger_code,
        "active_subtask_id": decision.active_subtask_id,
        "failure_hash": decision.failure_hash,
        "blocker_fingerprint": decision.blocker_fingerprint,
        "loop_back_target": decision.loop_back_target,
        "issues": decision.issues,
        "required_plan_changes": decision.required_plan_changes,
        "allowed_next_actions": decision.allowed_next_actions,
        "forbidden_next_actions": decision.forbidden_next_actions,
        "evidence_refs": decision.evidence_refs,
        "freshness_refs": decision.freshness_refs,
    }
    if decision.plan_revision_patch:
        payload["plan_revision_patch"] = decision.plan_revision_patch
    if decision.evidence:
        payload["evidence"] = decision.evidence
    return payload


def _retry_failure_hash(latest_failure: dict[str, Any]) -> str:
    if not latest_failure:
        return ""
    return hash_value({"latest_failure": latest_failure})[:16]


def _current_plan_version(ctx: ToolContext) -> int:
    plan = _read_phase_plan(ctx)
    if not isinstance(plan, dict):
        return 0
    try:
        return int(plan.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def _phase_id_from_retry_context(ctx: ToolContext) -> str:
    overlays = _context_overlays(ctx)
    for key in ("phase_node", "phase_manifest"):
        node = overlays.get(key)
        if isinstance(node, dict):
            value = str(node.get("id") or node.get("manifest_id") or "").strip()
            if value:
                return value
    task_id = str(getattr(ctx, "task_id", "") or "")
    parts = [part for part in task_id.split(":") if part]
    return parts[1] if len(parts) > 1 else ""


def _same_blocker_fingerprint(
    ctx: ToolContext,
    *,
    state: dict[str, Any],
    decision: RecoveryDecision,
) -> str:
    active_subtask = state.get("active_subtask")
    proof_contract = (
        active_subtask.get("proof")
        if isinstance(active_subtask, dict)
        else {}
    )
    task_id = str(state.get("task_id") or getattr(ctx, "task_id", "") or "")
    return hash_value(
        {
            "run_id": _run_id(ctx),
            "task_id": task_id,
            "phase_id": _phase_id_from_retry_context(ctx),
            "active_subtask_id": decision.active_subtask_id,
            "plan_version": _current_plan_version(ctx),
            "proof_contract_hash": hash_value(json_ready(proof_contract))[:16],
            "failure_hash": decision.failure_hash,
            "blocker_code": decision.trigger_code,
            "decision_kind": decision.kind,
            "workspace_change_marker": {
                "latest_repair_pos": state.get("latest_repair_pos"),
                "latest_repair_time": state.get("latest_repair_time"),
            },
        }
    )[:24]


def _record_same_blocker_fingerprint(
    ctx: ToolContext,
    *,
    fingerprint: str,
    decision_payload: dict[str, Any],
) -> dict[str, Any]:
    if not fingerprint:
        return {"fingerprint": "", "count": 0, "threshold": 2}
    state_dir = pathlib.Path(getattr(ctx, "drive_root", "")) / "state"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return {"fingerprint": fingerprint, "count": 1, "threshold": 2}
    path = state_dir / "same_blocker_guard.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    entries = payload.get("fingerprints")
    if not isinstance(entries, dict):
        entries = {}
    entry = entries.get(fingerprint)
    if not isinstance(entry, dict):
        entry = {"count": 0, "first_seen": time.time()}
    try:
        count = int(entry.get("count") or 0) + 1
    except (TypeError, ValueError):
        count = 1
    entry.update(
        {
            "count": count,
            "last_seen": time.time(),
            "last_decision": json_ready(decision_payload),
        }
    )
    entries[fingerprint] = entry
    payload["fingerprints"] = entries
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {
        "fingerprint": fingerprint,
        "count": count,
        "threshold": 2,
        "state_ref": "artifact:state/same_blocker_guard.json",
    }


def _same_blocker_blocked_decision(
    *,
    previous: RecoveryDecision,
    guard: dict[str, Any],
) -> RecoveryDecision:
    return RecoveryDecision(
        decision_id=hash_value(
            {
                "kind": "blocked_no_valid_next_action",
                "previous": previous.decision_id,
                "blocker_fingerprint": guard.get("fingerprint"),
                "count": guard.get("count"),
            }
        )[:16],
        kind="blocked_no_valid_next_action",
        trigger_code="same_blocker_repeated",
        active_subtask_id=previous.active_subtask_id,
        failure_hash=previous.failure_hash,
        blocker_fingerprint=str(guard.get("fingerprint") or ""),
        loop_back_target="none",
        issues=previous.issues,
        required_plan_changes=previous.required_plan_changes,
        plan_revision_patch=previous.plan_revision_patch,
        allowed_next_actions=[
            "change the plan contract, proof contract, or workspace state before another retry",
            "surface the existing RecoveryDecision instead of asking the LLM to repeat it",
        ],
        forbidden_next_actions=[
            "repeat the same watcher review without a plan/workspace/proof change",
            "start another execute LLM round for the same blocker fingerprint",
        ],
        evidence_refs=previous.evidence_refs,
        evidence=previous.evidence,
    )


def _plan_contract_revision_decision(
    *,
    subtask_id: str,
    proof_command: str,
    latest_failure: dict[str, Any],
    plan_revision_patch: dict[str, Any],
) -> RecoveryDecision:
    patch = dict(plan_revision_patch)
    files = patch.get("target_files") or []
    if isinstance(files, str):
        files = [files]
    target = str(patch.get("target_subtask_id") or subtask_id).strip()
    evidence = str(patch.get("evidence") or "").strip()
    failure_hash = _retry_failure_hash(latest_failure)
    patch["target_subtask_id"] = target
    patch["reason_code"] = str(patch.get("reason_code") or "bad_generated_oracle")
    patch["failure_hash"] = failure_hash
    patch["proof_command"] = proof_command
    patch.setdefault(
        "revision_id",
        hash_value(
            {
                "kind": "plan_contract_revision",
                "target_subtask_id": target,
                "proof_command": proof_command,
                "failure_hash": failure_hash,
                "required_deltas": patch.get("required_deltas") or [],
            }
        )[:16],
    )
    if evidence:
        patch["evidence"] = evidence
    raw_issues = patch.get("contract_issues")
    issues = (
        [json_ready(item) for item in raw_issues if isinstance(item, dict)]
        if isinstance(raw_issues, list)
        else []
    )
    if not issues:
        issues = [
            {
                "code": patch["reason_code"],
                "severity": "blocking",
                "target_subtask_id": target,
                "contract_path": str(patch.get("contract_path") or ""),
                "invalid_values": json_ready(patch.get("invalid_values") or []),
                "required_deltas": json_ready(patch.get("required_deltas") or []),
                "evidence_refs": json_ready(patch.get("evidence_refs") or []),
            }
        ]
    required_change = {
        "target_subtask_id": target,
        "reason_code": patch["reason_code"],
        "contract_path": str(patch.get("contract_path") or ""),
        "invalid_values": json_ready(patch.get("invalid_values") or []),
        "required_deltas": json_ready(patch.get("required_deltas") or []),
        "evidence_refs": json_ready(patch.get("evidence_refs") or []),
    }
    return RecoveryDecision(
        decision_id=hash_value(
            {
                "kind": "plan_contract_revision",
                "subtask_id": target,
                "proof_command": proof_command,
                "failure_hash": failure_hash,
                "revision_patch": patch,
            }
        )[:16],
        kind="plan_contract_revision",
        trigger_code=patch["reason_code"],
        active_subtask_id=target,
        failure_hash=failure_hash,
        loop_back_target="plan",
        issues=issues,
        required_plan_changes=[required_change],
        plan_revision_patch=json_ready(patch),
        allowed_next_actions=[
            "route to plan contract revision",
            (
                "call apply_plan_revision_patch with a semantic typed proof patch "
                "that satisfies every required_delta"
            ),
            "rerun run_subtask_proof after the plan mutation",
        ],
        forbidden_next_actions=[
            "direct test-file edits before apply_plan_revision_patch records the proof patch",
            "mark_subtask_complete without a fresh proof after contract revision",
        ],
        evidence=evidence,
    )


def _implementation_repair_decision(
    *,
    subtask_id: str,
    trigger_code: str,
    latest_failure: dict[str, Any],
) -> RecoveryDecision:
    failure_hash = _retry_failure_hash(latest_failure)
    return RecoveryDecision(
        decision_id=hash_value(
            {
                "kind": "implementation_repair",
                "subtask_id": subtask_id,
                "trigger_code": trigger_code,
                "failure_hash": failure_hash,
            }
        )[:16],
        kind="implementation_repair",
        trigger_code=trigger_code,
        active_subtask_id=subtask_id,
        failure_hash=failure_hash,
        loop_back_target="execute",
        allowed_next_actions=[
            "read active files",
            "repair implementation",
            "rerun run_subtask_proof",
        ],
        forbidden_next_actions=[
            "test-only oracle edits after a failing proof",
        ],
    )


def _retry_watcher_verdict_payload(
    *,
    status: str,
    failed_attempts: int,
    subtask_id: str,
    decision: RecoveryDecision,
) -> dict[str, Any]:
    """Return the typed retry-watcher decision surfaced to the agent."""

    base = {"recovery_decision": _recovery_decision_payload(decision)}
    if decision.kind == "plan_contract_revision":
        payload = {
            **base,
            "verdict": "bad_test_contract",
            "can_edit_tests": False,
            "requires_plan_mutation": True,
            "loop_back_target": decision.loop_back_target,
            "issues": decision.issues,
            "required_plan_changes": decision.required_plan_changes,
            "allowed_next_actions": decision.allowed_next_actions,
            "forbidden_next_actions": decision.forbidden_next_actions,
        }
        if decision.plan_revision_patch:
            payload["plan_revision_patch"] = decision.plan_revision_patch
        return payload
    if decision.kind == "blocked_no_valid_next_action":
        return {
            **base,
            "verdict": "blocked_no_valid_next_action",
            "can_edit_tests": False,
            "requires_plan_mutation": False,
            "loop_back_target": "none",
            "issues": decision.issues,
            "required_plan_changes": decision.required_plan_changes,
            "allowed_next_actions": decision.allowed_next_actions,
            "forbidden_next_actions": decision.forbidden_next_actions,
        }
    if status != "review_recorded":
        return {
            **base,
            "verdict": "not_required",
            "can_edit_tests": False,
            "requires_plan_mutation": False,
            "allowed_next_actions": decision.allowed_next_actions,
            "forbidden_next_actions": decision.forbidden_next_actions,
        }
    return {
        **base,
        "verdict": "implementation_bug",
        "can_edit_tests": False,
        "requires_plan_mutation": False,
        "allowed_next_actions": decision.allowed_next_actions or [
            "read the declared proof/test and related source files",
            "repair implementation files in active scope",
            "rerun run_subtask_proof after the repair",
        ],
        "forbidden_next_actions": decision.forbidden_next_actions or [
            "test-only oracle edits",
            "weakening assertions or proof selection",
        ],
        "confidence": "medium" if failed_attempts else "low",
    }


def _tool_row_args_payload(row: dict[str, Any]) -> dict[str, Any]:
    args = row.get("args") or {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _patch_text_from_args(args: dict[str, Any]) -> str:
    patch = args.get("patch")
    if isinstance(patch, str):
        return patch
    if isinstance(patch, dict):
        nested = patch.get("patch")
        if isinstance(nested, str):
            return nested
    return ""


def _patch_text_contains_escaped_line_endings(text: str) -> bool:
    for line in str(text or "").splitlines():
        stripped = line.rstrip()
        if (
            stripped.endswith("\\r")
            or stripped.endswith("\\n")
            or "\\r\\n" in stripped
        ):
            return True
    return False


def _recent_patch_hunk_mismatch_signal(ctx: ToolContext, task_id: str) -> dict[str, Any]:
    rows = _tool_log_rows_for_task(ctx, task_id)
    for row in reversed(rows[-300:]):
        if str(row.get("tool") or "") != "apply_workspace_patch":
            continue
        payload = _tool_row_result_payload(row)
        status = str(payload.get("status") or "").lower()
        if status == "applied":
            return {}
        reason = str(payload.get("reason") or "")
        if reason not in {"patch_hunk_mismatch", "patch_parse_error"}:
            continue
        args = _tool_row_args_payload(row)
        patch_text = _patch_text_from_args(args)
        file_path = str(payload.get("file_path") or args.get("file_path") or "").strip()
        if not file_path and "Update File:" in patch_text:
            match = re.search(r"\*\*\* Update File:\s*([^\r\n]+)", patch_text)
            if match:
                file_path = match.group(1).strip()
        escaped = bool(payload.get("escaped_line_endings_detected")) or (
            reason == "patch_hunk_mismatch"
            and _patch_text_contains_escaped_line_endings(patch_text)
        )
        read_hint = str(payload.get("read_file_hint") or "").strip()
        if escaped:
            target = f" for `{file_path}`" if file_path else ""
            hint = f" Use `{read_hint}` first." if read_hint else ""
            guidance = (
                f"Recent `apply_workspace_patch` mismatch{target} appears to "
                "come from JSON-rendered line endings copied into the hunk. "
                "Do not paste literal `\\r` or `\\n`; re-read the smallest "
                "line slice, then emit a tiny `*** Update File:` hunk with "
                f"real patch line breaks and exact source context.{hint}"
            )
            return {
                "guidance": guidance,
                "file_path": file_path,
                "reason": reason,
                "targets_test_path": _retry_path_is_test_path(file_path),
            }
        if reason == "patch_hunk_mismatch":
            target = f" for `{file_path}`" if file_path else ""
            hint = f" Use `{read_hint}` first." if read_hint else ""
            guidance = (
                f"Recent `apply_workspace_patch` mismatch{target} needs an "
                "exact-context repair. Re-read the target with `read_file` "
                "using `line_start`/`line_count`, then retry one tiny hunk "
                f"from the current file content.{hint}"
            )
            return {
                "guidance": guidance,
                "file_path": file_path,
                "reason": reason,
                "targets_test_path": _retry_path_is_test_path(file_path),
            }
    return {}


def _recent_patch_hunk_mismatch_guidance(ctx: ToolContext, task_id: str) -> str:
    return str(_recent_patch_hunk_mismatch_signal(ctx, task_id).get("guidance") or "")


def _phase_subtask_retry_watcher_review_payload(
    ctx: ToolContext,
    *,
    reason: str,
    contract_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    state = _phase_subtask_retry_state(ctx)
    base: dict[str, Any] = {
        "reviewer": "umbrella",
        "review_kind": "retry_watcher",
        "operator_reason": str(reason or "").strip(),
        "threshold": _PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD,
    }
    if not state:
        return {
            **base,
            "status": "review_not_applicable",
            "message": (
                "No active execute subtask with a typed proof command "
                "was found for retry watcher review."
            ),
        }

    failed_attempts = int(state.get("failures") or 0)
    watcher_reviews = int(state.get("watcher_reviews") or 0)
    patch_signal = _recent_patch_hunk_mismatch_signal(
        ctx, str(state.get("task_id") or "")
    )
    patch_guidance = str(patch_signal.get("guidance") or "")
    patch_guidance_targets_test = bool(patch_signal.get("targets_test_path"))
    latest_failure_row = state.get("latest_failure_evidence_row") or state.get(
        "latest_failure_row"
    )
    latest_failure: dict[str, Any] = {}
    if isinstance(latest_failure_row, dict):
        payload = _tool_row_result_payload(latest_failure_row)
        command = payload.get("command") or payload.get("argv")
        if not command:
            args = latest_failure_row.get("args")
            if isinstance(args, dict):
                command = args.get("command") or args.get("argv")
        shell_result = payload.get("shell_result")
        output_excerpt_source = (
            payload.get("output")
            or payload.get("stderr")
            or payload.get("stdout")
            or (
                shell_result.get("output")
                or shell_result.get("stderr")
                or shell_result.get("stdout")
                if isinstance(shell_result, dict)
                else ""
            )
            or latest_failure_row.get("result_preview")
            or ""
        )
        latest_failure = {
            "tool": str(latest_failure_row.get("tool") or ""),
            "command": command,
            "reason": str(
                state.get("latest_failure_evidence_reason")
                or state.get("latest_failure_reason")
                or ""
            ),
            "output_excerpt": _short_retry_excerpt(output_excerpt_source),
        }
        evidence_refs: list[str] = []
        for ref_key in ("proof_ref", "ledger_ref"):
            ref = payload.get(ref_key)
            if not isinstance(ref, dict):
                continue
            ref_id = str(ref.get("ref_id") or "").strip()
            if not ref_id:
                continue
            ref_type = str(ref.get("ref_type") or ref_key).strip() or ref_key
            evidence_refs.append(f"{ref_type}:{ref_id}")
        if evidence_refs:
            latest_failure["evidence_refs"] = evidence_refs
        for key in (
            "contract_issues",
            "invalid_required_properties",
            "required_removals",
        ):
            if isinstance(payload.get(key), list):
                latest_failure[key] = payload.get(key)
    requested_contract_issues = _normalise_requested_contract_issues(
        contract_issues,
        subtask_id=str(state.get("subtask_id") or ""),
        latest_failure=latest_failure,
    )
    if requested_contract_issues:
        latest_failure["contract_issues"] = requested_contract_issues

    has_latest_failure = bool(latest_failure)
    subtask_id = str(state.get("subtask_id") or "")
    proof_command = str(state.get("proof_command") or "")
    plan_revision_patch = _plan_revision_patch_from_typed_contract_issues(
        proof_command=proof_command,
        subtask_id=subtask_id,
        latest_failure=latest_failure,
    )
    text_lints = _bad_generated_success_test_text_lints(
        reason=str(reason or ""),
        latest_failure=latest_failure,
    )
    if plan_revision_patch:
        status = "review_recorded"
        decision = _plan_contract_revision_decision(
            subtask_id=subtask_id,
            proof_command=proof_command,
            latest_failure=latest_failure,
            plan_revision_patch=plan_revision_patch,
        )
    else:
        status = (
            "review_recorded"
            if (
                failed_attempts >= _PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD
                or (failed_attempts > 0 and has_latest_failure and not patch_guidance)
            )
            else "review_not_required"
        )
        decision = _implementation_repair_decision(
            subtask_id=subtask_id,
            trigger_code=(
                "retry_threshold_reached"
                if status == "review_recorded"
                else "below_retry_threshold"
            ),
            latest_failure=latest_failure,
        )
    same_blocker_guard: dict[str, Any] = {}
    if status == "review_recorded":
        fingerprint = _same_blocker_fingerprint(
            ctx,
            state=state,
            decision=decision,
        )
        decision = replace(decision, blocker_fingerprint=fingerprint)
        same_blocker_guard = _record_same_blocker_fingerprint(
            ctx,
            fingerprint=fingerprint,
            decision_payload=_recovery_decision_payload(decision),
        )
        if int(same_blocker_guard.get("count") or 0) > int(
            same_blocker_guard.get("threshold") or 2
        ):
            decision = _same_blocker_blocked_decision(
                previous=decision,
                guard=same_blocker_guard,
            )
    required_context_reads = list(state.get("required_context_reads") or [])[:20]
    review = {
        **base,
        "status": status,
        "subtask_id": subtask_id,
        "proof_command": proof_command,
        "required_context_reads": required_context_reads,
        "failed_attempts": failed_attempts,
        "prior_watcher_reviews": watcher_reviews,
        "latest_failure": latest_failure,
        "text_lints": text_lints,
        "patch_guidance": patch_guidance,
        "same_blocker_guard": same_blocker_guard,
        "recommendation": _phase_subtask_retry_recommendation(
            failed_attempts=failed_attempts,
            watcher_reviews=watcher_reviews,
            patch_guidance=patch_guidance,
        ),
    }
    if plan_revision_patch and decision.kind == "plan_contract_revision":
        review["plan_revision_patch"] = plan_revision_patch
        review["recommendation"] = _plan_revision_retry_recommendation(
            plan_revision_patch
        )
    elif decision.kind == "blocked_no_valid_next_action":
        review["recommendation"] = (
            "The same blocker fingerprint repeated without a plan, proof, or "
            "workspace-state change. Do not ask the LLM to repeat another "
            "repair loop; return the existing typed RecoveryDecision/PhaseRoute "
            "until the contract or workspace state changes."
        )
    review.update(
        _retry_watcher_verdict_payload(
            status=status,
            failed_attempts=failed_attempts,
            subtask_id=subtask_id,
            decision=decision,
        )
    )
    if (
        patch_guidance
        and patch_guidance_targets_test
        and not review.get("can_edit_tests")
        and review.get("verdict") != "bad_test_contract"
    ):
        review["patch_guidance"] = ""
        review["suppressed_patch_guidance"] = patch_guidance
        review["patch_guidance_suppressed_reason"] = (
            "latest patch mismatch targets a protected test/proof oracle while "
            "test edits are not allowed"
        )
        review["recommendation"] = _implementation_retry_recommendation(
            required_context_reads
        )
    if review.get("verdict") == "implementation_bug":
        source_paths = [
            path for path in required_context_reads if not _retry_path_is_test_path(path)
        ]
        review["repair_focus"] = {
            "kind": "implementation_bug",
            "source_files": source_paths,
            "test_oracle_files": [
                path for path in required_context_reads if _retry_path_is_test_path(path)
            ],
            "proof_command": str(state.get("proof_command") or ""),
        }
        if patch_guidance:
            review["secondary_patch_guidance"] = patch_guidance
        review["recommendation"] = _implementation_retry_recommendation(
            required_context_reads
        )
    if status == "review_not_required":
        if review.get("verdict") == "implementation_bug":
            review["message"] = (
                "The active subtask has not yet reached the repeated-failure "
                "threshold, but the next allowed direction is implementation "
                "repair, not test/proof oracle editing."
            )
        elif review.get("suppressed_patch_guidance"):
            review["message"] = (
                "The active subtask has not yet reached the repeated-failure "
                "threshold, and the latest patch mismatch targets a protected "
                "test/proof oracle. Continue with implementation repair and "
                "rerun the declared proof instead of retrying the test patch."
            )
        elif patch_guidance:
            review["message"] = (
                "The active subtask has not yet reached the repeated-failure "
                "threshold, but Umbrella found a patch-mismatch repair signal "
                "in the tool log. Follow the recommendation before retrying "
                "`apply_workspace_patch`."
            )
        else:
            review["message"] = (
                "The active subtask has not yet reached the repeated-failure "
                "threshold; continue normal diagnosis and repair."
            )
    return review


def _phase_subtask_retry_escalation_block(
    ctx: ToolContext, *, tool_name: str
) -> dict[str, Any] | None:
    """Require watcher review after repeated typed proof failures."""

    state = _phase_subtask_retry_state(ctx)
    if not state:
        return None
    subtask_id = str(state.get("subtask_id") or "")
    proof_command = str(state.get("proof_command") or "")
    required_context_reads = list(state.get("required_context_reads") or [])[:20]
    failures = int(state.get("failures") or 0)
    if failures < _PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD:
        return None
    watcher_after_failure = _row_position_after(
        state.get("latest_watcher_time"),
        int(state.get("latest_watcher_pos") or -1),
        state.get("latest_failure_time"),
        int(state.get("latest_failure_pos") or -1),
    )
    if watcher_after_failure:
        repair_after_watcher = _row_position_after(
            state.get("latest_repair_time"),
            int(state.get("latest_repair_pos") or -1),
            state.get("latest_watcher_time"),
            int(state.get("latest_watcher_pos") or -1),
        )
        if tool_name in _PHASE_SUBTASK_COMMAND_TOOLS and not repair_after_watcher:
            next_step = (
                "If you need source context, use `read_file` or `repo_read` "
                "(not shell/grep/python -c). Then apply one focused "
                "implementation repair with `apply_workspace_patch` or "
                "`replace_workspace_file` before rerunning the declared proof."
            )
            if (
                failures >= _PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD + 2
                or int(state.get("watcher_reviews") or 0) >= 2
            ):
                next_step = (
                    "Do not rerun the declared proof yet. First read the "
                    "full failing test and related source files with `read_file` "
                    "or `repo_read` (not shell/grep/python -c), audit the "
                    "schema/API/field contract end-to-end, then apply one "
                    "comprehensive implementation repair with "
                    "`apply_workspace_patch` or `replace_workspace_file`."
                )
            return {
                "status": "blocked",
                "reason": "phase_subtask_repair_required_after_watcher",
                "tool": tool_name,
                "subtask_id": subtask_id,
                "proof_command": proof_command,
                "required_context_reads": required_context_reads,
                "failed_attempts": failures,
                "threshold": _PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD,
                "prior_watcher_reviews": int(state.get("watcher_reviews") or 0),
                "message": (
                    f"The current execute subtask `{subtask_id}` already has "
                    "watcher review for repeated failures, but no successful "
                    "repair write has landed after that review."
                ),
                "allowed_context_tools": ["read_file", "repo_read", "list_files", "repo_list"],
                "forbidden_until_repair": [
                    "shell",
                    "terminal_session",
                    "run_workspace_command",
                    "test-file weakening edits",
                ],
                "next_step": next_step,
            }
        return None
    latest_watcher_pos = int(state.get("latest_watcher_pos") or -1)
    latest_failure_pos = int(state.get("latest_failure_pos") or -1)
    latest_failure_after_watcher = _row_position_after(
        state.get("latest_failure_time"),
        latest_failure_pos,
        state.get("latest_watcher_time"),
        latest_watcher_pos,
    )
    post_watcher_failures = int(state.get("post_watcher_failures") or 0)
    if (
        latest_watcher_pos >= 0
        and latest_failure_after_watcher
        and post_watcher_failures < _PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD
    ):
        repair_after_latest_failure = _row_position_after(
            state.get("latest_repair_time"),
            int(state.get("latest_repair_pos") or -1),
            state.get("latest_failure_time"),
            latest_failure_pos,
        )
        if tool_name in _PHASE_SUBTASK_COMMAND_TOOLS and not repair_after_latest_failure:
            return {
                "status": "blocked",
                "reason": "phase_subtask_repair_required_after_watcher",
                "tool": tool_name,
                "subtask_id": subtask_id,
                "proof_command": proof_command,
                "required_context_reads": required_context_reads,
                "failed_attempts": failures,
                "post_watcher_failed_attempts": post_watcher_failures,
                "threshold": _PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD,
                "prior_watcher_reviews": int(state.get("watcher_reviews") or 0),
                "message": (
                    f"The current execute subtask `{subtask_id}` has a new "
                    "declared proof failure after the latest watcher "
                    "review. Apply a focused repair before rerunning the same "
                    "proof."
                ),
                "allowed_context_tools": ["read_file", "repo_read", "list_files", "repo_list"],
                "forbidden_until_repair": [
                    "shell",
                    "terminal_session",
                    "run_workspace_command",
                    "test-file weakening edits",
                ],
                "next_step": (
                    "If you need source context, use `read_file` or `repo_read` "
                    "(not shell/grep/python -c). Then apply one focused "
                    "implementation repair with `apply_workspace_patch` or "
                    "`replace_workspace_file`, then rerun the declared "
                    "proof. A new watcher review is "
                    "only required if several post-watcher repair/test cycles "
                    "keep failing."
                ),
            }
        return None
    return {
        "status": "blocked",
        "reason": "phase_subtask_retry_escalation_required",
        "tool": tool_name,
        "subtask_id": subtask_id,
        "proof_command": proof_command,
        "required_context_reads": required_context_reads,
        "failed_attempts": failures,
        "post_watcher_failed_attempts": post_watcher_failures,
        "threshold": _PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD,
        "message": (
            f"The current execute subtask `{subtask_id}` has repeated failed "
            "runs of its declared proof without a fresh Umbrella "
            "watcher review record for those failures."
        ),
        "next_step": (
            "Call `request_watcher_review` with the latest failing test, files "
            "changed, and suspected blocker. The returned JSON must show "
            "`status=review_recorded`, `reviewer=umbrella`, and "
            "`review_kind=retry_watcher` before more writes, test reruns, or "
            "completion attempts. Then continue with a focused repair."
        ),
    }


def _phase_subtask_completion_issue(
    ctx: ToolContext,
    *,
    current_phase: dict[str, Any] | None,
    subtask_id: str,
) -> str:
    subtasks = _phase_subtasks(current_phase)
    if not subtasks:
        return ""
    requested = str(subtask_id or "").strip()
    if not requested:
        return "ERROR: subtask_id is required when the current phase has subtask cards"
    known_ids = {str(item.get("id") or "").strip() for item in subtasks}
    phase_id = str((current_phase or {}).get("id") or "").strip()
    if requested not in known_ids and requested != phase_id:
        return f"ERROR: subtask '{requested}' not found in plan"
    first = _first_incomplete_subtask(subtasks)
    if first is None:
        return ""
    first_id = str(first.get("id") or "").strip()
    if requested != first_id:
        return (
            "ERROR: mark_subtask_complete must follow the active phase plan "
            f"order. Next pending subtask is `{first_id}`; cannot mark "
            f"`{requested}` complete yet."
        )
    success_issue = _completion_proof_command_issue(
        ctx, subtask=first, subtask_id=requested
    )
    if success_issue:
        return success_issue
    return _workspace_verify_completion_issue(
        ctx,
        subtask_id=requested,
        subtask=first,
        subtasks=subtasks,
    )


_required_tool_from_success_test = _required_tool_from_proof_command
_strip_success_test_command_label = _strip_proof_command_label
_success_test_command_candidates = _proof_command_candidates
_success_test_retry_command_groups = _proof_retry_command_groups
_success_test_command_groups = _proof_command_groups
_retry_success_test_target_files = _retry_proof_target_files
_completion_success_test_issue = _completion_proof_command_issue


__all__ = [
    '_is_final_review_context',
    '_final_review_e2e_gate',
    '_latest_logged_e2e_result',
    '_current_phase_node',
    '_phase_subtasks',
    '_subtask_status',
    '_first_incomplete_subtask',
    '_required_tool_from_proof_command',
    '_required_tool_from_success_test',
    '_normalise_command_text',
    '_strip_proof_command_label',
    '_strip_success_test_command_label',
    '_proof_command_candidates',
    '_success_test_command_candidates',
    '_command_alternatives',
    '_proof_retry_command_groups',
    '_success_test_retry_command_groups',
    '_proof_command_groups',
    '_success_test_command_groups',
    '_pytest_output_is_skip_only',
    '_tool_row_result_payload',
    '_tool_row_output_text',
    '_tool_row_command_norms',
    '_tool_row_success_status',
    '_completion_command_success_issue',
    '_latest_tool_result_for_task',
    '_completion_proof_command_issue',
    '_completion_success_test_issue',
    '_verify_failed_texts',
    '_workspace_verify_relevant_failure_texts',
    '_row_position_after',
    '_tool_row_is_successful_repair_write',
    '_supported_llm_alias_memory_claim_issue',
    '_completion_llm_memory_claim_issue',
    '_tool_row_retry_watcher_payload',
    '_phase_subtask_retry_context',
    '_phase_subtask_retry_state',
    '_phase_subtask_retry_watcher_review_payload',
    '_phase_subtask_retry_escalation_block',
    '_phase_subtask_completion_issue',
]
