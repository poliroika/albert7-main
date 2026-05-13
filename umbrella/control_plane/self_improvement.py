"""
Self-improvement gate and execution.

Manages the gated path for manager self-improvement:
- Checks eligibility based on competency ledger
- Prepares checkpoints
- Executes self-improvement
- Resumes from checkpoint
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from umbrella.control_plane.models import (
    DecisionContext,
    ActionResult,
    ManagerState,
    NextAction,
    ActionType,
)
from umbrella.control_plane.models import generate_checkpoint_id

log = logging.getLogger(__name__)


# =============================================================================
# Self-Improvement Gate
# =============================================================================


@dataclass
class SelfImprovementGate:
    """Gate for self-improvement eligibility.

    Thresholds are kept low so the manager can self-improve early
    (like ouroboros evolution cycles) rather than spinning on workspace
    patches that yield no quality gain.
    """

    # Evidence thresholds -- intentionally low so self-improvement can
    # trigger after just a couple of stale iterations.
    min_iterations_without_progress: int = 2
    min_active_gaps: int = 0  # gaps are helpful but not required
    min_repeated_failures: int = 2

    # Cost thresholds -- removed as a hard gate; cost is tracked for
    # observability but should not prevent improvement.
    max_cost_without_progress_usd: float = 0.0
    max_iterations_total: int = 50

    # Gate configuration
    allow_retrieval_based: bool = True
    allow_human_feedback_based: bool = True

    def is_eligible(self, context: DecisionContext) -> tuple[bool, str]:
        """Check if self-improvement is eligible.

        Returns:
            (is_eligible, reason)
        """
        reasons = []

        if context.no_progress_iterations < self.min_iterations_without_progress:
            reasons.append(
                f"Only {context.no_progress_iterations} iterations without progress "
                f"(need {self.min_iterations_without_progress})"
            )

        if context.total_iterations >= self.max_iterations_total:
            reasons.append(
                f"Total iterations ({context.total_iterations}) at limit "
                f"({self.max_iterations_total}); consider escalation"
            )

        is_eligible = len(reasons) == 0

        if is_eligible:
            reason = "Eligible: evidence thresholds met"
        else:
            reason = "Not eligible: " + "; ".join(reasons)

        return is_eligible, reason


# =============================================================================
# Checkpoint and Resume
# =============================================================================


@dataclass
class ManagerCheckpoint:
    """Checkpoint for task resumption after self-improvement."""

    id: str
    task_id: str
    created_at: float
    manager_state: ManagerState

    # Task state to restore
    task_context: dict[str, Any] = field(default_factory=dict)

    # What triggered the checkpoint
    trigger_reason: str = ""
    trigger_decision_id: str = ""

    # Self-improvement info
    self_improvement_type: str = ""
    self_improvement_plan: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict."""
        return {
            "id": self.id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "manager_state": self.manager_state.model_dump(mode="json"),
            "task_context": self.task_context,
            "trigger_reason": self.trigger_reason,
            "trigger_decision_id": self.trigger_decision_id,
            "self_improvement_type": self.self_improvement_type,
            "self_improvement_plan": self.self_improvement_plan,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ManagerCheckpoint":
        """Restore from dict."""
        from umbrella.control_plane.models import ManagerState

        return cls(
            id=data["id"],
            task_id=data["task_id"],
            created_at=data["created_at"],
            manager_state=ManagerState(**data["manager_state"]),
            task_context=data.get("task_context", {}),
            trigger_reason=data.get("trigger_reason", ""),
            trigger_decision_id=data.get("trigger_decision_id", ""),
            self_improvement_type=data.get("self_improvement_type", ""),
            self_improvement_plan=data.get("self_improvement_plan", ""),
        )

    def save(self, checkpoint_dir: Path) -> Path:
        """Save checkpoint to file."""
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        path = checkpoint_dir / f"{self.id}.json"

        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

        log.info(f"Saved checkpoint {self.id} to {path}")
        return path


# =============================================================================
# Public API
# =============================================================================


def check_self_improvement_eligibility(
    context: DecisionContext,
    gate: SelfImprovementGate | None = None,
) -> tuple[bool, str, list[str]]:
    """Check if self-improvement is eligible based on evidence.

    Args:
        context: Decision context
        gate: Optional custom gate configuration

    Returns:
        (is_eligible, reason, suggestions)
    """
    gate = gate or SelfImprovementGate()

    is_eligible, reason = gate.is_eligible(context)

    suggestions = []
    if not is_eligible:
        if context.no_progress_iterations < gate.min_iterations_without_progress:
            suggestions.append("Continue workspace iterations to gather more evidence")
        if context.active_gaps < gate.min_active_gaps:
            suggestions.append("Record more lessons to build competency gap evidence")

    return is_eligible, reason, suggestions


def prepare_self_improvement(
    context: DecisionContext,
    improvement_type: str,
    plan: str,
    checkpoint_dir: Path,
) -> ManagerCheckpoint:
    """Prepare for self-improvement by creating a checkpoint.

    Args:
        context: Decision context
        improvement_type: Type of improvement (e.g., "prompt_optimization")
        plan: What we plan to improve
        checkpoint_dir: Where to save checkpoints

    Returns:
        Created checkpoint
    """
    checkpoint = ManagerCheckpoint(
        id=generate_checkpoint_id(),
        task_id=context.task_id,
        created_at=time.time(),
        manager_state=context.manager_state,
        task_context={"brief": context.task_brief.model_dump(mode="json")},
        trigger_reason=f"Self-improvement: {improvement_type}",
        self_improvement_type=improvement_type,
        self_improvement_plan=plan,
    )

    path = checkpoint.save(checkpoint_dir)
    log.info(f"Prepared checkpoint {checkpoint.id} at {path}")

    return checkpoint


def _try_delegate_to_ouroboros(
    plan: str,
    checkpoint: ManagerCheckpoint,
    repo_root: Path,
) -> ActionResult | None:
    """Attempt to delegate a self-improvement task to ouroboros.

    When Umbrella lacks competency for a specific improvement, it can delegate
    the work to the ouroboros agent (which has richer tool access, web search,
    code generation, etc.) without going through the GMAS retrieval pipeline.

    This version actually uses Ouroboros for code updates when appropriate.

    Returns None if ouroboros is not available.
    """
    # Try real Ouroboros integration first
    try:
        from umbrella.control_plane.ouroboros_integration import (
            run_ouroboros_improvement_sync,
            create_ouroboros_self_improvement_task,
        )

    except ImportError:
        pass

    if (
        "update code" in plan.lower()
        or "fix bug" in plan.lower()
        or "improve code" in plan.lower()
    ):
        try:
            result = run_ouroboros_improvement_sync(
                repo_root=repo_root,
                task_description=plan,
                workspace_id=checkpoint.task_id.split("_")[0]
                if "_" in checkpoint.task_id
                else "agent_research",
                use_live_llm=True,
            )
            if result.get("status") == "complete":
                return ActionResult(
                    action=NextAction(
                        action_type=ActionType.SELF_IMPROVE,
                        self_improvement_type="ouroboros_code_update",
                        description="Self-improvement via Ouroboros code update",
                    ),
                    outcome="success",
                    summary=f"Ouroboros made {len(result.get('changes_made', []))} changes",
                    details={
                        "task_id": result.get("task_id"),
                        "final_message": result.get("final_message", "")[:500],
                    },
                )
        except Exception as e:
            log.warning("Real Ouroboros delegation failed: %s", e)

    try:
        result = create_ouroboros_self_improvement_task(
            repo_root=repo_root,
            issue_description=plan,
            context=f"Self-improvement checkpoint {checkpoint.id}; original task {checkpoint.task_id}",
            workspace_id=checkpoint.task_id.split("_")[0]
            if "_" in checkpoint.task_id
            else "agent_research",
        )
        return ActionResult(
            action=NextAction(
                action_type=ActionType.SELF_IMPROVE,
                self_improvement_type="ouroboros_queued",
                description="Self-improvement queued for Ouroboros",
            ),
            outcome="partial",
            summary=f"Queued Ouroboros self-improvement task: {result.get('status', 'unknown')}",
            details={
                "delegated_task_id": result.get("task_id"),
                "delegated_status": result.get("status"),
            },
        )
    except Exception as e:
        log.warning("Ouroboros self-improvement queue failed: %s", e)
        return None


def execute_self_improvement(
    checkpoint: ManagerCheckpoint,
    improvement_type: str,
    plan: str,
    *,
    repo_root: Path | None = None,
    prompt_versions_dir: Path | None = None,
    prompt_proposals_dir: Path | None = None,
) -> ActionResult:
    """Execute a self-improvement action.

    Supported concrete paths:
    - prompt_stack_rewrite: creates and persists a prompt policy annotation
    - retrieval_config: persists a retrieval tuning note
    - general: attempts delegation, then falls back to annotation
    """
    log.info(f"Executing self-improvement: {improvement_type}")
    log.info(f"Plan: {plan}")

    if improvement_type == "prompt_stack_rewrite" and repo_root is not None:
        versions_dir = prompt_versions_dir or (
            repo_root / ".umbrella" / "control_plane" / "prompt_versions"
        )
        versions_dir.mkdir(parents=True, exist_ok=True)
        annotation_path = versions_dir / f"self_improve_note_{checkpoint.id}.md"
        annotation_path.write_text(
            f"# Self-Improvement Note\n\n"
            f"- **Type**: {improvement_type}\n"
            f"- **Plan**: {plan}\n"
            f"- **Task**: {checkpoint.task_id}\n"
            f"- **Checkpoint**: {checkpoint.id}\n\n"
            f"This note records a manager-level self-improvement decision.\n"
            f"The prompt governance pipeline should review and apply concrete changes.\n",
            encoding="utf-8",
        )
        log.info(f"Persisted self-improvement annotation at {annotation_path}")
        return ActionResult(
            action=NextAction(
                action_type=ActionType.SELF_IMPROVE,
                self_improvement_type=improvement_type,
                description=f"Self-improvement: {improvement_type}",
            ),
            outcome="success",
            summary=f"Prompt-stack self-improvement annotation persisted at {annotation_path.name}",
            details={
                "improvement_type": improvement_type,
                "plan": plan,
                "annotation_path": str(annotation_path),
            },
        )

    if improvement_type == "retrieval_config" and repo_root is not None:
        config_dir = repo_root / ".umbrella" / "retrieval"
        config_dir.mkdir(parents=True, exist_ok=True)
        note_path = config_dir / f"tuning_note_{checkpoint.id}.md"
        note_path.write_text(
            f"# Retrieval Tuning Note\n\n"
            f"- **Plan**: {plan}\n"
            f"- **Task**: {checkpoint.task_id}\n\n"
            f"Apply these retrieval configuration changes on next index build.\n",
            encoding="utf-8",
        )
        return ActionResult(
            action=NextAction(
                action_type=ActionType.SELF_IMPROVE,
                self_improvement_type=improvement_type,
                description=f"Self-improvement: {improvement_type}",
            ),
            outcome="success",
            summary=f"Retrieval tuning note persisted at {note_path.name}",
            details={
                "improvement_type": improvement_type,
                "plan": plan,
                "note_path": str(note_path),
            },
        )

    if improvement_type == "workspace_code_update" and repo_root is not None:
        try:
            from umbrella.control_plane.ouroboros_integration import (
                run_ouroboros_improvement_sync,
            )

            result = run_ouroboros_improvement_sync(
                repo_root=repo_root,
                task_description=plan,
                workspace_id=checkpoint.task_id.split("_")[0]
                if "_" in checkpoint.task_id
                else "agent_research",
                use_live_llm=True,
            )
            if result.get("status") == "complete":
                return ActionResult(
                    action=NextAction(
                        action_type=ActionType.SELF_IMPROVE,
                        self_improvement_type="workspace_code_update",
                        description="Workspace code update via Ouroboros",
                    ),
                    outcome="success",
                    summary=f"Ouroboros code update completed: {result.get('final_message', '')[:100]}",
                    details={
                        "improvement_type": improvement_type,
                        "plan": plan,
                        "task_id": result.get("task_id"),
                        "changes_count": len(result.get("changes_made", [])),
                    },
                )
            else:
                return ActionResult(
                    action=NextAction(
                        action_type=ActionType.SELF_IMPROVE,
                        self_improvement_type="workspace_code_update",
                        description="Workspace code update failed",
                    ),
                    outcome="failure",
                    summary=f"Ouroboros code update failed: {result.get('error', 'unknown')}",
                )
        except ImportError:
            # Ouroboros not available, fall back to annotation
            notes_dir = repo_root / ".umbrella" / "self_improvement"
            notes_dir.mkdir(parents=True, exist_ok=True)
            note_path = notes_dir / f"code_update_note_{checkpoint.id}.md"
            note_path.write_text(
                f"# Workspace Code Update Note\n\n"
                f"- **Plan**: {plan}\n"
                f"- **Task**: {checkpoint.task_id}\n\n"
                f"Ouroboros was not available to apply this update directly.\n"
                f"Manual code update may be required.\n",
                encoding="utf-8",
            )
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.SELF_IMPROVE,
                    self_improvement_type=improvement_type,
                    description=f"Self-improvement: {improvement_type}",
                ),
                outcome="partial",
                summary="Code update note persisted (Ouroboros unavailable)",
                details={
                    "improvement_type": improvement_type,
                    "plan": plan,
                    "note_path": str(note_path),
                },
            )

    # For "general" improvement type: try delegation first, then annotate
    if repo_root is not None:
        delegated = _try_delegate_to_ouroboros(plan, checkpoint, repo_root)
        if delegated is not None:
            return delegated

        # Fallback: persist an annotation so the improvement is at least recorded
        notes_dir = repo_root / ".umbrella" / "self_improvement"
        notes_dir.mkdir(parents=True, exist_ok=True)
        note_path = notes_dir / f"improvement_note_{checkpoint.id}.md"
        note_path.write_text(
            f"# Self-Improvement Note\n\n"
            f"- **Type**: {improvement_type}\n"
            f"- **Plan**: {plan}\n"
            f"- **Task**: {checkpoint.task_id}\n\n",
            encoding="utf-8",
        )
        return ActionResult(
            action=NextAction(
                action_type=ActionType.SELF_IMPROVE,
                self_improvement_type=improvement_type,
                description=f"Self-improvement: {improvement_type}",
            ),
            outcome="success",
            summary="Self-improvement note persisted (delegation unavailable)",
            details={
                "improvement_type": improvement_type,
                "plan": plan,
                "note_path": str(note_path),
            },
        )

    return ActionResult(
        action=NextAction(
            action_type=ActionType.SELF_IMPROVE,
            self_improvement_type=improvement_type,
            description=f"Self-improvement: {improvement_type}",
        ),
        outcome="failure",
        summary=f"Self-improvement type '{improvement_type}' has no repo_root configured.",
        details={
            "improvement_type": improvement_type,
            "plan": plan,
        },
    )


def resume_from_checkpoint(
    checkpoint_id: str,
    checkpoint_dir: Path,
) -> ManagerCheckpoint:
    """Resume a task from a checkpoint after self-improvement.

    Args:
        checkpoint_id: ID of checkpoint to resume
        checkpoint_dir: Directory containing checkpoints

    Returns:
        Loaded checkpoint

    Raises:
        FileNotFoundError: If checkpoint not found
    """
    path = checkpoint_dir / f"{checkpoint_id}.json"

    if not path.exists():
        raise FileNotFoundError(f"Checkpoint {checkpoint_id} not found at {path}")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    checkpoint = ManagerCheckpoint.from_dict(data)

    log.info(f"Resumed from checkpoint {checkpoint_id}")
    return checkpoint
