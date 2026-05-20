"""Agent-facing phase-contract tool handlers."""

from umbrella.deep_agent_tools.phase_contract_common import *
from umbrella.deep_agent_tools.phase_contract_base import *
from umbrella.deep_agent_tools.phase_contract_declarations import _iter_plan_strings
from umbrella.deep_agent_tools.phase_contract_success import *
from umbrella.deep_agent_tools.phase_contract_paths import *

def _coerce_object_payload(value: Any, *, field_name: str) -> tuple[dict[str, Any], str]:
    """Accept model-produced object payloads even when emitted as JSON text."""
    if value is None:
        return {}, ""
    if isinstance(value, dict):
        return value, ""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}, f"{field_name} was an empty string; treated as empty object"
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            try:
                import yaml  # type: ignore

                parsed = yaml.safe_load(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                return (
                    parsed,
                    f"{field_name} was parsed as YAML-compatible object after JSON parsing failed: {exc.msg}",
                )
            return (
                {"text": value},
                f"{field_name} was a string and JSON parsing failed: {exc.msg}",
            )
        if isinstance(parsed, dict):
            return parsed, ""
        return (
            {"value": parsed},
            f"{field_name} JSON decoded to {type(parsed).__name__}; wrapped as value",
        )
    return (
        {"value": value},
        f"{field_name} expected object, got {type(value).__name__}; wrapped as value",
    )


def _coerce_phase_plan_payload(value: Any) -> tuple[dict[str, Any], str]:
    plan, warning = _coerce_object_payload(value, field_name="plan")
    embedded = plan.get("plan") if isinstance(plan, dict) else None
    if isinstance(embedded, dict) and not _iter_plan_subtasks(plan):
        return embedded, _join_payload_warnings(
            warning,
            "unwrapped nested object from plan.plan",
        )
    if isinstance(embedded, str):
        if plan.get("plan_truncated") is True:
            return plan, _join_payload_warnings(
                warning,
                "plan.plan was marked truncated; keeping wrapper so validation can reject it clearly",
            )
        parsed, nested_warning = _coerce_object_payload(
            embedded,
            field_name="plan.plan",
        )
        if _iter_plan_subtasks(parsed):
            return parsed, _join_payload_warnings(
                warning,
                nested_warning,
                "unwrapped serialized object from plan.plan",
            )
        return plan, _join_payload_warnings(warning, nested_warning)
    return plan, warning


def _join_payload_warnings(*warnings: str) -> str:
    return "; ".join(str(item).strip() for item in warnings if str(item).strip())


def _list_files(ctx: ToolContext, workspace_id: str = "", subdir: str = "", max_entries: int = 300) -> str:
    return umbrella_tools.list_workspace_files(
        ctx,
        workspace_id=_workspace_id(ctx, workspace_id),
        subdir=subdir,
        max_entries=max_entries,
    )


def _read_file(
    ctx: ToolContext,
    workspace_id: str = "",
    file_path: str = "",
    max_chars: int = 30000,
    offset: int = 0,
    line_start: int = 0,
    line_count: int = 160,
) -> str:
    return umbrella_tools.read_workspace_file(
        ctx,
        workspace_id=_workspace_id(ctx, workspace_id),
        file_path=file_path,
        max_chars=max_chars,
        offset=offset,
        line_start=line_start,
        line_count=line_count,
    )


def _shell(
    ctx: ToolContext,
    workspace_id: str = "",
    command: str | list[str] | None = None,
    argv: list[str] | None = None,
    subdir: str = "",
    timeout_seconds: int = 180,
    allow_dependency_install: bool = False,
) -> str:
    if stop := _stop_requested_message(ctx, "shell"):
        return stop
    return umbrella_tools.run_workspace_command(
        ctx,
        workspace_id=_workspace_id(ctx, workspace_id),
        command=command,
        argv=argv,
        subdir=subdir,
        timeout_seconds=timeout_seconds,
        allow_dependency_install=allow_dependency_install,
    )


def _run_unit_tests(ctx: ToolContext, workspace_id: str = "", timeout_seconds: int = 600) -> str:
    if stop := _stop_requested_message(ctx, "run_unit_tests"):
        return stop
    return umbrella_tools.run_workspace_verify(
        ctx,
        workspace_id=_workspace_id(ctx, workspace_id),
        timeout_seconds=timeout_seconds,
    )


def _workspace_root_for_phase(ctx: ToolContext, workspace_id: str) -> pathlib.Path:
    repo_root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir)
    try:
        repo_root = pathlib.Path(umbrella_tools._resolve_umbrella_repo_root(ctx))
        return umbrella_tools._workspace_root(repo_root, workspace_id, ctx)
    except Exception:
        return repo_root / "workspaces" / workspace_id


def _workspace_e2e_goal_text(ctx: ToolContext, workspace_id: str) -> tuple[pathlib.Path, str]:
    root = _workspace_root_for_phase(ctx, workspace_id)
    chunks: list[str] = []
    for rel in ("TASK_MAIN.md", "workspace.toml", "verification.toml"):
        path = root / rel
        if path.is_file():
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return root, "\n".join(chunks)


def _workspace_requires_localhost_e2e(root: pathlib.Path, goal_text: str) -> bool:
    if _LOCALHOST_E2E_RE.search(goal_text or ""):
        return True
    has_frontend = (root / "frontend" / "package.json").is_file() or (
        root / "package.json"
    ).is_file()
    has_server = any(
        (root / rel).is_file()
        for rel in ("game_server.py", "web_server.py", "server.py", "app.py", "main.py")
    )
    return bool(has_frontend and has_server)


def _result_is_localhost_e2e_proof(result: dict[str, Any]) -> bool:
    if bool(result.get("optional")):
        return False
    if str(result.get("status") or "").lower() != "passed":
        return False
    kind = str(result.get("kind") or "").lower()
    if kind in {"http_boot", "behavioral_http"}:
        return True
    text = " ".join(
        str(result.get(key) or "")
        for key in ("name", "summary", "stdout_tail", "stderr_tail", "error")
    )
    return bool(_LOCALHOST_PROOF_RE.search(text))


