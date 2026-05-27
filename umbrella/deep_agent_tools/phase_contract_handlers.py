"""Agent-facing phase-contract tool handlers."""

import shlex

from umbrella.deep_agent_tools.phase_contract_common import *
from umbrella.deep_agent_tools.phase_contract_base import *
from umbrella.deep_agent_tools.phase_contract_declarations import _iter_plan_strings
from umbrella.deep_agent_tools.phase_contract_policy import (
    _phase_plan_empty_test_skeleton_issues,
    _phase_plan_file_reference_issues,
    _phase_plan_greenfield_layout_issues,
    _phase_plan_llm_env_issues,
    _phase_plan_llm_fallback_issues,
    _phase_plan_llm_provider_default_issues,
    _phase_plan_llm_test_double_issues,
    _phase_plan_missing_leaf_file_field_issues,
    _phase_plan_generic_success_test_issues,
    _phase_plan_success_test_issues,
    _phase_plan_workspace_prefix_issues,
    _workspace_existing_impl_roots,
)
from umbrella.deep_agent_tools.phase_control_base import (
    _llm_cached_decision_handoff_issue,
    _llm_fallback_handoff_issue,
    _llm_test_double_handoff_issue,
    _tool_log_rows_for_task,
)
from umbrella.deep_agent_tools.phase_control_research import (
    _negative_claim_contradiction_issue,
    _unread_existing_workspace_path_issue,
)
from umbrella.deep_agent_tools.phase_control_common import (
    _LLM_ENV_CONTEXT_RE,
    _LLM_ENV_OMISSION_REQUIRED_RE,
    _OPENAI_KEY_RE,
    _OPENAI_REQUIRED_RE,
    _WEB_SEARCH_ONLY_CONTEXT_RE,
)
from umbrella.deep_agent_tools.domain_policy import (
    HOST_LLM_ENV_BRIDGE_ALIASES,
    PUBLIC_LLM_ENV_ALIASES,
    unsupported_llm_env_alias_issues,
)
from umbrella.deep_agent_tools.research_provenance import (
    next_finding_source_hint as _next_finding_source_hint,
    research_finding_source_provenance_issue as _research_finding_source_provenance_issue,
    tool_result_content_grounding_issue as _tool_result_content_grounding_issue,
)
from umbrella.contracts import (
    ContractBundle,
    ContractIssue,
    ContractValidator,
    build_workspace_context,
    canonicalize_phase_plan,
    compile_phase_plan,
)

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
    "observation",
    "progress",
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


_PALACE_ADD_NON_LLM_TASK_CONTEXT_RE = re.compile(
    r"(?is)("
    r"\bnot\s+(?:an?\s+)?(?:llm|gmas|bot|agent)\b|"
    r"\bno\s+(?:llm|gmas|bot|agent)\b|"
    r"\bwithout\s+(?:llm|gmas|bot|agent)\s+(?:integration|runtime|calls?)\b|"
    r"\b(?:llm|gmas|bot|agent)\b.{0,80}\b(?:irrelevant|not\s+(?:required|applicable)|"
    r"non[-\s]?applicable)\b"
    r")"
)


def _palace_add_llm_env_contract_issue(text: str, *, subject: str) -> str:
    raw = str(text or "")
    for issue in unsupported_llm_env_alias_issues(raw, subject=subject):
        return issue.message
    if not _LLM_ENV_CONTEXT_RE.search(raw):
        return ""
    public_mentions = [
        alias
        for alias in PUBLIC_LLM_ENV_ALIASES
        if re.search(rf"\b{re.escape(alias)}\b", raw)
    ]
    host_mentions = [
        alias
        for alias in HOST_LLM_ENV_BRIDGE_ALIASES
        if re.search(rf"\b{re.escape(alias)}\b", raw)
    ]
    non_llm_task_context = bool(_PALACE_ADD_NON_LLM_TASK_CONTEXT_RE.search(raw))
    if non_llm_task_context and not host_mentions and not _OPENAI_REQUIRED_RE.search(raw):
        return ""
    web_search_only = bool(_WEB_SEARCH_ONLY_CONTEXT_RE.search(raw)) and not public_mentions
    contract_context = bool(_LLM_ENV_OMISSION_REQUIRED_RE.search(raw))
    mentions_provider_key = bool(_OPENAI_KEY_RE.search(raw)) and not web_search_only
    if not (
        public_mentions
        or host_mentions
        or contract_context
        or _OPENAI_REQUIRED_RE.search(raw)
        or mentions_provider_key
    ):
        return ""
    missing = [alias for alias in PUBLIC_LLM_ENV_ALIASES if alias not in public_mentions]
    if not missing:
        return ""
    return (
        f"{subject} uses an incomplete LLM runtime env contract. Generated "
        "workspace code/tests and phase memory must support public runtime "
        "`LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` aliases; Umbrella "
        "bridges host `OUROBOROS_*` launch aliases into those public names "
        "before running workspace commands. Do not require `OPENAI_API_KEY` "
        "or plain `LLM_API_KEY` as the only way to run real LLM/e2e behavior. "
        "Missing aliases: "
        + ", ".join(f"`{alias}`" for alias in missing)
        + "."
    )


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
    ctx: ToolContext,
    *,
    phase: str,
    kind: str,
    evidence_kind: str,
) -> bool:
    kind_l = str(kind or "").strip().lower()
    if phase == "research" and kind_l == "research_finding":
        return True
    if str(evidence_kind or "").strip().lower() != "verified_outcome":
        return False
    if kind_l not in _PALACE_ADD_VERIFIED_OUTCOME_KINDS:
        return False
    rules = _phase_memory_write_rules(ctx)
    rule = rules.get(kind_l) if isinstance(rules, dict) else None
    if isinstance(rule, dict) and rule.get("verified") is False:
        return False
    return True


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
    blocked_kinds = {"core_lesson", "accepted_lesson"}
    if str(kind or "").strip().lower() in blocked_kinds:
        return (
            "ERROR: palace_add cannot write core lessons directly. Use "
            "submit_reflection(proposed_bkb_rules=[...]) then accept_bkb_proposal."
        )
    from umbrella.memory.paths import normalize_workspace_id, parse_palace_path_hint

    ws = normalize_workspace_id(workspace_id or _workspace_id(ctx))
    phase = _palace_add_guard_phase(ctx)
    phase_path = phase or "phase"
    if str(kind or "").strip().lower() == "subtask_card":
        phase_path = f"{phase_path}/subtasks"
    _ws_hint, _event, logical = parse_palace_path_hint(
        palace_path,
        workspace_id=ws,
        default_kind=kind or "observation",
    )
    if _ws_hint:
        ws = _ws_hint
    path = logical or phase_path
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
        finding_text = "\n".join(part for part in (title, content) if part)
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
        llm_env_issue = _palace_add_llm_env_contract_issue(
            finding_text,
            subject=f"palace_add {phase} finding",
        )
        if llm_env_issue:
            return f"ERROR: {llm_env_issue} This memory entry was not saved."
    if phase == "research" and str(kind or "").strip().lower() == "research_finding":
        source_hint = _next_finding_source_hint(rows)
        provenance_issue = _research_finding_source_provenance_issue(
            rows,
            source_id=source_id,
        )
        if provenance_issue:
            return provenance_issue + source_hint
        grounding_issue = _tool_result_content_grounding_issue(
            rows,
            source_id=source_id,
            content="\n".join(part for part in (title, content) if part),
        )
        if grounding_issue:
            return grounding_issue + source_hint
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
        ctx,
        phase=phase,
        kind=kind,
        evidence_kind=evidence_kind,
    )
    mempalace_id = ""
    legacy_payload: Any = None
    if ws:
        from umbrella.deep_agent_tools.memory import _legacy_palace_available
        from umbrella.memory.kernel.models import memory_event_from_tool_write
        from umbrella.memory.kernel.writer import write_memory_event

        repo_root = umbrella_tools._resolve_umbrella_repo_root(ctx)
        mem_body = (
            body
            if isinstance(body, str)
            else json.dumps(body, ensure_ascii=False, indent=2)
        )
        mem_content = f"[{title}]\n{mem_body}" if title else mem_body
        event = memory_event_from_tool_write(
            content=mem_content,
            title=title or kind or "phase note",
            memory_kind=kind,
            workspace_id=ws,
            tags=tag_list,
            scope=mem_scope,
            tier=mem_tier,
            phase_id=phase,
            run_id=_run_id(ctx),
            subtask_id=subtask_id,
            source_path=source_id or "tool:palace_add",
            verified=memory_verified,
            palace_store=mem_store,
            metadata={
                "palace_path": palace_path,
                "evidence_kind": evidence_kind,
            },
        )
        try:
            write_result = write_memory_event(
                repo_root,
                event,
                workspace_id=ws,
                mirror_legacy=_legacy_palace_available(),
            )
            if write_result.saved:
                mempalace_id = write_result.canonical_id
            elif write_result.policy_issues:
                return _json(
                    {
                        "saved": False,
                        "status": "blocked",
                        "reason": "evidence_bound_memory",
                        "issues": list(write_result.policy_issues),
                    }
                )
        except Exception:
            mempalace_id = ""

    saved = bool(mempalace_id)
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
    }
    if legacy_payload is not None:
        payload["legacy"] = legacy_payload
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
                "Generated workspace projects should expose LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL as their public runtime contract.",
                "Umbrella maps host control-plane launch env into public LLM_* aliases before workspace commands run. Do not document, test, or require control-plane aliases inside generated projects. OPENAI_API_KEY is not required unless the generated project intentionally chooses OpenAI as its LLM provider.",
            ]
            if provider_ready and not present.get("LLM_API_KEY")
            else [],
        }
    )


