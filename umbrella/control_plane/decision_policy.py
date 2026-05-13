"""
Decision policy - workspace-first routing logic.

Implements the core decision logic:
- Default: improve workspace
- Self-improvement: only with evidence
- Escalation: for blocked cases
"""

import logging
from typing import Any

from umbrella.control_plane.models import (
    DecisionContext,
    DecisionRecord,
    DecisionRationale,
    NextAction,
    ActionType,
    PatchTarget,
    TaskBrief,
    TaskClass,
    ManagerPhase,
    ManagerState,
)
from umbrella.control_plane.models import generate_decision_id
from umbrella.control_plane.task_bridge import to_workspace_task_brief

log = logging.getLogger(__name__)


def _normalize_workspace_id(candidate: Any) -> str | None:
    """Return a safe workspace id string or ``None``."""
    if isinstance(candidate, str):
        value = candidate.strip()
        if value:
            return value
    return None


# =============================================================================
# Task Classification
# =============================================================================


def classify_task(task_input: str, task_id: str) -> TaskBrief:
    """Classify an incoming task into a structured brief.

    Args:
        task_input: Raw task description from user
        task_id: Unique task identifier

    Returns:
        Structured task brief
    """
    # Simple keyword-based classification
    # In production, this would use LLM-based classification

    task_input_lower = task_input.lower()

    # Determine task class (more specific patterns first)
    task_class = TaskClass.UNKNOWN
    # Forecast detection - check first as it's most specific
    if any(
        word in task_input_lower
        for word in [
            "forecast",
            "predict",
            "predict whether",
            "prediction",
            "will happen",
            "will be",
            "future",
            "trend",
        ]
    ):
        task_class = TaskClass.FORECAST
    elif any(
        word in task_input_lower
        for word in ["implement from", "code from", "from article", "from paper"]
    ):
        task_class = TaskClass.CODE_FROM_ARTICLE
    elif any(word in task_input_lower for word in ["code", "implement"]):
        task_class = TaskClass.CODE_FROM_ARTICLE
    elif any(
        word in task_input_lower for word in ["design", "architecture", "system design"]
    ):
        task_class = TaskClass.SYSTEM_DESIGN
    elif any(
        word in task_input_lower for word in ["data", "pipeline", "etl", "process"]
    ):
        task_class = TaskClass.DATA_PROCESSING
    elif any(word in task_input_lower for word in ["test", "evaluat", "benchmark"]):
        task_class = TaskClass.EVALUATION
    elif any(
        word in task_input_lower
        for word in ["research", "article", "paper", "write", "investigate"]
    ):
        task_class = TaskClass.RESEARCH

    # Create summary (first sentence or first 100 chars)
    lines = task_input.strip().split("\n")
    first_line = lines[0] if lines else task_input
    summary = first_line[:200] + ("..." if len(first_line) > 200 else "")

    return TaskBrief(
        task_id=task_id,
        original_input=task_input,
        task_class=task_class,
        summary=summary,
        requirements=[],  # Would extract from task_input
        constraints=[],
        success_criteria=[],
    )


def select_seed_workspace(task_brief: TaskBrief, registry: Any) -> str:
    """Select an appropriate seed workspace for the task.

    Args:
        task_brief: Classified task brief
        registry: Workspace registry (from umbrella.workspace_registry)

    Returns:
        Workspace ID to use
    """
    if registry is not None:
        try:
            selection_brief = to_workspace_task_brief(task_brief)
            best = registry.select_best(selection_brief)
            if best is not None:
                workspace_id = _normalize_workspace_id(
                    getattr(best, "workspace_id", None)
                )
                if workspace_id is not None:
                    return workspace_id
        except Exception as exc:
            log.debug(
                "Registry-based workspace selection failed, using fallback: %s", exc
            )

    task_to_workspace = {
        TaskClass.RESEARCH: "agent_research",
        TaskClass.CODE_FROM_ARTICLE: "agent_research",
        TaskClass.SYSTEM_DESIGN: "agent_research",
        TaskClass.DATA_PROCESSING: "agent_research",
        TaskClass.EVALUATION: "evaluation",
        TaskClass.FORECAST: "world_prediction",
        TaskClass.UNKNOWN: "agent_research",
    }

    return task_to_workspace.get(task_brief.task_class, "agent_research")