def _has_localhost_e2e_proof(payload: dict[str, Any]) -> bool:
    results = payload.get("results")
    if not isinstance(results, list):
        return False
    return any(
        _result_is_localhost_e2e_proof(r)
        for r in results
        if isinstance(r, dict)
    )


def _failed_required_count(payload: dict[str, Any]) -> int:
    raw = payload.get("failed_step_count")
    if isinstance(raw, int) and raw >= 0:
        return raw
    failed = 0
    results = payload.get("results")
    if isinstance(results, list):
        for result in results:
            if not isinstance(result, dict) or bool(result.get("optional")):
                continue
            if str(result.get("status") or "").lower() in {"failed", "error"}:
                failed += 1
    return failed


def _apply_real_e2e_adequacy_guard(
    payload: dict[str, Any],
    *,
    workspace_id: str,
    workspace_root: pathlib.Path,
    goal_text: str,
) -> dict[str, Any]:
    if not _workspace_requires_localhost_e2e(workspace_root, goal_text):
        payload["real_e2e_guard"] = {
            "required": False,
            "workspace_id": workspace_id,
        }
        return payload
    if _has_localhost_e2e_proof(payload):
        payload["real_e2e_guard"] = {
            "required": True,
            "passed": True,
            "workspace_id": workspace_id,
        }
        return payload

    results = payload.get("results")
    if not isinstance(results, list):
        results = []
        payload["results"] = results
    guard_result = {
        "name": "e2e_guard:localhost_ui",
        "kind": "e2e_guard",
        "status": "failed",
        "exit_code": None,
        "duration_seconds": 0.0,
        "summary": (
            "Workspace goal requires a localhost/web UI style delivery, but "
            "`run_real_e2e` found no passed HTTP/browser/localhost proof. "
            "Add a real http_boot/behavioral_http or browser-backed smoke "
            "step to verification.toml/workspace.toml, then rerun."
        ),
        "stdout_tail": "",
        "stderr_tail": "",
        "error": "missing_localhost_e2e_evidence",
        "optional": False,
        "request_payload_count": 0,
    }
    failed_count = _failed_required_count(payload) + 1
    results.append(guard_result)
    required_count = sum(
        1 for r in results if isinstance(r, dict) and not bool(r.get("optional"))
    )
    passed_count = sum(
        1
        for r in results
        if isinstance(r, dict)
        and not bool(r.get("optional"))
        and str(r.get("status") or "").lower() == "passed"
    )
    payload["passed"] = False
    payload["failed_step_count"] = failed_count
    payload["pass_rate"] = round(passed_count / required_count, 3) if required_count else 0.0
    summary = str(payload.get("summary") or "").rstrip()
    guard_line = (
        "- [required] `e2e_guard:localhost_ui` (e2e_guard) -> failed\n"
        "  missing localhost/browser/http proof for a web UI goal"
    )
    payload["summary"] = f"{summary}\n{guard_line}" if summary else guard_line
    payload["real_e2e_guard"] = {
        "required": True,
        "passed": False,
        "workspace_id": workspace_id,
        "reason": "missing_localhost_e2e_evidence",
    }
    return payload


def _run_real_e2e(ctx: ToolContext, workspace_id: str = "", timeout_seconds: int = 600) -> str:
    if stop := _stop_requested_message(ctx, "run_real_e2e"):
        return stop
    ws = _workspace_id(ctx, workspace_id)
    raw = umbrella_tools.run_workspace_verify(
        ctx,
        workspace_id=ws,
        timeout_seconds=timeout_seconds,
    )
    try:
        payload = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(payload, dict):
        return raw
    root, goal_text = _workspace_e2e_goal_text(ctx, ws)
    guarded = _apply_real_e2e_adequacy_guard(
        payload,
        workspace_id=ws,
        workspace_root=root,
        goal_text=goal_text,
    )
    guarded["tool"] = "run_real_e2e"
    return _json(guarded)


def _palace_search(
    ctx: ToolContext,
    query: str = "",
    palace_path: str = "",
    workspace_id: str = "",
    limit: int = 10,
    include_unverified: bool = False,
) -> str:
    return umbrella_tools.get_umbrella_memory(
        ctx,
        query=query,
        palace_path=palace_path,
        workspace_id=workspace_id or _workspace_id(ctx),
        limit=limit,
        include_unverified=include_unverified,
    )


def _palace_add_guard_phase(ctx: ToolContext) -> str:
    phase = _umbrella_phase_id(ctx).strip().lower()
    if phase in {"research", "plan"}:
        return phase
    return phase


_PHASE_MEMORY_PROGRESS_NOTE_RE = re.compile(
    r"(?i)\b(research\s+progress|scratchpad|status\s+update|todo)\b|"
    r"\b(?:evidence\s+ledger|current\s+finding\s+attempts?|"
    r"finding\s+attempts?|accepted\s+findings?)\b|"
    r"\b(?:need\s+to\s+continue\s+researching|continue\s+researching|"
    r"continue\s+gathering\s+evidence|let\s+me\s+explore|"
    r"make\s+at\s+least\s+\d+\s+palace_add\s+calls?)\b|"
    r"\b\d+\s*/\s*\d+\s+(?:palace\s+)?findings?\b"
)

_RESEARCH_NON_FINDING_EVIDENCE_KINDS = {
    "candidate",
    "draft",
    "hypothesis",
    "lead",
    "unverified",
}


def _metadata_verified_false(metadata: dict[str, Any]) -> bool:
    if "verified" not in metadata:
        return False
    value = metadata.get("verified")
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    text = str(value or "").strip().lower()
    return text in {"0", "false", "no", "off", "unverified"}


_PALACE_ADD_VERIFIED_OUTCOME_KINDS = {
    "completion_memory",
    "durable",
    "phase_completion",
    "subtask_completion",
    "verification_report",
    "verification_result",
    "verify_run",
}