def _palace_health(ctx: ToolContext) -> str:
    repo_root = pathlib.Path(getattr(ctx, "host_repo_root", None) or getattr(ctx, "repo_dir", ".")).resolve()
    ws = _workspace_id(ctx)
    health: dict[str, Any] = {"ok": True, "backend": "canonical_mempalace"}
    status = "ok"
    try:
        from umbrella.memory.palace.facade import MemPalace

        palace = MemPalace(repo_root, ws or None)
        try:
            health = palace.health()
            if not health.get("ok", True):
                status = "error"
        finally:
            palace.close()
    except Exception as exc:
        status = "error"
        health = {"ok": False, "error": str(exc)}
    try:
        tree = umbrella_tools.list_memory_tree(ctx, workspace_id=ws)
        tree_payload = json.loads(tree)
    except Exception as exc:
        tree_payload = {"error": str(exc)}
    try:
        from umbrella.memory.backends.hindsight import HindsightBackend

        hindsight = HindsightBackend.from_env(repo_root=repo_root, workspace_id=ws).health()
    except Exception as exc:
        hindsight = {"ok": False, "enabled": False, "backend": "hindsight", "error": str(exc)}
    mode = os.environ.get("UMBRELLA_MEMORY_DURABLE_BACKEND", "canonical")
    return _json(
        {
            "status": status,
            "health": health,
            "tree": tree_payload,
            "memory_backend": {
                "canonical_mempalace": health,
                "hindsight": hindsight,
                "mode": mode,
                "overall_ok": status == "ok",
            },
        }
    )


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


def _blocking_contract_issues(issues: list[ContractIssue]) -> list[ContractIssue]:
    return [
        issue
        for issue in issues
        if issue.severity in {"error", "blocking", "human_required"}
    ]


def _contract_issue_text(issues: list[ContractIssue], *, limit: int = 12) -> str:
    return "; ".join(
        (
            f"{issue.code}"
            f"{f'[{issue.subtask_id}]' if getattr(issue, 'subtask_id', '') else ''}: "
            f"{issue.message or issue.code}"
        )
        for issue in issues[:limit]
    )


def _plan_stub_intent_issue(plan: dict[str, Any], notes: str = "") -> str:
    if '"_depth_limit": true' in json.dumps(plan, ensure_ascii=False).lower():
        return (
            "phase plan contains a depth-limit placeholder; expand it into "
            "concrete executable leaf subtasks before proposing the plan"
        )
    for text in _iter_plan_strings({"plan": plan, "notes": notes}):
        lowered = str(text or "").lower()
        if not any(token in lowered for token in ("stub", "mock", "placeholder")):
            continue
        if re.search(
            r"\b(?:no|not|never|without|avoid|reject|forbid|forbidden|"
            r"disallow|prohibit|prohibited|anti[-_\s]?patterns?)\b"
            r".{0,120}\b(?:stub|mock|placeholder|dry[-\s]?run)\b",
            lowered,
        ):
            continue
        if any(
            token in lowered
            for token in ("implement", "build", "create", "add", "fix", "repair")
        ):
            return "plan proposes stub/mock/placeholder implementation for required behavior"
    return ""


def _plan_unknown_tool_issues(plan: dict[str, Any]) -> list[str]:
    from ouroboros.tools.registry import CORE_TOOL_NAMES

    known = set(CORE_TOOL_NAMES) | {
        "shell",
        "read_file",
        "list_files",
        "harness_run",
        "mutate_phase_plan",
        "run_subtask_proof",
        "request_watcher_review",
        "mark_subtask_complete",
        "submit_micro_review",
        "run_workspace_verify",
    }
    issues: list[str] = []

    def walk(value: Any, path: str = "plan") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                validates_tools = str(key) == "allowed_tools" or (
                    str(key) == "tools" and ".subtasks[" in path
                )
                if validates_tools:
                    raw_items = child if isinstance(child, list) else str(child).split(",")
                    for raw in raw_items:
                        name = str(raw or "").strip()
                        if name and name not in known:
                            issues.append(
                                f"plan field `{child_path}` declares unknown phase tool `{name}`"
                            )
                else:
                    walk(child, child_path)
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, f"{path}[{idx}]")

    walk(plan)
    return issues


