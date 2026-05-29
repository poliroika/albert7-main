"""State, signal, and log helpers for phase-control tools."""

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.deep_agent_tools.phase_control_text_quality import (
    _looks_like_mojibake,
    _normalize_handoff_text,
)
from umbrella.contracts import canonicalize_phase_plan
_PLAN_REVIEW_SUBMITTED_REL_PATH = (
    ".memory/drive/state/phase_plan_submitted_latest.json"
)


def _drive_state(ctx: ToolContext) -> pathlib.Path:
    p = ctx.drive_root / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_phase_plan(ctx: ToolContext) -> dict[str, Any] | None:
    plan_path = pathlib.Path(
        os.environ.get("OUROBOROS_PHASE_PLAN_PATH", str(_drive_state(ctx) / "phase_plan.json"))
    )
    if not plan_path.exists():
        return None
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(plan, dict):
        return None
    return plan


def _write_phase_plan(ctx: ToolContext, plan: dict[str, Any]) -> None:
    plan_path = pathlib.Path(
        os.environ.get("OUROBOROS_PHASE_PLAN_PATH", str(_drive_state(ctx) / "phase_plan.json"))
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")


from umbrella.phases.identity import (
    phase_id_from_task_id as _phase_id_from_task_id,
    resolve_phase_id as _phase_control_phase_id,
)

_LLM_CACHED_DECISION_HANDOFF_RE = re.compile(
    r"(?is)"
    r"\b(?:decision|action|response|reasoning)\s+caching\b|"
    r"\bcach(?:e|ed|ing)\b[^.;\n]{0,100}"
    r"\b(?:decisions?|actions?|responses?|outputs?|reasoning)\b|"
    r"\breuse\s+cached\s+"
    r"(?:decisions?|actions?|responses?|outputs?|reasoning)\b"
)
_NO_TEST_TAMPERING_TOKEN = r"no[_\-\s]?test[_\-\s]?tampering"
_BAD_REVIEW_REMOVE_NO_TEST_TAMPERING_RE = re.compile(
    rf"(?is)\b(?:remove|drop|delete|omit|strip)\b[^.;\n]{{0,120}}"
    rf"\b{_NO_TEST_TAMPERING_TOKEN}\b|"
    rf"\b{_NO_TEST_TAMPERING_TOKEN}\b[^.;\n]{{0,120}}"
    r"\b(?:remove|drop|delete|omit|strip|removed|dropped|deleted|omitted|stripped)\b"
)


def _write_control_signal(ctx: ToolContext, kind: str, payload: dict[str, Any]) -> str:
    signal_path = _drive_state(ctx) / "phase_control_signal.json"
    ledger_path = _drive_state(ctx) / "phase_control_signals.jsonl"
    tmp = signal_path.with_suffix(".tmp")
    phase = _phase_control_phase_id(ctx)
    data = {
        "signal_id": str(uuid.uuid4()),
        "created_at": time.time(),
        "kind": kind,
        "payload": payload,
        "actor": "worker",
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "run_id": _run_id(ctx),
        "phase": phase,
    }
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, signal_path)
    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")
    return data["signal_id"]


def _latest_phase_plan_payload(ctx: ToolContext) -> dict[str, Any]:
    latest = _drive_state(ctx) / "phase_plan_proposal_latest.json"
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _submitted_phase_plan_payload(ctx: ToolContext) -> dict[str, Any]:
    latest = _drive_state(ctx) / "phase_plan_submitted_latest.json"
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _submitted_or_latest_phase_plan_payload(ctx: ToolContext) -> dict[str, Any]:
    submitted = _submitted_phase_plan_payload(ctx)
    return submitted or _latest_phase_plan_payload(ctx)


