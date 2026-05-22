"""State, signal, and log helpers for phase-control tools."""

from umbrella.deep_agent_tools.phase_control_common import *


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
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_phase_plan(ctx: ToolContext, plan: dict[str, Any]) -> None:
    plan_path = pathlib.Path(
        os.environ.get("OUROBOROS_PHASE_PLAN_PATH", str(_drive_state(ctx) / "phase_plan.json"))
    )
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")


_KNOWN_UMBRELLA_PHASE_IDS = {
    "preflight",
    "research",
    "research_review",
    "plan",
    "plan_review",
    "execute",
    "subtask_review",
    "final_review",
    "verify",
}


_LLM_CACHED_DECISION_HANDOFF_RE = re.compile(
    r"(?is)"
    r"\b(?:decision|action|response|reasoning)\s+caching\b|"
    r"\bcach(?:e|ed|ing)\b[^.;\n]{0,100}"
    r"\b(?:decisions?|actions?|responses?|outputs?|reasoning)\b|"
    r"\breuse\s+cached\s+"
    r"(?:decisions?|actions?|responses?|outputs?|reasoning)\b"
)


def _phase_id_from_task_id(task_id: str) -> str:
    value = str(task_id or "").strip()
    if ":" in value:
        suffix = value.rsplit(":", 1)[-1].strip()
        if suffix in _KNOWN_UMBRELLA_PHASE_IDS:
            return suffix
    return ""


def _phase_control_phase_id(ctx: ToolContext) -> str:
    task_phase = _phase_id_from_task_id(str(getattr(ctx, "task_id", "") or ""))
    if task_phase:
        return task_phase
    view = getattr(ctx, "loop_state_view", None)
    phase = str(view.get("phase_label") or "").strip() if isinstance(view, dict) else ""
    if phase.lower() in {"linear", "phase"}:
        return ""
    return phase


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
    plan = submitted.get("plan") if isinstance(submitted.get("plan"), dict) else {}
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


def _looks_like_mojibake(text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    if any(marker in value for marker in _MOJIBAKE_STRONG_MARKERS):
        return True
    markers = _MOJIBAKE_MARKER_RE.findall(value)
    return len(markers) >= 3 and (len("".join(markers)) / max(1, len(value))) > 0.02


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
    raw = str(text or "")
    if not raw:
        return ""
    if not re.search(r"(?i)\b(llm|gmas|bot|agent|model)\b", raw):
        return ""
    for match in _BAD_REVIEW_FALLBACK_RE.finditer(raw):
        window = _review_policy_claim_window(raw, match)
        if _review_fallback_match_is_explicitly_dangerous(window):
            return (
                f"ERROR: {label} requests or preserves forbidden LLM fallback "
                "behavior. Research handoff must describe explicit configuration, "
                "retry/pause, or surfaced runtime errors, not fallback actions or "
                "replacement AI decisions."
            )
        if (
            _review_fallback_match_is_env_alias(window)
            or _review_fallback_match_is_protective(window)
        ):
            continue
        return (
            f"ERROR: {label} requests or preserves forbidden LLM fallback "
            "behavior. Research handoff must describe explicit configuration, "
            "retry/pause, or surfaced runtime errors, not fallback actions or "
            "replacement AI decisions."
        )
    return ""


def _llm_test_double_handoff_issue(text: str, *, label: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    if not re.search(r"(?i)\b(llm|gmas|bot|agent|model)\b", raw):
        return ""
    for match in _BAD_REVIEW_LLM_TEST_DOUBLE_RE.finditer(raw):
        claim = _review_policy_claim_window(raw, match)
        if _review_llm_test_double_match_is_protective(claim):
            continue
        matched = " ".join(match.group(0).split())[:220]
        return (
            f"ERROR: {label} requests or preserves mock/fake/dry-run LLM "
            "test-double behavior for an LLM/GMAS/bot path. Research and "
            "plan handoffs may require non-LLM unit seams, but core LLM bot "
            "behavior must be proved with the inherited real runtime env or "
            "fail/skip/pause with a clear real-LLM-required message. "
            f"Matched text: `{matched}`."
        )
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
    raw = str(text or "")
    if not re.search(r"(?i)\b(llm|gmas|bot|agent|model)\b", raw):
        return ""
    for match in _LLM_CACHED_DECISION_HANDOFF_RE.finditer(raw):
        claim = _review_policy_claim_window(raw, match)
        if _llm_cached_decision_match_is_protective(claim):
            continue
        matched = " ".join(match.group(0).split())[:220]
        return (
            f"ERROR: {label} proposes cached decision/action/response reuse "
            "for an LLM/GMAS/bot path. Research and plan handoffs may cache "
            "static reference data or prompts, but bot decisions must come from "
            "fresh inherited runtime-env LLM calls or fail/skip/pause with a "
            "clear real-LLM-required message. "
            f"Matched text: `{matched}`."
        )
    return ""


def _review_policy_claim_window(text: str, match: re.Match[str]) -> str:
    """Keep fallback/mock policy checks local to the matched claim."""
    raw = str(text or "")
    return raw[max(0, match.start() - 120) : min(len(raw), match.end() + 40)]


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


def _context_overlays(ctx: ToolContext) -> dict[str, Any]:
    raw = getattr(ctx, "context_overlays", None)
    return raw if isinstance(raw, dict) else {}


def _loop_state_view(ctx: ToolContext) -> dict[str, Any]:
    raw = getattr(ctx, "loop_state_view", None)
    return raw if isinstance(raw, dict) else {}


def _is_phase_run_context(ctx: ToolContext) -> bool:
    if str(getattr(ctx, "current_task_type", "") or "").lower() == "phase_run":
        return True
    overlays = _context_overlays(ctx)
    return isinstance(overlays.get("phase_node"), dict)


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
    '_json_obj_from_preview',
    '_tool_log_rows_for_task',
    '_tool_row_time',
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
    '_is_phase_run_context',
]
