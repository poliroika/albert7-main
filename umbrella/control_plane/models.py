"""
Control plane data models.

Defines the state machine, decision types, and action types
for the manager control plane.
"""

import time
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# =============================================================================
# State Machine
# =============================================================================


class ManagerPhase(StrEnum):
    """Phases of manager task processing."""

    # Initial phase
    TASK_RECEIVED = "task_received"

    # Workspace selection
    WORKSPACE_SELECTED = "workspace_selected"
    INSTANCE_PREPARED = "instance_prepared"

    # Execution
    KNOWLEDGE_RETRIEVED = "knowledge_retrieved"
    WORKSPACE_RUNNING = "workspace_running"
    RUN_COMPLETE = "run_complete"

    # Analysis
    INSPECTION_COMPLETE = "inspection_complete"
    DECISION_MADE = "decision_made"

    # Actions
    PATCH_PROPOSED = "patch_proposed"
    PATCH_APPLIED = "patch_applied"
    LESSON_RECORDED = "lesson_recorded"

    # Promotion path
    PROMOTION_CONSIDERATION = "promotion_consideration"

    # Self-improvement path
    SELF_IMPROVEMENT_PENDING = "self_improvement_pending"
    SELF_IMPROVEMENT_APPROVED = "self_improvement_approved"
    SELF_IMPROVEMENT_COMPLETE = "self_improvement_complete"

    # Escalation
    ESCALATED = "escalated"
    ESCALATION_RESOLVED = "escalation_resolved"

    # Terminal
    TASK_COMPLETE = "task_complete"
    TASK_FAILED = "task_failed"
    TASK_BLOCKED = "task_blocked"


class ManagerState(BaseModel):
    """Current state of the manager for a task."""

    task_id: str
    phase: ManagerPhase = ManagerPhase.TASK_RECEIVED
    current_workspace_id: str | None = None
    current_instance_path: Path | None = None
    iteration_count: int = 0
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    # Context carried across phases
    task_brief: Optional["TaskBrief"] = None
    selected_seed: str | None = None
    last_run_id: str | None = None
    last_decision: Optional["NextAction"] = None

    # Self-improvement tracking
    self_improvement_attempts: int = 0
    max_self_improvement_attempts: int = 3
    active_self_improvement_checkpoint_id: str | None = None
    pending_human_checkpoint_id: str | None = None
    last_prompt_proposal_id: str | None = None

    # Escalation tracking
    escalation_count: int = 0
    pending_escalations: list[str] = Field(default_factory=list)

    # Evaluation tracking
    last_comparison: Any | None = None  # ComparisonReport from evals
    baseline_eval_id: str | None = None
    promotion_candidate: Any | None = None  # PromotionCandidate from evals

    # Retrieval and patch traceability
    retrieval_query: str | None = None
    retrieval_summary: str | None = None
    retrieval_key_files: list[str] = Field(default_factory=list)
    retrieval_hit_count: int = 0
    last_patch_files: list[str] = Field(default_factory=list)
    last_patch_summary: str | None = None
    runtime_update_count: int = 0
    latest_runtime_update: str | None = None

    def touch(self) -> None:
        """Update the updated_at timestamp."""
        self.updated_at = time.time()

    @property
    def age_seconds(self) -> float:
        """Age of the state in seconds."""
        return time.time() - self.created_at

    def transition_to(self, new_phase: ManagerPhase) -> None:
        """Transition to a new phase."""
        self.phase = new_phase
        self.touch()


# =============================================================================
# Task and Context
# =============================================================================


class TaskClass(StrEnum):
    """High-level task classifications."""

    RESEARCH = "research"  # Investigation, writing articles
    CODE_FROM_ARTICLE = "code_from_article"  # Implementing a paper
    SYSTEM_DESIGN = "system_design"  # Designing multi-agent systems
    DATA_PROCESSING = "data_processing"  # ETL, analysis pipelines
    EVALUATION = "evaluation"  # Testing and benchmarking
    FORECAST = "forecast"  # Predictions, world modeling
    UNKNOWN = "unknown"


