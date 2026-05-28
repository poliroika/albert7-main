"""State, memory, and artifact helpers for phase-contract tools."""

import sys

from umbrella.deep_agent_tools.phase_contract_common import *
from umbrella.deep_agent_tools.phase_control_common import _UNRESOLVED_PASS_BLOCKER_RE


def _save_umbrella_memory(ctx: ToolContext, **kwargs: Any) -> str:
    phase_module = sys.modules.get("ouroboros.tools.phase_contract")
    patched_tools = getattr(phase_module, "umbrella_tools", None)
    if patched_tools is not None and hasattr(patched_tools, "save_umbrella_memory"):
        return patched_tools.save_umbrella_memory(ctx, **kwargs)
    module = importlib.import_module("ouroboros.tools.umbrella_tools")
    return module.save_umbrella_memory(ctx, **kwargs)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _active_workspace_id(ctx: ToolContext) -> str:
    view = getattr(ctx, "loop_state_view", None)
    if isinstance(view, dict) and view.get("active_workspace_id"):
        return str(view["active_workspace_id"])
    try:
        parts = pathlib.Path(ctx.drive_root).resolve().parts
        if "workspaces" in parts:
            idx = parts.index("workspaces")
            if idx + 1 < len(parts):
                return parts[idx + 1]
    except Exception:
        pass
    return ""


def _workspace_id(ctx: ToolContext, explicit: str = "") -> str:
    # Phase-manifest compatibility tools are scoped to the active workspace.
    # Treat explicit workspace_id values from the model as a fallback only, so
    # stale context cannot accidentally read or write another workspace.
    return _active_workspace_id(ctx) or str(explicit or "")


def _state_dir(ctx: ToolContext) -> pathlib.Path:
    path = pathlib.Path(ctx.drive_root) / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stop_request_matches_task(payload: Any, task_id: str) -> bool:
    if not isinstance(payload, dict):
        return True
    current = str(task_id or "").strip()
    if not current:
        return False
    if payload.get("internal_recovery_route") or str(payload.get("scope") or "") == "task":
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


