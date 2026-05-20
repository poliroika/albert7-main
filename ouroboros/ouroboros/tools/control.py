"""Control tools exposed to the Ouroboros agent.

This module is intentionally a thin adapter over Umbrella control-plane
APIs. Phase ownership, prompt governance decisions, human checkpoints,
memory policy, and promotion/supervision semantics belong in
``umbrella.control_plane`` / ``umbrella.memory``. Ouroboros should keep only
agent-facing tool schemas, argument normalization, and safe forwarding.
"""

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, write_text, run_cmd

log = logging.getLogger(__name__)

MAX_SUBTASK_DEPTH = 3
_IMPLEMENTATION_COMPLETE_RE = re.compile(
    r"(?i)\b(implement|build|write|create|design|generate|integrate|develop|code)\b"
)
_DISCOVERY_SOURCE_TOOLS = {
    "web": {"deep_search", "web_fetch"},
    "deep_search": {"deep_search"},
    "github": {"github_project_search", "github_extract_snippets"},
    "github_search": {"github_project_search"},
    "github_snippets": {"github_extract_snippets"},
    "mcp": {"mcp_discover", "mcp_install"},
    "mcp_discover": {"mcp_discover"},
    "mcp_install": {"mcp_install"},
}

_BEHAVIOR_EVIDENCE_RE = re.compile(
    r"(?i)\b(run_workspace_verify|acceptance_command|smoke|end[- ]?to[- ]?end|"
    r"created .*\.(?:pptx|pdf|png|jpg|jpeg|csv|json|html|docx)|"
    r"exit(?:_code)?\s*[=:]\s*0|exit\s+0|pytest|test[s]? passed|"
    r"artifact|output file|http\s+200|cli)\b"
)
_IMPORT_ONLY_RE = re.compile(
    r"(?i)\b(importable|imports?|compileall|py_compile|signature|inspect\.signature|"
    r"module exports?|syntax only)\b"
)


def _validate_delivery_contract(contract: dict[str, Any] | None) -> str:
    if not isinstance(contract, dict) or not contract:
        return (
            "⚠️ propose_task_plan: include a `delivery_contract` object that states "
            "the runnable user-facing outcome, the command/check that proves it, "
            "and the artifact/result expected. This is universal delivery evidence, "
            "not a domain-specific checklist."
        )
    outcome = str(
        contract.get("outcome") or contract.get("runnable_outcome") or ""
    ).strip()
    proof = str(
        contract.get("proof")
        or contract.get("acceptance_command")
        or contract.get("smoke_command")
        or ""
    ).strip()
    artifact = str(
        contract.get("artifact") or contract.get("expected_result") or ""
    ).strip()
    if not outcome or not proof:
        return (
            "⚠️ propose_task_plan: `delivery_contract` needs at least `outcome` "
            "and `proof`/`acceptance_command`. Include `artifact` or "
            "`expected_result` when the task produces a file or service."
        )
    if _IMPORT_ONLY_RE.search(proof) and not _BEHAVIOR_EVIDENCE_RE.search(proof):
        return (
            "⚠️ propose_task_plan: delivery_contract proof cannot be import/compile/signature-only. "
            "Use a behavioral smoke check that exercises the user-facing outcome."
        )
    del artifact
    return ""


def _needs_behavior_evidence(title: str, description: str, success_check: str) -> bool:
    text = f"{title}\n{description}\n{success_check}"
    if not _IMPLEMENTATION_COMPLETE_RE.search(text):
        return False
    return True


