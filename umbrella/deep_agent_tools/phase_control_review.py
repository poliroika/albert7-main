"""Review-policy validation helpers for phase-control tools."""

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.deep_agent_tools.phase_control_base import *
from umbrella.deep_agent_tools.phase_control_research import (
    _read_file_paths_for_task,
    _research_reference_was_read,
    _tool_rows_after,
)

_PLAN_REVIEW_SUBMITTED_REL_PATH = ".memory/drive/state/phase_plan_submitted_latest.json"

def _review_revision_policy_issue(
    ctx: ToolContext,
    *,
    verdict: str = "",
    revisions: list[str] | None = None,
    notes: str = "",
    reason: str = "",
) -> str:
    text = "\n".join(
        str(item or "")
        for item in [
            *(revisions or []),
            notes,
            reason,
        ]
        if str(item or "").strip()
    )
    if not text:
        return ""
    for match in _BAD_REVIEW_FALLBACK_RE.finditer(text):
        claim = _review_policy_claim_window(text, match)
        if _review_fallback_match_is_explicitly_dangerous(claim):
            phase = _plan_review_phase_label(ctx)
            return (
                "ERROR: review feedback cannot request hardcoded/static/default "
                "fallback behavior or cached/graceful-degradation LLM replacement. "
                "Require explicit configuration, retry/pause, or surfaced errors instead"
                + (f" (phase: {phase})" if phase else "")
            )
        if (
            _review_fallback_match_is_env_alias(claim)
            or _review_fallback_match_is_protective(claim)
        ):
            continue
        phase = _plan_review_phase_label(ctx)
        return (
            "ERROR: review feedback cannot request hardcoded/static/default "
            "fallback behavior or cached/graceful-degradation LLM replacement. "
            "Require explicit configuration, retry/pause, or surfaced errors instead"
            + (f" (phase: {phase})" if phase else "")
        )
    for match in _BAD_REVIEW_LLM_TEST_DOUBLE_RE.finditer(text):
        claim = _review_policy_claim_window(text, match)
        if (
            _review_llm_test_double_match_is_protective(claim)
        ):
            continue
        phase = _plan_review_phase_label(ctx)
        return (
            "ERROR: review feedback cannot request mock/fake/dry-run LLM "
            "test doubles as a required fix for LLM/GMAS/bot behavior. Require "
            "real runtime-env e2e proof for required LLM behavior, and use "
            "non-LLM unit seams only when they cannot be mistaken for the core proof"
            + (f" (phase: {phase})" if phase else "")
        )
    for match in _BAD_REVIEW_PROVIDER_MODEL_RE.finditer(text):
        claim = _review_policy_claim_window(text, match)
        if _review_provider_model_match_is_protective(claim):
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
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if ":" in task_id:
        suffix = task_id.rsplit(":", 1)[-1]
        if suffix in {"research_review", "plan_review", "subtask_review", "final_review"}:
            return suffix
    view = _loop_state_view(ctx)
    phase = str(view.get("phase_label") or "").strip()
    if phase:
        return phase
    return task_id.split(":", 1)[1] if ":" in task_id else task_id


def _iter_latest_plan_leaves(value: Any) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []
    if isinstance(value, dict):
        child_items: list[Any] = []
        for key in ("subtasks", "steps", "phases", "tasks", "items"):
            raw = value.get(key)
            if isinstance(raw, list):
                child_items.extend(raw)
        if child_items:
            for child in child_items:
                leaves.extend(_iter_latest_plan_leaves(child))
            return leaves
        if any(
            str(value.get(key) or "").strip()
            for key in (
                "id",
                "subtask_id",
                "title",
                "goal",
                "description",
                "success_test",
                "verification_command",
            )
        ):
            leaves.append(value)
    elif isinstance(value, list):
        for item in value:
            leaves.extend(_iter_latest_plan_leaves(item))
    return leaves


def _latest_plan_has_executable_leaves(ctx: ToolContext) -> tuple[bool, str]:
    payload = _submitted_or_latest_phase_plan_payload(ctx)
    plan = payload.get("plan") if isinstance(payload, dict) else None
    if not isinstance(plan, dict):
        return False, ""
    leaves = _iter_latest_plan_leaves(plan)
    executable = [
        item for item in leaves if _subtask_success_test_text(item).strip()
    ]
    if not executable:
        return False, json.dumps(plan, ensure_ascii=False)
    return True, json.dumps(plan, ensure_ascii=False).lower()