def _palace_add_memory_verified(
    *, phase: str, kind: str, evidence_kind: str
) -> bool:
    kind_l = str(kind or "").strip().lower()
    if phase == "research" and kind_l == "research_finding":
        return True
    if str(evidence_kind or "").strip().lower() != "verified_outcome":
        return False
    return kind_l in _PALACE_ADD_VERIFIED_OUTCOME_KINDS


def _plan_phase_direct_plan_memory_issue(
    *, phase: str, kind: str, tags: list[str]
) -> str:
    if str(phase or "").strip().lower() != "plan":
        return ""
    kind_l = str(kind or "").strip().lower()
    tag_l = {str(tag or "").strip().lower() for tag in tags}
    if {"phase_plan_submitted", "umbrella_plan_selected"} & tag_l:
        return ""
    if kind_l == "phase_plan" or {"phase_plan", "umbrella_plan"} & tag_l:
        return (
            "ERROR: palace_add cannot store executable phase plans from the "
            "plan phase. Use propose_phase_plan with the full plan, then "
            "submit_phase_plan to select the authoritative artifact. Save "
            "ordinary planning notes under a non-plan kind such as "
            "`planning_note`."
        )
    return ""


_VERIFIABLE_TOOL_SOURCE_IDS = {
    "deep_search",
    "github_extract_snippets",
    "github_project_search",
    "mcp_discover",
    "web_search",
}

_RESEARCH_EXACT_SOURCE_TOOL_IDS = {
    *_VERIFIABLE_TOOL_SOURCE_IDS,
    "env_check",
    "get_gmas_context",
    "palace_search",
    "read_file",
    "read_workspace_charter",
    "search_gmas_knowledge",
}

_RESEARCH_GMAS_SOURCE_TOOLS = {"get_gmas_context", "search_gmas_knowledge"}
_RESEARCH_RESULT_BEARING_SOURCE_TOOLS = {"github_project_search"}