def build_decision_context(
    task_brief: TaskBrief,
    manager_state: ManagerState,
    workspace_id: str | None = None,
    last_run_result: dict[str, Any] | None = None,
    memory_store: Any = None,
    policy_engine: Any = None,
) -> DecisionContext:
    """Build the decision context for making a choice.

    Args:
        task_brief: Current task brief
        manager_state: Current manager state
        workspace_id: Workspace being used (if any)
        last_run_result: Results from last workspace run
        memory_store: Memory store for lessons/gaps
        policy_engine: Policy engine for constraint checking

    Returns:
        Rich decision context
    """
    normalized_workspace_id = _normalize_workspace_id(workspace_id)

    context = DecisionContext(
        task_id=task_brief.task_id,
        task_brief=task_brief,
        manager_state=manager_state,
        workspace_id=normalized_workspace_id,
    )

    # Add run results if provided
    if last_run_result:
        context.last_run_outcome = last_run_result.get("status", "unknown")
        context.run_manifest = last_run_result.get("manifest", {})
        context.artifact_summary = last_run_result.get("artifact_summary")
        context.error_signatures = last_run_result.get("error_signatures", [])
        context.prompt_gap_signals = last_run_result.get("prompt_gap_signals", [])
        context.human_feedback_signals = last_run_result.get(
            "human_feedback_signals", []
        )
        context.cross_workspace_failures = int(
            last_run_result.get("cross_workspace_failures", 0)
        )
        context.retrieval_failures = int(last_run_result.get("retrieval_failures", 0))

    # Add memory context if store provided
    if memory_store:
        stats = memory_store.get_stats()
        context.relevant_lessons = stats.total_lessons
        context.active_gaps = stats.active_gaps

        from umbrella.memory.models import MemoryQuery

        try:
            recent_lessons = memory_store.query_lessons(
                MemoryQuery(
                    task_id=task_brief.task_id,
                    max_results=5,
                )
            )
            context.relevant_lesson_summaries = [
                f"[{lesson.lesson_type}] {lesson.change_summary}: {lesson.conclusion}"
                for lesson in recent_lessons
            ]
        except Exception:
            pass

        try:
            gaps = memory_store.get_active_gaps()
            context.active_gap_descriptions = [
                f"{gap.gap_type}: {gap.description}" for gap in gaps[:5]
            ]
        except Exception:
            pass

    # Add policy constraints
    if policy_engine:
        # Would check policy for what's allowed
        context.workspace_changes_allowed = True
        context.self_improvement_allowed = False  # Default: not allowed
        context.gm_changes_allowed = False

    context.escalation_count = manager_state.escalation_count
    return context


# =============================================================================
# Decision Functions
# =============================================================================


def decide_next_action(context: DecisionContext) -> NextAction:
    """Decide what to do next based on current context.

    This is the core decision policy implementing workspace-first logic.

    Args:
        context: Current decision context

    Returns:
        The next action to take
    """
    phase = context.manager_state.phase

    # Route based on current phase
    if phase == ManagerPhase.INSPECTION_COMPLETE:
        return _decide_after_inspection(context)

    elif phase == ManagerPhase.DECISION_MADE:
        # Re-evaluate based on last decision
        return _decide_continuation(context)

    elif phase == ManagerPhase.PATCH_PROPOSED:
        return NextAction(
            action_type=ActionType.PATCH_WORKSPACE,
            description="Execute proposed workspace patch",
        )

    elif phase == ManagerPhase.SELF_IMPROVEMENT_PENDING:
        return NextAction(
            action_type=ActionType.WAIT_FOR_INPUT,
            description="Awaiting self-improvement approval",
        )

    elif phase == ManagerPhase.ESCALATED:
        return NextAction(
            action_type=ActionType.WAIT_FOR_INPUT,
            description="Awaiting human resolution",
        )

    # Default based on phase
    return _default_action_for_phase(phase)