def _plan_review_feedback_has_plan_owner(review_text: str, plan_text: str) -> bool:
    if not plan_text:
        return False
    review_lc = str(review_text or "").lower()
    for group in _PLAN_REVIEW_DETAIL_TOPIC_GROUPS:
        if any(term in review_lc for term in group) and any(
            term in plan_text for term in group
        ):
            return True
    review_terms = {
        term
        for term in re.findall(r"[a-z0-9_]{4,}", review_lc)
        if term
        not in {
            "add",
            "also",
            "with",
            "that",
            "this",
            "must",
            "should",
            "phase",
            "subtask",
            "specify",
            "clarify",
            "document",
            "handling",
            "strategy",
            "tests",
            "test",
        }
    }
    if not review_terms:
        return False
    overlap = [term for term in review_terms if term in plan_text]
    return len(overlap) >= min(3, max(2, len(review_terms) // 2))


def _plan_review_hard_blocker_match_is_protective(
    review_text: str, match: re.Match[str]
) -> bool:
    """Avoid treating positive policy summaries as true revise blockers."""
    matched = str(match.group(0) or "").lower()
    if not re.search(
        r"\b(?:hardcoded|mock|fake|dry[-\s]?run|fallback|fall[-\s]?back)\b",
        matched,
    ):
        return False
    claim = _review_policy_claim_window(review_text, match)
    return (
        _review_fallback_match_is_env_alias(claim)
        or _review_fallback_match_is_protective(claim)
        or _review_llm_test_double_match_is_protective(claim)
        or bool(_PLAN_REVIEW_PROTECTIVE_FALLBACK_DETAIL_RE.search(claim))
    )


def _plan_review_has_nonprotective_hard_blocker(review_text: str) -> bool:
    for match in _PLAN_REVIEW_HARD_BLOCKING_REVISE_RE.finditer(review_text):
        if _plan_review_hard_blocker_match_is_protective(review_text, match):
            continue
        return True
    return False


def _plan_review_validation_issue(
    ctx: ToolContext,
    *,
    verdict: str,
    revisions: list[str] | None = None,
    notes: str = "",
) -> str:
    if _plan_review_phase_label(ctx) != "plan_review":
        return ""
    if str(verdict or "").strip().lower() != "revise":
        return ""
    review_text = "\n".join(
        str(item or "").strip()
        for item in [*(revisions or []), notes]
        if str(item or "").strip()
    )
    if not review_text:
        return ""
    has_executable_plan, plan_text = _latest_plan_has_executable_leaves(ctx)
    if has_executable_plan and _PLAN_REVIEW_BAD_SUCCESS_TEST_RE.search(review_text):
        return (
            "ERROR: plan_review revise cannot loop an executable plan back to "
            "replace checked-in pytest/verification success tests with `python -c` "
            "checks, or call tests created by the same subtask circular. Keep "
            "those as execution/subtask_review notes unless a concrete existing "
            "success_test is unsafe, missing, or non-automatable."
        )
    if _plan_review_has_nonprotective_hard_blocker(review_text):
        return ""
    if not _PLAN_REVIEW_IMPLEMENTATION_DETAIL_RE.search(review_text):
        return ""
    if not has_executable_plan:
        return ""
    if not _plan_review_feedback_has_plan_owner(review_text, plan_text):
        return ""
    return (
        "ERROR: plan_review revise is reserved for blocking plan defects that "
        "make execution unsafe, impossible, or unverifiable. The latest plan "
        "already has executable subtasks covering this area, so topology, "
        "reconnection, retry/backoff, exact dependency, docs-example, and other "
        "implementation-owned details must be submitted as verdict=ok notes for "
        "execute/subtask_review instead of looping plan again."
    )


def _plan_review_ok_artifact_issue(ctx: ToolContext, *, verdict: str) -> str:
    if _plan_review_phase_label(ctx) != "plan_review":
        return ""
    if str(verdict or "").strip().lower() != "ok":
        return ""
    payload = _submitted_phase_plan_payload(ctx)
    if not payload:
        return (
            "ERROR: plan_review ok requires the submitted plan artifact "
            f"{_PLAN_REVIEW_SUBMITTED_REL_PATH}. The review must approve the "
            "plan selected by submit_phase_plan, not a later unsubmitted "
            "proposal."
        )
    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)):
        created_at = None
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
        f"{_PLAN_REVIEW_SUBMITTED_REL_PATH} in this review phase after "
        "submit_phase_plan selects the executable plan. Review must verify the "
        "submitted handoff from memory before accepting it."
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
        from umbrella.deep_agent_tools.phase_contract_tools import (
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
        "ERROR: plan_review ok cannot accept the submitted phase plan artifact "
        "because it violates workspace policy: "
        + "; ".join(issues)
        + ". Loop back to plan with concrete revisions instead of allowing "
        "execute to start from an unsafe plan."
    )


__all__ = [
    '_review_revision_policy_issue',
    '_micro_review_feedback_issue',
    '_plan_review_phase_label',
    '_iter_latest_plan_leaves',
    '_latest_plan_has_executable_leaves',
    '_plan_review_feedback_has_plan_owner',
    '_plan_review_validation_issue',
    '_plan_review_ok_artifact_issue',
    '_plan_review_ok_policy_issue',
]