def _behavior_evidence_warning(cur: Any, evidence_text: str, summary: str) -> str:
    combined = f"{summary}\n{evidence_text}"
    if not _needs_behavior_evidence(cur.title, cur.description, cur.success_check):
        return ""
    has_behavior = bool(_BEHAVIOR_EVIDENCE_RE.search(combined))
    import_only = bool(_IMPORT_ONLY_RE.search(combined))
    if has_behavior and not (import_only and "run_workspace_verify" not in combined):
        return ""
    return json.dumps(
        {
            "status": "warning",
            "reason": "missing_behavior_evidence",
            "subtask": cur.title,
            "next_step": (
                "Implementation-shaped subtasks need behavioral evidence: run the "
                "delivery/smoke/acceptance command, create or validate the expected "
                "artifact/result, and include exit 0/output evidence. Import, compile, "
                "or signature checks alone are not enough."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


def _prompt_governance_root(ctx: ToolContext) -> Path:
    return ctx.drive_path("state/prompt_governance")


def _build_prompt_decision_context(ctx: ToolContext):
    """Build a minimal manager context so prompt checkpoints can reuse Umbrella governance."""
    from umbrella.control_plane.models import (
        DecisionContext,
        ManagerState,
        TaskBrief,
        TaskClass,
    )

    task_id = str(ctx.task_id or f"prompt_task_{uuid.uuid4().hex[:8]}")
    brief = TaskBrief(
        task_id=task_id,
        original_input=f"Ouroboros prompt-governance action for task type '{ctx.current_task_type or 'task'}'",
        task_class=TaskClass.UNKNOWN,
        summary="Ouroboros prompt-governance action",
    )
    state = ManagerState(task_id=task_id)
    return DecisionContext(
        task_id=task_id,
        task_brief=brief,
        manager_state=state,
        workspace_id="ouroboros_manager",
    )


def _request_restart(ctx: ToolContext, reason: str) -> str:
    if str(ctx.current_task_type or "") == "evolution" and not ctx.last_push_succeeded:
        return "⚠️ RESTART_BLOCKED: in evolution mode, commit+push first."
    # Persist expected SHA for post-restart verification
    try:
        sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=ctx.repo_dir)
        branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ctx.repo_dir)
        verify_path = ctx.drive_path("state") / "pending_restart_verify.json"
        write_text(
            verify_path,
            json.dumps(
                {
                    "ts": utc_now_iso(),
                    "expected_sha": sha,
                    "expected_branch": branch,
                    "reason": reason,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception:
        log.debug(
            "Failed to read VERSION file or git ref for restart verification",
            exc_info=True,
        )
        pass
    ctx.pending_events.append(
        {"type": "restart_request", "reason": reason, "ts": utc_now_iso()}
    )
    ctx.last_push_succeeded = False
    return f"Restart requested: {reason}"


def _promote_to_stable(ctx: ToolContext, reason: str) -> str:
    ctx.pending_events.append(
        {"type": "promote_to_stable", "reason": reason, "ts": utc_now_iso()}
    )
    return f"Promote to stable requested: {reason}"


def _schedule_task(
    ctx: ToolContext, description: str, context: str = "", parent_task_id: str = ""
) -> str:
    current_depth = getattr(ctx, "task_depth", 0)
    new_depth = current_depth + 1 if parent_task_id else 0
    if new_depth > MAX_SUBTASK_DEPTH:
        return f"ERROR: Subtask depth limit ({MAX_SUBTASK_DEPTH}) exceeded. Simplify your approach."

    if getattr(ctx, "is_direct_chat", False):
        from ouroboros.utils import append_jsonl

        try:
            append_jsonl(
                ctx.drive_logs() / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "schedule_task_from_direct_chat",
                    "description": description[:200],
                    "warning": "schedule_task called from direct chat context — potential duplicate work",
                },
            )
        except Exception:
            pass

    tid = uuid.uuid4().hex[:8]
    evt = {
        "type": "schedule_task",
        "description": description,
        "task_id": tid,
        "depth": new_depth,
        "ts": utc_now_iso(),
    }
    if context:
        evt["context"] = context
    if parent_task_id:
        evt["parent_task_id"] = parent_task_id
    ctx.pending_events.append(evt)
    return f"Scheduled task {tid}: {description}"


def _cancel_task(ctx: ToolContext, task_id: str) -> str:
    ctx.pending_events.append(
        {"type": "cancel_task", "task_id": task_id, "ts": utc_now_iso()}
    )
    return f"Cancel requested: {task_id}"


def _request_review(ctx: ToolContext, reason: str) -> str:
    ctx.pending_events.append(
        {"type": "review_request", "reason": reason, "ts": utc_now_iso()}
    )
    return f"Review requested: {reason}"


def _chat_history(
    ctx: ToolContext, count: int = 100, offset: int = 0, search: str = ""
) -> str:
    from ouroboros.memory import Memory

    mem = Memory(drive_root=ctx.drive_root)
    return mem.chat_history(count=count, offset=offset, search=search)


def _update_scratchpad(ctx: ToolContext, content: str) -> str:
    """LLM-driven scratchpad update (Constitution P3: LLM-first)."""
    from ouroboros.memory import Memory

    mem = Memory(drive_root=ctx.drive_root)
    mem.ensure_files()
    mem.save_scratchpad(content)
    mem.append_journal(
        {
            "ts": utc_now_iso(),
            "content_preview": content[:500],
            "content_len": len(content),
        }
    )
    return f"OK: scratchpad updated ({len(content)} chars)"


def _send_owner_message(ctx: ToolContext, text: str, reason: str = "") -> str:
    """Send a proactive message to the owner (not as reply to a task).

    Use when you have something genuinely worth saying — an insight,
    a question, a status update, or an invitation to collaborate.
    """
    if not ctx.current_chat_id:
        return "⚠️ No active chat — cannot send proactive message."
    if not text or not text.strip():
        return "⚠️ Empty message."

    from ouroboros.utils import append_jsonl

    ctx.pending_events.append(
        {
            "type": "send_message",
            "chat_id": ctx.current_chat_id,
            "text": text,
            "format": "markdown",
            "is_progress": False,
            "ts": utc_now_iso(),
        }
    )
    append_jsonl(
        ctx.drive_logs() / "events.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "proactive_message",
            "reason": reason,
            "text_preview": text[:200],
        },
    )
    return "OK: message queued for delivery."


def _toggle_evolution(ctx: ToolContext, enabled: bool) -> str:
    """Toggle evolution mode on/off via supervisor event."""
    ctx.pending_events.append(
        {
            "type": "toggle_evolution",
            "enabled": bool(enabled),
            "ts": utc_now_iso(),
        }
    )
    state_str = "ON" if enabled else "OFF"
    return f"OK: evolution mode toggled {state_str}."


def _toggle_consciousness(ctx: ToolContext, action: str = "status") -> str:
    """Control background consciousness: start, stop, or status."""
    ctx.pending_events.append(
        {
            "type": "toggle_consciousness",
            "action": action,
            "ts": utc_now_iso(),
        }
    )
    return f"OK: consciousness '{action}' requested."


def _switch_model(
    ctx: ToolContext,
    model: str = "",
    effort: str = "",
    max_tokens: int = 0,
    temperature: float | None = None,
    tool_choice: str = "",
) -> str:
    """LLM-driven runtime switch (Constitution P3: LLM-first).

    Stored in ToolContext, applied on the next LLM call in the loop.
    """
    from ouroboros.llm import LLMClient, normalize_reasoning_effort

    available = LLMClient().available_models()
    changes = []

    if model:
        if model not in available:
            return f"⚠️ Unknown model: {model}. Available: {', '.join(available)}"
        ctx.active_model_override = model
        changes.append(f"model={model}")

    if effort:
        normalized = normalize_reasoning_effort(effort, default="medium")
        ctx.active_effort_override = normalized
        changes.append(f"effort={normalized}")

    if max_tokens:
        if int(max_tokens) <= 0:
            return "⚠️ max_tokens must be a positive integer."
        ctx.active_max_tokens_override = int(max_tokens)
        changes.append(f"max_tokens={int(max_tokens)}")

    if temperature is not None:
        temp = float(temperature)
        if temp < 0:
            return "⚠️ temperature must be >= 0."
        ctx.active_temperature_override = temp
        changes.append(f"temperature={temp:g}")

    if tool_choice.strip():
        normalized_choice = tool_choice.strip()
        ctx.active_tool_choice_override = normalized_choice
        changes.append(f"tool_choice={normalized_choice}")

    if not changes:
        return (
            f"Current available models: {', '.join(available)}. "
            "Pass any of: model, effort, max_tokens, temperature, tool_choice."
        )

    return f"OK: switching to {', '.join(changes)} on next round."


def _list_prompt_surfaces(ctx: ToolContext) -> str:
    """List the formal manager prompt surfaces that may be rewritten."""
    from umbrella.control_plane.prompt_policy import identify_prompt_surfaces

    surfaces = identify_prompt_surfaces(ctx.repo_dir)
    payload = [
        {
            "id": surface.id,
            "path": str(surface.path),
            "kind": surface.kind.value,
            "foundational": surface.foundational,
            "human_checkpoint_required": surface.human_checkpoint_required,
        }
        for surface in surfaces
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _propose_prompt_patch(
    ctx: ToolContext,
    rationale: str,
    expected_behavioral_effect: str,
    surface_id: str = "",
    evidence: list[str] | None = None,
    proposed_content: str = "",
) -> str:
    """Create a reviewable prompt patch proposal and optional human checkpoint."""
    from umbrella.control_plane.prompt_policy import (
        get_prompt_surface,
        identify_prompt_surfaces,
        propose_prompt_patch,
        save_prompt_patch_proposal,
    )
    from umbrella.control_plane.human_checkpoints import create_human_checkpoint_request

    governance_root = _prompt_governance_root(ctx)
    versions_dir = governance_root / "versions"
    proposals_dir = governance_root / "proposals"
    human_checkpoints_dir = governance_root / "human_checkpoints"
    manager_checkpoints_dir = governance_root / "manager_checkpoints"

    if surface_id.strip():
        surface = get_prompt_surface(
            surface_id=surface_id.strip(), repo_root=ctx.repo_dir
        )
    else:
        surfaces = identify_prompt_surfaces(ctx.repo_dir)
        surface = next(
            (item for item in surfaces if item.id == "ouroboros_system_prompt"),
            surfaces[0],
        )

    proposal = propose_prompt_patch(
        surface,
        repo_root=ctx.repo_dir,
        version_store_dir=versions_dir,
        task_id=str(ctx.task_id or "prompt_patch"),
        rationale=rationale,
        expected_behavioral_effect=expected_behavioral_effect,
        evidence=[str(item) for item in (evidence or [])],
        proposed_content=proposed_content or None,
    )
    save_prompt_patch_proposal(proposal, proposals_dir)

    payload = {
        "proposal_id": proposal.id,
        "surface_id": proposal.surface.id,
        "surface_path": str(proposal.surface.path),
        "risk_level": proposal.risk_level.value,
        "requires_human_checkpoint": proposal.requires_human_checkpoint,
        "base_version_id": proposal.base_version_id,
        "candidate_version_id": proposal.candidate_version_id,
        "manager_checkpoint_id": "",
        "rollback_reference": proposal.rollback_reference,
        "diff_text": proposal.diff_text,
    }

    if proposal.requires_human_checkpoint:
        request = create_human_checkpoint_request(
            task_id=str(ctx.task_id or "prompt_patch"),
            proposal=proposal,
            checkpoint_dir=human_checkpoints_dir,
            manager_checkpoint_id="",
            description=f"Approve prompt rewrite for {proposal.surface.label}",
        )
        payload["human_checkpoint_id"] = request.id
        if request.notification_message:
            notify_result = _send_owner_message(
                ctx,
                text=request.notification_message,
                reason="prompt_checkpoint",
            )
            payload["owner_notification"] = notify_result

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _get_prompt_patch_proposal(ctx: ToolContext, proposal_id: str) -> str:
    """Load a previously created prompt patch proposal."""
    from umbrella.control_plane.prompt_policy import load_prompt_patch_proposal

    proposal = load_prompt_patch_proposal(
        proposal_id,
        _prompt_governance_root(ctx) / "proposals",
    )
    return json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _resolve_prompt_checkpoint(
    ctx: ToolContext, checkpoint_id: str, approved: bool, response: str = ""
) -> str:
    """Record a human checkpoint decision and surface resume information."""
    from umbrella.control_plane.human_checkpoints import (
        record_human_checkpoint_decision,
        resume_after_human_checkpoint,
    )

    governance_root = _prompt_governance_root(ctx)
    human_checkpoints_dir = governance_root / "human_checkpoints"
    manager_checkpoints_dir = governance_root / "manager_checkpoints"

    decision = record_human_checkpoint_decision(
        checkpoint_id,
        checkpoint_dir=human_checkpoints_dir,
        approved=approved,
        response=response,
    )

    payload = {"decision": decision.model_dump(mode="json")}
    if approved:
        resume = resume_after_human_checkpoint(
            checkpoint_id,
            checkpoint_dir=human_checkpoints_dir,
            manager_checkpoint_dir=manager_checkpoints_dir,
        )
        payload["resume"] = resume.model_dump(mode="json")

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _get_task_result(ctx: ToolContext, task_id: str) -> str:
    """Read the result of a completed subtask."""
    from ouroboros.utils import task_artifact_stem

    results_dir = Path(ctx.drive_root) / "task_results"
    result_file = results_dir / f"{task_artifact_stem(task_id)}.json"
    if not result_file.exists():
        return f"Task {task_id}: not found or not yet completed"
    data = json.loads(result_file.read_text())
    status = data.get("status", "unknown")
    result = data.get("result", "")
    cost = data.get("cost_usd", 0)
    return f"Task {task_id} [{status}]: cost=${cost:.2f}\n\n[BEGIN_SUBTASK_OUTPUT]\n{result}\n[END_SUBTASK_OUTPUT]"


def _wait_for_task(ctx: ToolContext, task_id: str) -> str:
    """Check if a subtask has completed. Call repeatedly to poll."""
    from ouroboros.utils import task_artifact_stem

    results_dir = Path(ctx.drive_root) / "task_results"
    result_file = results_dir / f"{task_artifact_stem(task_id)}.json"
    if result_file.exists():
        return _get_task_result(ctx, task_id)
    return f"Task {task_id}: still running. Call again later to check."


# ---------------------------------------------------------------------------
# Adaptive task planner tools (see ouroboros.task_planner for the data model
# and ouroboros.loop for orchestration). The handlers stay thin — all storage
# and validation lives in the planner module so we don't duplicate it.
# ---------------------------------------------------------------------------


def _propose_task_plan(
    ctx: ToolContext,
    steps: list[dict] | None = None,
    delivery_contract: dict[str, Any] | None = None,
) -> str:
    """Persist the initial plan returned by the planner round.

    The orchestrator drives a single dedicated round whose only acceptable
    terminator is this call. We deliberately allow it to be invoked at most
    once per task: subsequent restructuring must go through
    ``revise_remaining_plan`` so the cursor and history are preserved.
    """
    from ouroboros.task_planner import (
        current_task_id,
        current_workspace_id,
        store_for_ctx,
    )

    if not isinstance(steps, list) or not steps:
        return "⚠️ propose_task_plan: 'steps' must be a non-empty list of {title,description,success_check}."
    contract_msg = _validate_delivery_contract(delivery_contract)
    if contract_msg:
        return contract_msg
    store = store_for_ctx(ctx)
    task_id = current_task_id(ctx)
    existing = store.load(task_id)
    if existing is not None:
        return (
            "⚠️ propose_task_plan: a plan already exists for this task. "
            "Use revise_remaining_plan to change the upcoming steps."
        )
    # Tier 3.2 — planner must consult at least one source of context
    # (workspace memory, deep_search, github_*, mcp_discover, web_fetch,
    # or a read of TASK_MAIN/workspace files) before committing a plan.
    # Without this nudge we routinely see planners that invent plans
    # purely from the task brief and miss critical prior workspace
    # state. The gate is satisfied by ANY discovery call recorded in
    # ``loop_state_view``; if the loop state isn't wired through, we
    # stay silent so legacy tests pass.
    discovery_gate_msg = _check_planner_discovery_gate(ctx)
    if discovery_gate_msg:
        return discovery_gate_msg
    objective = str(getattr(ctx, "task_main_digest", "") or "")
    try:
        plan = store.create_from_steps(
            task_id=task_id,
            workspace_id=current_workspace_id(ctx),
            objective_digest=objective,
            steps=steps,
            delivery_contract=delivery_contract,
        )
    except ValueError as exc:
        return f"⚠️ propose_task_plan: {exc}"
    titles = ", ".join(f"#{i + 1} {s.title}" for i, s in enumerate(plan.subtasks))
    return f"OK: plan stored with {len(plan.subtasks)} subtask(s). {titles}"


def _propose_discovery_plan(
    ctx: ToolContext,
    intent: str = "",
    phases: list[dict] | None = None,
    reuse_policy: str = "",
) -> str:
    """Let the agent author its own research budget before planning work."""

    if not isinstance(phases, list) or not phases:
        return (
            "⚠️ propose_discovery_plan: 'phases' must be a non-empty list. "
            "Each item should say which phase/subtask will use memory, web, "
            "GitHub, MCP, GMAS, or workspace reads and how many times/budget."
        )
    allowed_sources = {
        "memory",
        "web",
        "github",
        "mcp",
        "gmas",
        "workspace",
        "docs",
        "none",
    }
    source_aliases = {
        "workspace_files": "workspace",
        "workspace-file": "workspace",
        "workspace_filesystem": "workspace",
        "repo": "workspace",
        "workspace_reads": "workspace",
        "workspace_read": "workspace",
        "library_verification": "docs",
        "library_docs": "docs",
        "git": "github",
        "github_search": "github",
        "deep_search": "web",
        "web_search": "web",
        "mcp_discover": "mcp",
        "mcp_server": "mcp",
        "mcp_servers": "mcp",
        "mcp_install": "mcp",
        "get_umbrella_memory": "memory",
        "get_gmas_context": "gmas",
        "search_gmas_knowledge": "gmas",
        "gmas_context": "gmas",
        "gmas_examples": "gmas",
    }
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(phases, start=1):
        if not isinstance(item, dict):
            return f"⚠️ propose_discovery_plan: phase #{idx} must be an object."
        raw_sources = item.get("sources") or item.get("tools") or []
        if isinstance(raw_sources, str):
            raw_sources = [
                part.strip() for part in raw_sources.replace(";", ",").split(",")
            ]
        if not isinstance(raw_sources, list):
            return f"⚠️ propose_discovery_plan: phase #{idx} sources/tools must be a list or comma string."
        sources = [
            source_aliases.get(str(src).strip().lower(), str(src).strip().lower())
            for src in raw_sources
            if str(src).strip()
        ]
        unknown = [src for src in sources if src not in allowed_sources]
        if unknown:
            return (
                f"⚠️ propose_discovery_plan: unknown source(s) in phase #{idx}: "
                f"{', '.join(unknown)}. Allowed: {', '.join(sorted(allowed_sources))}."
            )
        try:
            max_calls = max(
                0, min(int(item.get("max_calls", item.get("budget", 1))), 20)
            )
        except (TypeError, ValueError):
            max_calls = 1
        normalized.append(
            {
                "phase": str(
                    item.get("phase") or item.get("name") or f"phase_{idx}"
                ).strip(),
                "purpose": str(item.get("purpose") or item.get("why") or "").strip(),
                "sources": sources or ["none"],
                "max_calls": max_calls,
                "reuse": str(item.get("reuse") or "").strip(),
            }
        )

    plan = {
        "intent": str(intent or "").strip(),
        "phases": normalized,
        "reuse_policy": str(reuse_policy or "").strip(),
    }
    try:
        setattr(ctx, "discovery_plan", plan)
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            view = {}
            ctx.loop_state_view = view  # type: ignore[attr-defined]
        view["discovery_plan_proposed"] = True
        view["discovery_plan"] = plan
        path = ctx.drive_path("state") / "discovery_plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        log.debug("Failed to persist discovery plan", exc_info=True)
    rendered = "; ".join(
        f"{p['phase']}={','.join(p['sources'])}<={p['max_calls']}" for p in normalized
    )
    return f"OK: discovery plan stored. {rendered}"


def _revise_remaining_plan(
    ctx: ToolContext,
    steps: list[dict] | None = None,
    reason: str = "",
) -> str:
    """Replace the tail of the plan from the current cursor onward."""
    from ouroboros.task_planner import (
        active_plan_id,
        planner_replan_limit,
        store_for_ctx,
    )

    if not isinstance(steps, list):
        return "⚠️ revise_remaining_plan: 'steps' must be a list (use [] to clear all remaining)."
    store = store_for_ctx(ctx)
    plan = store.load(active_plan_id(ctx))
    if plan is None:
        return "⚠️ revise_remaining_plan: no plan exists. Call propose_task_plan first."
    cap = planner_replan_limit()
    if cap and plan.revisions >= cap:
        return f"⚠️ revise_remaining_plan: revision limit ({cap}) reached. Continue with the existing tail."
    try:
        plan = store.apply_revision(
            plan,
            replacement_steps_for_remaining=steps,
            reason=reason or "(no reason given)",
        )
    except ValueError as exc:
        return f"⚠️ revise_remaining_plan: {exc}"
    remaining = plan.remaining()
    titles = ", ".join(
        f"#{plan.cursor + i + 1} {s.title}" for i, s in enumerate(remaining)
    )
    return (
        f"OK: plan revised (revision {plan.revisions}). Remaining: {titles or '(none)'}"
    )


def _mark_subtask_complete(
    ctx: ToolContext,
    status: str = "done",
    summary: str = "",
    evidence: list[str] | None = None,
) -> str:
    """Close the current subtask. Terminates the active subtask phase."""
    from ouroboros.task_planner import active_plan_id, store_for_ctx

    store = store_for_ctx(ctx)
    plan_id = active_plan_id(ctx)
    plan = store.load(plan_id)
    if plan is None:
        return json.dumps(
            {
                "status": "control_plane_error",
                "reason": "active_plan_missing",
                "tool": "mark_subtask_complete",
                "active_plan_id": plan_id,
                "message": "Invariant violation: completion tool cannot see the active plan.",
            },
            ensure_ascii=False,
            indent=2,
        )
    cur = plan.current()
    if cur is None:
        return "⚠️ mark_subtask_complete: plan already complete."
    evidence_text = "\n".join(str(item) for item in (evidence or []))
    if status == "skipped":
        skip_text = f"{summary}\n{evidence_text}".lower()
        cap_markers = (
            "phase cap",
            "round cap",
            "max_phase_rounds",
            "no write yet",
            "ran out of rounds",
        )
        if any(marker in skip_text for marker in cap_markers):
            return (
                "⚠️ mark_subtask_complete: do not skip a subtask because the "
                "phase/tool budget was hit. Continue fixing the same subtask, "
                "or mark it failed only with a concrete external blocker. If "
                "the planned work is obsolete, use revise_remaining_plan in the "
                "review phase instead."
            )
    if status == "done":
        if not evidence_text.strip():
            return "⚠️ mark_subtask_complete: status='done' requires concrete evidence."
        success_check = (cur.success_check or "").lower()
        if "acceptance_command" in success_check:
            ok_markers = ("exit 0", "exit=0", "exit_code=0", "returncode=0", "passed")
            if not any(marker in evidence_text.lower() for marker in ok_markers):
                return (
                    "⚠️ mark_subtask_complete: this subtask declares an acceptance_command; "
                    "run it and include exit 0 evidence before marking done."
                )
        behavior_warning = _behavior_evidence_warning(cur, evidence_text, summary)
        if behavior_warning:
            return behavior_warning
        view = getattr(ctx, "loop_state_view", None)
        diff = view.get("subtask_diff") if isinstance(view, dict) else {}
        if isinstance(diff, dict):
            text = f"{cur.title}\n{cur.description}"
            lines_added = 0
            added_file = False
            for entry in diff.values():
                if not isinstance(entry, dict):
                    continue
                try:
                    lines_added += int(entry.get("lines_added") or 0)
                except (TypeError, ValueError):
                    pass
                added_file = added_file or bool(entry.get("added_file"))
            try:
                min_lines = int(os.environ.get("OUROBOROS_MIN_IMPL_LOC", "30"))
            except (TypeError, ValueError):
                min_lines = 30
            if (
                _IMPLEMENTATION_COMPLETE_RE.search(text)
                and lines_added < max(0, min_lines)
                and not added_file
            ):
                return json.dumps(
                    {
                        "status": "warning",
                        "reason": "low_implementation_signal",
                        "subtask": cur.title,
                        "lines_added": lines_added,
                        "min_lines": min_lines,
                        "next_step": (
                            "This implementation-shaped subtask made very little code progress. "
                            "Continue implementing, revise the plan if the task is smaller than it "
                            "sounds, or include stronger evidence before marking it complete."
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
        # Tier 3.1: enforce "discovery before completion" when the
        # planner tagged this subtask ``domain_unknown``. The agent must
        # have called at least one external-research tool inside the
        # current subtask before it can close it.
        gate_msg = _check_discovery_gate(ctx, current_subtask=cur)
        if gate_msg:
            return gate_msg
        # Tier 1.3: refuse closure when verify evidence is stale or red.
        # ``cur`` carries no special tag here — this gate applies to every
        # ``done`` completion. The verifier is the canonical authority on
        # "did the work succeed".
        verify_gate_msg = _check_verify_evidence_gate(
            ctx, gate_kind="mark_subtask_complete"
        )
        if verify_gate_msg:
            return verify_gate_msg
        warning = _discovery_plan_completion_warning(ctx)
        if warning:
            return warning
    completed = store.complete_current(
        plan,
        status=status,
        summary=summary,
        evidence=evidence,
    )
    title = completed.title if completed else cur.title
    return f"OK: subtask '{title}' marked {completed.status if completed else status}."


def _discovery_plan_completion_warning(ctx: ToolContext) -> str:
    view = getattr(ctx, "loop_state_view", None)
    if not isinstance(view, dict):
        return ""
    plan = view.get("discovery_plan")
    if not isinstance(plan, dict):
        return ""
    phase_label = str(view.get("phase_label") or "").lower()
    phases = plan.get("phases")
    if not isinstance(phases, list):
        return ""
    expected_groups: list[set[str]] = []
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        phase_name = str(phase.get("phase") or "").lower()
        if phase_name and phase_name not in phase_label and "subtask" not in phase_name:
            continue
        for source in phase.get("sources") or []:
            tools = _DISCOVERY_SOURCE_TOOLS.get(str(source).lower(), set())
            if tools:
                expected_groups.append(set(tools))
    if not expected_groups:
        return ""
    counts = view.get("subtask_discovery_calls_by_tool")
    if not isinstance(counts, dict):
        counts = {}
    missing = sorted(
        "/".join(sorted(group))
        for group in expected_groups
        if not any(int(counts.get(tool) or 0) > 0 for tool in group)
    )
    if not missing:
        return ""
    return json.dumps(
        {
            "status": "warning",
            "reason": "declared_discovery_not_used",
            "missing_tools": missing,
            "next_step": (
                "Your own discovery plan declared these sources for this phase, but "
                "none were used in the current subtask window. Use the relevant "
                "discovery tool or revise the remaining plan if the source is no longer useful."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


def _mark_remediation_complete(
    ctx: ToolContext,
    summary: str = "",
    evidence: list[str] | None = None,
) -> str:
    """Terminate the remediation phase after fixing skipped/failed work."""
    evidence_items = [
        str(item).strip() for item in (evidence or []) if str(item).strip()
    ]
    if not evidence_items:
        return "⚠️ mark_remediation_complete: include concrete evidence from fixes/verifications."
    # Tier 1.3: remediation must be backed by a fresh PASSING verify run.
    # Otherwise the agent ends up marking the loop "done" while the
    # verifier still reports failures (the news_cards_ai pattern).
    verify_gate_msg = _check_verify_evidence_gate(
        ctx, gate_kind="mark_remediation_complete"
    )
    if verify_gate_msg:
        return verify_gate_msg
    from ouroboros.task_planner import load_active_plan, store_for_ctx

    store = store_for_ctx(ctx)
    plan = load_active_plan(ctx)
    if plan is not None:
        for subtask in plan.subtasks:
            if subtask.status in {"failed", "skipped"}:
                subtask.summary = (
                    summary or subtask.summary or "Remediation completed."
                ).strip()[:2000]
                subtask.evidence = (subtask.evidence or []) + evidence_items[:5]
        store.save(plan)
    return "OK: remediation phase marked complete with evidence."


# Backwards-compatible re-exports — the gate helpers themselves now
# live in ``completion_gates.py`` so control.py stays under the smoke
# test line cap. Tests and the planner module import these names from
# either location.
from ouroboros.tools.completion_gates import (  # noqa: E402
    check_discovery_gate as _check_discovery_gate,
    check_planner_discovery_gate as _check_planner_discovery_gate,
    check_verify_evidence_gate as _check_verify_evidence_gate,
)


def _get_current_plan(ctx: ToolContext) -> str:
    """Return a JSON snapshot of the live plan (useful after compaction)."""
    from ouroboros.task_planner import active_plan_id, store_for_ctx

    store = store_for_ctx(ctx)
    plan = store.load(active_plan_id(ctx))
    if plan is None:
        return "No plan stored for this task yet."
    return json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)


def _run_workspace_task(
    ctx: ToolContext, task_input: str, workspace_id: str = "", max_iterations: int = 5
) -> str:
    """Delegate a task to the Umbrella workspace-first manager."""
    try:
        from umbrella.integration import run_manager_task
    except ImportError:
        return "umbrella package not available on PYTHONPATH; cannot delegate to workspace manager."

    repo_root = Path(ctx.host_repo_root or ctx.repo_dir) if ctx.repo_dir else None
    kwargs: dict[str, Any] = {
        "task_input": task_input,
        "repo_root": repo_root,
        "max_iterations": max_iterations,
    }
    if workspace_id:
        kwargs["workspace_id"] = workspace_id

    try:
        result = run_manager_task(**kwargs)
        summary = (
            f"Workspace task completed.\n"
            f"  Status: {result.status}\n"
            f"  Task success: {result.task_success}\n"
            f"  Iterations: {result.iterations}\n"
            f"  Duration: {result.duration_str}\n"
            f"  Workspace: {result.workspace_id or 'auto'}\n"
        )
        if result.instance_path:
            summary += f"  Instance: {result.instance_path}\n"
        if result.evidence:
            summary += (
                "  Evidence:\n"
                + "\n".join(f"    - {e}" for e in result.evidence[:5])
                + "\n"
            )
        return summary
    except Exception as exc:
        return f"Workspace task failed: {exc}"


def _planner_tool_entries() -> list[ToolEntry]:
    """Tool entries for the adaptive planner. Extracted so ``get_tools``
    stays under the per-function line cap enforced by the smoke tests."""
    step_item_schema = {
        "type": "object",
        "required": ["title", "description"],
        "properties": {
            "title": {"type": "string", "description": "Short subtask title."},
            "description": {
                "type": "string",
                "description": "Self-contained subtask body.",
            },
            "success_check": {
                "type": "string",
                "description": "Concrete check that proves this subtask is done.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional control tags, e.g. domain_unknown to require memory/web/GitHub/MCP discovery before completion.",
            },
        },
    }
    return [
        ToolEntry(
            "propose_discovery_plan",
            {
                "name": "propose_discovery_plan",
                "description": (
                    "[PLANNER] Before propose_task_plan, author your own discovery "
                    "strategy from the task prompt. Declare which phases/subtasks "
                    "will use memory, web/deep_search, GitHub project/snippet search, "
                    "MCP discovery, GMAS retrieval, and workspace reads, with rough "
                    "max_calls and how findings should be reused as ideas, lessons, "
                    "snippets, or code references. For non-trivial coding work, "
                    "external prior-art discovery is important; if you skip web/"
                    "GitHub/MCP, state the reason in the relevant phase purpose."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["phases"],
                    "properties": {
                        "intent": {"type": "string", "default": ""},
                        "phases": {
                            "type": "array",
                            "description": "Discovery budget by phase/subtask.",
                            "items": {"type": "object"},
                        },
                        "reuse_policy": {"type": "string", "default": ""},
                    },
                },
            },
            _propose_discovery_plan,
        ),
        ToolEntry(
            "propose_task_plan",
            {
                "name": "propose_task_plan",
                "description": (
                    "[PLANNER] Submit the initial decomposition of the main task into "
                    "ordered subtasks. Call exactly once during the planner phase. "
                    "Each step needs a short title, a self-contained description, and "
                    "an explicit success_check. Also include delivery_contract: the "
                    "runnable user-facing outcome and behavioral proof command/check. "
                    "Use revise_remaining_plan later to change the upcoming tail."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["steps", "delivery_contract"],
                    "properties": {
                        "delivery_contract": {
                            "type": "object",
                            "description": (
                                "Universal delivery contract. Include outcome, proof or "
                                "acceptance_command, and artifact/expected_result when relevant. "
                                "Proof must exercise behavior, not only import/compile/signature."
                            ),
                        },
                        "steps": {
                            "type": "array",
                            "description": "Ordered list of subtasks to execute sequentially.",
                            "items": step_item_schema,
                        },
                    },
                },
            },
            _propose_task_plan,
        ),
        ToolEntry(
            "revise_remaining_plan",
            {
                "name": "revise_remaining_plan",
                "description": (
                    "[PLANNER] Replace the upcoming tail of the plan (from the current "
                    "cursor onward) with a new ordered list of subtasks. Use only when "
                    "new evidence makes the planned tail wrong. Completed subtasks are "
                    "preserved unchanged."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["steps", "reason"],
                    "properties": {
                        "steps": {
                            "type": "array",
                            "description": "New tail (use [] to clear all remaining steps).",
                            "items": step_item_schema,
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why the tail must change.",
                        },
                    },
                },
            },
            _revise_remaining_plan,
        ),
        ToolEntry(
            "mark_subtask_complete",
            {
                "name": "mark_subtask_complete",
                "description": (
                    "[PLANNER] Close the current subtask and advance the plan cursor. "
                    "Call only when the success_check is satisfied (status='done'), "
                    "the planned work became obsolete after new evidence "
                    "(status='skipped'), or it cannot be finished due to a concrete "
                    "external blocker (status='failed'). Never use status='skipped' "
                    "just because a phase cap, tool preflight error, or local attempt "
                    "failed; continue fixing the same subtask instead. Always include "
                    "a concise summary and concrete evidence (file paths, command "
                    "outputs, urls)."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["status", "summary"],
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["done", "failed", "skipped"],
                            "description": "Terminal status for the current subtask.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Concise outcome summary (<=2000 chars).",
                        },
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of evidence pointers (paths, urls, command names).",
                        },
                    },
                },
            },
            _mark_subtask_complete,
        ),
        ToolEntry(
            "mark_remediation_complete",
            {
                "name": "mark_remediation_complete",
                "description": (
                    "[PLANNER] Close the remediation phase after you have fixed or "
                    "verified previously skipped/failed subtasks. Include concrete "
                    "evidence from file writes, tests, or verification commands."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["summary", "evidence"],
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "What remediation fixed.",
                        },
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Evidence pointers proving remediation work happened.",
                        },
                    },
                },
            },
            _mark_remediation_complete,
        ),
        ToolEntry(
            "get_current_plan",
            {
                "name": "get_current_plan",
                "description": (
                    "[PLANNER] Return a JSON snapshot of the active plan, including "
                    "cursor and per-subtask status. Useful after the message history "
                    "was compacted and you need to refresh on what is left."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            _get_current_plan,
        ),
    ]


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            "request_restart",
            {
                "name": "request_restart",
                "description": "Ask supervisor to restart runtime (after successful push).",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                },
            },
            _request_restart,
        ),
        ToolEntry(
            "promote_to_stable",
            {
                "name": "promote_to_stable",
                "description": "Promote ouroboros -> ouroboros-stable. Call when you consider the code stable.",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                },
            },
            _promote_to_stable,
        ),
        ToolEntry(
            "schedule_task",
            {
                "name": "schedule_task",
                "description": "Schedule a background task. Returns task_id for later retrieval. For complex tasks, decompose into focused subtasks with clear scope.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "Task description — be specific about scope and expected deliverable",
                        },
                        "context": {
                            "type": "string",
                            "description": "Optional context from parent task: background info, constraints, style guide, etc.",
                        },
                        "parent_task_id": {
                            "type": "string",
                            "description": "Optional parent task ID for tracking lineage",
                        },
                    },
                    "required": ["description"],
                },
            },
            _schedule_task,
        ),
        ToolEntry(
            "cancel_task",
            {
                "name": "cancel_task",
                "description": "Cancel a task by ID.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
            _cancel_task,
        ),
        ToolEntry(
            "request_review",
            {
                "name": "request_review",
                "description": "Request a deep review of code, prompts, and state. You decide when a review is needed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Why you want a review (context for the reviewer)",
                        },
                    },
                    "required": ["reason"],
                },
            },
            _request_review,
        ),
        ToolEntry(
            "chat_history",
            {
                "name": "chat_history",
                "description": "Retrieve messages from chat history. Supports search.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "count": {
                            "type": "integer",
                            "default": 100,
                            "description": "Number of messages (from latest)",
                        },
                        "offset": {
                            "type": "integer",
                            "default": 0,
                            "description": "Skip N from end (pagination)",
                        },
                        "search": {
                            "type": "string",
                            "default": "",
                            "description": "Text filter",
                        },
                    },
                    "required": [],
                },
            },
            _chat_history,
        ),
        ToolEntry(
            "update_scratchpad",
            {
                "name": "update_scratchpad",
                "description": "Update your working memory. Write freely — any format you find useful. "
                "This persists across sessions and is read at every task start.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Full scratchpad content",
                        },
                    },
                    "required": ["content"],
                },
            },
            _update_scratchpad,
        ),
        ToolEntry(
            "send_owner_message",
            {
                "name": "send_owner_message",
                "description": "Send a proactive message to the owner. Use when you have something "
                "genuinely worth saying — an insight, a question, or an invitation to collaborate. "
                "This is NOT for task responses (those go automatically).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Message text"},
                        "reason": {
                            "type": "string",
                            "description": "Why you're reaching out (logged, not sent)",
                        },
                    },
                    "required": ["text"],
                },
            },
            _send_owner_message,
        ),
        ToolEntry(
            "toggle_evolution",
            {
                "name": "toggle_evolution",
                "description": "Enable or disable evolution mode. When enabled, Ouroboros runs continuous self-improvement cycles.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "enabled": {
                            "type": "boolean",
                            "description": "true to enable, false to disable",
                        },
                    },
                    "required": ["enabled"],
                },
            },
            _toggle_evolution,
        ),
        ToolEntry(
            "toggle_consciousness",
            {
                "name": "toggle_consciousness",
                "description": "Control background consciousness: 'start', 'stop', or 'status'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["start", "stop", "status"],
                            "description": "Action to perform",
                        },
                    },
                    "required": ["action"],
                },
            },
            _toggle_consciousness,
        ),
        ToolEntry(
            "switch_model",
            {
                "name": "switch_model",
                "description": "Switch LLM runtime settings. "
                "Use when you need more power, more context budget, or different tool behavior. "
                "Takes effect on the next round and stays active until changed again.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "model": {
                            "type": "string",
                            "description": "Model name (e.g. anthropic/claude-sonnet-4). Leave empty to keep current.",
                        },
                        "effort": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "xhigh"],
                            "description": "Reasoning effort level. Leave empty to keep current.",
                        },
                        "max_tokens": {
                            "type": "integer",
                            "description": "Max completion tokens for future LLM calls.",
                        },
                        "temperature": {
                            "type": "number",
                            "description": "Sampling temperature for future LLM calls.",
                        },
                        "tool_choice": {
                            "type": "string",
                            "description": "Tool behavior override, e.g. auto, none, required.",
                        },
                    },
                    "required": [],
                },
            },
            _switch_model,
        ),
        ToolEntry(
            "list_prompt_surfaces",
            {
                "name": "list_prompt_surfaces",
                "description": "List the formal manager prompt surfaces that count as Ouroboros's prompt stack.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            _list_prompt_surfaces,
        ),
        ToolEntry(
            "propose_prompt_patch",
            {
                "name": "propose_prompt_patch",
                "description": "Create a governed prompt rewrite proposal with diff, version ids, rollback reference, and optional human checkpoint. Use this instead of hiding prompt edits inside generic code patching.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "surface_id": {
                            "type": "string",
                            "description": "Optional prompt surface id from list_prompt_surfaces; defaults to the main system prompt.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Why the manager prompt stack needs this change.",
                        },
                        "expected_behavioral_effect": {
                            "type": "string",
                            "description": "What behavior should change after the prompt rewrite.",
                        },
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Evidence from competency gaps, failures, or human feedback.",
                        },
                        "proposed_content": {
                            "type": "string",
                            "description": "Optional full candidate content for the target surface.",
                        },
                    },
                    "required": ["rationale", "expected_behavioral_effect"],
                },
            },
            _propose_prompt_patch,
        ),
        ToolEntry(
            "get_prompt_patch_proposal",
            {
                "name": "get_prompt_patch_proposal",
                "description": "Load a previously created prompt patch proposal, including its diff and risk metadata.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "proposal_id": {
                            "type": "string",
                            "description": "Proposal id returned by propose_prompt_patch",
                        },
                    },
                    "required": ["proposal_id"],
                },
            },
            _get_prompt_patch_proposal,
        ),
        ToolEntry(
            "resolve_prompt_checkpoint",
            {
                "name": "resolve_prompt_checkpoint",
                "description": "Record the approval or rejection of a prompt rewrite human checkpoint and return resume metadata.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "checkpoint_id": {
                            "type": "string",
                            "description": "Human checkpoint id returned by propose_prompt_patch",
                        },
                        "approved": {
                            "type": "boolean",
                            "description": "Whether the prompt rewrite was approved",
                        },
                        "response": {
                            "type": "string",
                            "description": "Reviewer response or approval note",
                        },
                    },
                    "required": ["checkpoint_id", "approved"],
                },
            },
            _resolve_prompt_checkpoint,
        ),
        ToolEntry(
            "get_task_result",
            {
                "name": "get_task_result",
                "description": "Read the result of a completed subtask. Use after schedule_task to collect results.",
                "parameters": {
                    "type": "object",
                    "required": ["task_id"],
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Task ID returned by schedule_task",
                        },
                    },
                },
            },
            _get_task_result,
        ),
        ToolEntry(
            "wait_for_task",
            {
                "name": "wait_for_task",
                "description": "Check if a subtask has completed. Returns result if done, or 'still running' message. Call repeatedly to poll. Default timeout: 120s.",
                "parameters": {
                    "type": "object",
                    "required": ["task_id"],
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Task ID to check",
                        },
                    },
                },
            },
            _wait_for_task,
        ),
        *_planner_tool_entries(),
        ToolEntry(
            "run_workspace_task",
            {
                "name": "run_workspace_task",
                "description": (
                    "Delegate a task to the Umbrella workspace-first manager. "
                    "Selects the best workspace, creates an instance, runs it, "
                    "evaluates, and returns structured results. "
                    "Use this instead of solving tasks directly when a workspace class matches."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["task_input"],
                    "properties": {
                        "task_input": {
                            "type": "string",
                            "description": "Task description",
                        },
                        "workspace_id": {
                            "type": "string",
                            "description": "Optional workspace id (auto-selected if omitted)",
                        },
                        "max_iterations": {
                            "type": "integer",
                            "description": "Max manager iterations (default: 5)",
                        },
                    },
                },
            },
            _run_workspace_task,
        ),
    ]