def _record_submitted_phase_plan_artifact(
    ctx: ToolContext,
    *,
    payload: dict[str, Any],
    plan_id: str,
    notes: str = "",
) -> None:
    """Persist the selected proposal as the reviewed/executed plan contract."""
    if not isinstance(payload, dict):
        return
    state = _drive_state(ctx)
    submitted = dict(payload)
    submitted["plan_id"] = str(plan_id or submitted.get("plan_id") or "").strip()
    submitted["submitted_at"] = time.time()
    submitted["submitted_task_id"] = str(getattr(ctx, "task_id", "") or "")
    submitted["submit_notes"] = notes
    submitted.setdefault("task_id", str(getattr(ctx, "task_id", "") or ""))
    submitted.setdefault("phase", _phase_control_phase_id(ctx))
    submitted.setdefault("workspace_id", _workspace_id_from_drive(ctx))
    submitted.setdefault("run_id", _run_id(ctx))
    try:
        (state / "phase_plan_submitted_latest.json").write_text(
            json.dumps(submitted, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (state / "phase_plan_submitted.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(submitted, ensure_ascii=False) + "\n")
    except Exception:
        pass
    _mirror_submitted_phase_plan_memory(ctx, submitted=submitted)


def _mirror_submitted_phase_plan_memory(
    ctx: ToolContext, *, submitted: dict[str, Any]
) -> None:
    """Expose the selected plan to hot recall without promoting plan drafts."""
    if not isinstance(submitted, dict):
        return
    workspace_id = str(
        submitted.get("workspace_id") or _workspace_id_from_drive(ctx) or ""
    ).strip()
    if not workspace_id:
        return
    raw_plan = submitted.get("plan") if isinstance(submitted.get("plan"), dict) else {}
    plan = canonicalize_phase_plan(raw_plan)
    if plan and plan != raw_plan:
        submitted["plan"] = plan
    subtasks = plan.get("subtasks") if isinstance(plan.get("subtasks"), list) else []
    subtask_ids = [
        str(item.get("id") or "").strip()
        for item in subtasks
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    content = {
        "artifact": "phase_plan_submitted",
        "path": ".memory/drive/state/phase_plan_submitted_latest.json",
        "run_id": submitted.get("run_id") or _run_id(ctx),
        "workspace_id": workspace_id,
        "plan_id": submitted.get("plan_id") or "",
        "subtask_count": len(subtask_ids),
        "subtask_ids": subtask_ids,
    }
    try:
        from umbrella.memory.palace.facade import MemPalace

        repo_root = pathlib.Path(
            getattr(ctx, "host_repo_root", None)
            or getattr(ctx, "repo_dir", None)
            or pathlib.Path(ctx.drive_root).parents[2]
        )
        palace = MemPalace(repo_root, workspace_id)
        try:
            palace.add(
                store="palace.run",
                content=json.dumps(content, ensure_ascii=False, indent=2),
                tier="hot",
                scope="run_scoped",
                tags=[
                    "phase_plan_submitted",
                    "umbrella_plan_selected",
                    "phase_plan",
                ],
                phase="plan",
                run_id=str(submitted.get("run_id") or _run_id(ctx) or ""),
                source_path=".memory/drive/state/phase_plan_submitted_latest.json",
                verified=True,
            )
        finally:
            palace.close()
    except Exception:
        pass


def _phase_plan_payload_by_id(ctx: ToolContext, plan_id: str) -> dict[str, Any]:
    selected = str(plan_id or "").strip()
    latest_payload = _latest_phase_plan_payload(ctx)
    latest_id = str(latest_payload.get("plan_id") or "").strip()
    latest_plan = latest_payload.get("plan")
    if not latest_id and isinstance(latest_plan, dict):
        latest_id = next(
            (
                str(latest_plan.get(key) or "").strip()
                for key in ("plan_id", "phase_id", "id", "name")
                if str(latest_plan.get(key) or "").strip()
            ),
            "",
        )
    if latest_payload and (not selected or latest_id == selected):
        return latest_payload
    proposals = _drive_state(ctx) / "phase_plan_proposals.jsonl"
    try:
        lines = proposals.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        candidate_id = str(payload.get("plan_id") or "").strip()
        plan = payload.get("plan")
        if not candidate_id and isinstance(plan, dict):
            candidate_id = next(
                (
                    str(plan.get(key) or "").strip()
                    for key in ("plan_id", "phase_id", "id", "name")
                    if str(plan.get(key) or "").strip()
                ),
                "",
            )
        if candidate_id == selected:
            return payload
    return {}


def _latest_phase_plan_id(ctx: ToolContext) -> str:
    payload = _latest_phase_plan_payload(ctx)
    if not payload:
        return ""
    value = payload.get("plan_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    plan = payload.get("plan")
    if isinstance(plan, dict):
        for key in ("plan_id", "phase_id", "id", "name"):
            nested = plan.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _run_id(ctx: ToolContext) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "")
    return task_id.split(":", 1)[0] if ":" in task_id else task_id


def _stop_request_matches_task(payload: Any, task_id: str) -> bool:
    if not isinstance(payload, dict):
        return True
    current = str(task_id or "").strip()
    if not current:
        return False
    if str(payload.get("scope") or "") == "task":
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


def _matching_stop_request(ctx: ToolContext) -> dict[str, Any] | None:
    stop_path = _drive_state(ctx) / "stop_requested.json"
    if not stop_path.exists():
        return None
    try:
        payload = json.loads(stop_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        payload = {}
    if not _stop_request_matches_task(payload, str(getattr(ctx, "task_id", "") or "")):
        return None
    return payload if isinstance(payload, dict) else {}


def _stop_requested_message(ctx: ToolContext, tool_name: str) -> str:
    payload = _matching_stop_request(ctx)
    if payload is None:
        return ""
    return (
        "ERROR: stop_requested: stop was requested from the web UI; "
        f"refusing `{tool_name}` for task {getattr(ctx, 'task_id', '')}. "
        f"run_id={payload.get('run_id') or ''}"
    )


def _workspace_id_from_drive(ctx: ToolContext) -> str:
    try:
        parts = pathlib.Path(ctx.drive_root).resolve().parts
    except Exception:
        return ""
    if "workspaces" not in parts:
        return ""
    idx = parts.index("workspaces")
    if idx + 1 >= len(parts):
        return ""
    return parts[idx + 1]


def _record_research_summary_artifact(
    ctx: ToolContext,
    *,
    architecture_id: str,
    findings_ids: list[str],
    notes: str = "",
    coverage_status: str = "",
    coverage_report: dict[str, Any] | None = None,
    source_scarcity_reason: str = "",
) -> None:
    state = _drive_state(ctx)
    payload = {
        "created_at": time.time(),
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "phase": _phase_control_phase_id(ctx),
        "workspace_id": _workspace_id_from_drive(ctx),
        "run_id": _run_id(ctx),
        "architecture_id": architecture_id,
        "findings_ids": findings_ids,
        "notes": notes,
        "coverage_status": coverage_status or "verified",
        "coverage_report": coverage_report or {},
        "source_scarcity_reason": source_scarcity_reason,
    }
    try:
        (state / "research_summary_latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (state / "research_summaries.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _json_obj_from_preview(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _tool_log_rows_for_task(ctx: ToolContext, task_id: str) -> list[dict[str, Any]]:
    path = pathlib.Path(ctx.drive_root) / "logs" / "tools.jsonl"
    if not task_id or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and str(row.get("task_id") or "") == task_id:
                rows.append(row)
    except OSError:
        return rows
    return rows


def _tool_row_time(row: dict[str, Any]) -> float | None:
    raw = row.get("ts") or row.get("created_at")
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _llm_fallback_handoff_issue(text: str, *, label: str) -> str:
    del text, label
    return ""


def _llm_test_double_handoff_issue(text: str, *, label: str) -> str:
    del text, label
    return ""


def _llm_cached_decision_match_is_protective(text: str) -> bool:
    lowered = str(text or "").lower()
    cache_target = (
        r"(?:decision|action|response|reasoning)\s+caching|"
        r"cach(?:e|ed|ing)\b[^.;\n]{0,100}"
        r"(?:decisions?|actions?|responses?|outputs?|reasoning)|"
        r"reuse\s+cached\s+"
        r"(?:decisions?|actions?|responses?|outputs?|reasoning)"
    )
    negative = (
        r"no(?!-)|never|not|must\s+not|without|forbid(?:s|den)?|"
        r"disallow(?:s|ed)?|prohibit(?:s|ed)?|block(?:s|ed)?|"
        r"refuse(?:s)?\s+to|reject(?:s|ed)?"
    )
    if re.search(rf"\b(?:{negative})\b[^.;\n]{{0,160}}\b(?:{cache_target})\b", lowered):
        return True
    if re.search(
        rf"\b(?:detects?|asserts?|enforces?|prevents?|proves?|confirms?)"
        rf"\b[^.;\n]{{0,180}}\b(?:{cache_target})\b",
        lowered,
    ):
        return True
    if re.search(
        rf"\b(?:replace|remove|rewrite|revise|correct|fix)\b"
        rf"[^.;\n]{{0,180}}\b(?:{cache_target})\b",
        lowered,
    ):
        return True
    return False


def _llm_cached_decision_handoff_issue(text: str, *, label: str) -> str:
    del text, label
    return ""


def _review_policy_claim_window(text: str, match: re.Match[str]) -> str:
    """Keep fallback/mock policy checks local to the matched claim."""
    raw = str(text or "")
    return raw[max(0, match.start() - 120) : min(len(raw), match.end() + 40)]


def _review_no_test_tampering_removal_is_protective(text: str) -> bool:
    lowered = str(text or "").lower()
    token = _NO_TEST_TAMPERING_TOKEN
    if re.search(
        rf"\b(?:no|not|never|must\s+not|should\s+not|do\s+not|don't|without)\b"
        rf"[^.;\n]{{0,100}}\b(?:remove|drop|delete|omit|strip)\b"
        rf"[^.;\n]{{0,100}}\b{token}\b",
        lowered,
    ):
        return True
    if re.search(
        rf"\b(?:keep|preserve|retain)\b[^.;\n]{{0,100}}\b{token}\b",
        lowered,
    ):
        return True
    if re.search(
        rf"\b{token}\b[^.;\n]{{0,100}}"
        r"\b(?:stay|remain|be\s+kept|be\s+preserved|be\s+retained)\b",
        lowered,
    ):
        return True
    return False


def _review_no_test_tampering_removal_issue(text: str, *, label: str) -> str:
    for match in _BAD_REVIEW_REMOVE_NO_TEST_TAMPERING_RE.finditer(str(text or "")):
        window = _review_policy_claim_window(text, match)
        if _review_no_test_tampering_removal_is_protective(window):
            continue
        matched = " ".join(match.group(0).split())[:220]
        return (
            f"ERROR: {label} cannot request removing `no_test_tampering` "
            "from test-changing/test-verification subtasks. Keep the "
            "anti-tamper property and fix proof scope, pytest targets, or "
            "oracle strength instead. "
            f"Matched text: `{matched}`."
        )
    return ""


def _review_fallback_claim_is_ai_runtime_related(text: str) -> bool:
    lowered = str(text or "").lower()
    return bool(
        re.search(
            r"\b(?:llm|gmas|bot|model|provider|openai|llm_api_key|"
            r"llm_base_url|llm_model|api[-_\s]?keys?|"
            r"base[-_\s]?url|credentials?|runtime\s+env|"
            r"env(?:ironment)?\s+(?:vars?|variables?)|ai\s+(?:decisions?|actions?)|"
            r"agent(?:s)?\s+(?:decisions?|actions?|runtime|behaviou?r))\b",
            lowered,
        )
    )


def _review_fallback_match_is_explicitly_dangerous(text: str) -> bool:
    lowered = str(text or "").lower()
    if re.search(
        r"\b(?:no|never|must\s+not|do\s+not|don't|without|rejects?|"
        r"forbid(?:s|den)?|disallow(?:s|ed)?|prohibit(?:s|ed)?)\b"
        r"[^.;\n]{0,80}\b(?:fallback|fall[-\s]+back)\b",
        lowered,
    ):
        return False
    dangerous_decision = (
        r"mock|fake|stub|deterministic|heuristics?|static|default|cached|"
        r"human[-\s]?only|disabled|offline|degraded|bots?|agents?|"
        r"opponents?|ai\s+decisions?|actions?|rules?"
    )
    if re.search(
        r"\b(?:fallback|fall[-\s]+back)\b[^.;\n]{0,80}\bmode\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:must|should|required?|requires?|provide|use|using|enable|"
        r"add|create|support|allow)\b[^.;\n]{0,100}"
        r"\b(?:fallback|fall[-\s]+back)\b[^.;\n]{0,100}"
        rf"\b(?:{dangerous_decision})\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:fallback|fall[-\s]+back)\b[^.;\n]{0,80}"
        r"\b(?:mode|strategy|policy|behaviou?r|handling|logic)\b"
        rf"[^.;\n]{{0,100}}\b(?:{dangerous_decision})\b",
        lowered,
    ):
        return True
    if re.search(
        rf"\busing\s+(?:{dangerous_decision})\s+"
        r"(?:bots?|agents?|opponents?|ai\s+decisions?|actions?)\b",
        lowered,
    ):
        return True
    return False


def _review_fallback_match_is_protective(text: str) -> bool:
    lowered = str(text or "").lower()
    if re.search(
        r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
        r"disallow(?:s|ed)?|prohibit(?:s|ed)?|block(?:s|ed)?|"
        r"refuse(?:s)?\s+to|reject(?:s|ed)?)\b[^.;\n]{0,100}"
        r"\b(?:fallback|fall[-\s]+back|hardcoded|localhost\s+defaults?)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
        r"disallow(?:s|ed)?|prohibit(?:s|ed)?|block(?:s|ed)?|"
        r"refuse(?:s)?\s+to|reject(?:s|ed)?)\b[^.;\n]{0,100}"
        r"\b(?:static|heuristics?|random|default|mock|stub|cached\s+decisions?|"
        r"cached\s+actions?|graceful\s+degradation|safe\s+minimal\s+actions?|"
        r"ai\s+decisions?|actions?|rules?)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(detects?|verif(?:y|ies)|asserts?|enforces?|prevents?|"
        r"proves?|confirms?)\b.{0,140}"
        r"\b(?:fallback|fall[-\s]+back|hardcoded|localhost\s+defaults?)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(replace|remove|rewrite|revise|correct|fix)\b[^.;\n]{0,140}"
        r"\b(?:fallback|fall[-\s]+back|hardcoded|localhost\s+defaults?|"
        r"static|heuristics?|random|default|mock|stub|cached\s+decisions?|"
        r"cached\s+actions?|graceful\s+degradation|safe\s+minimal\s+actions?|"
        r"ai\s+decisions?|actions?|rules?)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:still\s+)?contains?\s+(?:unrevised|unsafe|forbidden|"
        r"policy[-\s]?violating)\b[^.;\n]{0,160}"
        r"\b(?:fallback|fall[-\s]+back|hardcoded|localhost\s+defaults?|"
        r"static|heuristics?|random|default|mock|stub|cached\s+decisions?|"
        r"cached\s+actions?|graceful\s+degradation|safe\s+minimal\s+actions?|"
        r"ai\s+decisions?|actions?|rules?)\b",
        lowered,
    ):
        return True
    return False


def _review_fallback_match_is_env_alias(text: str) -> bool:
    raw = str(text or "")
    if not _ENV_ALIAS_FALLBACK_RE.search(raw):
        return False
    if _DANGEROUS_FALLBACK_RE.search(raw) and not _review_fallback_match_is_protective(raw):
        return False
    return True


def _review_llm_test_double_match_is_protective(text: str) -> bool:
    lowered = str(text or "").lower()
    if re.search(
        r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
        r"disallow(?:s|ed)?|prohibit(?:s|ed)?|prohibition|block(?:s|ed)?|"
        r"refuse(?:s)?\s+to|reject(?:s|ed)?)\b.{0,140}"
        r"\b(?:mock|fake|dry[-\s]?run|test\s+double)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(detects?|asserts?|enforces?|prevents?|proves?|confirms?)"
        r"\b.{0,160}\b(?:mock|fake|dry[-\s]?run|test\s+double)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(replace|remove|rewrite|revise|correct|fix)\b[^.;\n]{0,160}"
        r"\b(?:mock|fake|dry[-\s]?run|test\s+double)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:still\s+)?contains?\s+(?:unrevised|unsafe|forbidden|"
        r"policy[-\s]?violating)\b[^.;\n]{0,160}"
        r"\b(?:mock|fake|dry[-\s]?run|test\s+double)\b",
        lowered,
    ):
        return True
    return False


def _review_provider_model_match_is_protective(text: str) -> bool:
    value = str(text or "")
    provider_ref = (
        r"(?:\bOPENAI_API_KEY\b|\bopenai[-_\s]?only\b|\bopenai\b|"
        r"\b(?:openai/)?gpt-[a-z0-9_.:-]+\b|\bgpt-\*\b|"
        r"https://api\.openai\.com)"
    )
    negative_before = (
        r"\b(?:no|not|never|without|avoid|reject(?:s|ed)?|"
        r"forbid(?:s|den)?|disallow(?:s|ed)?|prohibit(?:s|ed)?|"
        r"prohibition|do\s+not|does\s+not|must\s+not|should\s+not|"
        r"cannot|can't)\b"
    )
    negative_after = (
        r"\b(?:not\s+allowed|forbidden|disallowed|rejected|must\s+not|"
        r"do\s+not|avoid(?:ed)?|provider[-\s]?neutral)\b"
    )
    return bool(
        re.search(rf"(?is){negative_before}.{{0,100}}{provider_ref}", value)
        or re.search(rf"(?is){provider_ref}.{{0,100}}{negative_after}", value)
    )


def _review_text_blocks(
    *,
    issues: list[dict[str, Any]] | None,
    revisions: list[str] | None,
    notes: str,
    include_notes: bool = True,
    required_plan_changes: list[Any] | None = None,
) -> str:
    parts: list[str] = []
    for item in issues or []:
        if isinstance(item, dict):
            parts.append(str(item.get("message") or ""))
    parts.extend(str(item) for item in (revisions or []))
    if include_notes:
        parts.append(str(notes or ""))
    parts.extend(str(item) for item in (required_plan_changes or []))
    return "\n".join(part.strip() for part in parts if str(part).strip())


def _plan_review_validation_issue(
    ctx: ToolContext,
    *,
    verdict: str,
    issues: list[dict[str, Any]] | None,
    revisions: list[str] | None,
    notes: str,
    required_plan_changes: list[Any] | None = None,
) -> str:
    phase = _phase_control_phase_id(ctx)
    if phase not in {"plan_review", "subtask_review"}:
        return ""
    verdict_lc = str(verdict or "").strip().lower()
    review_text = _review_text_blocks(
        issues=issues,
        revisions=revisions,
        notes=notes,
        include_notes=verdict_lc in {"revise", "abort"},
        required_plan_changes=required_plan_changes,
    )
    if verdict_lc in {"revise", "abort"} and not review_text.strip():
        return (
            "ERROR: submit_micro_review contract rejected: revise requires "
            "actionable feedback in typed issues, required_plan_changes, or notes."
        )
    if not review_text.strip():
        return ""
    label = f"{phase} {verdict_lc or 'verdict'}"
    anti_tamper_issue = _review_no_test_tampering_removal_issue(
        review_text,
        label=label,
    )
    if anti_tamper_issue:
        return anti_tamper_issue
    if _BAD_REVIEW_MEMORY_EDIT_RE.search(review_text):
        return (
            "ERROR: plan review cannot request edits to memory/research hall "
            "artifacts; loop back to research or use palace tools instead."
        )
    if _BAD_REVIEW_NONPORTABLE_COMMAND_RE.search(review_text):
        return (
            "ERROR: plan review cannot prescribe non-portable Unix shell "
            "operators in proof commands; use argv arrays with shell=false."
        )
    for match in _BAD_REVIEW_PROVIDER_MODEL_RE.finditer(review_text):
        window = _review_policy_claim_window(review_text, match)
        if _review_provider_model_match_is_protective(window):
            continue
        return (
            "ERROR: (phase: plan_review) plan review cannot prescribe "
            "provider-specific model choices in plan revisions; use inherited "
            "runtime env aliases."
        )
    return ""


def _context_overlays(ctx: ToolContext) -> dict[str, Any]:
    raw = getattr(ctx, "context_overlays", None)
    return raw if isinstance(raw, dict) else {}


def _loop_state_view(ctx: ToolContext) -> dict[str, Any]:
    raw = getattr(ctx, "loop_state_view", None)
    if isinstance(raw, dict):
        return raw
    view: dict[str, Any] = {}
    try:
        ctx.loop_state_view = view  # type: ignore[attr-defined]
    except Exception:
        pass
    return view


def _set_loop_state_key(ctx: ToolContext, key: str, value: Any) -> None:
    view = _loop_state_view(ctx)
    view[key] = value


def _set_typed_action_gate(ctx: ToolContext, gate: dict[str, Any]) -> None:
    _set_loop_state_key(ctx, "typed_action_gate", gate)


def _clear_typed_action_gate(ctx: ToolContext) -> None:
    view = _loop_state_view(ctx)
    view.pop("typed_action_gate", None)


def _completion_tools_after_passed_proof(ctx: ToolContext) -> list[str]:
    phase = _phase_control_phase_id(ctx)
    common = ["read_file", "run_subtask_proof"]
    if phase.endswith("_review"):
        return ["submit_micro_review", *common]
    return ["mark_subtask_complete", *common]


def _set_completion_session(ctx: ToolContext, session: dict[str, Any]) -> None:
    _set_loop_state_key(ctx, "completion_session", session)
    overlays = _context_overlays(ctx)
    phase_node = overlays.get("phase_node")
    if isinstance(phase_node, dict):
        node_overlay = phase_node.get("overlay")
        if not isinstance(node_overlay, dict):
            node_overlay = {}
            phase_node["overlay"] = node_overlay
        node_overlay["completion_session"] = session


def _is_phase_run_context(ctx: ToolContext) -> bool:
    if str(getattr(ctx, "current_task_type", "") or "").lower() == "phase_run":
        return True
    overlays = _context_overlays(ctx)
    return isinstance(overlays.get("phase_node"), dict)


def _review_revision_policy_issue(
    ctx: ToolContext,
    *,
    verdict: str = "",
    revisions: list[str] | None = None,
    notes: str = "",
    reason: str = "",
) -> str:
    verdict_lc = str(verdict or "").strip().lower()
    notes_are_actionable = verdict_lc in {"revise", "abort"}
    text = "\n".join(
        str(item or "")
        for item in [
            *(revisions or []),
            *( [notes, reason] if notes_are_actionable else [] ),
        ]
        if str(item or "").strip()
    )
    if not text:
        return ""
    anti_tamper_issue = _review_no_test_tampering_removal_issue(
        text,
        label="review feedback",
    )
    if anti_tamper_issue:
        return anti_tamper_issue
    for match in _BAD_REVIEW_PROVIDER_MODEL_RE.finditer(text):
        window = _review_policy_claim_window(text, match)
        if _review_provider_model_match_is_protective(window):
            continue
        phase = _plan_review_phase_label(ctx)
        return (
            "ERROR: review feedback cannot require provider-specific model "
            "names or OpenAI-only recommendations for a provider-neutral "
            "Umbrella/Ouroboros workspace. Require the runtime alias contract "
            "and provider-neutral configuration docs instead"
            + (f" (phase: {phase})" if phase else "")
        )
    if _BAD_REVIEW_MEMORY_EDIT_RE.search(text):
        phase = _plan_review_phase_label(ctx)
        return (
            "ERROR: review feedback cannot require editing existing palace/"
            "memory/research hall artifacts as a plan-phase fix. Add a corrected "
            "palace finding or loop back to research when the handoff is unsafe; "
            "plans should ignore stale memory instead of rewriting old drawers"
            + (f" (phase: {phase})" if phase else "")
        )
    if _BAD_REVIEW_NONPORTABLE_COMMAND_RE.search(text):
        phase = _plan_review_phase_label(ctx)
        return (
            "ERROR: review feedback cannot require non-portable Unix shell "
            "commands such as grep, timeout, `|| true`, ps, or pkill as mandatory "
            "workspace verification. Require a checked-in pytest/node/browser test, "
            "a portable Python script, or an explicit PowerShell command instead"
            + (f" (phase: {phase})" if phase else "")
        )
    return ""


def _micro_review_feedback_issue(
    *, verdict: str, revisions: list[str] | None = None, notes: str = ""
) -> str:
    verdict_lc = str(verdict or "").strip().lower()
    if verdict_lc not in {"revise", "abort"}:
        return ""
    feedback_parts = [
        str(item or "").strip()
        for item in [*(revisions or []), notes]
        if str(item or "").strip()
    ]
    if feedback_parts:
        return ""
    return (
        "ERROR: submit_micro_review with verdict=revise or verdict=abort "
        "requires actionable feedback in revisions or notes. Name the exact "
        "blocking issue(s), affected subtask/path/contract, and the required "
        "correction before asking the previous phase to retry."
    )


def _plan_review_phase_label(ctx: ToolContext) -> str:
    phase = _phase_control_phase_id(ctx)
    if phase:
        return phase
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    return task_id.split(":", 1)[1] if ":" in task_id else task_id


def _plan_review_ok_artifact_issue(ctx: ToolContext, *, verdict: str) -> str:
    if _plan_review_phase_label(ctx) != "plan_review":
        return ""
    if str(verdict or "").strip().lower() != "ok":
        return ""
    payload = _submitted_phase_plan_payload(ctx)
    if not payload:
        return ""
    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)):
        created_at = None
    from umbrella.deep_agent_tools.phase_control_research import (
        _read_file_paths_for_task,
        _research_reference_was_read,
        _tool_rows_after,
    )

    rows = _tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or ""))
    rows_since_plan = _tool_rows_after(
        rows,
        float(created_at) if created_at is not None else None,
    )
    read_paths = _read_file_paths_for_task(ctx, rows_since_plan)
    if _research_reference_was_read(_PLAN_REVIEW_SUBMITTED_REL_PATH, read_paths):
        return ""
    return (
        "ERROR: plan_review ok requires reading "
        f"{_PLAN_REVIEW_SUBMITTED_REL_PATH} in this review phase before "
        "accepting the submitted handoff from memory."
    )


def _plan_review_ok_policy_issue(ctx: ToolContext, *, verdict: str) -> str:
    if _plan_review_phase_label(ctx) != "plan_review":
        return ""
    if str(verdict or "").strip().lower() != "ok":
        return ""
    payload = _submitted_phase_plan_payload(ctx)
    plan = payload.get("plan") if isinstance(payload, dict) else None
    if not isinstance(plan, dict):
        return ""
    try:
        from umbrella.deep_agent_tools.phase_contract_policy import (
            _phase_plan_policy_issues,
        )

        issues = _phase_plan_policy_issues(
            plan,
            ctx=ctx,
            notes=str(payload.get("notes") or ""),
        )
    except Exception:
        issues = []
    if not issues:
        return ""
    return (
        "ERROR: plan_review ok cannot accept the latest phase plan artifact "
        "because it violates workspace policy: "
        + "; ".join(issues)
        + ". Loop back to plan with concrete revisions instead of allowing "
        "execute to start from an unsafe plan."
    )


__all__ = [
    '_drive_state',
    '_read_phase_plan',
    '_write_phase_plan',
    '_write_control_signal',
    '_phase_control_phase_id',
    '_latest_phase_plan_payload',
    '_submitted_phase_plan_payload',
    '_submitted_or_latest_phase_plan_payload',
    '_record_submitted_phase_plan_artifact',
    '_phase_plan_payload_by_id',
    '_latest_phase_plan_id',
    '_run_id',
    '_stop_request_matches_task',
    '_matching_stop_request',
    '_stop_requested_message',
    '_workspace_id_from_drive',
    '_record_research_summary_artifact',
    '_looks_like_mojibake',
    '_normalize_handoff_text',
    '_json_obj_from_preview',
    '_tool_log_rows_for_task',
    '_tool_row_time',
    '_plan_review_validation_issue',
    '_review_text_blocks',
    '_llm_fallback_handoff_issue',
    '_llm_test_double_handoff_issue',
    '_llm_cached_decision_handoff_issue',
    '_llm_cached_decision_match_is_protective',
    '_review_policy_claim_window',
    '_review_fallback_match_is_explicitly_dangerous',
    '_review_fallback_match_is_protective',
    '_review_fallback_match_is_env_alias',
    '_review_llm_test_double_match_is_protective',
    '_review_provider_model_match_is_protective',
    '_context_overlays',
    '_loop_state_view',
    '_set_typed_action_gate',
    '_clear_typed_action_gate',
    '_completion_tools_after_passed_proof',
    '_set_completion_session',
    '_is_phase_run_context',
    '_review_revision_policy_issue',
    '_micro_review_feedback_issue',
    '_plan_review_phase_label',
    '_plan_review_ok_artifact_issue',
    '_plan_review_ok_policy_issue',
    '_PLAN_REVIEW_SUBMITTED_REL_PATH',
]