class TaskBrief(BaseModel):
    """Structured task brief after classification."""

    task_id: str
    original_input: str
    task_class: TaskClass
    summary: str  # One-line summary
    requirements: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)

    # Estimated difficulty and cost
    estimated_iterations: int = 5
    estimated_cost_usd: float = 0.0

    created_at: float = Field(default_factory=time.time)


class ManagerTask(BaseModel):
    """A task assigned to the manager."""

    id: str
    brief: TaskBrief
    status: Literal["pending", "active", "complete", "failed", "blocked"] = "pending"
    state: ManagerState = Field(default_factory=lambda: ManagerState(task_id=""))
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    # Results
    final_artifact_path: Path | None = None
    final_summary: str | None = None


class DecisionContext(BaseModel):
    """Context available when making a decision."""

    task_id: str
    task_brief: TaskBrief
    manager_state: ManagerState

    # Workspace info
    workspace_id: str | None = None
    workspace_type: str | None = None
    instance_path: Path | None = None

    # Run results
    last_run_outcome: Literal["success", "failure", "partial", "unknown"] = "unknown"
    run_manifest: dict[str, Any] = Field(default_factory=dict)
    artifact_summary: str | None = None
    error_signatures: list[str] = Field(default_factory=list)

    # Retrieval context (content, not just counts)
    retrieval_recommended_pattern: str = ""
    retrieval_key_symbols: list[str] = Field(default_factory=list)
    retrieval_key_files: list[str] = Field(default_factory=list)
    retrieval_anti_patterns: list[str] = Field(default_factory=list)
    retrieval_confidence: float = 0.0

    # Memory context (lesson bodies, not just counts)
    relevant_lessons: int = 0
    relevant_lesson_summaries: list[str] = Field(default_factory=list)
    active_gaps: int = 0
    active_gap_descriptions: list[str] = Field(default_factory=list)
    repeated_failures: int = 0
    cross_workspace_failures: int = 0
    retrieval_failures: int = 0
    prompt_gap_signals: list[str] = Field(default_factory=list)
    human_feedback_signals: list[str] = Field(default_factory=list)

    # Policy constraints
    workspace_changes_allowed: bool = True
    self_improvement_allowed: bool = False
    gm_changes_allowed: bool = False
    escalation_count: int = 0

    # Quality tracking
    last_eval_score: float | None = None
    quality_completion_threshold: float = 0.80
    completion_gate_passed: bool | None = None

    # Iteration tracking
    total_iterations: int = 0
    no_progress_iterations: int = 0
    cost_so_far_usd: float = 0.0


# =============================================================================
# Decisions
# =============================================================================


class ActionType(StrEnum):
    """Types of actions the manager can take."""

    # Workspace actions (default path)
    UPDATE_TASK_SCOPE = "update_task_scope"
    RUN_WORKSPACE = "run_workspace"
    PATCH_WORKSPACE = "patch_workspace"
    UPDATE_WORKSPACE_CODE = "update_workspace_code"
    RECORD_LESSON = "record_lesson"
    PROMOTE_LESSON = "promote_lesson"
    RERUN_WORKSPACE = "rerun_workspace"

    # Manager actions (gated)
    SELF_IMPROVE = "self_improve"
    REWRITE_PROMPT_STACK = "rewrite_prompt_stack"
    MODIFY_POLICY = "modify_policy"

    # Escalation
    ESCALATE_TO_HUMAN = "escalate_to_human"

    # Control flow
    WAIT_FOR_INPUT = "wait_for_input"
    COMPLETE_TASK = "complete_task"
    FAIL_TASK = "fail_task"


class PatchTarget(StrEnum):
    """What to patch."""

    WORKSPACE_GRAPH = "workspace_graph"
    WORKSPACE_AGENTS = "workspace_agents"
    WORKSPACE_PROMPTS = "workspace_prompts"
    WORKSPACE_TOOLS = "workspace_tools"
    WORKSPACE_CONFIG = "workspace_config"

    MANAGER_PROMPT = "manager_prompt"
    MANAGER_POLICY = "manager_policy"
    MANAGER_CODE = "manager_code"