def _tool_result_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("result_preview") or row.get("result") or {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _mapping_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _tool_row_provenance_payloads(row: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    args_payload = _mapping_payload(row.get("args"))
    if args_payload:
        payloads.append(args_payload)
    result_payload = _tool_result_payload(row)
    if result_payload:
        payloads.append(result_payload)
    return payloads


def _tool_row_has_usable_result(row: dict[str, Any]) -> bool:
    preview = str(row.get("result_preview") or row.get("result") or "")
    if "TOOL_ARG_ERROR" in preview or preview.strip().startswith("ERROR:"):
        return False
    payload = _tool_result_payload(row)
    if not payload:
        return bool(preview.strip())
    status = str(payload.get("status") or "").strip().lower()
    if status:
        if status not in {"ok", "success"}:
            return False
    tool = str(row.get("tool") or "").strip().lower()
    if tool in _RESEARCH_RESULT_BEARING_SOURCE_TOOLS:
        results = payload.get("results")
        if isinstance(results, list):
            return bool(results)
    if int(payload.get("exit_code") or 0) != 0 and "exit_code" in payload:
        return False
    return "error" not in payload and "reason" not in payload


def _github_namespace_source_seen(rows: list[dict[str, Any]], target: str) -> bool:
    wanted = str(target or "").strip().strip("/").lower()
    if not wanted:
        return False
    for row in rows:
        if str(row.get("tool") or "") != "github_project_search":
            continue
        if not _tool_row_has_usable_result(row):
            continue
        payload = _tool_result_payload(row)
        results = payload.get("results")
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            full_name = str(result.get("full_name") or "").strip().lower()
            url = str(result.get("html_url") or "").strip().lower()
            if wanted == full_name or url.rstrip("/").endswith("/" + wanted):
                return True
    return False


def _tool_qualified_source_seen(
    rows: list[dict[str, Any]], *, tool_name: str, qualifier: str
) -> bool:
    tool_l = str(tool_name or "").strip().lower()
    needle = str(qualifier or "").strip().lower()
    if not tool_l or tool_l not in _RESEARCH_EXACT_SOURCE_TOOL_IDS:
        return False
    for row in rows:
        if str(row.get("tool") or "").strip().lower() != tool_l:
            continue
        if not _tool_row_has_usable_result(row):
            continue
        if not needle:
            return True
        haystack_parts: list[str] = []
        for payload in _tool_row_provenance_payloads(row):
            for key in ("intent", "query", "url", "title", "source_id"):
                value = payload.get(key)
                if isinstance(value, str):
                    haystack_parts.append(value)
            results = payload.get("results")
            if isinstance(results, list):
                for result in results[:20]:
                    if not isinstance(result, dict):
                        continue
                    for key in ("full_name", "html_url", "url", "title", "name"):
                        value = result.get(key)
                        if isinstance(value, str):
                            haystack_parts.append(value)
        if needle in "\n".join(haystack_parts).lower():
            return True
    return False


def _research_finding_source_provenance_issue(
    rows: list[dict[str, Any]], *, source_id: str = ""
) -> str:
    source = str(source_id or "").strip()
    source_l = source.lower()
    if not source:
        return (
            "ERROR: palace_add research_finding requires a source_id tied to "
            "current discovery evidence. Use an exact tool source such as "
            "`github_project_search`, `mcp_discover`, `web_search`, "
            "`deep_search`, `search_gmas_knowledge`; a tool-qualified source "
            "such as `deep_search:<intent-or-query>`; or a concrete namespace "
            "source like `github:owner/repo` / `gmas:topic`. Save summaries, "
            "progress notes, and self-referential memory as `kind=observation`."
        )
    if source_l in {"palace_add", "tool:palace_add", "self", "memory"} or source_l.startswith(
        "tool:palace_add"
    ):
        return (
            "ERROR: palace_add research_finding cannot use palace_add itself "
            "as provenance. Cite the current discovery tool/result that "
            "supports the finding, or save this as `kind=observation`."
        )
    if source_l in _RESEARCH_EXACT_SOURCE_TOOL_IDS:
        if any(
            str(row.get("tool") or "").strip().lower() == source_l
            and _tool_row_has_usable_result(row)
            for row in rows
        ):
            return ""
        return (
            f"ERROR: palace_add research_finding source_id `{source}` has no "
            "usable logged result in this research task. Run/cite the discovery "
            "tool first, or save this note as `kind=observation`."
        )
    if source_l.startswith("github:"):
        target = source.split(":", 1)[1]
        if _github_namespace_source_seen(rows, target):
            return ""
        return (
            f"ERROR: palace_add research_finding source_id `{source}` does not "
            "match any repository returned by `github_project_search` in this "
            "research task. Use a returned `github:owner/repo` value, the exact "
            "`github_project_search` tool source, or save this as "
            "`kind=observation`."
        )
    if source_l.startswith("gmas:"):
        if any(
            str(row.get("tool") or "").strip().lower() in _RESEARCH_GMAS_SOURCE_TOOLS
            and _tool_row_has_usable_result(row)
            for row in rows
        ):
            return ""
        return (
            f"ERROR: palace_add research_finding source_id `{source}` requires "
            "a successful current `get_gmas_context` or `search_gmas_knowledge` "
            "call. Run/cite GMAS discovery first, or save this as "
            "`kind=observation`."
        )
    if ":" in source:
        tool_name, qualifier = source.split(":", 1)
        if _tool_qualified_source_seen(
            rows,
            tool_name=tool_name,
            qualifier=qualifier,
        ):
            return ""
    return (
        f"ERROR: palace_add research_finding source_id `{source}` is not a "
        "verifiable current discovery source. Use an exact discovery tool id, "
        "`tool:intent-or-query`, `github:owner/repo`, or `gmas:topic`; keep local summaries and "
        "memory bookkeeping as `kind=observation`."
    )


def _tool_source_verified_outcome_issue(
    rows: list[dict[str, Any]], *, source_id: str = "", evidence_kind: str = ""
) -> str:
    source = str(source_id or "").strip()
    if str(evidence_kind or "").strip().lower() != "verified_outcome":
        return ""
    if source not in _VERIFIABLE_TOOL_SOURCE_IDS:
        return ""
    source_rows = [row for row in rows if str(row.get("tool") or "") == source]
    if not source_rows:
        return (
            f"ERROR: palace_add cannot mark source_id `{source}` as "
            "verified_outcome because that tool has no logged call in this task."
        )
    for row in source_rows:
        preview = str(row.get("result_preview") or "")
        if "TOOL_ARG_ERROR" in preview or preview.strip().startswith("ERROR:"):
            continue
        try:
            payload = json.loads(preview)
        except Exception:
            if preview.strip():
                return ""
            continue
        status = str(payload.get("status") or "").strip().lower()
        if status in {"ok", "success"}:
            return ""
        if status:
            continue
        if "error" not in payload and "reason" not in payload:
            return ""
    return (
        f"ERROR: palace_add cannot mark source_id `{source}` as "
        "verified_outcome because its logged calls in this task did not succeed. "
        "Use a successful discovery result as the source, lower evidence_kind, "
        "or cite the accepted finding/tool that actually provided the evidence."
    )


def _infer_phase_palace_add_kind(
    *,
    phase: str,
    kind: str,
    tags: list[str],
    title: str,
    content: str,
) -> str:
    """Apply manifest-level provenance defaults for compatibility palace_add."""

    phase_l = str(phase or "").strip().lower()
    kind_s = str(kind or "").strip()
    kind_l = kind_s.lower()
    tag_l = {str(tag or "").strip().lower() for tag in tags}
    if phase_l != "research":
        return kind_s or "observation"
    if kind_l:
        return kind_s
    if tag_l.intersection(
        {"research_finding", "research_summary", "mcp_candidate", "skill_candidate"}
    ):
        return kind_s or "observation"
    text = "\n".join(part for part in (title, content) if part)
    if _PHASE_MEMORY_PROGRESS_NOTE_RE.search(text):
        return kind_s or "observation"
    return "research_finding"


def _palace_add(
    ctx: ToolContext,
    title: str = "",
    content: str = "",
    palace_path: str = "",
    kind: str = "",
    workspace_id: str = "",
    tags: str = "",
    source_id: str = "",
    evidence_kind: str = "",
    **metadata: Any,
) -> str:
    if stop := _stop_requested_message(ctx, "palace_add"):
        return stop
    ws = workspace_id or _workspace_id(ctx)
    phase = _palace_add_guard_phase(ctx)
    phase_path = phase or "phase"
    if str(kind or "").strip().lower() == "subtask_card":
        phase_path = f"{phase_path}/subtasks"
    path = palace_path or (f"workspaces/{ws}/{phase_path}" if ws else phase_path)
    tag_list = _split_tag_string(tags)
    requested_kind_l = str(kind or "").strip().lower()
    requested_research_finding = (
        requested_kind_l == "research_finding"
        or "research_finding" in {tag.lower() for tag in tag_list}
    )
    kind = _infer_phase_palace_add_kind(
        phase=phase,
        kind=kind,
        tags=tag_list,
        title=title,
        content=content,
    )
    if (
        phase == "research"
        and requested_research_finding
        and str(kind or "").strip().lower() != "research_finding"
    ):
        return (
            "ERROR: palace_add was tagged as `research_finding` but would be "
            "stored as `observation`, so it would not count for "
            "`submit_research_summary`. To save a counted finding, call "
            "`palace_add` with `kind=\"research_finding\"`, concrete content, "
            "a source_id/evidence_kind from current discovery, and no "
            "unverified/progress metadata. Otherwise remove the "
            "`research_finding` tag and keep it as an observation/lead."
        )
    evidence_kind_l = str(evidence_kind or "").strip().lower()
    invalid_research_evidence = (
        evidence_kind_l in _RESEARCH_NON_FINDING_EVIDENCE_KINDS
        or bool(evidence_kind_l and not re.fullmatch(r"[a-z0-9_-]+", evidence_kind_l))
    )
    if (
        phase == "research"
        and str(kind or "").strip().lower() == "research_finding"
        and invalid_research_evidence
    ):
        if requested_research_finding:
            return (
                "ERROR: palace_add research_finding cannot use hypothesis, "
                "candidate, unverified, draft, or malformed evidence metadata. "
                "Save it as `kind=observation`/`kind=research_lead`, or cite "
                "a concrete verified tool/log outcome for `research_finding`."
            )
        kind = "observation"
    if (
        phase == "research"
        and str(kind or "").strip().lower() == "research_finding"
        and _metadata_verified_false(metadata)
    ):
        return (
            "ERROR: palace_add research_finding cannot be saved with "
            "`verified=false`. Save uncertain material as `kind=observation`/"
            "`kind=research_lead`, or save a concrete current finding without "
            "unverified metadata so it can be cited by `submit_research_summary`."
        )
    if phase == "research" and str(kind or "").strip().lower() != "research_finding":
        tag_list = [
            tag
            for tag in tag_list
            if str(tag or "").strip().lower() != "research_finding"
        ]
    if kind and kind not in tag_list:
        tag_list.append(kind)
    if phase and phase not in tag_list:
        tag_list.append(phase)
    direct_plan_issue = _plan_phase_direct_plan_memory_issue(
        phase=phase,
        kind=kind,
        tags=tag_list,
    )
    if direct_plan_issue:
        return direct_plan_issue
    extra = {
        key: value
        for key, value in {
            "source_id": source_id,
            "evidence_kind": evidence_kind,
            **metadata,
        }.items()
        if value not in ("", None, [], {})
    }
    body = content or title or ""
    if extra:
        body = (
            body.rstrip()
            + "\n\nMetadata:\n```json\n"
            + json.dumps(extra, ensure_ascii=False, indent=2)
            + "\n```"
        )
    if (
        phase == "research"
        and str(kind or "").strip().lower() == "research_finding"
        and _PHASE_MEMORY_PROGRESS_NOTE_RE.search(
            "\n".join(part for part in (title, body) if part)
        )
    ):
        return (
            "ERROR: palace_add research_finding cannot be a progress ledger, "
            "scratchpad, status update, or finding-count note. Save progress as "
            "`kind=observation`/`kind=research_progress`, then save only a "
            "concrete claim with evidence as `research_finding`."
        )
    rows = _tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or ""))
    source_issue = _tool_source_verified_outcome_issue(
        rows, source_id=source_id, evidence_kind=evidence_kind
    )
    if source_issue:
        return source_issue
    contradiction = _negative_claim_contradiction_issue(
        ctx,
        rows=rows,
        text="\n".join(part for part in (title, body) if part),
        label="palace_add content",
    )
    if contradiction:
        return contradiction + (
            " Do not save contradicted code findings; write a corrected finding "
            "or explicitly mark stale verification memory as stale."
        )
    unread_issue = _unread_existing_workspace_path_issue(
        ctx,
        text="\n".join(part for part in (title, body) if part),
        label="palace_add content",
    )
    if unread_issue:
        return unread_issue + (
            " Do not save current-workspace findings until their referenced "
            "files were read in this phase."
        )
    if phase in {"research", "plan"}:
        finding_text = "\n".join(part for part in (title, body) if part)
        fallback_issue = _llm_fallback_handoff_issue(
            finding_text,
            label=f"palace_add {phase} finding",
        )
        if fallback_issue:
            return fallback_issue + (
                " This memory entry was not saved. Save a corrected finding "
                "that names explicit configuration, retry, paused turn, or "
                "surfaced runtime errors, then cite only the accepted id."
            )
        test_double_issue = _llm_test_double_handoff_issue(
            finding_text,
            label=f"palace_add {phase} finding",
        )
        if test_double_issue:
            return test_double_issue + (
                " This memory entry was not saved. Save a corrected finding "
                "that separates non-LLM unit seams from real runtime proof, "
                "then cite only the accepted id."
            )
        cached_decision_issue = _llm_cached_decision_handoff_issue(
            finding_text,
            label=f"palace_add {phase} finding",
        )
        if cached_decision_issue:
            return cached_decision_issue + (
                " This memory entry was not saved. Save a corrected finding "
                "that caches only static reference data/prompts and keeps bot "
                "decisions on fresh real runtime calls, then cite only the "
                "accepted id."
            )
        env_issue = _llm_env_contract_issue_from_text(
            finding_text,
            subject=f"palace_add {phase} finding",
            require_explicit_contract=False,
        )
        if env_issue:
            return (
                "ERROR: "
                + env_issue
                + " This memory entry was not saved. Save a corrected finding "
                "with the public LLM_* runtime aliases and optional inherited "
                "Umbrella compatibility aliases, then cite only the accepted id."
            )
    if phase == "research" and str(kind or "").strip().lower() == "research_finding":
        provenance_issue = _research_finding_source_provenance_issue(
            rows,
            source_id=source_id,
        )
        if provenance_issue:
            return provenance_issue
    mem_store, mem_tier, mem_scope = _palace_add_store_policy(
        ctx,
        palace_path=palace_path,
        kind=kind,
        tags=tag_list,
    )
    subtask_id = _subtask_id_from_phase_memory(
        title=title,
        body=body,
        palace_path=palace_path,
        kind=kind,
        tags=tag_list,
    )
    memory_verified = _palace_add_memory_verified(
        phase=phase,
        kind=kind,
        evidence_kind=evidence_kind,
    )
    mempalace_id = ""
    if ws:
        try:
            from umbrella.memory.palace.facade import MemPalace

            repo_root = umbrella_tools._resolve_umbrella_repo_root(ctx)
            palace = MemPalace(repo_root, ws)
            mem_body = (
                body
                if isinstance(body, str)
                else json.dumps(body, ensure_ascii=False, indent=2)
            )
            mem_content = f"[{title}]\n{mem_body}" if title else mem_body
            try:
                mempalace_id = palace.add(
                    store=mem_store,
                    content=mem_content,
                    tier=mem_tier,
                    scope=mem_scope,
                    tags=tag_list,
                    phase=phase,
                    subtask_id=subtask_id or None,
                    run_id=_run_id(ctx),
                    source_path=source_id or "tool:palace_add",
                    verified=memory_verified,
                    extra={
                        "palace_path": palace_path,
                        "kind": kind,
                    },
                )
            finally:
                palace.close()
        except Exception:
            mempalace_id = ""

    legacy_result = _save_umbrella_memory(
        ctx,
        palace_path=path,
        title=title or kind or "phase note",
        content=body,
        kind=kind,
        workspace_id=ws,
        tags=",".join(tag_list) if tag_list else tags,
        metadata_extra={
            "canonical_id": mempalace_id,
            "verified": memory_verified,
            "store": mem_store,
            "tier": mem_tier,
            "scope": mem_scope,
            "phase": phase,
            "run_id": _run_id(ctx),
            "subtask_id": subtask_id,
            "source_path": source_id or "tool:palace_add",
            "evidence_kind": evidence_kind,
        },
    )
    legacy_payload: Any = legacy_result
    try:
        legacy_payload = json.loads(str(legacy_result))
    except Exception:
        pass
    saved = bool(mempalace_id) or not str(legacy_result).startswith("WARNING:")
    payload = {
        "saved": saved,
        "id": mempalace_id,
        "store": mem_store,
        "tier": mem_tier,
        "scope": mem_scope,
        "phase": phase,
        "run_id": _run_id(ctx),
        "verified": memory_verified,
        "source_path": source_id or "tool:palace_add",
        "subtask_id": subtask_id,
        "kind": kind,
        "tags": tag_list,
        "legacy": legacy_payload,
    }
    if not mempalace_id and not saved:
        payload["status"] = "error"
    return _json(payload)