def _decide_after_inspection(context: DecisionContext) -> NextAction:
    """Decide next action after inspecting workspace run results.

    This is where the workspace-first policy is enforced.
    A run that completed but produced low-quality output is treated as
    "partial" so the manager keeps iterating instead of stopping early.
    """
    quality_threshold = context.quality_completion_threshold
    outcome = context.last_run_outcome
    eval_score = context.last_eval_score
    completion_gate_passed = context.completion_gate_passed

    # Downgrade "success" only when the explicit completion gate rejects the run
    # or when no explicit gate result exists and eval quality is below threshold.
    if outcome == "success":
        if completion_gate_passed is False:
            log.info(
                "Decision policy: completion gate rejected the latest run; treating as partial",
            )
            outcome = "partial"
        elif (
            completion_gate_passed is None
            and eval_score is not None
            and eval_score < quality_threshold
        ):
            log.info(
                "Decision policy: run succeeded but eval score %.2f < %.2f; treating as partial",
                eval_score,
                quality_threshold,
            )
            outcome = "partial"

    # Success -> record lesson and complete
    if outcome == "success":
        return NextAction(
            action_type=ActionType.RECORD_LESSON,
            description="Task succeeded, recording lesson",
        )

    # Failure -> decide what to fix
    elif outcome == "failure":
        return _decide_after_failure(context)

    # Partial -> decide if worth iterating
    elif outcome == "partial":
        if context.no_progress_iterations >= 3:
            return _consider_manager_intervention(context)
        else:
            description = "Improve workspace configuration"
            if eval_score is not None:
                description = f"Improve workspace (eval score {eval_score:.2f} below {quality_threshold})"
            return NextAction(
                action_type=ActionType.PATCH_WORKSPACE,
                patch_target=PatchTarget.WORKSPACE_CONFIG,
                description=description,
            )

    # Unknown/first run -> just re-run
    else:
        return NextAction(
            action_type=ActionType.RUN_WORKSPACE,
            description="Initial workspace run",
        )


def _decide_after_failure(context: DecisionContext) -> NextAction:
    """Decide what to do after a workspace failure.

    Workspace-first: always try to fix the workspace first.
    Uses retrieval guidance and lesson history to make informed decisions.
    """
    # Build evidence-informed description
    retrieval_hint = ""
    if context.retrieval_recommended_pattern:
        retrieval_hint = (
            f" (retrieval suggests: {context.retrieval_recommended_pattern[:80]})"
        )

    lesson_hint = ""
    avoid_patterns = [
        s for s in context.relevant_lesson_summaries if "failure" in s.lower()
    ]
    if avoid_patterns:
        lesson_hint = f" Lessons warn: {avoid_patterns[0][:60]}"

    # Check if this looks like a workspace-level issue
    if _is_workspace_level_issue(context):
        error_desc = (
            context.error_signatures[0] if context.error_signatures else "Unknown"
        )
        return NextAction(
            action_type=ActionType.PATCH_WORKSPACE,
            patch_target=_determine_workspace_patch_target(context),
            description=f"Fix workspace issue: {error_desc}{retrieval_hint}",
            metadata={
                "retrieval_pattern": context.retrieval_recommended_pattern,
                "retrieval_key_files": context.retrieval_key_files,
                "lesson_warnings": lesson_hint,
            },
        )

    # Check for repeated manager-level failures
    elif _is_manager_level_issue(context):
        # Still try workspace patch first unless clear manager gap
        if context.total_iterations < 5:
            return NextAction(
                action_type=ActionType.PATCH_WORKSPACE,
                patch_target=PatchTarget.WORKSPACE_GRAPH,
                description="Try alternative workspace configuration",
            )
        else:
            return _consider_manager_intervention(context)

    # Default: try workspace patch
    return NextAction(
        action_type=ActionType.PATCH_WORKSPACE,
        description="Fix workspace and retry",
    )