def _stop_requested_message(ctx: ToolContext, tool_name: str) -> str:
    stop_path = _state_dir(ctx) / "stop_requested.json"
    if not stop_path.exists():
        return ""
    try:
        payload = json.loads(stop_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        payload = {}
    if not _stop_request_matches_task(
        payload,
        str(getattr(ctx, "task_id", "") or ""),
    ):
        return ""
    if isinstance(payload, dict) and payload.get("internal_recovery_route"):
        try:
            stop_path.unlink(missing_ok=True)
        except OSError:
            pass
    run_id = payload.get("run_id") if isinstance(payload, dict) else ""
    return (
        "ERROR: stop_requested: stop was requested from the web UI; "
        f"refusing `{tool_name}` for task {getattr(ctx, 'task_id', '')}. "
        f"run_id={run_id or ''}"
    )


def _write_phase_signal(ctx: ToolContext, kind: str, payload: dict[str, Any]) -> str:
    state = _state_dir(ctx)
    signal = {
        "signal_id": str(uuid.uuid4()),
        "created_at": time.time(),
        "kind": kind,
        "payload": payload,
        "actor": "worker",
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "phase": _umbrella_phase_id(ctx),
    }
    tmp = state / "phase_control_signal.tmp"
    tmp.write_text(json.dumps(signal, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, state / "phase_control_signal.json")
    with (state / "phase_control_signals.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(signal, ensure_ascii=False) + "\n")
    return str(signal["signal_id"])


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


def _phase_id_from_task_id(ctx: ToolContext) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if ":" not in task_id:
        return ""
    suffix = task_id.rsplit(":", 1)[-1].strip().lower()
    return suffix if suffix in _KNOWN_UMBRELLA_PHASE_IDS else ""


def _phase_label(ctx: ToolContext) -> str:
    view = getattr(ctx, "loop_state_view", None)
    if isinstance(view, dict):
        phase = str(view.get("phase_label") or "").strip()
        if phase and phase.lower() not in {"linear", "phase"}:
            return phase
    task_phase = _phase_id_from_task_id(ctx)
    if task_phase:
        return task_phase
    if isinstance(view, dict):
        return str(view.get("phase_label") or "")
    return ""


def _umbrella_phase_id(ctx: ToolContext) -> str:
    overlays = getattr(ctx, "context_overlays", {}) or {}
    if isinstance(overlays, dict):
        for key in ("phase_node", "phase_manifest"):
            value = overlays.get(key)
            if not isinstance(value, dict):
                continue
            for field in ("id", "manifest_id"):
                phase_id = str(value.get(field) or "").strip()
                if phase_id:
                    return phase_id.lower()
    task_phase = _phase_id_from_task_id(ctx)
    if task_phase:
        return task_phase
    return _phase_label(ctx).lower()


def _run_id(ctx: ToolContext) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "")
    return task_id.split(":", 1)[0] if ":" in task_id else task_id


def _persist_phase_memory(
    ctx: ToolContext,
    *,
    workspace_id: str,
    content: str,
    tags: list[str],
    store: str = "palace.run",
    tier: str = "hot",
    scope: str = "run_scoped",
    source_path: str = "",
    verified: bool = False,
) -> None:
    """Mirror phase-contract artifacts into the PhaseRunner recall store."""
    try:
        from umbrella.memory.palace.facade import MemPalace

        repo_root = umbrella_tools._resolve_umbrella_repo_root(ctx)
        palace = MemPalace(repo_root, workspace_id)
        try:
            palace.add(
                store=store,
                content=content,
                tier=tier,
                scope=scope,
                tags=tags,
                phase=_umbrella_phase_id(ctx),
                run_id=_run_id(ctx),
                source_path=source_path or None,
                verified=verified,
            )
        finally:
            palace.close()
    except Exception:
        # Memory mirroring should never make the phase-completion tool fail.
        pass


def _persist_run_hot_memory(
    ctx: ToolContext,
    *,
    workspace_id: str,
    content: str,
    tags: list[str],
) -> None:
    _persist_phase_memory(
        ctx,
        workspace_id=workspace_id,
        content=content,
        tags=tags,
        store="palace.run",
        tier="hot",
        scope="run_scoped",
    )


def _split_tag_string(value: str) -> list[str]:
    tags: list[str] = []
    for raw in re.split(r"[,;\s]+", str(value or "")):
        tag = raw.strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _mentions_unresolved_pass_blocker(text: str) -> bool:
    return bool(_UNRESOLVED_PASS_BLOCKER_RE.search(str(text or "")))


def _phase_manifest_payload(ctx: ToolContext) -> dict[str, Any]:
    overlays = getattr(ctx, "context_overlays", {}) or {}
    payload = overlays.get("phase_manifest") if isinstance(overlays, dict) else None
    return payload if isinstance(payload, dict) else {}


def _phase_memory_write_rules(ctx: ToolContext) -> dict[str, Any]:
    manifest = _phase_manifest_payload(ctx)
    memory = manifest.get("memory") if isinstance(manifest.get("memory"), dict) else {}
    rules = memory.get("write_rules") if isinstance(memory.get("write_rules"), dict) else {}
    return rules if isinstance(rules, dict) else {}


def _phase_palace_exit_store(ctx: ToolContext) -> str:
    manifest = _phase_manifest_payload(ctx)
    criteria = (
        manifest.get("exit_criteria")
        if isinstance(manifest.get("exit_criteria"), dict)
        else {}
    )
    for key in ("required_palace_writes", "min_palace_writes"):
        rules = criteria.get(key) if isinstance(criteria, dict) else []
        if isinstance(rules, list):
            for rule in rules:
                if isinstance(rule, dict) and rule.get("store"):
                    return str(rule["store"])
    return ""


def _palace_add_store_policy(
    ctx: ToolContext,
    *,
    palace_path: str,
    kind: str,
    tags: list[str],
) -> tuple[str, str, str]:
    """Infer the logical MemPalace target for a compatibility palace_add call."""

    logical_stores = {
        "palace.charter",
        "palace.lesson",
        "palace.idea",
        "palace.codeptr",
        "palace.skill_index",
        "palace.run",
        "palace.phase",
        "palace.subtask",
        "palace.durable",
        "palace.transient",
    }
    normalized_path = str(palace_path or "").strip()
    normalized_path_l = normalized_path.strip("/").lower()
    kind_l = str(kind or "").strip().lower()
    tag_set = {str(tag or "").strip().lower() for tag in tags}
    rules = _phase_memory_write_rules(ctx)
    rule = rules.get(kind) if kind in rules else None
    if not isinstance(rule, dict):
        rule = None
        for tag in tags:
            candidate = rules.get(tag)
            if isinstance(candidate, dict):
                rule = candidate
                break

    store = ""
    if normalized_path in logical_stores:
        store = normalized_path
    if not store and rule and rule.get("store"):
        store = str(rule["store"])
    if not store and (
        kind_l == "subtask_card"
        or "subtask_card" in tag_set
        or re.search(r"(?:^|/)plan/subtasks(?:/|$)", normalized_path_l)
    ):
        store = "palace.subtask"
    if not store:
        store = _phase_palace_exit_store(ctx)
    if not store:
        store = "palace.run" if _umbrella_phase_id(ctx) else "palace.idea"

    tier = str((rule or {}).get("tier") or "")
    if not tier:
        if store in {"palace.charter", "palace.durable"}:
            tier = "always_on"
        elif store in {"palace.run", "palace.phase", "palace.subtask"}:
            tier = "hot"
        else:
            tier = "warm"

    scope = str((rule or {}).get("scope") or "")
    if not scope:
        if store in {"palace.lesson", "palace.durable"}:
            scope = "cross_run_durable"
        elif store == "palace.subtask":
            scope = "subtask_scoped"
        else:
            scope = "run_scoped"

    return store, tier, scope


def _subtask_id_from_phase_memory(
    *,
    title: str,
    body: Any,
    palace_path: str,
    kind: str,
    tags: list[str],
) -> str:
    """Extract a stable subtask id for subtask-scoped phase memory."""

    path_l = str(palace_path or "").lower()
    kind_l = str(kind or "").lower()
    tags_l = {str(tag or "").lower() for tag in tags}
    title_s = str(title or "").strip()
    is_subtask = (
        kind_l == "subtask_card"
        or "subtask_card" in tags_l
        or "/plan/subtasks" in path_l
        or title_s.lower().startswith("subtask:")
    )
    if not is_subtask:
        return ""

    match = re.search(r"(?i)\bsubtask\s*:\s*([A-Za-z0-9_.-]+)", title_s)
    if match:
        return match.group(1).strip()

    candidates: list[Any] = [body]
    if isinstance(body, dict):
        for key in ("content", "body", "payload"):
            if key in body:
                candidates.append(body.get(key))
    for candidate in candidates:
        data = candidate
        if isinstance(candidate, str):
            stripped = candidate.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except Exception:
                continue
        if isinstance(data, dict):
            for key in ("subtask_id", "id"):
                value = data.get(key)
                if isinstance(value, str) and re.fullmatch(
                    r"[A-Za-z0-9_.-]+", value.strip()
                ):
                    return value.strip()
    return ""


def _record_phase_plan_artifact(
    ctx: ToolContext,
    *,
    plan: dict[str, Any],
    notes: str = "",
) -> str:
    """Persist the proposed Umbrella plan for later review phases.

    Phase-review agents do not share the previous LLM conversation. The plan
    contract therefore has to store a concrete artifact, not just emit a
    completion signal. This is still a proposal, not the live phase plan
    state; only submit_phase_plan/phase_plan.json should be treated as the
    current execution contract.
    """
    state = _state_dir(ctx)
    plan_id = _phase_plan_identifier(plan)
    payload = {
        "created_at": time.time(),
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "phase": _umbrella_phase_id(ctx),
        "workspace_id": _workspace_id(ctx),
        "run_id": _run_id(ctx),
        "plan_id": plan_id,
        "plan": plan or {},
        "notes": notes,
    }
    try:
        (state / "phase_plan_proposal_latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (state / "phase_plan_proposals.jsonl").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

    content = json.dumps(
        {
            "artifact": "phase_plan_proposal",
            "run_id": payload["run_id"],
            "workspace_id": payload["workspace_id"],
            "notes": notes,
            "plan": plan or {},
        },
        ensure_ascii=False,
        indent=2,
    )
    ws = payload["workspace_id"]
    if ws:
        _persist_run_hot_memory(
            ctx,
            workspace_id=ws,
            content=content,
            tags=["phase_plan_proposal", "umbrella_plan_candidate"],
        )
        try:
            _save_umbrella_memory(
                ctx,
                palace_path=f"workspaces/{ws}/phase_plan/proposals",
                title="Umbrella phase plan proposal",
                content=content,
                kind="phase_plan_proposal",
                workspace_id=ws,
                tags="phase_plan_proposal,umbrella_plan_candidate",
            )
        except Exception:
            pass
    return plan_id


def _record_subtask_proposal_artifact(
    ctx: ToolContext,
    *,
    steps: list[dict[str, Any]],
    notes: str = "",
) -> str:
    state = _state_dir(ctx)
    proposal_id = "subtasks:" + str(uuid.uuid4())
    payload = {
        "created_at": time.time(),
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "phase": _umbrella_phase_id(ctx),
        "workspace_id": _workspace_id(ctx),
        "run_id": _run_id(ctx),
        "proposal_id": proposal_id,
        "steps": steps,
        "notes": notes,
    }
    try:
        (state / "subtask_proposal_latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (state / "subtask_proposals.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    ws = payload["workspace_id"]
    if ws:
        _persist_run_hot_memory(
            ctx,
            workspace_id=ws,
            content=json.dumps(
                {
                    "artifact": "subtask_proposal",
                    "run_id": payload["run_id"],
                    "workspace_id": ws,
                    "notes": notes,
                    "steps": steps,
                },
                ensure_ascii=False,
                indent=2,
            ),
            tags=["subtask", "umbrella_plan"],
        )
    return proposal_id


def _phase_plan_identifier(plan: dict[str, Any] | None) -> str:
    """Return a stable human/passable identifier for a proposed phase plan."""
    data = plan if isinstance(plan, dict) else {}
    for key in ("plan_id", "phase_id", "id", "name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("phases", "subtasks", "steps"):
        value = data.get(key)
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                first_id = first.get("id") or first.get("phase_id")
                if isinstance(first_id, str) and first_id.strip():
                    return f"phase_plan:{first_id.strip()}"
    return f"phase_plan_{uuid.uuid4().hex[:12]}"


__all__ = [
    '_save_umbrella_memory',
    '_json',
    '_active_workspace_id',
    '_workspace_id',
    '_state_dir',
    '_stop_request_matches_task',
    '_stop_requested_message',
    '_write_phase_signal',
    '_phase_label',
    '_umbrella_phase_id',
    '_run_id',
    '_persist_phase_memory',
    '_persist_run_hot_memory',
    '_split_tag_string',
    '_mentions_unresolved_pass_blocker',
    '_phase_manifest_payload',
    '_phase_memory_write_rules',
    '_phase_palace_exit_store',
    '_palace_add_store_policy',
    '_subtask_id_from_phase_memory',
    '_record_phase_plan_artifact',
    '_record_subtask_proposal_artifact',
    '_phase_plan_identifier',
]