def _palace_link(
    ctx: ToolContext,
    source_id: str = "",
    target_id: str = "",
    relation: str = "related",
    notes: str = "",
    workspace_id: str = "",
) -> str:
    if stop := _stop_requested_message(ctx, "palace_link"):
        return stop
    ws = workspace_id or _workspace_id(ctx)
    body = _json(
        {
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "notes": notes,
        }
    )
    return umbrella_tools.record_idea(
        ctx,
        workspace_id=ws,
        kind="palace_link",
        title=f"{relation}: {source_id} -> {target_id}",
        body=body,
        evidence_kind="observation_from_log",
    )


def _read_workspace_charter(ctx: ToolContext, workspace_id: str = "", max_chars: int = 20000) -> str:
    ws = _workspace_id(ctx, workspace_id)
    repo_root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir)
    root = repo_root / "workspaces" / ws if ws else repo_root / "workspaces"
    files: dict[str, str] = {}
    for name in ("TASK_MAIN.md", "workspace.toml", "README.md", "verification.toml"):
        path = root / name
        if path.exists() and path.is_file():
            files[name] = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    return _json({"workspace_id": ws, "root": str(root), "files": files})


def _env_check(ctx: ToolContext) -> str:
    keys = [
        "LLM_MODEL",
        "OUROBOROS_MODEL",
        "LLM_BASE_URL",
        "OUROBOROS_LLM_BASE_URL",
        "LLM_API_KEY",
        "OUROBOROS_LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    ]
    present = {key: bool(os.environ.get(key)) for key in keys}
    api_key_vars = [
        "LLM_API_KEY",
        "OUROBOROS_LLM_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    ]
    model_vars = ["LLM_MODEL", "OUROBOROS_MODEL"]
    api_key_ready = any(present.get(key) for key in api_key_vars)
    model_ready = any(present.get(key) for key in model_vars)
    provider_ready = api_key_ready and model_ready
    return _json(
        {
            "status": "ok" if provider_ready else "missing_llm_env",
            "python": platform.python_version(),
            "repo_dir": str(ctx.repo_dir),
            "host_repo_root": str(ctx.host_repo_root or ""),
            "drive_root": str(ctx.drive_root),
            "env_present": present,
            "llm_provider_ready": provider_ready,
            "accepted_api_key_vars": api_key_vars,
            "accepted_model_vars": model_vars,
            "advisories": [
                "LLM_API_KEY is the standalone project credential alias; when Umbrella launches the workspace, OUROBOROS_LLM_API_KEY can be accepted as inherited compatibility.",
                "For generated workspaces, prefer a runtime resolver whose public env contract is LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL. It may also read OUROBOROS_LLM_API_KEY, OUROBOROS_LLM_BASE_URL, and OUROBOROS_MODEL as inherited compatibility aliases. OPENAI_API_KEY is not required unless the chosen provider is OpenAI or the task specifically uses web_search.",
            ]
            if provider_ready and not present.get("LLM_API_KEY")
            else [],
        }
    )


def _palace_health(ctx: ToolContext) -> str:
    try:
        tree = umbrella_tools.list_memory_tree(ctx, workspace_id=_workspace_id(ctx))
        return _json({"status": "ok", "tree": json.loads(tree)})
    except Exception as exc:
        return _json({"status": "error", "error": str(exc)})


def _mcp_health(ctx: ToolContext) -> str:
    repo_root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir)
    registry = repo_root / ".umbrella" / "mcp"
    return _json(
        {
            "status": "ok",
            "registry_path": str(registry),
            "registry_exists": registry.exists(),
        }
    )


def _skill_audit(ctx: ToolContext, workspace_id: str = "") -> str:
    repo_root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir)
    skills_root = repo_root / "umbrella" / "skills" / "library"
    skills = (
        sorted(p.parent.name for p in skills_root.glob("*/SKILL.md"))
        if skills_root.exists()
        else []
    )
    return _json({"status": "ok", "workspace_id": workspace_id or _workspace_id(ctx), "skills": skills})