def _consider_manager_intervention(context: DecisionContext) -> NextAction:
    """Consider if manager-level intervention is needed.

    This is the gateway to self-improvement or escalation.
    """
    # Check for self-improvement eligibility
    if _check_self_improvement_signals(context):
        evidence = _collect_self_improvement_evidence(context)
        return NextAction(
            action_type=_select_manager_intervention_action(evidence),
            description="Manager-level intervention may be needed",
            self_improvement_type="prompt_stack_rewrite"
            if evidence.get("prompt_issues")
            else "general",
            metadata={"evidence": evidence.get("signals", [])},
        )

    # Otherwise escalate to human
    return NextAction(
        action_type=ActionType.ESCALATE_TO_HUMAN,
        description="Unable to make progress, escalating to human",
    )


def _decide_continuation(context: DecisionContext) -> NextAction:
    """Decide how to continue after a decision was made."""
    last_decision = context.manager_state.last_decision

    if last_decision.action_type == ActionType.PATCH_WORKSPACE:
        # Workspace patch was decided, go to knowledge retrieval for re-run
        return NextAction(
            action_type=ActionType.RECORD_LESSON,
            description="Record lesson from patch and re-run",
        )

    elif last_decision.action_type == ActionType.SELF_IMPROVE:
        # Self-improvement was decided
        return NextAction(
            action_type=ActionType.SELF_IMPROVE,
            description="Execute self-improvement plan",
        )

    elif last_decision.action_type == ActionType.ESCALATE_TO_HUMAN:
        return NextAction(
            action_type=ActionType.WAIT_FOR_INPUT,
            description="Waiting for human input",
        )

    return NextAction(
        action_type=ActionType.RUN_WORKSPACE,
        description="Continue with workspace execution",
    )


def _default_action_for_phase(phase: ManagerPhase) -> NextAction:
    """Get default action for a given phase."""
    actions = {
        ManagerPhase.TASK_RECEIVED: NextAction(
            action_type=ActionType.RUN_WORKSPACE,
            description="Select workspace for task",
        ),
        ManagerPhase.WORKSPACE_SELECTED: NextAction(
            action_type=ActionType.PATCH_WORKSPACE,
            description="Prepare task instance",
        ),
        ManagerPhase.INSTANCE_PREPARED: NextAction(
            action_type=ActionType.RUN_WORKSPACE,
            description="Retrieve knowledge and prepare to run",
        ),
    }

    return actions.get(
        phase,
        NextAction(action_type=ActionType.WAIT_FOR_INPUT, description="Awaiting input"),
    )


# =============================================================================
# Helper Functions
# =============================================================================


def should_patch_workspace(context: DecisionContext) -> DecisionRecord:
    """Determine if workspace should be patched.

    Args:
        context: Decision context

    Returns:
        Decision record with rationale
    """
    # Default: workspace should be patched
    is_workspace_issue = _is_workspace_level_issue(context)

    return DecisionRecord(
        id=generate_decision_id(),
        task_id=context.task_id,
        context_snapshot=context,
        action=NextAction(
            action_type=ActionType.PATCH_WORKSPACE,
            patch_target=_determine_workspace_patch_target(context),
            description="Workspace-level issue detected"
            if is_workspace_issue
            else "Workspace iteration needed",
        ),
        rationale=DecisionRationale(
            action_taken=ActionType.PATCH_WORKSPACE,
            reason="Workspace-level issue detected"
            if is_workspace_issue
            else "Workspace iteration needed",
            evidence=context.error_signatures,
            confidence=0.8 if is_workspace_issue else 0.6,
            alternatives_considered=[
                ActionType.SELF_IMPROVE,
                ActionType.ESCALATE_TO_HUMAN,
            ],
            why_not_alternatives={
                "self_improve": "No clear manager-level gap detected yet",
                "escalate": "Workspace options not exhausted",
            },
        ),
    )