class DecisionRationale(BaseModel):
    """Human-readable explanation of a decision."""

    action_taken: ActionType
    reason: str  # Why this action?
    evidence: list[str] = Field(default_factory=list)  # What supports this?
    confidence: float = 1.0  # 0.0 to 1.0

    # Alternatives considered
    alternatives_considered: list[ActionType] = Field(default_factory=list)
    why_not_alternatives: dict[str, str] = Field(default_factory=dict)


class DecisionRecord(BaseModel):
    """A record of a control-plane decision."""

    id: str
    task_id: str
    context_snapshot: DecisionContext
    action: "NextAction"
    rationale: DecisionRationale

    created_at: float = Field(default_factory=time.time)

    # Approval if needed
    requires_approval: bool = False
    approved: bool = False
    approved_by: Literal["human", "auto", "policy"] = "auto"
    approved_at: float | None = None


class NextAction(BaseModel):
    """The next action to take."""

    action_type: ActionType
    patch_target: PatchTarget | None = None
    description: str
    estimated_duration_seconds: float | None = None

    # For workspace patches
    workspace_changes: dict[str, Any] = Field(default_factory=dict)

    # For self-improvement
    self_improvement_type: str | None = None
    checkpoint_id: str | None = None
    prompt_proposal_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# Prompt Stack Governance
# =============================================================================


class PromptSurfaceKind(StrEnum):
    """Kinds of manager-side prompt surfaces."""

    SYSTEM_PROMPT = "system_prompt"
    CONSTITUTION = "constitution"
    CONTEXT_ASSEMBLY = "context_assembly"
    POLICY_FRAGMENT = "policy_fragment"
    HUMAN_GATE_POLICY = "human_gate_policy"


class PromptRiskLevel(StrEnum):
    """Risk classes for prompt rewrites."""

    SAFE_LOCAL_TUNING = "safe_local_tuning"
    MEDIUM_POLICY_CHANGE = "medium_policy_change"
    HIGH_FOUNDATIONAL_CHANGE = "high_foundational_change"