def _request_human_checkpoint(ctx: ToolContext, reason: str = "", payload: dict[str, Any] | None = None) -> str:
    if stop := _stop_requested_message(ctx, "request_human_checkpoint"):
        return stop
    signal_id = _write_phase_signal(
        ctx,
        "request_human_checkpoint",
        {"reason": reason, "payload": payload or {}},
    )
    return f"OK: human checkpoint requested (signal: {signal_id})"


_EXTRA_SUBTASK_CONTROL_GATE_RE = re.compile(
    r"(?i)\b(?:false[-\s]?positive\s+skill[_\s-]?compliance|"
    r"mark\s+(?:the\s+)?(?:multi[_-]?agent[_-]?gmas|skill|verification)\s+"
    r"(?:as\s+)?optional|blocker_note|configure_workspace_skills|"
    r"verification\s+framework\s+(?:should|must)|"
    r"update\s+verification\s+to\s+scope\s+skill\s+checks)\b"
)


def _request_extra_subtask_policy_issue(
    ctx: ToolContext,
    *,
    reason: str,
    proposed_subtask: dict[str, Any] | None,
) -> str:
    if not isinstance(proposed_subtask, dict) or not proposed_subtask:
        return ""
    issues = _phase_plan_policy_issues(
        {"subtasks": [proposed_subtask]},
        ctx=ctx,
        notes=reason,
    )
    if issues:
        return (
            "ERROR: request_extra_subtask rejected: proposed subtask violates "
            "workspace policy: "
            + "; ".join(issues)
            + ". Extra subtasks must be executable product work inside the "
            "workspace, not edits to Umbrella memory/control-plane state."
        )
    text = "\n".join(
        _iter_plan_strings(
            {"reason": reason, "proposed_subtask": proposed_subtask}
        )
    )
    if _EXTRA_SUBTASK_CONTROL_GATE_RE.search(text):
        return (
            "ERROR: request_extra_subtask rejected: extra subtasks cannot be "
            "used to mark Umbrella skill/verification gates optional, add "
            "blocker notes, or work around a suspected control-plane false "
            "positive from inside the generated workspace. Use "
            "`request_watcher_review` for diagnosis, continue with the planned "
            "future subtask that owns the capability, or fix Umbrella policy in "
            "the main product code."
        )
    return ""