def should_patch_manager(context: DecisionContext) -> DecisionRecord:
    """Determine if manager should be patched (self-improvement).

    Args:
        context: Decision context

    Returns:
        Decision record with rationale
    """
    has_manager_gap = _check_self_improvement_signals(context)
    evidence = _collect_self_improvement_evidence(context)
    action_type = _select_manager_intervention_action(evidence)

    return DecisionRecord(
        id=generate_decision_id(),
        task_id=context.task_id,
        context_snapshot=context,
        action=NextAction(
            action_type=action_type,
            self_improvement_type="prompt_stack_rewrite"
            if evidence.get("prompt_issues")
            else "general",
            description="Rewrite the manager prompt stack"
            if action_type == ActionType.REWRITE_PROMPT_STACK
            else "Trigger manager self-improvement",
            metadata={"evidence": evidence.get("signals", [])},
        ),
        rationale=DecisionRationale(
            action_taken=action_type,
            reason="Manager-level gap detected"
            if has_manager_gap
            else "Evaluating manager improvement",
            evidence=evidence.get("signals", []),
            confidence=0.7 if has_manager_gap else 0.3,
            alternatives_considered=[
                ActionType.PATCH_WORKSPACE,
                ActionType.SELF_IMPROVE
                if action_type == ActionType.REWRITE_PROMPT_STACK
                else ActionType.REWRITE_PROMPT_STACK,
                ActionType.ESCALATE_TO_HUMAN,
            ],
            why_not_alternatives={
                "patch_workspace": "Multiple workspace iterations have failed",
                "rewrite_prompt_stack": "No prompt-surface evidence yet",
                "self_improve": "Prompt stack appears to be the narrower fix",
                "escalate": "Can be resolved internally",
            },
        ),
        requires_approval=True,  # Self-improvement always needs approval
    )


def should_escalate(context: DecisionContext, reason: str = "") -> DecisionRecord:
    """Determine if case should be escalated to human.

    Args:
        context: Decision context
        reason: Why escalation is being considered

    Returns:
        Decision record with rationale
    """
    # Check if this is a blocking case
    is_blocking = _is_blocking_case(context)

    return DecisionRecord(
        id=generate_decision_id(),
        task_id=context.task_id,
        context_snapshot=context,
        action=NextAction(
            action_type=ActionType.ESCALATE_TO_HUMAN,
            description=reason or "Blocking constraint encountered",
        ),
        rationale=DecisionRationale(
            action_taken=ActionType.ESCALATE_TO_HUMAN,
            reason=reason or "Blocking constraint encountered",
            evidence=[],
            confidence=0.9 if is_blocking else 0.5,
            alternatives_considered=[
                ActionType.PATCH_WORKSPACE,
                ActionType.SELF_IMPROVE,
            ],
            why_not_alternatives={
                "patch_workspace": "Would violate policy constraints",
                "self_improve": "Requires human approval anyway",
            },
        ),
        requires_approval=True,
    )


# =============================================================================
# Internal Helpers
# =============================================================================


def _is_workspace_level_issue(context: DecisionContext) -> bool:
    """Check if the issue is workspace-level."""
    # Workspace-level issues:
    # - Specific errors in workspace code
    # - Workspace configuration issues
    # - Tool failures specific to workspace

    if not context.error_signatures:
        return True  # Default to workspace

    # Check for manager-level indicators
    manager_indicators = ["retrieval", "policy", "framework", "gm_api"]
    for sig in context.error_signatures:
        sig_lower = sig.lower()
        if any(ind in sig_lower for ind in manager_indicators):
            return False

    return True