class HumanCheckpointStatus(StrEnum):
    """Lifecycle for human checkpoint requests."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PromptSurface(BaseModel):
    """A file or rule-set that belongs to the manager prompt stack."""

    id: str
    path: Path
    kind: PromptSurfaceKind
    label: str
    description: str
    foundational: bool = False
    human_checkpoint_required: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PromptPatchProposal(BaseModel):
    """A reviewable proposal for changing a prompt surface."""

    id: str
    task_id: str
    surface: PromptSurface
    rationale: str
    expected_behavioral_effect: str
    rollback_reference: str | None = None
    evidence: list[str] = Field(default_factory=list)
    proposed_content: str | None = None
    diff_text: str = ""
    base_version_id: str | None = None
    candidate_version_id: str | None = None
    risk_level: PromptRiskLevel = PromptRiskLevel.MEDIUM_POLICY_CHANGE
    requires_human_checkpoint: bool = False
    touches_human_gate_policy: bool = False
    changed_lines: int = 0
    created_at: float = Field(default_factory=time.time)


class PromptVersionRecord(BaseModel):
    """Audit record for a prompt-surface snapshot."""

    id: str
    task_id: str | None = None
    surface_id: str
    surface_path: Path
    content_hash: str
    snapshot_path: Path
    label: str
    created_at: float = Field(default_factory=time.time)


class HumanCheckpointRequest(BaseModel):
    """A human approval request that reuses the existing owner-contact path."""

    id: str
    task_id: str
    checkpoint_type: str
    description: str
    status: HumanCheckpointStatus = HumanCheckpointStatus.PENDING
    proposal_id: str | None = None
    manager_checkpoint_id: str | None = None
    notification_channel: str = "send_owner_message"
    notification_message: str | None = None
    reuse_existing_human_contact: bool = True
    created_at: float = Field(default_factory=time.time)
    resolved_at: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HumanCheckpointDecision(BaseModel):
    """Decision taken by a human reviewer."""

    checkpoint_id: str
    approved: bool
    response: str
    decided_by: Literal["human", "auto", "policy"] = "human"
    decided_at: float = Field(default_factory=time.time)


class CheckpointResumeResult(BaseModel):
    """Result of resuming a task after a human checkpoint."""

    checkpoint_id: str
    task_id: str
    resumed: bool
    next_phase: ManagerPhase | None = None
    resume_reason: str = ""
    manager_checkpoint_id: str | None = None
    human_decision: HumanCheckpointDecision | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# Escalation
# =============================================================================


class EscalationReason(StrEnum):
    """Reasons for escalating to a human."""

    FRAMEWORK_CHANGE = "framework_change"  # Modifying GMAS
    SEED_PROMOTION = "seed_promotion"  # Promoting to seed workspace
    MISSION_CHANGE = "mission_change"  # Changing TASK_MAIN.md
    STRATEGIC_SHIFT = "strategic_shift"  # Major direction change
    POLICY_VIOLATION = "policy_violation"  # Would violate policy
    SAFETY_CONCERN = "safety_concern"  # Potential safety issue
    BLOCKED_BY_DECISION = "blocked_by_decision"  # No good path forward
    HUMAN_REQUESTED = "human_requested"  # Human asked to intervene
    UNKNOWN = "unknown"


class EscalationStatus(StrEnum):
    """Status of an escalation."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class EscalationRecord(BaseModel):
    """A record of escalation to a human."""

    id: str
    task_id: str
    reason: EscalationReason
    description: str
    status: EscalationStatus = EscalationStatus.PENDING

    # What triggered this
    triggering_action: ActionType | None = None
    decision_id: str | None = None

    # Context
    current_phase: ManagerPhase
    workspace_id: str | None = None

    # Resolution
    human_response: str | None = None
    resolved_at: float | None = None

    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


# =============================================================================
# Results
# =============================================================================


class ActionResult(BaseModel):
    """Result of executing an action."""

    action: NextAction
    outcome: Literal["success", "failure", "partial", "cancelled"]
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)

    # Artifacts produced
    artifact_paths: list[Path] = Field(default_factory=list)

    # Next steps suggested
    suggested_next_actions: list[ActionType] = Field(default_factory=list)

    executed_at: float = Field(default_factory=time.time)
    duration_seconds: float = 0.0


class ExecutionOutcome(BaseModel):
    """Final outcome of a manager task."""

    task_id: str
    status: Literal["complete", "failed", "blocked"]
    final_state: ManagerPhase
    summary: str

    # Metrics
    total_iterations: int = 0
    total_duration_seconds: float = 0.0
    total_cost_usd: float = 0.0

    # Results
    final_artifact_path: Path | None = None
    lessons_learned: int = 0
    self_improvements_made: int = 0
    escalations: int = 0

    completed_at: float = Field(default_factory=time.time)


# =============================================================================
# Factory helpers
# =============================================================================


def generate_decision_id() -> str:
    """Generate a unique decision ID."""
    import uuid

    return f"decision_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def generate_escalation_id() -> str:
    """Generate a unique escalation ID."""
    import uuid

    return f"escalation_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def generate_checkpoint_id() -> str:
    """Generate a unique checkpoint ID for self-improvement."""
    import uuid

    return f"checkpoint_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def generate_prompt_proposal_id() -> str:
    """Generate a unique prompt patch proposal ID."""
    import uuid

    return f"prompt_patch_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def generate_prompt_version_id() -> str:
    """Generate a unique prompt version record ID."""
    import uuid

    return f"prompt_version_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def generate_human_checkpoint_id() -> str:
    """Generate a unique human checkpoint request ID."""
    import uuid

    return f"human_checkpoint_{int(time.time())}_{uuid.uuid4().hex[:8]}"