def _request_extra_subtask(ctx: ToolContext, reason: str = "", proposed_subtask: dict[str, Any] | None = None) -> str:
    if stop := _stop_requested_message(ctx, "request_extra_subtask"):
        return stop
    if policy_issue := _request_extra_subtask_policy_issue(
        ctx,
        reason=reason,
        proposed_subtask=proposed_subtask,
    ):
        return policy_issue
    signal_id = _write_phase_signal(
        ctx,
        "request_extra_subtask",
        {"reason": reason, "proposed_subtask": proposed_subtask or {}},
    )
    return f"OK: extra subtask requested (signal: {signal_id})"


def _register_temp_tool(ctx: ToolContext, name: str, description: str = "", schema: dict[str, Any] | None = None) -> str:
    if stop := _stop_requested_message(ctx, "register_temp_tool"):
        return stop
    signal_id = _write_phase_signal(
        ctx,
        "register_temp_tool",
        {"name": name, "description": description, "schema": schema or {}},
    )
    return f"OK: temp tool candidate registered for review (signal: {signal_id})"


def _propose_phase_plan(
    ctx: ToolContext,
    plan: dict[str, Any] | str | None = None,
    notes: str = "",
    content: dict[str, Any] | str | None = None,
    **extra: Any,
) -> str:
    if stop := _stop_requested_message(ctx, "propose_phase_plan"):
        return stop
    if plan is None and content is not None:
        plan = content
    if not notes:
        for key in ("note", "rationale", "explanation", "summary"):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                notes = value.strip()
                break
    coerced_plan, warning = _coerce_phase_plan_payload(plan)
    policy_issues = _phase_plan_policy_issues(coerced_plan, ctx=ctx, notes=notes)
    if policy_issues:
        return (
            "ERROR: phase plan violates workspace policy: "
            + "; ".join(policy_issues)
            + ". Revise the plan before submitting it."
            + _phase_plan_repair_hint(policy_issues)
        )
    final_notes = notes
    if warning:
        final_notes = f"{notes}\n\nPayload warning: {warning}".strip()
    plan_id = _record_phase_plan_artifact(ctx, plan=coerced_plan, notes=final_notes)
    signal_id = _write_phase_signal(
        ctx,
        "propose_phase_plan",
        {
            "plan_id": plan_id,
            "plan": coerced_plan,
            "notes": final_notes,
            "payload_warning": warning,
        },
    )
    return f"OK: phase plan proposal recorded (plan_id: {plan_id}, signal: {signal_id})"


def _propose_subtasks(ctx: ToolContext, steps: list[dict[str, Any]] | None = None, notes: str = "") -> str:
    if stop := _stop_requested_message(ctx, "propose_subtasks"):
        return stop
    plan = {"subtasks": steps or []}
    policy_issues = _phase_plan_policy_issues(plan, ctx=ctx, notes=notes)
    if policy_issues:
        return (
            "ERROR: subtask proposal violates workspace policy: "
            + "; ".join(policy_issues)
            + ". Revise the subtask cards before recording them."
            + _phase_plan_repair_hint(policy_issues)
        )
    proposal_id = _record_subtask_proposal_artifact(ctx, steps=steps or [], notes=notes)
    signal_id = _write_phase_signal(
        ctx,
        "propose_subtasks",
        {"proposal_id": proposal_id, "steps": steps or [], "notes": notes},
    )
    return f"OK: subtask proposal recorded (proposal_id: {proposal_id}, signal: {signal_id})"