def _is_manager_level_issue(context: DecisionContext) -> bool:
    """Check if the issue is manager-level."""
    # Manager-level indicators:
    # - Repeated retrieval failures
    # - Policy violations
    # - Multiple workspaces failing similarly
    # - Prompt/strategy issues

    return (
        context.repeated_failures >= 3
        or context.cross_workspace_failures >= 2
        or context.retrieval_failures >= 3
        or context.active_gaps >= 2
        or context.no_progress_iterations >= 5
        or bool(context.prompt_gap_signals)
        or bool(context.human_feedback_signals)
    )


def _is_blocking_case(context: DecisionContext) -> bool:
    """Check if this is a blocking case that requires escalation."""
    # Blocking cases:
    # - Would modify GMAS
    # - Would modify seed workspace without approval
    # - Policy would be violated
    # - Safety concerns

    if context.gm_changes_allowed:
        return True

    # High iteration count with no progress
    if context.no_progress_iterations >= 10:
        return True

    # Many escalations already
    if context.escalation_count >= 3:
        return True

    return False


def _determine_workspace_patch_target(context: DecisionContext) -> PatchTarget:
    """Determine what part of workspace to patch."""
    # Simple heuristic based on error signatures
    errors = " ".join(context.error_signatures).lower()

    if "graph" in errors or "topology" in errors:
        return PatchTarget.WORKSPACE_GRAPH
    elif "agent" in errors:
        return PatchTarget.WORKSPACE_AGENTS
    elif "prompt" in errors:
        return PatchTarget.WORKSPACE_PROMPTS
    elif "tool" in errors:
        return PatchTarget.WORKSPACE_TOOLS
    else:
        return PatchTarget.WORKSPACE_CONFIG


def _check_self_improvement_signals(context: DecisionContext) -> bool:
    """Check if there are signals suggesting self-improvement is needed.

    Triggers earlier than before so the manager can evolve (like ouroboros)
    rather than spinning on workspace patches.
    """
    has_gap_signal = (
        context.active_gaps > 0
        or context.repeated_failures >= 2
        or context.cross_workspace_failures >= 1
        or context.retrieval_failures >= 2
        or bool(context.prompt_gap_signals)
        or bool(context.human_feedback_signals)
    )
    return context.no_progress_iterations >= 2 and has_gap_signal


def _select_manager_intervention_action(evidence: dict[str, Any]) -> ActionType:
    """Choose the narrowest manager intervention that fits the evidence."""
    if evidence.get("prompt_issues"):
        return ActionType.REWRITE_PROMPT_STACK
    return ActionType.SELF_IMPROVE


def _collect_self_improvement_evidence(context: DecisionContext) -> dict[str, Any]:
    """Collect evidence for self-improvement decision."""
    evidence = {
        "signals": [],
        "prompt_issues": False,
        "retrieval_issues": False,
    }

    if context.no_progress_iterations >= 3:
        evidence["signals"].append(
            f"{context.no_progress_iterations} iterations with no progress"
        )

    if context.active_gaps > 0:
        evidence["signals"].append(f"{context.active_gaps} active competency gaps")

    if context.cross_workspace_failures > 0:
        evidence["signals"].append(
            f"{context.cross_workspace_failures} cross-workspace failures suggest a manager-level gap"
        )

    if context.retrieval_failures > 0:
        evidence["signals"].append(
            f"{context.retrieval_failures} retrieval failures detected"
        )
        evidence["retrieval_issues"] = True

    prompt_signals = list(context.prompt_gap_signals) + list(
        context.human_feedback_signals
    )
    if prompt_signals:
        evidence["signals"].extend(prompt_signals)
        evidence["prompt_issues"] = True

    for sig in context.error_signatures:
        sig_lower = sig.lower()
        if "retrieval" in sig_lower:
            evidence["retrieval_issues"] = True
            evidence["signals"].append("Retrieval failures detected")
        if any(
            keyword in sig_lower
            for keyword in ("prompt", "instruction", "policy", "context")
        ):
            evidence["prompt_issues"] = True
            evidence["signals"].append(f"Prompt-surface issue detected: {sig}")

    return evidence
