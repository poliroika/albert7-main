"""
Umbrella Control Plane - Manager decision-making and orchestration.

The control plane is the "brain" that decides:
- Choose a workspace for the task
- Run the workspace and inspect results
- Decide: patch workspace vs self-improvement vs escalate
- Gate self-improvement behind evidence requirements
- Maintain traceability of all decisions

Core principle: Workspace-first, self-improvement only when proven necessary.
"""

from umbrella.control_plane.models import (
    # States
    ManagerState,
    ManagerPhase,
    # Task and context
    ManagerTask,
    TaskBrief,
    TaskClass,
    # Decisions
    DecisionContext,
    DecisionRecord,
    DecisionRationale,
    NextAction,
    ActionType,
    PatchTarget,
    PromptSurface,
    PromptSurfaceKind,
    PromptPatchProposal,
    PromptVersionRecord,
    PromptRiskLevel,
    HumanCheckpointRequest,
    HumanCheckpointDecision,
    HumanCheckpointStatus,
    CheckpointResumeResult,
    # Escalation
    EscalationRecord,
    EscalationReason,
    EscalationStatus,
    # Results
    ActionResult,
    ExecutionOutcome,
)

from umbrella.control_plane.state import (
    ManagerStateMachine,
    StateTransition,
    transition_to,
)

from umbrella.control_plane.decision_policy import (
    decide_next_action,
    should_patch_workspace,
    should_patch_manager,
    should_escalate,
    classify_task,
    select_seed_workspace,
    build_decision_context,
)

from umbrella.control_plane.self_improvement import (
    SelfImprovementGate,
    check_self_improvement_eligibility,
    prepare_self_improvement,
    execute_self_improvement,
    resume_from_checkpoint,
)

from umbrella.control_plane.prompt_policy import (
    identify_prompt_surfaces,
    get_prompt_surface,
    propose_prompt_patch,
    classify_prompt_risk,
    requires_human_checkpoint,
    save_prompt_patch_proposal,
    load_prompt_patch_proposal,
    apply_prompt_patch,
)

from umbrella.control_plane.prompt_versioning import (
    PromptVersionStore,
    record_prompt_version,
    load_prompt_version,
)

from umbrella.control_plane.prompt_diff import (
    render_prompt_diff,
)

from umbrella.control_plane.human_checkpoints import (
    create_human_checkpoint_request,
    load_human_checkpoint_request,
    record_human_checkpoint_decision,
    resume_after_human_checkpoint,
)

from umbrella.control_plane.escalation import (
    HumanEscalation,
    escalate_to_human,
    check_blocking_constraints,
    get_pending_escalations,
)

from umbrella.control_plane.tracing import (
    DecisionTrace,
    trace_decision,
    get_decision_history,
    explain_decision,
)

from umbrella.control_plane.engine import (
    ControlPlaneEngine,
    create_engine,
)

__all__ = [
    # Models
    "ManagerState",
    "ManagerPhase",
    "ManagerTask",
    "TaskBrief",
    "TaskClass",
    "DecisionContext",
    "DecisionRecord",
    "DecisionRationale",
    "NextAction",
    "ActionType",
    "PatchTarget",
    "PromptSurface",
    "PromptSurfaceKind",
    "PromptPatchProposal",
    "PromptVersionRecord",
    "PromptRiskLevel",
    "HumanCheckpointRequest",
    "HumanCheckpointDecision",
    "HumanCheckpointStatus",
    "CheckpointResumeResult",
    "EscalationRecord",
    "EscalationReason",
    "EscalationStatus",
    "ActionResult",
    "ExecutionOutcome",
    # State
    "ManagerStateMachine",
    "StateTransition",
    "transition_to",
    # Decision Policy
    "decide_next_action",
    "should_patch_workspace",
    "should_patch_manager",
    "should_escalate",
    "classify_task",
    "select_seed_workspace",
    "build_decision_context",
    # Self-Improvement
    "SelfImprovementGate",
    "check_self_improvement_eligibility",
    "prepare_self_improvement",
    "execute_self_improvement",
    "resume_from_checkpoint",
    # Prompt Governance
    "identify_prompt_surfaces",
    "get_prompt_surface",
    "propose_prompt_patch",
    "classify_prompt_risk",
    "requires_human_checkpoint",
    "save_prompt_patch_proposal",
    "load_prompt_patch_proposal",
    "apply_prompt_patch",
    "PromptVersionStore",
    "record_prompt_version",
    "load_prompt_version",
    "render_prompt_diff",
    "create_human_checkpoint_request",
    "load_human_checkpoint_request",
    "record_human_checkpoint_decision",
    "resume_after_human_checkpoint",
    # Escalation
    "HumanEscalation",
    "escalate_to_human",
    "check_blocking_constraints",
    "get_pending_escalations",
    # Tracing
    "DecisionTrace",
    "trace_decision",
    "get_decision_history",
    "explain_decision",
    # Engine
    "ControlPlaneEngine",
    "create_engine",
]