def _iter_plan_subtasks_for_policy(value: Any) -> list[dict[str, Any]]:
    subtasks: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in {"subtasks", "phases", "ordered_subtasks"} and isinstance(child, list):
                subtasks.extend(item for item in child if isinstance(item, dict))
            subtasks.extend(_iter_plan_subtasks_for_policy(child))
    elif isinstance(value, list):
        for child in value:
            subtasks.extend(_iter_plan_subtasks_for_policy(child))
    return subtasks


def _declared_plan_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in {
                "files",
                "file",
                "deliverables",
                "files_to_create",
                "files_to_change",
                "files_affected",
            }:
                raw_items = child if isinstance(child, list) else [child]
                paths.extend(str(item or "") for item in raw_items)
            else:
                paths.extend(_declared_plan_paths(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(_declared_plan_paths(child))
    return [path.replace("\\", "/").strip().strip("/") for path in paths if str(path).strip()]


def _plan_existing_workspace_policy_issues(ctx: ToolContext, plan: dict[str, Any]) -> list[str]:
    existing_roots = _workspace_existing_impl_roots(ctx)
    if not existing_roots:
        return []
    issues: list[str] = []
    migration_text = "\n".join(_iter_plan_strings(plan)).lower()
    has_migration_intent = any(
        token in migration_text
        for token in ("migrate", "move existing", "integrate existing", "remove obsolete")
    )
    allowed_new_roots = {"tests", "docs", "src", "scripts", "assets"}
    for path in _declared_plan_paths(plan):
        top = path.split("/", 1)[0]
        if (
            top
            and "/" in path
            and top not in existing_roots
            and top not in allowed_new_roots
            and not has_migration_intent
        ):
            issues.append(
                f"new top-level implementation root `{top}` would be introduced "
                "beside existing workspace roots; migrate/integrate existing code "
                "or declare an explicit migration instead of scaffolding a parallel root"
            )
    for subtask in _iter_plan_subtasks_for_policy(plan):
        subtask_id = str(subtask.get("id") or subtask.get("title") or "<unknown>")
        text = "\n".join(_iter_plan_strings(subtask)).lower()
        if "without scaffolding" in text or "not scaffold" in text:
            continue
        if any(
            phrase in text
            for phrase in (
                "setup project structure",
                "project setup",
                "create full-stack",
                "from scratch",
                "scaffold",
            )
        ):
            issues.append(
                f"subtask `{subtask_id}` proposes setup/scaffold/create-from-scratch "
                "work for an existing workspace; repair or integrate the current "
                "implementation instead of scaffolding/building project structure from scratch"
            )
    return issues


def _plan_success_test_policy_issues(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for subtask in _iter_plan_subtasks_for_policy(plan):
        subtask_id = str(subtask.get("id") or subtask.get("title") or "<unknown>")
        raw_success = (
            subtask.get("verification_command")
            or subtask.get("success_test")
            or subtask.get("success_checks")
            or subtask.get("success_check")
        )
        verification_command = str(subtask.get("verification_command") or "").strip()
        if not verification_command and isinstance(subtask.get("success_test"), list):
            issues.append(
                f"subtask `{subtask_id}` success_test must be a single "
                "executable command or a typed proof; split multiple checks "
                "into separate subtasks or a checked-in verifier script"
            )
        if not verification_command and isinstance(subtask.get("success_test"), dict):
            command_value = str(subtask["success_test"].get("command") or "").strip()
            if command_value.startswith("-"):
                issues.append(
                    f"subtask `{subtask_id}` success_test command is missing "
                    "an executable; write the full command, e.g. `python -m "
                    "pytest ...`, instead of relying on type metadata"
                )
        success_text = verification_command or str(raw_success or "")
        lowered = success_text.lower()
        if not success_text:
            continue
        if isinstance(raw_success, str):
            try:
                shlex.split(raw_success)
            except ValueError as exc:
                issues.append(
                    f"subtask `{subtask_id}` success_test has unbalanced "
                    f"double quotes or invalid shell quoting: {exc}"
                )
        if lowered.strip() in {"run_workspace_verify", "run_unit_tests"}:
            issues.append(
                f"subtask `{subtask_id}` has bare `run_workspace_verify`/"
                "`run_unit_tests` as success_test: use an executable project "
                "command or typed proof for the subtask; Umbrella runs "
                "supervisor verification separately"
            )
        elif re.match(r"^\s*(?:run_workspace_verify|run_unit_tests)\s+\S+", lowered):
            issues.append(
                f"subtask `{subtask_id}` uses a generic Umbrella supervisor "
                "tool with pseudo-arguments in success_test; replace it with "
                "a real executable project command such as `python -m pytest ...`"
            )
        elif re.match(
            r"^\s*(?:harness_run|run_workspace_verify|run_unit_tests):",
            lowered,
        ):
            issues.append(
                f"subtask `{subtask_id}` uses a generic Umbrella supervisor "
                "tool with pseudo-arguments in success_test; replace it with "
                "a real executable project command such as `python -m pytest ...`"
            )
        if re.search(r"(?:^|&&|\|\||;)\s*echo\b", lowered):
            issues.append(
                f"subtask `{subtask_id}` has decorative shell output command "
                "`echo` in success_test; replace it with a real assertion, "
                "build command, test command, or checked-in verifier script"
            )
        if re.search(
            r"\s-\s+(?:must|should|verifies?|validates?|checks?|exit\s+code)\b",
            lowered,
        ):
            issues.append(
                f"subtask `{subtask_id}` success_test contains descriptive "
                "acceptance text after an executable command; move prose into "
                "goal/notes and keep success_test as argv-only proof"
            )
        if re.match(r"\s*command\s*:", lowered):
            issues.append(
                f"subtask `{subtask_id}` success_test contains descriptive "
                "text with a prefix `Command:`; provide only the executable "
                "command"
            )
        if re.search(r"\b(?:succeeds?|passes?)\s*;\s*without\b", lowered):
            issues.append(
                f"subtask `{subtask_id}` success_test contains descriptive "
                "pass/fail outcome prose; encode this as pytest assertions or "
                "a checked-in verifier script"
            )
        parenthetical = re.search(r"\s\(([^)]{8,120})\)\s*$", success_text)
        if (
            parenthetical
            and re.search(r"\b(?:pytest|python -m pytest|npm)\b", lowered)
            and not re.search(
                r"\b(?:cd|npm|python|pytest|not|and|or)\b|&&|\|\|",
                parenthetical.group(1).lower(),
            )
        ):
            issues.append(
                f"subtask `{subtask_id}` success_test contains parenthetical "
                "explanatory prose; keep proof commands machine-executable"
            )
        if re.search(r"(?:^|\s)curl\b", lowered) and re.search(
            r"https?://(?:127\.0\.0\.1|localhost)", lowered
        ):
            issues.append(
                f"subtask `{subtask_id}` success_test uses a direct HTTP "
                "shell command against localhost; use a managed server "
                "harness with readiness and cleanup, or a checked-in verifier "
                "that starts/stops the service deterministically"
            )
        if "--passwithnotests" in lowered:
            issues.append(
                f"subtask `{subtask_id}` success_test allows empty JavaScript "
                "test suites via `--passWithNoTests`; require real tests or "
                "use a build/typecheck proof instead"
            )
        if re.search(r"\b(?:mock|fake|dry[-\s]?run|--mock(?:[-_][a-z0-9]+)?)\b", lowered):
            context_text = "\n".join(_iter_plan_strings(subtask)).lower()
            if re.search(r"\b(?:llm|gmas|bot|agent|e2e|integration|runtime)\b", context_text):
                issues.append(
                    f"subtask `{subtask_id}` success_test uses a "
                    "mocked path / mock/fake/dry-run path "
                    "for an LLM/e2e/integration proof; required behavior must "
                    "use the inherited real runtime env or fail/skip/pause "
                    "explicitly when that env is absent"
                )
        if re.search(r"(?:^|\s)(?:cd\s+)?workspaces[/\\]", lowered):
            issues.append(
                f"subtask `{subtask_id}` success_test references a host "
                "workspace path; proof commands already run inside the "
                "workspace and should use workspace-relative paths"
            )
        if re.search(r"(?:^|\s)cd\s+src(?:\s|$)", lowered):
            issues.append(
                f"subtask `{subtask_id}` success_test changes into source "
                "root `src`; proof commands should run from the workspace "
                "root with workspace-relative test and source paths"
            )
        if (
            (
                "error_llm" in lowered
                or re.search(r"\b(?:llm|gmas|bot|agent|model)\b", lowered)
            )
            and re.search(r"\bassert\b", lowered)
            and (
                "error_llm" in lowered
                or re.search(r"\bor\b[^;\n]{0,120}[\"']error[\"']", lowered)
            )
        ):
            issues.append(
                f"subtask `{subtask_id}` success_test treats an LLM/GMAS "
                "error path as a passing outcome; proof must require real "
                "success or assert that failures are surfaced/paused"
            )
        if any(
            phrase in lowered
            for phrase in (
                "user reports",
                "manual",
                "human player",
                "human verifies",
                "network inspector",
                "browser console has",
            )
        ):
            issues.append(
                f"subtask `{subtask_id}` has non-automatable success_test: "
                "describes browser/user observation; replace manual/"
                "user-reported proof with an executable command or harness"
            )
        elif "documentation of" in lowered and not any(
            token in lowered for token in ("pytest", "python", "npm", "run_", "harness")
        ):
            issues.append(
                f"subtask `{subtask_id}` has non-automatable success_test: "
                "replace prose documentation criteria with an executable command or typed proof"
            )
    return issues


def _plan_read_path_issues(ctx: ToolContext, plan: dict[str, Any]) -> list[str]:
    try:
        root = _workspace_root_for_phase(ctx, _workspace_id(ctx))
    except Exception:
        return []
    issues: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key) == "files_to_read":
                    raw_items = child if isinstance(child, list) else [child]
                    for raw in raw_items:
                        rel = str(raw or "").replace("\\", "/").strip().strip("/")
                        if rel and not (root / rel).exists():
                            issues.append(f"non-existent file `{rel}` referenced in phase plan")
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(plan)
    return issues


def _plan_compactness_issues(plan: dict[str, Any]) -> list[str]:
    subtasks = _iter_plan_subtasks_for_policy(plan)
    if len(subtasks) <= 16:
        return []
    plan_text = "\n".join(_iter_plan_strings(plan)).lower()
    if not re.search(
        r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|frontend|backend|"
        r"websocket|fastapi|react|typescript|civilization|game)\b",
        plan_text,
    ):
        return []
    return [
        "phase plan has "
        f"{len(subtasks)} executable leaves; keep large greenfield Umbrella "
        "plans compact at roughly 8-16 leaves by grouping related work into "
        "vertical slices with one real typed proof each. A good repair target "
        "is 12-14 leaves. Do not oscillate between many tiny leaves and one "
        "oversized leaf; merge adjacent vertical slices while preserving "
        "proof ownership."
    ]


_PLAN_CODE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
}


def _plan_subtask_declared_paths(subtask: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("files_to_create", "files_to_change", "files_affected"):
        raw = subtask.get(key)
        if isinstance(raw, str):
            paths.append(raw)
        elif isinstance(raw, list):
            paths.extend(str(item) for item in raw if str(item).strip())
    proof = subtask.get("proof")
    if isinstance(proof, dict):
        scope = proof.get("scope")
        if isinstance(scope, dict):
            for key in ("files_under_test", "changed_files_expected"):
                raw = scope.get(key)
                if isinstance(raw, str):
                    paths.append(raw)
                elif isinstance(raw, list):
                    paths.extend(str(item) for item in raw if str(item).strip())
    return [path.replace("\\", "/").strip("/") for path in paths if str(path).strip()]


def _plan_path_looks_like_code(path: str) -> bool:
    suffix = pathlib.PurePosixPath(str(path or "").replace("\\", "/")).suffix.lower()
    return suffix in _PLAN_CODE_EXTENSIONS


def _plan_path_looks_like_test(path: str) -> bool:
    normalised = str(path or "").replace("\\", "/").lower().strip("/")
    if not normalised:
        return False
    parts = [part for part in normalised.split("/") if part]
    if any(part in {"test", "tests", "__tests__"} for part in parts[:-1]):
        return True
    name = parts[-1]
    stem = name.rsplit(".", 1)[0]
    return (
        stem.startswith("test_")
        or stem.endswith("_test")
        or name.endswith((".test.js", ".test.jsx", ".test.ts", ".test.tsx"))
        or name.endswith((".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx"))
    )


def _plan_broad_leaf_issues(plan: dict[str, Any]) -> list[str]:
    subtasks = _iter_plan_subtasks_for_policy(plan)
    if len(subtasks) < 6:
        return []
    plan_text = "\n".join(_iter_plan_strings(plan)).lower()
    if not re.search(
        r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|frontend|backend|"
        r"websocket|fastapi|react|typescript|civilization|game)\b",
        plan_text,
    ):
        return []
    too_broad: list[str] = []
    for idx, subtask in enumerate(subtasks, start=1):
        paths = sorted(set(_plan_subtask_declared_paths(subtask)))
        code_paths = [path for path in paths if _plan_path_looks_like_code(path)]
        if len(paths) <= 4 or len(code_paths) <= 3:
            continue
        subtask_id = str(
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or subtask.get("name")
            or f"subtask #{idx}"
        )
        too_broad.append(f"{subtask_id} ({len(paths)} files)")
    if not too_broad:
        return []
    return [
        "phase plan has implementation subtask(s) that are too broad for a "
        "bounded Umbrella execute loop: "
        + ", ".join(too_broad[:8])
        + ". [PHASE_PLAN_REPAIR_SCAFFOLD] Split broad leaves into narrower "
        "vertical product slices of about 2-4 files each, keep one behavioral "
        "typed proof per leaf, and move future/optional files into later "
        "subtasks only when they are required by the current user goal."
    ]


_PLAN_ALLOWED_ENV_FILES = {".env.example", ".env.sample", ".env.template"}
_PLAN_FORBIDDEN_SECRET_DIRS = {"secret", "secrets", "credential", "credentials"}
_PLAN_FORBIDDEN_CONTROL_DIRS = {".memory", ".umbrella", ".umbrella_scratch"}
_PLAN_FORBIDDEN_CONTROL_FILES = {"workspace.toml"}


def _plan_forbidden_file_issues(plan: dict[str, Any]) -> list[str]:
    offending: list[str] = []
    for path in _declared_plan_paths(plan):
        rel = path.replace("\\", "/").strip("/")
        parts = [part.lower() for part in pathlib.PurePosixPath(rel).parts if part]
        if not parts:
            continue
        basename = parts[-1]
        if rel.startswith("../") or "/../" in rel or parts[0] == ".git":
            offending.append(path)
            continue
        if parts[0] in _PLAN_FORBIDDEN_CONTROL_DIRS:
            offending.append(path)
            continue
        if len(parts) == 1 and basename in _PLAN_FORBIDDEN_CONTROL_FILES:
            offending.append(path)
            continue
        if basename.startswith(".env") and basename not in _PLAN_ALLOWED_ENV_FILES:
            offending.append(path)
            continue
        if any(part in _PLAN_FORBIDDEN_SECRET_DIRS for part in parts[:-1]):
            offending.append(path)
    if not offending:
        return []
    return [
        "phase plan references protected secret/env workspace path(s), "
        "paths outside the active candidate workspace, or "
        "workspace/control/evaluator path(s); do not create or modify "
        "`.memory`, `.umbrella`, `workspace.toml`, real `.env` files, or "
        "secret/credential directories, and never target `.git` or `..` "
        "host paths "
        "from generated workspace tasks. Use phase tools for memory/control "
        "signals, documented env contracts, `.env.example`, or tests that "
        "inherit Umbrella runtime aliases instead. Offending path(s): "
        + ", ".join(offending[:8])
    ]


def _plan_annotated_path_issues(plan: dict[str, Any]) -> list[str]:
    file_keys = {
        "files_to_create",
        "files_to_change",
        "files_affected",
        "files_under_test",
        "changed_files_expected",
    }
    offending: list[str] = []

    def visit(value: Any, *, in_file_field: bool = False) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, in_file_field=str(key).lower() in file_keys)
            return
        if isinstance(value, list):
            for child in value:
                visit(child, in_file_field=in_file_field)
            return
        if not in_file_field or not isinstance(value, str):
            return
        raw = value.strip()
        if re.search(r"\.[A-Za-z0-9]{1,8}\s+\([^)]{1,120}\)$", raw):
            offending.append(raw)

    visit(plan)
    if not offending:
        return []
    return [
        "phase plan file fields contain annotated pseudo-paths; use plain "
        "workspace-relative paths and move notes into goal/notes metadata. "
        "Offending path(s): " + ", ".join(offending[:8])
    ]


def _plan_frontend_test_path_issues(plan: dict[str, Any]) -> list[str]:
    offending = [
        path
        for path in _declared_plan_paths(plan)
        if path.replace("\\", "/").lower().startswith("tests/frontend/")
        and pathlib.PurePosixPath(path).suffix.lower() in {".js", ".jsx", ".ts", ".tsx"}
    ]
    if not offending:
        return []
    return [
        "frontend package test files are declared outside the frontend "
        "package; move them under `frontend/` so npm/vitest ownership, "
        "imports, and package scripts are consistent. Offending path(s): "
        + ", ".join(offending[:8])
    ]


def _pytest_targets_from_success_text(value: str) -> list[str]:
    try:
        parts = shlex.split(str(value or ""))
    except ValueError:
        parts = str(value or "").split()
    targets: list[str] = []
    for part in parts:
        token = part.replace("\\", "/").strip().strip("'\"")
        if "::" in token:
            token = token.split("::", 1)[0]
        if "/" not in token and "." not in pathlib.PurePosixPath(token).name:
            continue
        if _plan_path_looks_like_test(token):
            targets.append(token)
    return targets


def _plan_pytest_target_ownership_issues(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    declared_so_far: set[str] = set()
    for subtask in _iter_plan_subtasks_for_policy(plan):
        subtask_id = str(subtask.get("id") or subtask.get("title") or "<unknown>")
        declared = {
            path.replace("\\", "/").strip("/")
            for path in _plan_subtask_declared_paths(subtask)
        }
        success_text = str(
            subtask.get("verification_command")
            or subtask.get("success_test")
            or subtask.get("success_checks")
            or subtask.get("success_check")
            or ""
        )
        if "pytest" not in success_text.lower():
            declared_so_far.update(declared)
            continue
        targets = _pytest_targets_from_success_text(success_text)
        label = " ".join(
            str(subtask.get(key) or "")
            for key in ("id", "title", "goal")
        ).lower()
        if ("final" in label or "deployment" in label) and targets:
            reused = [
                target
                for target in targets
                if target in declared_so_far and target not in declared
            ]
            if reused:
                issues.append(
                    f"subtask `{subtask_id}` reuses prior pytest target(s) "
                    f"{reused[:8]} for final verification; declare a "
                    "distinct final proof artifact in this leaf"
                )
        missing = [
            target
            for target in targets
            if target not in declared and target not in declared_so_far
        ]
        if missing:
            issues.append(
                f"subtask `{subtask_id}` success_test references pytest "
                f"target(s) {missing[:8]} that are not declared in "
                "`files_to_create`/`files_to_change` on the same or an "
                "earlier plan leaf"
            )
        declared_so_far.update(declared)
    return issues


def _plan_frontend_build_entrypoint_issues(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    declared_so_far: set[str] = set()
    for subtask in _iter_plan_subtasks_for_policy(plan):
        declared = {
            path.replace("\\", "/")
            for path in _plan_subtask_declared_paths(subtask)
        }
        success_text = str(
            subtask.get("verification_command")
            or subtask.get("success_test")
            or subtask.get("success_check")
            or subtask.get("success_checks")
            or ""
        ).lower()
        subtask_id = str(subtask.get("id") or subtask.get("title") or "<unknown>")
        if re.search(
            r"\b(?:npm|pnpm|yarn)(?:\s+\S+){0,6}\s+(?:run\s+)?build\b|"
            r"\bvite\s+build\b",
            success_text,
        ):
            available = declared_so_far | declared
            has_index = "frontend/index.html" in available
            has_main = any(
                path in available
                for path in (
                    "frontend/src/main.tsx",
                    "frontend/src/main.jsx",
                    "frontend/src/main.ts",
                    "frontend/src/main.js",
                )
            )
            has_app = any(
                path in available
                for path in (
                    "frontend/src/App.tsx",
                    "frontend/src/App.jsx",
                    "frontend/src/App.ts",
                    "frontend/src/App.js",
                )
            )
            if not (has_index and has_main and has_app):
                issues.append(
                    f"subtask `{subtask_id}` has frontend build success_test "
                    "before the files needed for a Vite/React entrypoint are "
                    "declared; declare `frontend/index.html`, "
                    "`frontend/src/<entry>.tsx`, and `frontend/src/App.tsx` "
                    "in the same or an earlier leaf, or move the build proof "
                    "after the entrypoint leaf"
                )
        declared_so_far.update(declared)
    return issues


def _plan_revision_contract_issues(ctx: ToolContext, plan: dict[str, Any]) -> list[str]:
    overlays = getattr(ctx, "context_overlays", {}) or {}
    phase_node = overlays.get("phase_node") if isinstance(overlays, dict) else None
    overlay = phase_node.get("overlay") if isinstance(phase_node, dict) else None
    if not isinstance(overlay, dict):
        return []
    reason = str(overlay.get("retry_reason") or "").strip().lower()
    if not reason.startswith("micro review requested revisions"):
        return []
    contract = overlay.get("revision_contract")
    if not isinstance(contract, dict):
        return []
    raw_revisions = contract.get("required_plan_changes") or contract.get("revisions") or []
    revisions = [str(item).strip() for item in raw_revisions if str(item).strip()]
    if not revisions:
        return []
    plan_text = json.dumps(plan, ensure_ascii=False).lower()
    stopwords = {
        "with",
        "from",
        "into",
        "that",
        "this",
        "phase",
        "project",
        "subtask",
        "subtasks",
        "replace",
        "these",
        "fields",
        "provide",
        "specify",
        "exactly",
        "validates",
        "pytest-cov",
        "platform-appropriate",
    }
    issues: list[str] = []
    for revision in revisions:
        revision_l = revision.lower()
        if re.match(r"\s*(?:consider|optional|maybe|could|nice to have)\b", revision_l):
            continue
        if re.search(r"\bconcrete\s+executable\s+commands?\b", revision_l):
            success_texts = [
                str(
                    subtask.get("verification_command")
                    or subtask.get("success_test")
                    or subtask.get("success_check")
                    or subtask.get("success_checks")
                    or ""
                ).strip()
                for subtask in _iter_plan_subtasks_for_policy(plan)
                if isinstance(subtask, dict)
            ]
            if success_texts and all(
                re.search(
                    r"\b(?:python|py|pytest|node|npm|npx|pnpm|yarn|uv|"
                    r"playwright|run_workspace_verify|run_unit_tests|"
                    r"harness_run|http_boot|behavioral_http)\b",
                    text.lower(),
                )
                for text in success_texts
            ):
                continue
        if re.search(
            r"\bsplit\b.{0,80}\btest\s+creation\b.{0,80}\bvalidation\b"
            r".{0,80}\bseparate\s+subtasks\b",
            revision_l,
        ):
            subtasks = [
                subtask
                for subtask in _iter_plan_subtasks_for_policy(plan)
                if isinstance(subtask, dict)
            ]
            success_texts = [
                str(
                    subtask.get("verification_command")
                    or subtask.get("success_test")
                    or subtask.get("success_check")
                    or subtask.get("success_checks")
                    or ""
                ).strip()
                for subtask in subtasks
            ]
            has_test_files = any(
                any("test" in path.lower() for path in _plan_subtask_declared_paths(subtask))
                for subtask in subtasks
            )
            if (
                len(subtasks) >= 2
                and has_test_files
                and success_texts
                and all(text for text in success_texts)
            ):
                continue
        if re.search(r"(?:\$|\bbudget\b|\busd\b|\bresources?\b)", revision_l) and not re.search(
            r"\b(?:add|replace|remove|rename|specify|set|change|include|"
            r"create|split|use)\b",
            revision_l,
        ):
            continue
        if "replace" in revision_l and " with " in revision_l:
            positive = revision_l.split(" with ", 1)[1]
        elif "revision requires" in revision_l:
            positive = revision_l.split("revision requires", 1)[1]
        else:
            positive = revision_l
        semantic_numbers = re.findall(
            r"\b(\d+(?:\.\d+)?)\s*(?:times?|retries?|attempts?|turns?|"
            r"interactions?|rounds?|%)\b",
            positive,
        )
        missing_numbers = [
            number for number in semantic_numbers if number not in plan_text
        ]
        if missing_numbers:
            issues.append(
                "review revision numeric requirement appears unaddressed: "
                f"`{revision}`; missing number(s): "
                + ", ".join(missing_numbers[:8])
            )
            continue
        alternatives = [
            item.strip()
            for item in re.split(r"\bor\b", positive)
            if item.strip()
        ] or [positive]
        missing_by_alternative: list[list[str]] = []
        revision_satisfied = False
        for alternative in alternatives:
            keywords = [
                item
                for raw in re.findall(r"[a-z0-9_.-]{4,}", alternative)
                for item in (raw.strip("._-"),)
                if item
                and item not in stopwords
                and not re.fullmatch(
                    r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?",
                    item,
                )
            ]
            if not keywords:
                revision_satisfied = True
                break
            covered = [item for item in keywords if item in plan_text]
            floor = 1 if len(alternatives) > 1 else 2
            required = min(len(keywords), max(floor, (len(keywords) + 1) // 2))
            if len(covered) >= required:
                revision_satisfied = True
                break
            missing_by_alternative.append([item for item in keywords if item not in covered])
        if revision_satisfied:
            continue
        missing = min(missing_by_alternative, key=len) if missing_by_alternative else []
        issues.append(
            "review revision appears unaddressed: "
            f"`{revision}`; missing keyword(s): " + ", ".join(missing[:8])
        )
    return issues


def _legacy_success_test_to_proof(subtask: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(subtask.get("proof"), dict):
        return None
    raw = (
        subtask.get("verification_command")
        or subtask.get("success_test")
        or subtask.get("success_checks")
        or subtask.get("success_check")
    )
    if raw is None and isinstance(subtask.get("verification"), dict):
        verification = subtask["verification"]
        commands = verification.get("commands")
        if isinstance(commands, list) and len(commands) == 1:
            raw = commands[0]
        elif isinstance(commands, str):
            raw = commands
        elif isinstance(verification.get("command"), str):
            raw = verification.get("command")
    command_text = ""
    if isinstance(raw, str):
        command_text = raw.strip()
    elif isinstance(raw, dict):
        for key in ("command", "value", "cmd"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                command_text = value.strip()
                break
    if not command_text:
        return None
    try:
        command = shlex.split(command_text)
    except ValueError:
        command = command_text.split()
    lowered = command_text.lower()
    kind = "pytest" if "pytest" in lowered else "command"
    if re.search(r"\bnpm\b.{0,40}\b(?:build|run\s+build)\b", lowered):
        kind = "build"
    paths = _plan_subtask_declared_paths(subtask)
    test_paths = [path for path in paths if _plan_path_looks_like_test(path)]
    non_test_paths = [path for path in paths if path not in test_paths]
    files_under_test = non_test_paths or paths
    required_properties = ["no_test_tampering"] if test_paths else []
    if kind == "build":
        required_properties.append("build_succeeds")
    return {
        "execution": {"kind": kind, "command": command, "shell": False},
        "oracle": {
            "oracle_type": "build" if kind == "build" else "unit_assertions",
            "required_properties": required_properties,
            "negative_cases_required": kind != "build",
        },
        "scope": {
            "files_under_test": files_under_test,
            "changed_files_expected": paths,
            "pytest_targets": [item for item in command if "test" in item.lower()]
            if kind == "pytest"
            else [],
        },
        "anti_gaming": {"requires_real_runtime": False},
        "required_capabilities": [],
    }


def _migrate_legacy_success_tests(plan: dict[str, Any]) -> dict[str, Any]:
    subtasks = plan.get("subtasks")
    if not isinstance(subtasks, list):
        return plan
    changed = False
    migrated: list[Any] = []
    for item in subtasks:
        if not isinstance(item, dict):
            migrated.append(item)
            continue
        proof = _legacy_success_test_to_proof(item)
        if proof is None:
            migrated.append(item)
            continue
        migrated.append({**item, "proof": proof})
        changed = True
    if not changed:
        return plan
    return {**plan, "subtasks": migrated}


def _validate_phase_plan_contract(
    ctx: ToolContext,
    plan: dict[str, Any],
    *,
    notes: str = "",
) -> list[ContractIssue]:
    if notes and isinstance(plan, dict) and not plan.get("notes"):
        plan = {**plan, "notes": notes}
    plan_ir, compile_issues = compile_phase_plan(
        plan,
        run_id=_run_id(ctx),
        workspace_id=_workspace_id(ctx),
    )
    issues = list(compile_issues)
    contradiction = _negative_claim_contradiction_issue(
        ctx,
        rows=_tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or "")),
        text=json.dumps({"plan": plan, "notes": notes}, ensure_ascii=False),
        label="phase plan",
    )
    if contradiction:
        issues.append(
            ContractIssue(
                code="stale_plan_claim",
                severity="blocking",
                phase="plan",
                message=contradiction.removeprefix("ERROR: ").strip(),
            )
        )
    if stub_issue := _plan_stub_intent_issue(plan, notes):
        issues.append(
            ContractIssue(
                code="stub_plan_intent",
                severity="blocking",
                phase="plan",
                message=stub_issue,
            )
        )
    for tool_issue in _plan_unknown_tool_issues(plan):
        issues.append(
            ContractIssue(
                code="unknown_plan_tool",
                severity="blocking",
                phase="plan",
                message=tool_issue,
            )
        )
    for policy_issue in (
        *_plan_existing_workspace_policy_issues(ctx, plan),
        *_plan_success_test_policy_issues(plan),
        *_phase_plan_success_test_issues(plan),
        *_phase_plan_generic_success_test_issues(plan),
        *_plan_read_path_issues(ctx, plan),
        *_plan_compactness_issues(plan),
        *_plan_broad_leaf_issues(plan),
        *_plan_forbidden_file_issues(plan),
        *_plan_annotated_path_issues(plan),
        *_plan_frontend_test_path_issues(plan),
        *_plan_pytest_target_ownership_issues(plan),
        *_plan_frontend_build_entrypoint_issues(plan),
        *_phase_plan_file_reference_issues(ctx, plan),
        *_phase_plan_missing_leaf_file_field_issues(plan),
        *_phase_plan_llm_fallback_issues(plan),
        *_phase_plan_llm_test_double_issues(plan),
        *_phase_plan_llm_env_issues(plan),
        *_phase_plan_llm_provider_default_issues(plan),
        *_phase_plan_empty_test_skeleton_issues(plan),
        *_phase_plan_workspace_prefix_issues(ctx, plan),
        *_phase_plan_greenfield_layout_issues(ctx, plan),
        *_plan_revision_contract_issues(ctx, plan),
    ):
        issues.append(
            ContractIssue(
                code="phase_plan_policy",
                severity="blocking",
                phase="plan",
                message=policy_issue,
            )
        )
    workspace_id = _workspace_id(ctx)
    context = build_workspace_context(
        repo_root=pathlib.Path(ctx.host_repo_root or ctx.repo_dir),
        workspace_root=_workspace_root_for_phase(ctx, workspace_id),
        workspace_id=workspace_id,
    )
    drive_root = pathlib.Path(ctx.drive_root) if getattr(ctx, "drive_root", None) else None
    return ContractValidator.validate(
        ContractBundle(
            run_id=_run_id(ctx),
            workspace_id=workspace_id,
            plan=plan_ir,
            issues=tuple(issues),
        ),
        context=context,
        drive_root=drive_root,
    )


def _request_extra_subtask_policy_issue(
    ctx: ToolContext,
    *,
    reason: str,
    proposed_subtask: dict[str, Any] | None,
) -> str:
    if not isinstance(proposed_subtask, dict) or not proposed_subtask:
        return ""
    issues = _blocking_contract_issues(
        _validate_phase_plan_contract(ctx, {"subtasks": [proposed_subtask]})
    )
    if issues:
        return (
            "ERROR: request_extra_subtask rejected: proposed subtask violates "
            "the typed contract: "
            + _contract_issue_text(issues)
            + ". Extra subtasks must be executable product work inside the "
            "workspace with a typed proof."
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
    plan: dict[str, Any] | None = None,
    notes: str = "",
    **extra: Any,
) -> str:
    if stop := _stop_requested_message(ctx, "propose_phase_plan"):
        return stop
    if plan is None and isinstance(extra.get("content"), dict):
        plan = extra["content"]
    if not isinstance(plan, dict):
        return (
            "ERROR: phase plan contract rejected: `plan` must be a typed "
            "object with a top-level `subtasks` array."
        )
    embedded_plan = plan.get("plan")
    if isinstance(embedded_plan, str) and embedded_plan.strip():
        if plan.get("plan_truncated"):
            return (
                "ERROR: phase plan contract rejected: truncated serialized "
                "text in `plan.plan`; submit a compact JSON object with "
                "top-level `subtasks` instead of a large serialized blob."
            )
        try:
            decoded = json.loads(embedded_plan)
        except json.JSONDecodeError as exc:
            return (
                "ERROR: phase plan contract rejected: serialized text in "
                f"`plan.plan` is not valid JSON: {exc}"
            )
        if not isinstance(decoded, dict):
            return (
                "ERROR: phase plan contract rejected: serialized text in "
                "`plan.plan` must decode to a typed object."
            )
        plan = decoded
    plan = canonicalize_phase_plan(plan)
    plan = _migrate_legacy_success_tests(plan)
    if not notes:
        for key in ("note", "rationale", "explanation", "summary"):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                notes = value.strip()
                break
    contract_issues = _blocking_contract_issues(
        _validate_phase_plan_contract(ctx, plan, notes=notes)
    )
    if contract_issues:
        policy_issues = [
            issue for issue in contract_issues if issue.code == "phase_plan_policy"
        ]
        if policy_issues and len(policy_issues) == len(contract_issues):
            return (
                "ERROR: phase plan violates workspace policy: "
                + _contract_issue_text(policy_issues, limit=8)
            )
        return (
            "ERROR: phase plan contract rejected: "
            + _contract_issue_text(contract_issues, limit=8)
            + ". Revise the typed proof contract before submitting it."
        )
    plan_id = _record_phase_plan_artifact(ctx, plan=plan, notes=notes)
    signal_id = _write_phase_signal(
        ctx,
        "propose_phase_plan",
        {
            "plan_id": plan_id,
            "plan": plan,
            "notes": notes,
        },
    )
    return f"OK: phase plan proposal recorded (plan_id: {plan_id}, signal: {signal_id})"


def _propose_subtasks(ctx: ToolContext, steps: list[dict[str, Any]] | None = None, notes: str = "") -> str:
    if stop := _stop_requested_message(ctx, "propose_subtasks"):
        return stop
    plan = {"subtasks": steps or []}
    contract_issues = _blocking_contract_issues(_validate_phase_plan_contract(ctx, plan))
    if contract_issues:
        return (
            "ERROR: subtask proposal contract rejected: "
            + _contract_issue_text(contract_issues, limit=8)
            + ". Revise the typed proof contracts before recording them."
        )
    proposal_id = _record_subtask_proposal_artifact(ctx, steps=steps or [], notes=notes)
    signal_id = _write_phase_signal(
        ctx,
        "propose_subtasks",
        {"proposal_id": proposal_id, "steps": steps or [], "notes": notes},
    )
    return f"OK: subtask proposal recorded (proposal_id: {proposal_id}, signal: {signal_id})"


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


def _promote_to_durable(
    ctx: ToolContext,
    title: str = "",
    content: str = "",
    workspace_id: str = "",
    tags: str = "",
    evidence_refs: list[Any] | None = None,
    trust_level: str = "public_verified",
    verification_report_ref: dict[str, Any] | None = None,
) -> str:
    if stop := _stop_requested_message(ctx, "promote_to_durable"):
        return stop
    if _umbrella_phase_id(ctx) == "verify" and _mentions_unresolved_pass_blocker(content):
        return (
            "ERROR: promote_to_durable cannot promote a passing verification "
            "record that mentions unresolved blockers or limitations. Loop back "
            "to execute with the concrete failures, then verify again."
        )
    from umbrella.contracts import EvidenceRef, VerificationReportRef, json_ready
    from umbrella.contracts.evidence import EvidenceResolver
    from umbrella.contracts.validators import validate_verification_report_ref
    from umbrella.deep_agent_tools.memory import memory_write_policy_issues

    ws = workspace_id or _workspace_id(ctx)
    body = content or title or ""
    tag_list = [tag.strip() for tag in str(tags or "durable").replace(";", ",").split(",") if tag.strip()]
    if "durable" not in {tag.lower() for tag in tag_list}:
        tag_list.append("durable")
    phase = _umbrella_phase_id(ctx) or "verify"
    if phase == "verify" and "verification_report" not in {tag.lower() for tag in tag_list}:
        tag_list.append("verification_report")

    repo_root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir)
    try:
        repo_root = pathlib.Path(umbrella_tools._resolve_umbrella_repo_root(ctx))
    except Exception:
        pass
    ws_ctx = build_workspace_context(
        repo_root=str(repo_root.resolve()),
        workspace_root=str(_workspace_root_for_phase(ctx, ws).resolve()),
        workspace_id=ws,
    )

    typed_refs: list[dict[str, Any]] = []
    resolver_refs: list[EvidenceRef] = []
    if verification_report_ref:
        report = VerificationReportRef.from_mapping(verification_report_ref)
        issues = validate_verification_report_ref(
            report,
            context=ws_ctx,
            phase=phase,
        )
        if issues:
            return _json(
                {
                    "saved": False,
                    "status": "blocked",
                    "reason": "invalid_verification_report_ref",
                    "issues": [issue.message for issue in issues],
                }
            )
        report_ref = report.evidence_ref(phase=phase)
        typed_refs.append(json_ready(report_ref))
        resolver_refs.append(report_ref)
    for raw in evidence_refs or []:
        if isinstance(raw, dict):
            ref = EvidenceRef.from_mapping(raw)
            typed_refs.append(json_ready(ref))
            resolver_refs.append(ref)

    if resolver_refs:
        resolver_issues = EvidenceResolver(ws_ctx).validate_refs(
            tuple(resolver_refs),
            phase=phase,
        )
        if resolver_issues:
            return _json(
                {
                    "saved": False,
                    "status": "blocked",
                    "reason": "invalid_evidence_refs",
                    "issues": [issue.message for issue in resolver_issues],
                }
            )

    metadata_extra: dict[str, Any] = {
        "verify_run_id": _run_id(ctx),
        "evidence_refs": typed_refs,
        "trust_level": trust_level,
        "scope": "cross_run_durable",
        "tier": "warm",
        "phase": phase,
        "verified": True,
    }
    policy_issues = memory_write_policy_issues(
        kind="durable",
        tags=tag_list,
        metadata=metadata_extra,
    )
    if policy_issues:
        return _json(
            {
                "saved": False,
                "status": "blocked",
                "reason": "evidence_bound_memory",
                "issues": policy_issues,
            }
        )

    legacy_result = _save_umbrella_memory(
        ctx,
        palace_path=f"workspaces/{ws}/durable" if ws else "durable",
        title=title or "durable phase artifact",
        content=body,
        kind="durable",
        workspace_id=ws,
        tags=",".join(tag_list) if tag_list else (tags or "durable"),
        metadata_extra=metadata_extra,
    )
    try:
        payload = json.loads(legacy_result)
    except Exception:
        return legacy_result
    if not isinstance(payload, dict):
        return legacy_result
    if payload.get("saved") is False:
        return legacy_result
    canonical_id = str(payload.get("canonical_id") or payload.get("durable_node_id") or "")
    if canonical_id:
        payload["durable_store"] = "palace.durable"
        payload["durable_node_id"] = canonical_id
        payload["store"] = "palace.durable"
        try:
            from umbrella.memory.backends.base import DurableEvent
            from umbrella.memory.backends.factory import retain_hindsight_event_best_effort

            retain_hindsight_event_best_effort(
                repo_root=repo_root,
                workspace_id=ws,
                event=DurableEvent(
                    event_id=canonical_id,
                    kind="verification_report",
                    content=body,
                    workspace_id=ws,
                    run_id=_run_id(ctx),
                    phase_id=phase,
                    trust_level=trust_level,
                    evidence_refs=typed_refs,
                    tags=[
                        "kind:verification_report",
                        "phase:verify",
                        f"trust:{trust_level}",
                        "tier:durable",
                    ],
                    metadata={
                        "umbrella_id": canonical_id,
                        "palace_node_id": canonical_id,
                        "kind": "verification_report",
                        "trust_level": trust_level,
                    },
                ),
                op="retain_verification_report",
            )
        except Exception:
            if os.environ.get("UMBRELLA_HINDSIGHT_FAIL_CLOSED") == "1":
                raise
    return _json(payload)


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
    '_validate_phase_plan_contract',
    '_register_temp_tool',
    '_propose_phase_plan',
    '_propose_subtasks',
    '_read_drive_log',
    '_read_terminal_scrollback',
    '_promote_to_durable',
    '_blocked_destructive',
    '_schema',
]