def _phase_plan_repair_hint(policy_issues: list[str]) -> str:
    """Give compact, reusable steering after plan validation failures."""

    text = " ".join(str(issue or "") for issue in policy_issues).lower()
    hints: list[str] = []
    if (
        "8-16" in text
        or "too broad" in text
        or "executable leaves" in text
        or "implementation subtask" in text
    ):
        hints.append(
            "Resubmit one complete flat `subtasks` array with roughly 8-16 "
            "vertical leaves; keep each implementation leaf to about 2-4 files "
            "and one behavior-focused `success_test`."
        )
        hints.append(
            "[PHASE_PLAN_REPAIR_SCAFFOLD] For a large full-stack/LLM app, aim "
            "for 12-14 leaves such as: docs-env, project-setup, domain-state, "
            "domain-rules, llm-runtime, agent-graph, ai-turn-api, backend-api, "
            "realtime-integration, frontend-shell, frontend-client-state, "
            "frontend-game-ui, final-e2e. Do not oscillate by merging rejected "
            "leaves back into broad 5+ file domains; instead keep only the "
            "first runnable 2-4 owned files on the current leaf and move "
            "future/optional files to a later leaf or the goal checklist."
        )
    if "llm runtime env contract" in text or "credential contract" in text:
        hints.append(
            "Add a top-level `llm_runtime_contract` and relevant leaf goals "
            "that spell the exact pairs "
            "`OUROBOROS_LLM_API_KEY`/`LLM_API_KEY`, "
            "`OUROBOROS_LLM_BASE_URL`/`LLM_BASE_URL`, and "
            "`OUROBOROS_MODEL`/`LLM_MODEL`; wildcard shorthand alone is not "
            "enough."
        )
    if (
        "mock/fake/dry-run" in text
        or "heuristic fallback" in text
        or "deterministic/static" in text
        or "generic fallback" in text
    ):
        hints.append(
            "Remove mock/fake/dry-run/heuristic/random fallback wording from "
            "implementation and test strategy; say `real LLM runtime only` and "
            "put missing-env skip/error behavior inside checked-in tests."
        )
    if "success_test" in text or "non-automatable" in text:
        hints.append(
            "Leave every `success_test` as one exact command such as "
            "`python -m pytest tests/test_x.py -q` or `cd frontend && npm run "
            "build`; do not include prose, custom skip flags, manual steps, or "
            "pseudo-arguments."
        )
    if not hints:
        return ""
    return " Repair recipe: " + " ".join(hints)


def _read_drive_log(ctx: ToolContext, log_name: str = "events.jsonl", tail: int = 100) -> str:
    path = pathlib.Path(ctx.drive_root) / "logs" / pathlib.Path(log_name).name
    if not path.exists():
        return _json({"status": "missing", "path": str(path), "rows": []})
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, tail):]
    return _json({"status": "ok", "path": str(path), "rows": lines})


def _read_terminal_scrollback(ctx: ToolContext, workspace_id: str = "", last_lines: int = 200) -> str:
    return umbrella_tools.terminal_view(
        ctx,
        workspace_id=_workspace_id(ctx, workspace_id),
        last_lines=last_lines,
    )


def _promote_to_durable(ctx: ToolContext, title: str = "", content: str = "", workspace_id: str = "", tags: str = "") -> str:
    if stop := _stop_requested_message(ctx, "promote_to_durable"):
        return stop
    if _umbrella_phase_id(ctx) == "verify" and _mentions_unresolved_pass_blocker(content):
        return (
            "ERROR: promote_to_durable cannot promote a passing verification "
            "record that mentions unresolved blockers or limitations. Loop back "
            "to execute with the concrete failures, then verify again."
        )
    ws = workspace_id or _workspace_id(ctx)
    body = content or title or ""
    legacy_result = _save_umbrella_memory(
        ctx,
        palace_path=f"workspaces/{ws}/durable" if ws else "durable",
        title=title or "durable phase artifact",
        content=body,
        kind="durable",
        workspace_id=ws,
        tags=tags or "durable",
    )
    try:
        from umbrella.memory.palace.facade import MemPalace

        repo_root = pathlib.Path(getattr(ctx, "host_repo_root", None) or getattr(ctx, "repo_dir", ".")).resolve()
        tag_list = [tag.strip() for tag in str(tags or "durable").replace(";", ",").split(",") if tag.strip()]
        if "durable" not in {tag.lower() for tag in tag_list}:
            tag_list.append("durable")
        if _umbrella_phase_id(ctx) == "verify" and "verification_report" not in {tag.lower() for tag in tag_list}:
            tag_list.append("verification_report")
        node_id = MemPalace(repo_root, ws or None).add(
            store="palace.durable",
            content=body,
            tier="always_on",
            scope="cross_run_durable",
            tags=tag_list,
            verified=True,
            phase=_umbrella_phase_id(ctx) or "verify",
            run_id=_run_id(ctx),
            extra={
                "title": title or "durable phase artifact",
                "task_id": str(getattr(ctx, "task_id", "") or ""),
            },
        )
    except Exception:
        node_id = ""
    if node_id:
        try:
            payload = json.loads(legacy_result)
            if isinstance(payload, dict):
                payload["durable_store"] = "palace.durable"
                payload["durable_node_id"] = node_id
                return _json(payload)
        except Exception:
            pass
    return legacy_result


def _blocked_destructive(_ctx: ToolContext, **_kwargs: Any) -> str:
    return "ERROR: this destructive phase-contract tool is intentionally blocked by policy."


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


__all__ = [
    '_coerce_object_payload',
    '_coerce_phase_plan_payload',
    '_join_payload_warnings',
    '_list_files',
    '_read_file',
    '_shell',
    '_run_unit_tests',
    '_workspace_root_for_phase',
    '_workspace_e2e_goal_text',
    '_workspace_requires_localhost_e2e',
    '_result_is_localhost_e2e_proof',
    '_has_localhost_e2e_proof',
    '_failed_required_count',
    '_apply_real_e2e_adequacy_guard',
    '_run_real_e2e',
    '_palace_search',
    '_palace_add',
    '_palace_link',
    '_read_workspace_charter',
    '_env_check',
    '_palace_health',
    '_mcp_health',
    '_skill_audit',
    '_request_human_checkpoint',
    '_request_extra_subtask',
    '_register_temp_tool',
    '_propose_phase_plan',
    '_propose_subtasks',
    '_read_drive_log',
    '_read_terminal_scrollback',
    '_promote_to_durable',
    '_blocked_destructive',
    '_schema',
]
