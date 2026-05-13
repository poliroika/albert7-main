"""
Control Plane Engine - main orchestrator for the manager.

This is the brain that:
1. Receives tasks
2. Classifies and selects workspaces
3. Creates/resumes task instances with TASK_MAIN.md
4. Optionally preloads GMAS knowledge or leaves it to workspace skill tools
5. Runs workspaces through the runtime
6. Inspects results (manifest → summary → tail)
7. Decides: patch workspace vs record lesson vs self-improve vs escalate
8. Maintains traceability of all decisions
"""

import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from umbrella.control_plane.models import (
    ManagerTask,
    TaskBrief,
    ManagerState,
    ManagerPhase,
    DecisionContext,
    DecisionRecord,
    DecisionRationale,
    ActionResult,
    NextAction,
    ActionType,
    EscalationReason,
    HumanCheckpointStatus,
    PromptPatchProposal,
    PromptSurface,
    generate_decision_id,
)
from umbrella.control_plane.state import ManagerStateMachine
from umbrella.control_plane.task_bridge import to_workspace_task_brief
from umbrella.control_plane.workspace_patching import (
    apply_workspace_patch,
    WorkspacePatchResult,
)
from umbrella.control_plane.workspace_code_update import (
    apply_code_update_to_seed,
    update_seed_workspace_from_instance,
)

# Workspace integration
from umbrella.workspace_registry.models import SeedWorkspaceProfile, WorkspaceRef
from umbrella.workspace_registry import WorkspaceRegistry
from umbrella.workspace_registry.discovery import load_task_instance_profile
from umbrella.workspace_runtime import (
    inspect_run,
    load_task_main_document,
    prepare_instance,
    run_workspace,
)
from umbrella.workspace_runtime.models import (
    WorkspaceInstance,
    WorkspaceRunResult,
    WorkspaceRunRequest,
    WorkspaceRunStatus,
)
from umbrella.workspace_runtime.instances import (
    create_task_instance,
    snapshot_instance,
)

# Retrieval integration
from umbrella.retrieval.service import RetrievalService
from umbrella.retrieval.models import RetrievalCard

# Memory integration
from umbrella.memory.store import MemoryStore
from umbrella.memory.models import (
    MemoryConfig,
    WorkspaceLessonRecord,
    ManagerLessonRecord,
    generate_lesson_id,
)

# Decision policy
from umbrella.control_plane.decision_policy import (
    classify_task,
    select_seed_workspace,
    build_decision_context,
    decide_next_action,
    should_patch_workspace,
    should_patch_manager,
)

# Self-improvement
from umbrella.control_plane.self_improvement import (
    SelfImprovementGate,
    check_self_improvement_eligibility,
    prepare_self_improvement,
)

# Prompt governance
from umbrella.control_plane.prompt_policy import (
    get_prompt_surface,
    load_prompt_patch_proposal,
    propose_prompt_patch,
    save_prompt_patch_proposal,
)
from umbrella.control_plane.human_checkpoints import (
    create_human_checkpoint_request,
    load_human_checkpoint_request,
    record_human_checkpoint_decision,
    resume_after_human_checkpoint,
)
from umbrella.control_plane.task_updates import (
    RuntimeTaskUpdate,
    consume_runtime_task_updates,
)

# Escalation
from umbrella.control_plane.escalation import (
    HumanEscalation,
    escalate_to_human,
    check_blocking_constraints,
)

# Tracing
from umbrella.control_plane.tracing import (
    trace_decision,
    TraceManager,
)

# Evaluations
from umbrella.evals import (
    evaluate_run,
    compare_runs,
    build_promotion_candidate,
    decide_promotion,
    create_default_policy,
    EvaluationRecord,
    ComparisonReport,
)
from umbrella.evals.models import (
    TaskSuccessRating,
    OutputQualityRating,
    StabilityRating,
    PromotionEligibility,
)

# Telemetry
from umbrella.telemetry import (
    WorkspaceSelectedEvent,
    RunStartedEvent,
    RunCompletedEvent,
    PatchProposedEvent,
    PatchAppliedEvent,
    WorkspaceCodeUpdatedEvent,
    EvalCompletedEvent,
    PromotionCandidateCreatedEvent,
    PromotionDecisionEvent,
    emit_event,
    get_metrics_registry,
)

log = logging.getLogger(__name__)


# =============================================================================
# Human Checkpoint Hooks
# =============================================================================


class HumanCheckpoint:
    """A human approval checkpoint for risky operations.

    Created when the manager wants to perform a high-risk action that
    requires explicit human approval before proceeding.
    """

    def __init__(
        self,
        checkpoint_id: str,
        task_id: str,
        checkpoint_type: Literal[
            "prompt_stack_rewrite",
            "seed_promotion",
            "task_main_change",
            "strategic_shift",
            "gmas_boundary_cross",
            "final_completion_review",
        ],
        description: str,
        proposed_change: dict[str, Any],
        checkpoint_dir: Path,
    ):
        self.id = checkpoint_id
        self.task_id = task_id
        self.checkpoint_type = checkpoint_type
        self.description = description
        self.proposed_change = proposed_change
        self.checkpoint_dir = checkpoint_dir
        self.created_at = time.time()
        self.status = "pending"  # pending, approved, rejected
        self.human_response: str | None = None
        self.resolved_at: float | None = None

    def approve(self, response: str) -> None:
        """Approve the checkpoint."""
        self.status = "approved"
        self.human_response = response
        self.resolved_at = time.time()
        self._save()

    def reject(self, response: str) -> None:
        """Reject the checkpoint."""
        self.status = "rejected"
        self.human_response = response
        self.resolved_at = time.time()
        self._save()

    def _save(self) -> None:
        """Save checkpoint to disk."""
        import json

        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        path = self.checkpoint_dir / f"{self.id}.json"

        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "id": self.id,
                    "task_id": self.task_id,
                    "checkpoint_type": self.checkpoint_type,
                    "description": self.description,
                    "proposed_change": self.proposed_change,
                    "created_at": self.created_at,
                    "status": self.status,
                    "human_response": self.human_response,
                    "resolved_at": self.resolved_at,
                },
                f,
                indent=2,
            )

    @classmethod
    def load(
        cls, checkpoint_id: str, checkpoint_dir: Path
    ) -> Optional["HumanCheckpoint"]:
        """Load a checkpoint from disk."""
        import json

        path = checkpoint_dir / f"{checkpoint_id}.json"
        if not path.exists():
            return None

        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        checkpoint = cls.__new__(cls)
        checkpoint.id = data["id"]
        checkpoint.task_id = data["task_id"]
        checkpoint.checkpoint_type = data["checkpoint_type"]
        checkpoint.description = data["description"]
        checkpoint.proposed_change = data["proposed_change"]
        checkpoint.created_at = data["created_at"]
        checkpoint.status = data["status"]
        checkpoint.human_response = data.get("human_response")
        checkpoint.resolved_at = data.get("resolved_at")
        checkpoint.checkpoint_dir = checkpoint_dir
        return checkpoint


# =============================================================================
# Layered Log Inspection
# =============================================================================


class LayeredInspector:
    """Performs layered inspection of workspace runs.

    Layers:
    1. Run manifest (status, errors, warnings)
    2. Artifacts summary
    3. Log summary (if available)
    4. Error signatures
    5. Raw log tail (only as fallback)
    """

    def __init__(self, workspace_runtime_root: Path):
        self.workspace_runtime_root = workspace_runtime_root

    def inspect_run_result(
        self,
        result: WorkspaceRunResult,
        instance: WorkspaceInstance,
    ) -> dict[str, Any]:
        """Perform layered inspection of a run result.

        Returns:
            Inspection data with layers:
            - manifest: run status, errors, warnings, tokens, duration
            - artifacts: summary of produced artifacts
            - log_summary: aggregated log info (if available)
            - error_signatures: extracted error patterns
            - raw_tail: raw log tail (only if needed)
        """
        inspection = inspect_run(result, instance)

        # Layer 1: Run manifest
        manifest = {
            "run_id": result.run_id,
            "workspace_id": result.workspace_id,
            "task_id": result.task_id,
            "status": result.status.value,
            "errors": result.errors or [],
            "warnings": result.warnings or [],
            "total_tokens": result.total_tokens,
            "duration_seconds": result.duration_seconds,
            "final_answer": result.final_answer,
        }

        # Layer 2: Artifacts summary
        artifacts = {
            "count": len(inspection.artifact_paths)
            if hasattr(inspection, "artifact_paths")
            else 0,
            "paths": list(getattr(inspection, "artifact_paths", [])),
            "final_answer": result.final_answer,
            "output_dir": str(instance.path / "output")
            if (instance.path / "output").exists()
            else None,
        }

        # Layer 3: Log summary (try to get from workspace logs)
        log_summary = self._extract_log_summary(instance, result)

        # Layer 4: Error signatures
        error_signatures = self._extract_error_signatures(result, instance)

        # Layer 5: Raw log tail (only if needed - lazy loaded)
        raw_tail = None
        if result.status == WorkspaceRunStatus.FAILED or error_signatures:
            raw_tail = self._tail_logs(instance, lines=50)

        return {
            "manifest": manifest,
            "artifacts": artifacts,
            "log_summary": log_summary,
            "error_signatures": error_signatures,
            "raw_tail": raw_tail,
            "inspection": inspection,
        }

    def _extract_log_summary(
        self,
        instance: WorkspaceInstance,
        result: WorkspaceRunResult,
    ) -> dict[str, Any]:
        """Extract log summary from workspace logs."""
        log_path = instance.path / "output" / "run.log"
        if not log_path.exists():
            log_path = instance.path / "run.log"

        if not log_path.exists():
            return {"status": "no_logs"}

        try:
            content = log_path.read_text(encoding="utf-8", errors="ignore")
            lines = content.strip().split("\n")

            # Count by log level
            error_count = sum(
                1 for line in lines if "ERROR" in line.upper() or "ERROR:" in line
            )
            warning_count = sum(
                1 for line in lines if "WARNING" in line.upper() or "WARNING:" in line
            )

            # Get last few lines as summary
            tail_lines = lines[-10:] if len(lines) > 10 else lines

            return {
                "status": "available",
                "total_lines": len(lines),
                "error_count": error_count,
                "warning_count": warning_count,
                "tail": tail_lines,
            }
        except Exception as e:
            log.warning(f"Failed to extract log summary: {e}")
            return {"status": "error", "error": str(e)}

    def _extract_error_signatures(
        self,
        result: WorkspaceRunResult,
        instance: WorkspaceInstance,
    ) -> list[str]:
        """Extract error signatures from run result."""
        signatures = []

        # From result errors
        if result.errors:
            signatures.extend(result.errors)

        # Common patterns
        error_patterns = [
            "FileNotFoundError",
            "ImportError",
            "ModuleNotFoundError",
            "KeyError",
            "ValueError",
            "AttributeError",
            "timeout",
            "connection",
            "authentication",
        ]

        # Check in result for these patterns
        result_str = str(result.final_answer) if result.final_answer else ""
        for pattern in error_patterns:
            if pattern.lower() in result_str.lower():
                signatures.append(f"{pattern}_detected")

        return list(set(signatures))

    def _tail_logs(
        self,
        instance: WorkspaceInstance,
        lines: int = 50,
    ) -> list[str]:
        """Get tail of logs."""
        log_path = instance.path / "output" / "run.log"
        if not log_path.exists():
            log_path = instance.path / "run.log"

        if not log_path.exists():
            return []

        try:
            content = log_path.read_text(encoding="utf-8", errors="ignore")
            all_lines = content.strip().split("\n")
            return all_lines[-lines:] if len(all_lines) > lines else all_lines
        except Exception:
            return []


# =============================================================================
# Control Plane Engine
# =============================================================================


class ControlPlaneEngine:
    """Main control plane orchestrator.

    This is the primary interface for the manager control plane.
    It orchestrates all the components:
    - State machine for progress tracking
    - Workspace runtime for execution
    - Optional retrieval for GMAS knowledge
    - Memory for lessons and competency tracking
    - Decision policy for action selection
    - Self-improvement gate
    - Escalation management
    - Decision tracing
    - Human checkpoints for risky operations
    """

    def __init__(
        self,
        workspace_registry: WorkspaceRegistry | None = None,
        repo_root: Path | None = None,
        memory_store: MemoryStore | None = None,
        retrieval_service: RetrievalService | None = None,
        policy_engine: Any | None = None,
        workspaces_root: Path | None = None,
        control_state_dir: Path | None = None,
        use_live_llm: bool = False,
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        runtime_config: Any | None = None,
    ):
        """Initialize the control plane engine.

        Args:
            workspace_registry: Workspace registry for selection
            repo_root: Repository root for GMAS retrieval
            memory_store: Manager memory system
            policy_engine: Policy engine for constraint checking
            workspaces_root: Root directory for workspaces
            control_state_dir: Directory for control plane state
            use_live_llm: Whether to use live LLM (vs degraded mode)
            llm_model: LLM model name
            llm_api_key: LLM API key
            llm_base_url: LLM base URL
            runtime_config: ``UmbrellaRuntimeConfig`` controlling quality thresholds, budget, etc.
        """
        from umbrella.config import UmbrellaRuntimeConfig

        self.runtime_config: UmbrellaRuntimeConfig = (
            runtime_config or UmbrellaRuntimeConfig()
        )

        self.repo_root = repo_root or Path.cwd()
        self.workspace_registry = workspace_registry
        self.memory_store = memory_store or MemoryStore(
            MemoryConfig(memory_root=control_state_dir or Path(".umbrella/memory"))
        )
        self.policy_engine = policy_engine
        self.workspaces_root = workspaces_root or Path("workspaces")
        self.control_state_dir = control_state_dir or Path(".umbrella/control_plane")

        # LLM configuration for workspace runs
        self.use_live_llm = use_live_llm
        self.llm_model = llm_model
        self.llm_api_key = llm_api_key
        self.llm_base_url = llm_base_url

        # Initialize retrieval service
        self.retrieval: RetrievalService | None = retrieval_service

        # Initialize sub-components
        self.state_machine: ManagerStateMachine | None = None
        self.trace_manager = TraceManager(self.control_state_dir / "traces")
        self.escalation_mgr = HumanEscalation(self.control_state_dir / "escalations")
        self.self_improvement_gate = SelfImprovementGate()
        self.inspector = LayeredInspector(self.workspaces_root)
        self.prompt_versions_dir = self.control_state_dir / "prompt_versions"
        self.prompt_proposals_dir = self.control_state_dir / "prompt_proposals"
        self.human_checkpoint_dir = self.control_state_dir / "human_checkpoints"
        self.checkpoint_dir = self.control_state_dir / "checkpoints"

        # Active task and instance
        self.active_task: ManagerTask | None = None
        self.active_instance: WorkspaceInstance | None = None
        self.last_run_result: WorkspaceRunResult | None = None
        self.last_inspection: dict[str, Any] | None = None
        self.last_retrieval_card: RetrievalCard | None = None
        self.last_patch_result: WorkspacePatchResult | None = None

        # Evaluation tracking
        self.last_eval_record: EvaluationRecord | None = None
        self.baseline_eval_record: EvaluationRecord | None = None
        self.eval_history: list[EvaluationRecord] = []

        # Telemetry and metrics
        self.metrics_registry = get_metrics_registry()
        self.promotion_policy = create_default_policy()

    def _reset_retrieval_state(self, task: ManagerTask) -> None:
        """Clear proactive retrieval state for the current task."""
        self.last_retrieval_card = None
        task.state.retrieval_query = None
        task.state.retrieval_summary = None
        task.state.retrieval_key_files = []
        task.state.retrieval_hit_count = 0

    def _persist_task_checkpoint(self, task: ManagerTask) -> None:
        """Persist task state so live terminal updates can target active runs."""
        try:
            checkpoint_dir = self.control_state_dir / "checkpoints"
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            checkpoint_path = checkpoint_dir / f"{task.id}.json"
            checkpoint_path.write_text(
                json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            log.warning(
                "Failed to persist task checkpoint for %s", task.id, exc_info=True
            )

    def _normalize_workspace_id(self, candidate: Any) -> str | None:
        """Return a safe workspace id string or ``None``."""
        if isinstance(candidate, str):
            value = candidate.strip()
            if value:
                return value
        return None

    def _ensure_minimal_seed_workspace(self, workspace_id: str) -> Path:
        """Create a minimal seed workspace skeleton for isolated tests."""
        workspace_path = self.workspaces_root / workspace_id
        workspace_path.mkdir(parents=True, exist_ok=True)

        workspace_toml_path = workspace_path / "workspace.toml"
        if not workspace_toml_path.exists():
            workspace_toml_path.write_text(
                (
                    f'workspace_id = "{workspace_id}"\n'
                    f'name = "{workspace_id}"\n'
                    f'description = "Minimal generated seed workspace for tests"\n'
                ),
                encoding="utf-8",
            )

        task_main_path = workspace_path / "TASK_MAIN.md"
        if not task_main_path.exists():
            task_main_path.write_text(
                "# Seed Workspace\n\n## Objective\n\nProvide a minimal task contract for tests.\n",
                encoding="utf-8",
            )

        return workspace_path

    def _fallback_seed_profile(self, workspace_id: str) -> SeedWorkspaceProfile:
        """Build a minimal seed profile when the registry is absent or mocked."""
        workspace_path = self._ensure_minimal_seed_workspace(workspace_id)
        return SeedWorkspaceProfile(
            ref=WorkspaceRef(
                workspace_id=workspace_id,
                name=workspace_id,
                description=f"Seed workspace: {workspace_id}",
                path=workspace_path,
            )
        )

    def _resolve_seed_profile(self, workspace_id: str) -> SeedWorkspaceProfile:
        """Return a usable seed profile, tolerating loosely-configured mocks."""
        if self.workspace_registry is not None:
            try:
                candidate = self.workspace_registry.get_seed_profile(workspace_id)
                candidate_id = self._normalize_workspace_id(
                    getattr(candidate, "workspace_id", None)
                )
                candidate_path = getattr(candidate, "path", None)
                if (
                    isinstance(candidate, SeedWorkspaceProfile)
                    and candidate_id == workspace_id
                    and isinstance(candidate_path, Path)
                ):
                    return candidate

                workspace_ref = self.workspace_registry.get_workspace(workspace_id)
                ref_id = self._normalize_workspace_id(
                    getattr(workspace_ref, "workspace_id", None)
                )
                ref_path = getattr(workspace_ref, "path", None)
                if (
                    isinstance(workspace_ref, WorkspaceRef)
                    and ref_id == workspace_id
                    and isinstance(ref_path, Path)
                    and ref_path.exists()
                ):
                    return SeedWorkspaceProfile(ref=workspace_ref)
            except Exception as exc:
                log.debug("Failed to resolve seed profile from registry: %s", exc)

        return self._fallback_seed_profile(workspace_id)

    # =======================================================================
    # Task Lifecycle
    # =======================================================================

    def start_task(
        self,
        task_input: str,
        task_id: str | None = None,
        workspace_id: str | None = None,
    ) -> ManagerTask:
        """Start a new task.

        Args:
            task_input: Raw task description
            task_id: Optional task ID (auto-generated if None)
            workspace_id: Optional workspace ID to use (auto-selected if None)

        Returns:
            Started task
        """
        if task_id is None:
            task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:8]}"

        # Classify the task
        brief = classify_task(task_input, task_id)

        # Select workspace if not specified
        if workspace_id is None:
            workspace_id = select_seed_workspace(brief, self.workspace_registry)
        workspace_id = self._normalize_workspace_id(workspace_id) or "agent_research"

        # Create task and state
        state = ManagerState(task_id=task_id)
        state.current_workspace_id = workspace_id
        self.state_machine = ManagerStateMachine(state)

        task = ManagerTask(
            id=task_id,
            brief=brief,
            status="active",
            state=state,
            started_at=time.time(),
        )

        self.active_task = task

        # Transition to workspace selection
        self.state_machine.transition_to(
            ManagerPhase.WORKSPACE_SELECTED,
            reason="Task classified, selecting workspace",
        )

        # Emit telemetry event for workspace selection
        emit_event(
            WorkspaceSelectedEvent(
                task_id=task_id,
                workspace_id=workspace_id,
                seed_workspace_id=workspace_id,
                selection_reason=f"Task class: {brief.task_class.value}",
                confidence=0.8,
            )
        )

        # Record in metrics registry
        self.metrics_registry.increment_counter("tasks_started")

        self._persist_task_checkpoint(task)

        log.info(f"Started task {task_id} with workspace {workspace_id}")
        return task

    def process_task_step(
        self,
        task: ManagerTask | None = None,
    ) -> ActionResult:
        """Process one step of the task.

        Args:
            task: Task to process (uses active task if None)

        Returns:
            Result of the step
        """
        task = task or self.active_task
        if task is None:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT, description="No active task"
                ),
                outcome="cancelled",
                summary="No active task to process",
            )

        try:
            pending_updates = consume_runtime_task_updates(
                self.control_state_dir, task.id
            )
            if pending_updates:
                return self._handle_runtime_task_updates(task, pending_updates)

            phase = task.state.phase

            # Route to appropriate phase handler
            if phase == ManagerPhase.WORKSPACE_SELECTED:
                return self._handle_workspace_selected(task)

            elif phase == ManagerPhase.INSTANCE_PREPARED:
                return self._handle_instance_prepared(task)

            elif phase == ManagerPhase.KNOWLEDGE_RETRIEVED:
                return self._handle_knowledge_retrieved(task)

            elif phase == ManagerPhase.WORKSPACE_RUNNING:
                return self._handle_workspace_running(task)

            elif phase == ManagerPhase.RUN_COMPLETE:
                return self._handle_run_complete(task)

            elif phase == ManagerPhase.INSPECTION_COMPLETE:
                return self._handle_inspection_complete(task)

            elif phase == ManagerPhase.DECISION_MADE:
                return self._handle_decision_made(task)

            elif phase == ManagerPhase.PATCH_APPLIED:
                return self._handle_patch_applied(task)

            elif phase == ManagerPhase.LESSON_RECORDED:
                return self._handle_lesson_recorded(task)

            elif phase == ManagerPhase.PROMOTION_CONSIDERATION:
                return self._handle_promotion_consideration(task)

            elif phase == ManagerPhase.SELF_IMPROVEMENT_PENDING:
                return self._handle_self_improvement_pending(task)

            elif phase == ManagerPhase.SELF_IMPROVEMENT_APPROVED:
                return self._handle_self_improvement_approved(task)

            elif phase == ManagerPhase.SELF_IMPROVEMENT_COMPLETE:
                return self._handle_self_improvement_complete(task)

            elif phase == ManagerPhase.ESCALATED:
                return self._handle_escalated(task)

            elif phase == ManagerPhase.ESCALATION_RESOLVED:
                return self._handle_escalation_resolved(task)

            elif phase in (
                ManagerPhase.TASK_COMPLETE,
                ManagerPhase.TASK_FAILED,
                ManagerPhase.TASK_BLOCKED,
            ):
                status = "complete" if phase == ManagerPhase.TASK_COMPLETE else "failed"
                return ActionResult(
                    action=NextAction(
                        action_type=ActionType.COMPLETE_TASK
                        if status == "complete"
                        else ActionType.FAIL_TASK,
                        description=f"Task already in terminal phase {phase.value}",
                    ),
                    outcome=status,
                    summary=f"Task is in terminal phase: {phase.value}",
                )

            else:
                log.warning(
                    "Unhandled phase %s, transitioning to KNOWLEDGE_RETRIEVED to resume",
                    phase,
                )
                if self.state_machine.can_transition_to(
                    ManagerPhase.KNOWLEDGE_RETRIEVED
                ):
                    self.state_machine.transition_to(
                        ManagerPhase.KNOWLEDGE_RETRIEVED,
                        reason=f"Recovering from unhandled phase {phase.value}",
                    )
                    return self._handle_knowledge_retrieved(task)
                return ActionResult(
                    action=NextAction(
                        action_type=ActionType.FAIL_TASK,
                        description=f"Phase {phase} not recoverable",
                    ),
                    outcome="failure",
                    summary=f"Phase {phase} has no handler and no valid recovery path",
                )

        except Exception as e:
            log.error(f"Error processing task {task.id}: {e}", exc_info=True)
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description="Error during processing",
                ),
                outcome="failure",
                summary=f"Processing error: {e}",
            )
        finally:
            self._persist_task_checkpoint(task)

    def _append_runtime_instruction_to_task_main(
        self,
        task: ManagerTask,
        update: RuntimeTaskUpdate,
    ) -> None:
        """Append a human runtime instruction to the instance TASK_MAIN."""
        instance_path = task.state.current_instance_path
        if not instance_path:
            return

        task_main_path = instance_path / "TASK_MAIN.md"
        if not task_main_path.exists():
            return

        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(update.created_at))
        note = (
            f"\n\n## Runtime Update ({stamp})\n\n"
            f"Source: {update.source}\n\n"
            f"{update.instruction.strip()}\n"
        )
        with task_main_path.open("a", encoding="utf-8") as handle:
            handle.write(note)

    def _retarget_after_runtime_update(self, task: ManagerTask) -> None:
        """Redirect stale post-run phases back into an execution path."""
        phase = task.state.phase
        rerun_phases = {
            ManagerPhase.RUN_COMPLETE,
            ManagerPhase.INSPECTION_COMPLETE,
            ManagerPhase.DECISION_MADE,
            ManagerPhase.LESSON_RECORDED,
            ManagerPhase.PROMOTION_CONSIDERATION,
            ManagerPhase.SELF_IMPROVEMENT_PENDING,
            ManagerPhase.SELF_IMPROVEMENT_APPROVED,
            ManagerPhase.SELF_IMPROVEMENT_COMPLETE,
            ManagerPhase.ESCALATED,
            ManagerPhase.ESCALATION_RESOLVED,
        }

        if phase not in rerun_phases or self.state_machine is None:
            return

        task.state.last_decision = NextAction(
            action_type=ActionType.RUN_WORKSPACE,
            description="Re-run workspace with updated human task scope",
        )
        task.state.pending_human_checkpoint_id = None
        task.state.active_self_improvement_checkpoint_id = None
        self.state_machine.force_phase(
            ManagerPhase.KNOWLEDGE_RETRIEVED,
            reason="Human runtime task update applied",
        )

    def _handle_runtime_task_updates(
        self,
        task: ManagerTask,
        updates: list[RuntimeTaskUpdate],
    ) -> ActionResult:
        """Apply runtime task updates before continuing normal processing."""
        applied: list[str] = []
        for update in updates:
            normalized = update.instruction.strip()
            if not normalized:
                continue
            stamp = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(update.created_at)
            )
            task.brief.original_input = (
                task.brief.original_input.rstrip()
                + f"\n\nAdditional human instruction ({stamp}, {update.source}):\n{normalized}\n"
            )
            if normalized not in task.brief.requirements:
                task.brief.requirements.append(normalized)
            task.state.task_brief = task.brief
            task.state.runtime_update_count += 1
            task.state.latest_runtime_update = normalized[:500]
            self._append_runtime_instruction_to_task_main(task, update)
            applied.append(normalized)

        task.state.touch()
        self._retarget_after_runtime_update(task)

        summary = f"Applied {len(applied)} runtime task update(s) from human input"
        if applied:
            summary += f"; latest: {applied[-1][:120]}"

        return ActionResult(
            action=NextAction(
                action_type=ActionType.UPDATE_TASK_SCOPE,
                description="Apply human runtime task updates",
            ),
            outcome="success",
            summary=summary,
            details={
                "updates_applied": len(applied),
                "latest_runtime_update": applied[-1] if applied else "",
            },
        )

    # =======================================================================
    # Phase Handlers
    # =======================================================================

    def _handle_workspace_selected(self, task: ManagerTask) -> ActionResult:
        """Handle workspace selection phase - create task instance."""
        # Get seed workspace profile
        workspace_id = task.state.current_workspace_id
        if not workspace_id:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description="No workspace selected",
                ),
                outcome="failure",
                summary="No workspace selected",
            )

        seed_profile = self._resolve_seed_profile(workspace_id)

        try:
            runtime_task_brief = to_workspace_task_brief(
                task.brief,
                preferred_workspace_id=workspace_id,
            )

            # Create task instance
            instance = create_task_instance(
                seed_profile,
                runtime_task_brief,
                instances_root=self.workspaces_root
                / seed_profile.workspace_id
                / "instances",
                task_id=task.id,
                copy_seed_files=True,
            )

            self.active_instance = instance
            task.state.current_instance_path = instance.path

            if self.workspace_registry is not None:
                instance_profile = load_task_instance_profile(instance.path)
                if instance_profile is not None:
                    instance_profile.seed_profile = seed_profile
                    self.workspace_registry.register_task_instance(instance_profile)

            # Load or create TASK_MAIN.md
            task_main_doc = load_task_main_document(instance)
            if task_main_doc is None:
                # Initialize TASK_MAIN.md from task brief
                self._initialize_task_main(instance, task.brief)

            # Transition to instance prepared
            self.state_machine.transition_to(
                ManagerPhase.INSTANCE_PREPARED, reason="Task instance created"
            )

            return ActionResult(
                action=NextAction(
                    action_type=ActionType.RUN_WORKSPACE,
                    description=f"Task instance created at {instance.path.name}",
                ),
                outcome="success",
                summary=f"Workspace {workspace_id} selected, instance created",
                details={"instance_path": str(instance.path), "simulated": False},
            )
        except Exception as e:
            log.error(f"Failed to create task instance: {e}", exc_info=True)
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description="Instance creation failed",
                ),
                outcome="failure",
                summary=f"Failed to create workspace instance: {e}",
                details={"error": str(e), "workspace_id": workspace_id},
            )

    def _handle_instance_prepared(self, task: ManagerTask) -> ActionResult:
        """Handle instance prepared phase - prepare and validate."""
        instance = self.active_instance
        if not instance:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description="Missing workspace instance",
                ),
                outcome="failure",
                summary="Workspace instance is missing during preparation",
            )

        # Prepare the workspace (validate resources, etc.)
        prepared = prepare_instance(instance)
        if not prepared.ready:
            reason = prepared.not_ready_reason or "; ".join(prepared.validation_issues)
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description=f"Workspace not ready: {reason}",
                ),
                outcome="failure",
                summary=f"Workspace preparation failed: {reason}",
            )

        self.state_machine.transition_to(
            ManagerPhase.KNOWLEDGE_RETRIEVED,
            reason="Workspace prepared, entering optional context stage",
        )

        # Continue to knowledge retrieval
        return self._handle_knowledge_retrieved(task)

    def _handle_knowledge_retrieved(self, task: ManagerTask) -> ActionResult:
        """Handle the optional context preload phase before a workspace run."""
        self._reset_retrieval_state(task)

        if getattr(self.runtime_config, "auto_retrieve_gmas_context", False):
            # Build retrieval index if needed
            if self.retrieval is None:
                self.retrieval = RetrievalService(self.repo_root)

            # Query GMAS for relevant knowledge based on task
            query = f"{task.brief.summary} {task.brief.original_input[:200]}"
            task.state.retrieval_query = query
            try:
                retrieval_card = self.retrieval.search(query, max_results=10)
                self.last_retrieval_card = retrieval_card
                task.state.task_brief = task.brief
                task.state.retrieval_summary = retrieval_card.recommended_pattern
                task.state.retrieval_key_files = [
                    str(path) for path in retrieval_card.key_files[:5]
                ]
                task.state.retrieval_hit_count = len(retrieval_card.hits)
            except Exception as e:
                log.warning(f"Retrieval failed: {e}")
                self._reset_retrieval_state(task)

        # Transition to workspace running
        self.state_machine.transition_to(
            ManagerPhase.WORKSPACE_RUNNING,
            reason=(
                "Knowledge preloaded, starting workspace run"
                if task.state.retrieval_hit_count > 0
                else "Workspace will use on-demand context tools if needed"
            ),
        )

        return self._handle_workspace_running(task)

    def _handle_workspace_running(self, task: ManagerTask) -> ActionResult:
        """Handle workspace running phase - execute the workspace."""
        instance = self.active_instance

        # Emit run started event
        emit_event(
            RunStartedEvent(
                task_id=task.id,
                workspace_id=task.state.current_workspace_id or "unknown",
                run_id=f"{task.id}_run_{task.state.iteration_count}",
                instance_id=instance.instance_id if instance else "",
            )
        )
        if not instance:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description="Missing workspace instance",
                ),
                outcome="failure",
                summary="Cannot run workspace without a real instance",
            )

        # PRODUCTION MODE: Real workspace instance available
        # Create run request
        request = WorkspaceRunRequest(
            task_id=task.id,
            query=task.brief.original_input,
            live=self.use_live_llm,
            mock_loops=not self.use_live_llm,  # Use mock loops if not live
            max_agent_executions=32,
            model=self.llm_model,
            api_key=self.llm_api_key,
            base_url=self.llm_base_url,
            metadata={
                "manager_summary": task.brief.summary,
                "retrieval_context": self._build_retrieval_context(
                    self.last_retrieval_card
                ),
                "retrieval_hit_count": len(self.last_retrieval_card.hits)
                if self.last_retrieval_card
                else 0,
                "retrieval_key_files": [
                    str(path) for path in self.last_retrieval_card.key_files[:5]
                ]
                if self.last_retrieval_card
                else [],
            },
        )

        # Run the workspace
        try:
            result = run_workspace(instance, request, prepare=False)
            self.last_run_result = result
        except Exception as e:
            log.error(f"Workspace run failed: {e}", exc_info=True)
            result = WorkspaceRunResult(
                workspace_id=instance.workspace_id,
                task_id=task.id,
                status=WorkspaceRunStatus.FAILED,
                errors=[str(e)],
            )
            self.last_run_result = result

        # Transition based on result
        if result.status == WorkspaceRunStatus.FAILED:
            self.state_machine.transition_to(
                ManagerPhase.RUN_COMPLETE, reason="Workspace run failed"
            )
        else:
            self.state_machine.transition_to(
                ManagerPhase.RUN_COMPLETE, reason="Workspace run completed"
            )

        # Continue to inspection
        return self._handle_run_complete(task)

    def _handle_run_complete(self, task: ManagerTask) -> ActionResult:
        """Handle run complete phase - inspect results using layered approach."""
        result = self.last_run_result
        instance = self.active_instance

        if not result:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK, description="No run result"
                ),
                outcome="failure",
                summary="Cannot inspect without run result",
            )

        if not instance:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description="Missing workspace instance",
                ),
                outcome="failure",
                summary="Cannot inspect a run without the originating workspace instance",
            )

        inspection_data = self.inspector.inspect_run_result(result, instance)

        self.last_inspection = inspection_data

        # Emit run completed event
        emit_event(
            RunCompletedEvent(
                task_id=task.id,
                workspace_id=task.state.current_workspace_id or result.workspace_id,
                run_id=result.run_id,
                instance_id=instance.instance_id if instance else "",
                status=result.status.value,
                duration_seconds=result.duration_seconds,
                total_tokens=result.total_tokens,
                error_count=len(result.errors) if result.errors else 0,
            )
        )

        # Evaluate the run
        instance_path = instance.path if instance else Path(".")
        try:
            eval_record = evaluate_run(
                result,
                instance_path,
                task_class=task.brief.task_class,
                previous_evals=self.eval_history,
                repo_root=self.repo_root,
                min_article_word_count=self.runtime_config.min_article_word_count,
                required_artifact_types=self.runtime_config.required_artifact_types,
                task_input=task.brief.original_input,  # Pass original task for LLM evaluation
                use_llm=self.use_live_llm,
            )
            self.last_eval_record = eval_record
            self.eval_history.append(eval_record)

            # Record in metrics registry
            self.metrics_registry.record_run(
                task.state.current_workspace_id or result.workspace_id,
                eval_record,
            )

            # Emit eval completed event
            emit_event(
                EvalCompletedEvent(
                    task_id=task.id,
                    workspace_id=task.state.current_workspace_id or result.workspace_id,
                    run_id=result.run_id,
                    task_success=eval_record.task_success.value,
                    output_quality=eval_record.output_quality.value,
                    overall_score=eval_record.overall_score,
                    total_cost_usd=eval_record.total_cost_usd,
                )
            )

            # Store eval summary in memory for future reference
            if self.memory_store:
                self._store_eval_summary_in_memory(task, eval_record)

        except Exception as e:
            log.warning(f"Evaluation failed: {e}")

        # Compare with baseline if we have one
        if self.baseline_eval_record and self.last_eval_record:
            try:
                comparison = compare_runs(
                    self.baseline_eval_record,
                    self.last_eval_record,
                )
                # Store comparison for potential promotion
                task.state.last_comparison = comparison

                # Consider promotion if there's improvement
                if comparison.overall_improvement.value == "improved":
                    self._consider_promotion(task, comparison, result)
            except Exception as e:
                log.warning(f"Comparison failed: {e}")
        else:
            # Set first successful eval as baseline
            if self.last_eval_record and self.last_eval_record.task_success in (
                TaskSuccessRating.COMPLETE,
                TaskSuccessRating.PARTIAL,
            ):
                self.baseline_eval_record = self.last_eval_record

        # Extract key info from inspection
        manifest = inspection_data["manifest"]
        error_signatures = inspection_data["error_signatures"]

        eval_score = (
            self.last_eval_record.overall_score if self.last_eval_record else None
        )

        if result.status == WorkspaceRunStatus.FAILED:
            outcome = "failure"
        elif result.status == WorkspaceRunStatus.COMPLETED:
            quality_ok, quality_reason = self._completion_quality_ok()
            if not quality_ok:
                outcome = "partial"
                log.info(
                    "Run completed but completion gate is not satisfied; treating as partial (%s)",
                    quality_reason or "quality gate blocked",
                )
            else:
                outcome = "success"
        else:
            outcome = "partial"

        # Build context with inspection data
        context = self._build_context_with_inspection(task, inspection_data)
        context.last_run_outcome = outcome
        context.last_eval_score = eval_score

        # Stage-aware human checkpoints
        stage_gate_result = self._check_stage_human_review(task, instance)
        if stage_gate_result is not None:
            return stage_gate_result

        # Transition to inspection complete
        self.state_machine.transition_to(
            ManagerPhase.INSPECTION_COMPLETE, reason="Run results inspected"
        )

        # Decide next action
        next_action = decide_next_action(context)

        # Record decision
        decision = DecisionRecord(
            id=generate_decision_id(),
            task_id=task.id,
            context_snapshot=context,
            action=next_action,
            rationale=DecisionRationale(
                action_taken=next_action.action_type,
                reason=next_action.description,
                confidence=0.7,
                evidence=[f"Run outcome: {outcome}"] + error_signatures,
                alternatives_considered=[
                    ActionType.PATCH_WORKSPACE,
                    ActionType.ESCALATE_TO_HUMAN,
                ],
                why_not_alternatives={},
            ),
        )
        trace_decision(decision)

        self.state_machine.transition_to(
            ManagerPhase.DECISION_MADE,
            reason=f"Decision made: {next_action.action_type}",
        )

        task.state.last_decision = next_action

        return self._execute_action(task, next_action, context)

    def _handle_inspection_complete(self, task: ManagerTask) -> ActionResult:
        """Handle inspection complete phase."""
        context = self._build_context(task)
        next_action = decide_next_action(context)
        return self._execute_action(task, next_action, context)

    def _handle_decision_made(self, task: ManagerTask) -> ActionResult:
        """Handle decision made phase."""
        next_action = task.state.last_decision
        context = self._build_context(task)

        if not next_action:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="No decision to execute",
                ),
                outcome="partial",
                summary="No decision to execute",
            )

        return self._execute_action(task, next_action, context)

    def _record_patch_result(
        self,
        task: ManagerTask,
        patch_result: WorkspacePatchResult,
    ) -> None:
        """Persist the latest instance mutation for later compare/promotion steps."""
        self.last_patch_result = patch_result
        task.state.last_patch_files = list(patch_result.changed_files)
        task.state.last_patch_summary = patch_result.summary

    def _clear_patch_result(self, task: ManagerTask) -> None:
        """Clear stale patch state when a rerun should not be treated as a new mutation."""
        self.last_patch_result = None
        task.state.last_patch_files = []
        task.state.last_patch_summary = None

    def _handle_patch_applied(self, task: ManagerTask) -> ActionResult:
        """Handle a successfully applied workspace patch by re-running the instance."""
        context = self._build_context(task)
        return self._run_workspace(task, context)

    def _handle_lesson_recorded(self, task: ManagerTask) -> ActionResult:
        """Handle a recorded lesson by either completing or re-running."""
        context = self._build_context(task)
        quality_ok, quality_reason = self._completion_quality_ok()

        if context.last_run_outcome == "success" and quality_ok:
            completion_review = self._request_final_completion_review(task)
            if completion_review is not None:
                return completion_review
            return self._complete_task(
                task,
                NextAction(
                    action_type=ActionType.COMPLETE_TASK,
                    description="Lesson recorded after successful run",
                ),
            )
        if quality_reason:
            log.info("Completion gate blocked: %s", quality_reason)
        return self._schedule_workspace_rerun(
            task,
            reason=(
                "Latest run does not yet satisfy completion requirements"
                + (f" ({quality_reason})" if quality_reason else "")
            ),
        )

    def _handle_promotion_consideration(self, task: ManagerTask) -> ActionResult:
        """Handle promotion consideration phase."""
        candidate = task.state.promotion_candidate
        if not candidate:
            # No candidate to consider, move to next phase
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="No promotion candidate",
                ),
                outcome="partial",
                summary="No promotion candidate to consider",
            )

        # Check if human review is pending
        checkpoint_id = f"{task.id}_promotion_{candidate.id}"
        checkpoint = HumanCheckpoint.load(
            checkpoint_id, self.control_state_dir / "checkpoints"
        )

        if checkpoint and checkpoint.status == "approved":
            # Promotion approved, could apply to seed here
            # For now just record and continue
            log.info(f"Promotion {candidate.id} approved by human")
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="Promotion approved",
                ),
                outcome="success",
                summary=f"Promotion candidate {candidate.id} approved",
            )
        elif checkpoint and checkpoint.status == "rejected":
            # Promotion rejected
            log.info(f"Promotion {candidate.id} rejected by human")
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="Promotion rejected",
                ),
                outcome="partial",
                summary=f"Promotion candidate {candidate.id} rejected",
            )
        else:
            # Awaiting human review
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="Awaiting promotion review",
                ),
                outcome="partial",
                summary=f"Awaiting human review for promotion {candidate.id}",
                details={"checkpoint_id": checkpoint_id} if checkpoint else {},
            )

    def _handle_self_improvement_pending(self, task: ManagerTask) -> ActionResult:
        """Handle self-improvement pending phase."""
        context = self._build_context(task)

        pending_checkpoint_id = task.state.pending_human_checkpoint_id
        if pending_checkpoint_id:
            request = load_human_checkpoint_request(
                pending_checkpoint_id, self.human_checkpoint_dir
            )
            if request is None:
                return ActionResult(
                    action=NextAction(
                        action_type=ActionType.FAIL_TASK,
                        description="Missing human checkpoint",
                    ),
                    outcome="failure",
                    summary=f"Human checkpoint {pending_checkpoint_id} is missing",
                )

            if request.status == HumanCheckpointStatus.PENDING:
                return ActionResult(
                    action=NextAction(
                        action_type=ActionType.WAIT_FOR_INPUT,
                        description="Awaiting human checkpoint approval",
                    ),
                    outcome="partial",
                    summary="Waiting for human checkpoint approval",
                    details={
                        "checkpoint_id": request.id,
                        "notification_message": request.notification_message,
                        "proposal_id": request.proposal_id,
                    },
                )

            resume_result = resume_after_human_checkpoint(
                pending_checkpoint_id,
                checkpoint_dir=self.human_checkpoint_dir,
                manager_checkpoint_dir=self.checkpoint_dir,
            )

            if not resume_result.resumed:
                return ActionResult(
                    action=NextAction(
                        action_type=ActionType.WAIT_FOR_INPUT,
                        description="Human checkpoint rejected",
                    ),
                    outcome="partial",
                    summary=resume_result.resume_reason or "Human checkpoint rejected",
                    details={"checkpoint_id": pending_checkpoint_id},
                )

            task.state.pending_human_checkpoint_id = None
            task.state.active_self_improvement_checkpoint_id = (
                resume_result.manager_checkpoint_id
            )
            if self.state_machine.can_transition_to(
                ManagerPhase.SELF_IMPROVEMENT_APPROVED
            ):
                self.state_machine.transition_to(
                    ManagerPhase.SELF_IMPROVEMENT_APPROVED,
                    reason=resume_result.resume_reason or "Human checkpoint approved",
                )

            return ActionResult(
                action=task.state.last_decision
                or NextAction(
                    action_type=ActionType.REWRITE_PROMPT_STACK,
                    description="Prompt rewrite approved",
                ),
                outcome="success",
                summary="Human checkpoint approved; prompt rewrite can proceed",
                details={"resume": resume_result.model_dump(mode="json")},
            )

        # Check eligibility
        is_eligible, reason, suggestions = check_self_improvement_eligibility(
            context, self.self_improvement_gate
        )

        if not is_eligible:
            # Not eligible, escalate instead
            return self._escalate_for_reason(
                task, context, "Self-improvement requested but not eligible", reason
            )

        # Check for human checkpoint if prompt stack rewrite
        if (
            task.state.last_decision
            and task.state.last_decision.self_improvement_type == "prompt_stack_rewrite"
        ):
            checkpoint_result = self._check_human_checkpoint(
                task,
                context,
                "prompt_stack_rewrite",
                "Prompt stack rewrite requires human approval",
            )
            if checkpoint_result:
                return checkpoint_result

        # Prepare self-improvement
        checkpoint = prepare_self_improvement(
            context,
            task.state.last_decision.self_improvement_type or "general",
            "Improve manager prompts",
            self.checkpoint_dir,
        )
        task.state.active_self_improvement_checkpoint_id = checkpoint.id

        # Transition to approved
        self.state_machine.transition_to(
            ManagerPhase.SELF_IMPROVEMENT_APPROVED, reason="Self-improvement approved"
        )

        return ActionResult(
            action=NextAction(
                action_type=ActionType.SELF_IMPROVE,
                description="Execute self-improvement",
            ),
            outcome="success",
            summary="Self-improvement prepared",
        )

    def _handle_self_improvement_approved(self, task: ManagerTask) -> ActionResult:
        """Handle approved self-improvement or prompt rewrite checkpoints."""
        # Handle prompt rewrite
        if (
            task.state.last_decision
            and task.state.last_decision.action_type == ActionType.REWRITE_PROMPT_STACK
        ):
            proposal: PromptPatchProposal | None = None
            if task.state.last_prompt_proposal_id:
                try:
                    proposal = load_prompt_patch_proposal(
                        task.state.last_prompt_proposal_id,
                        self.prompt_proposals_dir,
                    )
                except FileNotFoundError:
                    return ActionResult(
                        action=task.state.last_decision,
                        outcome="failure",
                        summary=f"Prompt proposal {task.state.last_prompt_proposal_id} not found",
                    )

            if proposal and proposal.proposed_content:
                try:
                    # Apply the prompt patch
                    from umbrella.control_plane.prompt_policy import apply_prompt_patch

                    version_record = apply_prompt_patch(
                        proposal,
                        self.repo_root,
                        self.prompt_versions_dir,
                    )

                    log.info(
                        f"Applied prompt patch {proposal.id} to {proposal.surface.path}, "
                        f"version {version_record.id}"
                    )

                    # Transition to complete
                    if self.state_machine.can_transition_to(
                        ManagerPhase.SELF_IMPROVEMENT_COMPLETE
                    ):
                        self.state_machine.transition_to(
                            ManagerPhase.SELF_IMPROVEMENT_COMPLETE,
                            reason="Prompt patch applied successfully",
                        )

                    return ActionResult(
                        action=task.state.last_decision,
                        outcome="success",
                        summary="Prompt rewrite applied successfully",
                        details={
                            "proposal_id": proposal.id,
                            "surface_id": proposal.surface.id,
                            "surface_path": str(proposal.surface.path),
                            "version_id": version_record.id,
                            "rollback_reference": proposal.rollback_reference,
                        },
                    )
                except Exception as e:
                    log.error(f"Failed to apply prompt patch: {e}", exc_info=True)
                    return ActionResult(
                        action=task.state.last_decision,
                        outcome="failure",
                        summary=f"Failed to apply prompt patch: {e}",
                    )
            else:
                return ActionResult(
                    action=task.state.last_decision,
                    outcome="failure",
                    summary="Prompt rewrite was approved but no concrete content was available to apply",
                )

        # Handle other self-improvement actions
        if task.state.last_decision:
            return self._execute_self_improvement(task, task.state.last_decision)

        return ActionResult(
            action=NextAction(
                action_type=ActionType.WAIT_FOR_INPUT,
                description="No approved action to execute",
            ),
            outcome="partial",
            summary="No approved self-improvement action to execute",
        )

    def _handle_self_improvement_complete(self, task: ManagerTask) -> ActionResult:
        """Handle completed self-improvement by resuming workspace iteration."""
        log.info("Self-improvement complete, resuming workspace run cycle")
        self.state_machine.transition_to(
            ManagerPhase.KNOWLEDGE_RETRIEVED,
            reason="Resuming after self-improvement",
        )
        return self._handle_knowledge_retrieved(task)

    def _handle_escalation_resolved(self, task: ManagerTask) -> ActionResult:
        """Handle a resolved escalation by resuming the decision cycle."""
        log.info("Escalation resolved, resuming task")
        self.state_machine.transition_to(
            ManagerPhase.DECISION_MADE,
            reason="Continuing after escalation resolved",
        )
        context = self._build_context(task)
        return self._run_workspace(task, context)

    def _handle_escalated(self, task: ManagerTask) -> ActionResult:
        """Handle escalated phase - wait for human input."""
        return ActionResult(
            action=NextAction(
                action_type=ActionType.WAIT_FOR_INPUT,
                description="Awaiting human resolution",
            ),
            outcome="partial",
            summary="Waiting for human input",
        )

    # =======================================================================
    # Action Execution
    # =======================================================================

    def _execute_action(
        self,
        task: ManagerTask,
        action: NextAction,
        context: DecisionContext,
    ) -> ActionResult:
        """Execute a decided action.

        Args:
            task: Current task
            action: Action to execute
            context: Decision context

        Returns:
            Result of execution
        """
        action_type = action.action_type

        # Check for blocking constraints
        is_blocked, blocking_reasons = check_blocking_constraints(context, action_type)

        if is_blocked:
            return self._escalate_for_reason(
                task,
                context,
                "Action blocked by constraints",
                "; ".join(blocking_reasons),
            )

        # Route to handler
        if action_type == ActionType.PATCH_WORKSPACE:
            return self._execute_workspace_patch(task, action, context)

        elif action_type == ActionType.UPDATE_WORKSPACE_CODE:
            return self._execute_workspace_code_update(task, action, context)

        elif action_type == ActionType.REWRITE_PROMPT_STACK:
            return self._execute_prompt_rewrite(task, action, context)

        elif action_type == ActionType.SELF_IMPROVE:
            return self._execute_self_improvement(task, action)

        elif action_type == ActionType.ESCALATE_TO_HUMAN:
            return self._execute_escalation(task, context)

        elif action_type == ActionType.RECORD_LESSON:
            return self._record_lesson_and_continue(task, context)

        elif action_type == ActionType.RUN_WORKSPACE:
            return self._run_workspace(task, context)

        elif action_type in (ActionType.COMPLETE_TASK, ActionType.FAIL_TASK):
            return self._complete_task(task, action)

        else:
            return ActionResult(
                action=action,
                outcome="partial",
                summary=f"Action {action_type} not implemented",
            )

    def _execute_workspace_patch(
        self,
        task: ManagerTask,
        action: NextAction,
        context: DecisionContext,
    ) -> ActionResult:
        """Execute a workspace patch."""
        # Record decision
        decision = should_patch_workspace(context)
        trace_decision(decision)

        # Emit patch proposed event
        target_files = []
        if action.metadata:
            target_files = action.metadata.get("target_files", [])

        emit_event(
            PatchProposedEvent(
                task_id=task.id,
                workspace_id=task.state.current_workspace_id or "unknown",
                patch_description=action.description,
                target_files=target_files,
                expected_outcome="Improved workspace behavior",
            )
        )

        # Transition to patch proposed
        self.state_machine.transition_to(
            ManagerPhase.PATCH_PROPOSED, reason="Workspace patch proposed"
        )

        # Check for human checkpoint if risky patch
        if action.patch_target and action.patch_target.value.startswith("manager_"):
            checkpoint_result = self._check_human_checkpoint(
                task,
                context,
                "manager_patch",
                f"Manager-level patch to {action.patch_target.value} requires approval",
            )
            if checkpoint_result:
                return checkpoint_result

        instance = self.active_instance
        if instance is None:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description="Missing workspace instance",
                ),
                outcome="failure",
                summary="Cannot patch a workspace before an instance exists",
            )

        snapshot = snapshot_instance(
            instance,
            label="pre_patch",
            description=action.description,
            include_artifacts=True,
        )
        patch_result = apply_workspace_patch(
            instance,
            patch_description=action.description,
            retrieval_card=self.last_retrieval_card,
            inspection_data=self.last_inspection,
            snapshot_path=str(snapshot.snapshot_path),
        )

        if not patch_result.applied:
            return ActionResult(
                action=action,
                outcome="failure",
                summary=patch_result.summary,
                details={
                    "snapshot_path": str(snapshot.snapshot_path),
                    "changed_files": patch_result.changed_files,
                },
            )

        self._record_patch_result(task, patch_result)

        self.state_machine.transition_to(
            ManagerPhase.PATCH_APPLIED, reason="Workspace patch applied"
        )

        # Emit patch applied event
        emit_event(
            PatchAppliedEvent(
                task_id=task.id,
                workspace_id=task.state.current_workspace_id or "unknown",
                patch_description=action.description,
                files_modified=patch_result.changed_files,
            )
        )

        # Record in metrics
        self.metrics_registry.increment_counter("patches_applied")

        return ActionResult(
            action=action,
            outcome="success",
            summary=patch_result.summary,
            details={
                "changed_files": patch_result.changed_files,
                "patch_note_path": patch_result.patch_note_path,
                "snapshot_path": patch_result.snapshot_path,
                "graph_changed": patch_result.graph_changed,
            },
            suggested_next_actions=[ActionType.RUN_WORKSPACE],
        )

    def _execute_workspace_code_update(
        self,
        task: ManagerTask,
        action: NextAction,
        context: DecisionContext,
    ) -> ActionResult:
        """Execute a direct code update to the seed workspace.

        This bypasses the instance-only patch mechanism and updates files
        directly in the seed workspace. All future instances will use the updated code.
        """
        instance = self.active_instance
        if instance is None:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description="Missing workspace instance",
                ),
                outcome="failure",
                summary="Cannot update workspace code before an instance exists",
            )

        # Get metadata from action
        metadata = action.metadata or {}
        code_updates = metadata.get("code_updates", {})
        files_to_copy = metadata.get("files_to_copy", [])
        commit_message = metadata.get(
            "commit_message", f"Umbrella code update: {action.description}"
        )

        if not code_updates and not files_to_copy:
            return ActionResult(
                action=action,
                outcome="failure",
                summary="No code updates specified. Provide code_updates or files_to_copy in action metadata.",
            )

        # Apply the update
        if code_updates:
            update_result = apply_code_update_to_seed(
                instance=instance,
                code_updates=code_updates,
                create_backup=True,
                commit_message=commit_message,
            )
        elif files_to_copy:
            update_result = update_seed_workspace_from_instance(
                instance=instance,
                files_to_update=files_to_copy,
                create_backup=True,
            )
        else:
            return ActionResult(
                action=action,
                outcome="failure",
                summary="No valid update specified",
            )

        # Record the update in task state
        task.state.last_patch_files = update_result.updated_files

        # Emit event
        emit_event(
            WorkspaceCodeUpdatedEvent(
                task_id=task.id,
                workspace_id=task.state.current_workspace_id or "unknown",
                updated_files=update_result.updated_files,
                backup_path=update_result.backup_path or "",
                description=action.description,
            )
        )

        if not update_result.applied:
            return ActionResult(
                action=action,
                outcome="failure",
                summary=update_result.summary,
                details={"error": update_result.error},
            )

        return ActionResult(
            action=action,
            outcome="success",
            summary=update_result.summary,
            details={
                "updated_files": update_result.updated_files,
                "backup_path": update_result.backup_path,
            },
            suggested_next_actions=[ActionType.RUN_WORKSPACE],
        )

    def _select_prompt_surface(
        self,
        action: NextAction,
        context: DecisionContext,
    ) -> PromptSurface:
        """Choose the prompt surface that best matches the observed manager gap."""
        metadata = action.metadata or {}
        surface_id = metadata.get("surface_id") or metadata.get("prompt_surface_id")
        if isinstance(surface_id, str) and surface_id.strip():
            return get_prompt_surface(surface_id=surface_id, repo_root=self.repo_root)

        combined = "\n".join(
            list(context.prompt_gap_signals)
            + list(context.human_feedback_signals)
            + list(context.error_signatures)
        ).lower()

        if "human checkpoint" in combined or "approval" in combined:
            return get_prompt_surface(
                surface_id="umbrella_human_gate_policy", repo_root=self.repo_root
            )
        if "bible" in combined or "constitution" in combined:
            return get_prompt_surface(
                surface_id="ouroboros_bible", repo_root=self.repo_root
            )
        if any(
            keyword in combined
            for keyword in (
                "context",
                "tool history",
                "scratchpad",
                "cache",
                "assembly",
            )
        ):
            return get_prompt_surface(
                surface_id="ouroboros_context_assembly", repo_root=self.repo_root
            )
        if "policy" in combined:
            return get_prompt_surface(
                surface_id="umbrella_prompt_policy", repo_root=self.repo_root
            )

        return get_prompt_surface(
            surface_id="ouroboros_system_prompt", repo_root=self.repo_root
        )

    def _build_prompt_patch_proposal(
        self,
        task: ManagerTask,
        action: NextAction,
        context: DecisionContext,
    ) -> PromptPatchProposal:
        """Create and persist the reviewable prompt patch proposal."""
        metadata = action.metadata or {}
        surface = self._select_prompt_surface(action, context)
        evidence = metadata.get("evidence")
        if isinstance(evidence, list):
            normalized_evidence = [str(item) for item in evidence]
        else:
            normalized_evidence = (
                list(context.prompt_gap_signals)
                + list(context.human_feedback_signals)
                + list(context.error_signatures[:3])
            )

        proposal = propose_prompt_patch(
            surface,
            repo_root=self.repo_root,
            version_store_dir=self.prompt_versions_dir,
            task_id=task.id,
            rationale=str(metadata.get("rationale") or action.description),
            expected_behavioral_effect=str(
                metadata.get("expected_behavioral_effect")
                or "Improve manager behavior without changing workspace logic first."
            ),
            evidence=normalized_evidence,
            proposed_content=metadata.get("proposed_content")
            if isinstance(metadata.get("proposed_content"), str)
            else None,
            rollback_reference=metadata.get("rollback_reference")
            if isinstance(metadata.get("rollback_reference"), str)
            else None,
        )
        save_prompt_patch_proposal(proposal, self.prompt_proposals_dir)
        task.state.last_prompt_proposal_id = proposal.id
        return proposal

    def _execute_prompt_rewrite(
        self,
        task: ManagerTask,
        action: NextAction,
        context: DecisionContext,
    ) -> ActionResult:
        """Prepare a governed prompt rewrite with checkpoint and audit data."""
        decision = should_patch_manager(context)
        trace_decision(decision)

        proposal = self._build_prompt_patch_proposal(task, action, context)
        checkpoint = prepare_self_improvement(
            context,
            "prompt_stack_rewrite",
            proposal.expected_behavioral_effect,
            self.checkpoint_dir,
        )

        task.state.active_self_improvement_checkpoint_id = checkpoint.id
        action.checkpoint_id = checkpoint.id
        action.prompt_proposal_id = proposal.id
        task.state.last_decision = action

        if proposal.requires_human_checkpoint:
            request = create_human_checkpoint_request(
                task_id=task.id,
                proposal=proposal,
                checkpoint_dir=self.human_checkpoint_dir,
                manager_checkpoint_id=checkpoint.id,
                description=f"Approve prompt rewrite for {proposal.surface.label}",
            )
            task.state.pending_human_checkpoint_id = request.id

            if self.state_machine.can_transition_to(
                ManagerPhase.SELF_IMPROVEMENT_PENDING
            ):
                self.state_machine.transition_to(
                    ManagerPhase.SELF_IMPROVEMENT_PENDING,
                    reason="Prompt rewrite requires human checkpoint",
                )

            escalate_to_human(
                self.control_state_dir / "escalations",
                context,
                EscalationReason.HUMAN_REQUESTED,
                request.description,
                ActionType.REWRITE_PROMPT_STACK,
            )

            return ActionResult(
                action=action,
                outcome="partial",
                summary="Prompt rewrite proposal created; awaiting human checkpoint",
                details={
                    "proposal_id": proposal.id,
                    "surface_id": proposal.surface.id,
                    "checkpoint_id": request.id,
                    "manager_checkpoint_id": checkpoint.id,
                    "diff_text": proposal.diff_text,
                    "notification_message": request.notification_message,
                },
            )

        if self.state_machine.can_transition_to(ManagerPhase.SELF_IMPROVEMENT_PENDING):
            self.state_machine.transition_to(
                ManagerPhase.SELF_IMPROVEMENT_PENDING,
                reason="Prompt rewrite prepared",
            )
        if self.state_machine.can_transition_to(ManagerPhase.SELF_IMPROVEMENT_APPROVED):
            self.state_machine.transition_to(
                ManagerPhase.SELF_IMPROVEMENT_APPROVED,
                reason="Prompt rewrite approved automatically",
            )

        return ActionResult(
            action=action,
            outcome="success",
            summary="Prompt rewrite proposal created",
            details={
                "proposal_id": proposal.id,
                "surface_id": proposal.surface.id,
                "manager_checkpoint_id": checkpoint.id,
                "diff_text": proposal.diff_text,
            },
        )

    def _execute_self_improvement(
        self,
        task: ManagerTask,
        action: NextAction,
    ) -> ActionResult:
        """Execute self-improvement with real code rewriting."""
        from umbrella.control_plane.code_improver import improve_system_from_context

        context = self._build_context(task)

        log.info("=" * 60)
        log.info("🧠 SELF-IMPROVEMENT - Analyzing and improving system")
        log.info("=" * 60)
        log.info(f"Task: {task.id}")
        log.info(f"Type: {action.self_improvement_type or 'general'}")
        log.info("")

        # Gather context for improvement
        improvement_context = {
            "task_id": task.id,
            "instance_path": str(context.instance_path)
            if hasattr(context, "instance_path")
            else None,
            "total_iterations": context.total_iterations
            if hasattr(context, "total_iterations")
            else 0,
            "no_progress_iterations": context.no_progress_iterations
            if hasattr(context, "no_progress_iterations")
            else 0,
        }

        if task.state.last_decision:
            improvement_context["last_decision"] = {
                "action_type": task.state.last_decision.action_type.value,
                "description": task.state.last_decision.description,
            }

        # Add eval info
        if self.last_eval_record:
            improvement_context["eval"] = {
                "overall_score": self.last_eval_record.overall_score,
                "task_success": self.last_eval_record.task_success,
                "output_quality": self.last_eval_record.output_quality,
                "manager_level_issues": self.last_eval_record.manager_level_issues,
            }

        # Add comparison info if available
        if hasattr(context, "last_comparison") and context.last_comparison:
            improvement_context["comparison"] = {
                "score_delta": context.last_comparison.score_delta
                if hasattr(context.last_comparison, "score_delta")
                else 0,
                "overall_improvement": context.last_comparison.overall_improvement
                if hasattr(context.last_comparison, "overall_improvement")
                else "",
            }

        if self.last_run_result and self.last_run_result.errors:
            improvement_context["run_errors"] = list(self.last_run_result.errors)

        if self.last_inspection:
            improvement_context["inspection"] = {
                "manifest": self.last_inspection.get("manifest", {}),
                "artifacts": self.last_inspection.get("artifacts", {}),
                "log_summary": self.last_inspection.get("log_summary", {}),
                "error_signatures": self.last_inspection.get("error_signatures", []),
                "raw_tail": self.last_inspection.get("raw_tail", []),
            }

        # Apply real improvements
        try:
            instance_path = (
                context.instance_path if hasattr(context, "instance_path") else None
            )
            if not instance_path:
                raise ValueError("No instance path available for improvement")

            summary = improve_system_from_context(
                task_id=task.id,
                instance_path=instance_path,
                repo_root=self.repo_root,
                context=improvement_context,
            )

            applied_count = summary.get("applied_count", 0)
            failed_count = summary.get("failed_count", 0)

            log.info("")
            log.info("=" * 60)
            log.info(f"✓ Self-improvement complete: {applied_count} changes applied")
            if failed_count > 0:
                log.info(f"  ⚠ {failed_count} changes failed")
            log.info("=" * 60)

            if applied_count > 0:
                changed_files = [
                    str((instance_path / str(change.get("file", "")).strip()).resolve())
                    for change in summary.get("applied", [])
                    if str(change.get("file", "")).strip()
                ]
                patch_result = WorkspacePatchResult(
                    applied=True,
                    summary=(
                        f"Applied {applied_count} self-improvement change(s) to the instance; "
                        "promotion deferred until rerun evaluation proves the gain"
                    ),
                    changed_files=changed_files,
                    graph_changed=any(
                        Path(path).as_posix().endswith("graph/topology.toml")
                        for path in changed_files
                    ),
                )
                self._record_patch_result(task, patch_result)

                # Show what was changed
                for change in summary.get("applied", [])[:5]:
                    log.info(f"  - {change['file']}: {change['description']}")

                # Create checkpoint for tracking
                checkpoint = prepare_self_improvement(
                    context,
                    action.self_improvement_type or "code_improvement",
                    f"Applied {applied_count} code improvements",
                    self.checkpoint_dir,
                )

                result = ActionResult(
                    action=action,
                    outcome="success",
                    summary=f"Self-improvement applied {applied_count} code changes",
                    details=summary,
                )

                # Transition to complete
                if self.state_machine.can_transition_to(
                    ManagerPhase.SELF_IMPROVEMENT_COMPLETE
                ):
                    self.state_machine.transition_to(
                        ManagerPhase.SELF_IMPROVEMENT_COMPLETE,
                        reason=f"Self-improvement completed: {applied_count} changes applied",
                    )

                return result

            else:
                self._clear_patch_result(task)
                # No improvements applied, fall back to annotation
                log.info("No improvements applied, using annotation fallback")
                checkpoint = prepare_self_improvement(
                    context,
                    action.self_improvement_type or "general",
                    "No improvements generated - annotation only",
                    self.checkpoint_dir,
                )

                result = ActionResult(
                    action=action,
                    outcome="partial",
                    summary="Self-improvement: no changes applied (annotation only)",
                )

                if self.state_machine.can_transition_to(
                    ManagerPhase.SELF_IMPROVEMENT_COMPLETE
                ):
                    self.state_machine.transition_to(
                        ManagerPhase.SELF_IMPROVEMENT_COMPLETE,
                        reason="Self-improvement annotation recorded",
                    )

                return result

        except Exception as e:
            self._clear_patch_result(task)
            log.warning(f"Self-improvement failed: {e}")
            log.warning("Falling back to annotation-only mode")

            # Fallback to annotation
            checkpoint = prepare_self_improvement(
                context,
                action.self_improvement_type or "general",
                f"Self-improvement failed: {e}",
                self.checkpoint_dir,
            )

            return ActionResult(
                action=action,
                outcome="partial",
                summary=f"Self-improvement failed: {e}",
            )

    def _execute_escalation(
        self,
        task: ManagerTask,
        context: DecisionContext,
    ) -> ActionResult:
        """Execute escalation to human using terminal check."""
        from umbrella.control_plane.terminal_check import TerminalCheckpointAdapter

        # Build prompt for human
        description = (
            decision.rationale.reason[:500]
            if hasattr(decision, "rationale")
            else "Human input needed"
        )
        stage = context.last_phase if hasattr(context, "last_phase") else "escalation"

        # Use terminal check instead of file-based escalation
        adapter = TerminalCheckpointAdapter(self.control_state_dir)

        # Build context for auto-decision
        eval_context = {}
        if self.last_eval_record:
            eval_context["eval_context"] = {
                "overall_score": self.last_eval_record.overall_score,
                "task_success": self.last_eval_record.task_success,
                "output_quality": self.last_eval_record.output_quality,
            }

        # Request terminal check with 60 second timeout
        result = adapter.request_review(
            task_id=task.id,
            stage=stage,
            prompt=f"""ESCALATION - Human Input Needed

{description}

**Context:**
- Task: {task.brief.summary[:100] if task.brief else "Unknown"}...
- Iterations: {context.total_iterations if hasattr(context, "total_iterations") else "N/A"}
- Phase: {stage}

**Options:**
1. Approve - Continue with current approach
2. Reject - Stop and wait for further instructions
3. Skip - Let Umbrella/Ouroboros decide

Your input:""",
            context=eval_context,
            timeout_seconds=60.0,  # 1 minute
        )

        # Process result
        if result.timed_out:
            # Use auto-decision
            log.info(
                "Terminal check timed out, using auto-decision: %s",
                result.auto_decision,
            )

            # Based on auto-decision, either continue or wait
            if "approved" in (result.auto_decision or "").lower():
                return ActionResult(
                    action=NextAction(
                        action_type=ActionType.RUN_WORKSPACE,
                        description=f"Auto-approved: {result.auto_decision}",
                    ),
                    outcome="success",
                    summary=f"Escalation auto-resolved: {result.auto_decision}",
                )
            else:
                # Still wait for human but with note about auto-decision
                if self.state_machine.can_transition_to(ManagerPhase.ESCALATED):
                    self.state_machine.transition_to(
                        ManagerPhase.ESCALATED,
                        reason="Escalation awaiting human (auto-decision: retry)",
                    )

                return ActionResult(
                    action=decision.action
                    if hasattr(decision, "action")
                    else NextAction(
                        action_type=ActionType.WAIT_FOR_INPUT,
                        description=result.auto_decision or "Awaiting human",
                    ),
                    outcome="partial",
                    summary=f"Escalation with auto-decision: {result.auto_decision}",
                )

        elif result.response == "approve":
            # Human approved - continue
            log.info("Human approved escalation, continuing execution")
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.RUN_WORKSPACE,
                    description="Human approved - continuing",
                ),
                outcome="success",
                summary="Human approved escalation",
            )

        elif result.response == "reject":
            # Human rejected - wait for further input
            log.info("Human rejected escalation, awaiting further instructions")

            if self.state_machine.can_transition_to(ManagerPhase.ESCALATED):
                self.state_machine.transition_to(
                    ManagerPhase.ESCALATED,
                    reason="Human rejected - awaiting instructions",
                )

            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="Human rejected - awaiting instructions",
                ),
                outcome="partial",
                summary="Human rejected escalation",
            )

        else:
            # Skip - let Umbrella decide
            log.info("Human skipped - Umbrella will decide")
            # Continue with knowledge retrieval
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.RECORD_LESSON,
                    description="Human skipped - recording lesson and continuing",
                ),
                outcome="success",
                summary="Human skipped, Umbrella continuing",
            )

    def _record_lesson_and_continue(
        self,
        task: ManagerTask,
        context: DecisionContext,
    ) -> ActionResult:
        """Record a lesson and continue."""
        # Extract lesson from inspection data
        if self.last_inspection and self.last_run_result:
            self._record_lesson_from_run(
                task, self.last_inspection, self.last_run_result
            )

        self.state_machine.transition_to(
            ManagerPhase.LESSON_RECORDED,
            reason="Structured lesson stored in memory",
        )

        quality_ok, quality_reason = self._completion_quality_ok()

        if context.last_run_outcome == "success" and quality_ok:
            completion_review = self._request_final_completion_review(task)
            if completion_review is not None:
                return completion_review
            return self._complete_task(
                task,
                NextAction(
                    action_type=ActionType.COMPLETE_TASK,
                    description="Task completed successfully",
                ),
            )

        if quality_reason:
            log.info("Completion gate blocked: %s", quality_reason)

        return self._schedule_workspace_rerun(
            task,
            reason=(
                "Recorded lesson, but another workspace iteration is required"
                + (f" ({quality_reason})" if quality_reason else "")
            ),
        )

    def _run_workspace(
        self,
        task: ManagerTask,
        context: DecisionContext,
    ) -> ActionResult:
        """Run the workspace."""
        if not self.active_instance:
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.FAIL_TASK,
                    description="Missing workspace instance",
                ),
                outcome="failure",
                summary="Cannot run workspace without a prepared instance",
            )

        try:
            update_instance_metadata(
                self.active_instance.path,
                {
                    "status": "running",
                    "last_manager_phase": task.state.phase.value,
                },
            )
        except Exception:
            log.debug(
                "Failed to mark instance %s as running",
                self.active_instance.path,
                exc_info=True,
            )

        if self.state_machine.can_transition_to(ManagerPhase.KNOWLEDGE_RETRIEVED):
            self.state_machine.transition_to(
                ManagerPhase.KNOWLEDGE_RETRIEVED,
                reason="Preparing rerun after patch/lesson",
            )
            return self._handle_knowledge_retrieved(task)

        if self.state_machine.can_transition_to(ManagerPhase.WORKSPACE_RUNNING):
            self.state_machine.transition_to(
                ManagerPhase.WORKSPACE_RUNNING, reason="Executing workspace"
            )
            return self._handle_workspace_running(task)

        return ActionResult(
            action=NextAction(
                action_type=ActionType.FAIL_TASK, description="Invalid run transition"
            ),
            outcome="failure",
            summary=f"Cannot transition to a runnable phase from {task.state.phase.value}",
        )

    def _schedule_workspace_rerun(
        self,
        task: ManagerTask,
        *,
        reason: str,
    ) -> ActionResult:
        """Queue the next workspace iteration without hiding another run in the same step."""
        rerun_action = NextAction(
            action_type=ActionType.RUN_WORKSPACE,
            description="Run workspace again with the latest task context",
        )
        task.state.last_decision = rerun_action

        if self.state_machine.can_transition_to(ManagerPhase.KNOWLEDGE_RETRIEVED):
            self.state_machine.transition_to(
                ManagerPhase.KNOWLEDGE_RETRIEVED,
                reason=reason,
            )
        else:
            self.state_machine.force_phase(
                ManagerPhase.KNOWLEDGE_RETRIEVED,
                reason=reason,
            )

        return ActionResult(
            action=rerun_action,
            outcome="partial",
            summary=reason,
            suggested_next_actions=[ActionType.RUN_WORKSPACE],
        )

    def _complete_task(self, task: ManagerTask, action: NextAction) -> ActionResult:
        """Complete the task."""
        if action.action_type == ActionType.COMPLETE_TASK:
            task.status = "complete"
            self.state_machine.transition_to(ManagerPhase.TASK_COMPLETE)
            outcome = "success"
        else:
            task.status = "failed"
            self.state_machine.transition_to(ManagerPhase.TASK_FAILED)
            outcome = "failure"

        task.completed_at = time.time()
        if self.last_run_result:
            preferred_report = next(
                (
                    artifact
                    for artifact in self.last_run_result.artifacts
                    if artifact.artifact_type.value == "report"
                ),
                None,
            )
            if preferred_report is not None:
                task.final_artifact_path = preferred_report.path
            task.final_summary = (
                self.last_run_result.summary or self.last_run_result.final_answer
            )

        if task.state.current_instance_path is not None:
            try:
                update_instance_metadata(
                    Path(task.state.current_instance_path),
                    {
                        "status": task.status,
                        "task_completed_at": datetime.fromtimestamp(
                            task.completed_at,
                            timezone.utc,
                        ).isoformat(),
                    },
                )
            except Exception:
                log.debug(
                    "Failed to mark instance %s as %s",
                    task.state.current_instance_path,
                    task.status,
                    exc_info=True,
                )

        # Finalize trace
        self.trace_manager.finalize_trace(task.id)

        return ActionResult(
            action=action,
            outcome=outcome,
            summary=f"Task {outcome}: {action.description}",
        )

    def _completion_quality_ok(self) -> tuple[bool, str]:
        """Return whether the latest evaluation is strong enough for final completion."""
        eval_record = self.last_eval_record
        if eval_record is None:
            return False, "No evaluation record available for completion"

        threshold = self.runtime_config.quality_completion_threshold
        score_ok = (
            eval_record.overall_score >= threshold
            or self._completion_near_threshold_grace(
                eval_record,
                threshold,
            )
        )
        task_ok = eval_record.task_success == TaskSuccessRating.COMPLETE
        output_ok = eval_record.output_quality in (
            OutputQualityRating.GOOD,
            OutputQualityRating.EXCELLENT,
        )

        if score_ok and task_ok and output_ok:
            return True, ""

        reasons: list[str] = []
        if not task_ok:
            reasons.append(f"task_success={eval_record.task_success.value}")
        if not output_ok:
            reasons.append(f"output_quality={eval_record.output_quality.value}")
        if not score_ok:
            reasons.append(
                f"score={eval_record.overall_score:.2f} < threshold={threshold:.2f}"
            )
        return False, "; ".join(reasons)

    def _completion_near_threshold_grace(
        self,
        eval_record: EvaluationRecord,
        threshold: float,
    ) -> bool:
        """Allow the first strong run to complete without a false stability penalty."""
        if threshold <= 0:
            return True
        if eval_record.overall_score >= threshold:
            return True

        score_gap = threshold - eval_record.overall_score
        if score_gap > 0.02:
            return False
        if eval_record.task_success != TaskSuccessRating.COMPLETE:
            return False
        if eval_record.output_quality not in (
            OutputQualityRating.GOOD,
            OutputQualityRating.EXCELLENT,
        ):
            return False
        if eval_record.stability != StabilityRating.UNKNOWN:
            return False
        if eval_record.iterations_limit_reached:
            return False
        if eval_record.raw_log_inspection_required:
            return False
        if not eval_record.structured_summary_sufficient:
            return False
        if eval_record.manager_level_issues:
            return False
        return True

    def _request_final_completion_review(
        self, task: ManagerTask
    ) -> ActionResult | None:
        """Require an explicit human approval in the terminal before final completion."""
        timeout = float(self.runtime_config.human_review_timeout_seconds or 0)
        if timeout <= 0:
            return None

        from umbrella.control_plane.terminal_check import (
            HumanResponse,
            TerminalCheckpointAdapter,
        )

        eval_record = self.last_eval_record
        eval_context: dict[str, Any] = {}
        if eval_record is not None:
            eval_context = {
                "eval_context": {
                    "overall_score": eval_record.overall_score,
                    "task_success": eval_record.task_success.value,
                    "output_quality": eval_record.output_quality.value,
                }
            }

        eval_score_text = (
            f"{eval_record.overall_score:.2f}" if eval_record is not None else "unknown"
        )
        task_success_text = (
            eval_record.task_success.value if eval_record is not None else "unknown"
        )
        output_quality_text = (
            eval_record.output_quality.value if eval_record is not None else "unknown"
        )

        prompt = (
            "Final completion review.\n\n"
            f"Task: {task.brief.summary[:160] if task.brief else task.id}\n"
            f"Workspace: {task.state.current_workspace_id or 'unknown'}\n"
            f"Eval score: {eval_score_text}\n"
            f"Task success: {task_success_text}\n"
            f"Output quality: {output_quality_text}\n\n"
            "Approve only if the output is actually ready to stop on.\n"
            "Respond:\n"
            "  1. Approve\n"
            "  2. Reject\n"
            "  3. Skip"
        )

        run_id = (
            self.last_run_result.run_id
            if self.last_run_result and self.last_run_result.run_id
            else "latest"
        )
        checkpoint_id = f"{task.id}_final_completion_{run_id}"
        checkpoint_dir = self.control_state_dir / "checkpoints"
        existing = HumanCheckpoint.load(checkpoint_id, checkpoint_dir)

        if existing and existing.status == "approved":
            return None
        if existing and existing.status == "rejected":
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="Human rejected final completion",
                ),
                outcome="partial",
                summary=f"Final completion review rejected: {existing.human_response or 'no response provided'}",
                details={"checkpoint_id": checkpoint_id},
            )

        if not sys.stdin.isatty():
            checkpoint = existing or HumanCheckpoint(
                checkpoint_id=checkpoint_id,
                task_id=task.id,
                checkpoint_type="final_completion_review",  # type: ignore[arg-type]
                description="Final human approval is required before task completion",
                proposed_change={
                    "workspace_id": task.state.current_workspace_id or "unknown",
                    "run_id": run_id,
                    "eval_score": eval_score_text,
                    "task_success": task_success_text,
                    "output_quality": output_quality_text,
                },
                checkpoint_dir=checkpoint_dir,
            )
            if existing is None:
                checkpoint._save()
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="Awaiting final human approval",
                ),
                outcome="partial",
                summary="Final completion review is required before Umbrella can stop",
                details={"checkpoint_id": checkpoint_id},
            )

        adapter = TerminalCheckpointAdapter(self.control_state_dir / "terminal_reviews")
        review_result = adapter.request_review(
            task_id=task.id,
            stage="final_completion_review",
            prompt=prompt,
            context=eval_context,
            timeout_seconds=min(timeout, 300.0),
        )

        if review_result.response == HumanResponse.APPROVE:
            checkpoint = existing or HumanCheckpoint(
                checkpoint_id=checkpoint_id,
                task_id=task.id,
                checkpoint_type="final_completion_review",  # type: ignore[arg-type]
                description="Final human approval is required before task completion",
                proposed_change={
                    "workspace_id": task.state.current_workspace_id or "unknown",
                    "run_id": run_id,
                    "eval_score": eval_score_text,
                    "task_success": task_success_text,
                    "output_quality": output_quality_text,
                },
                checkpoint_dir=checkpoint_dir,
            )
            checkpoint.approve(
                getattr(review_result, "human_input", None) or "Approved in terminal"
            )
            return None

        if review_result.response == HumanResponse.REJECT:
            checkpoint = existing or HumanCheckpoint(
                checkpoint_id=checkpoint_id,
                task_id=task.id,
                checkpoint_type="final_completion_review",  # type: ignore[arg-type]
                description="Final human approval is required before task completion",
                proposed_change={
                    "workspace_id": task.state.current_workspace_id or "unknown",
                    "run_id": run_id,
                    "eval_score": eval_score_text,
                    "task_success": task_success_text,
                    "output_quality": output_quality_text,
                },
                checkpoint_dir=checkpoint_dir,
            )
            checkpoint.reject(
                getattr(review_result, "human_input", None) or "Rejected in terminal"
            )
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="Human rejected final completion",
                ),
                outcome="partial",
                summary="Human rejected final completion; awaiting further instructions",
                details={"checkpoint_id": checkpoint_id},
            )

        if review_result.timed_out:
            checkpoint = existing or HumanCheckpoint(
                checkpoint_id=checkpoint_id,
                task_id=task.id,
                checkpoint_type="final_completion_review",  # type: ignore[arg-type]
                description="Final human approval is required before task completion",
                proposed_change={
                    "workspace_id": task.state.current_workspace_id or "unknown",
                    "run_id": run_id,
                    "eval_score": eval_score_text,
                    "task_success": task_success_text,
                    "output_quality": output_quality_text,
                },
                checkpoint_dir=checkpoint_dir,
            )
            if existing is None:
                checkpoint._save()
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.WAIT_FOR_INPUT,
                    description="Final completion review timed out",
                ),
                outcome="partial",
                summary="Final completion needs explicit human approval",
                details={"checkpoint_id": checkpoint_id},
            )

        checkpoint = existing or HumanCheckpoint(
            checkpoint_id=checkpoint_id,
            task_id=task.id,
            checkpoint_type="final_completion_review",  # type: ignore[arg-type]
            description="Final human approval is required before task completion",
            proposed_change={
                "workspace_id": task.state.current_workspace_id or "unknown",
                "run_id": run_id,
                "eval_score": eval_score_text,
                "task_success": task_success_text,
                "output_quality": output_quality_text,
            },
            checkpoint_dir=checkpoint_dir,
        )
        if existing is None:
            checkpoint._save()
        return ActionResult(
            action=NextAction(
                action_type=ActionType.WAIT_FOR_INPUT,
                description="Human skipped final completion review",
            ),
            outcome="partial",
            summary="Final completion was skipped; explicit approval is still required",
            details={"checkpoint_id": checkpoint_id},
        )

    # =======================================================================
    # Context Building
    # =======================================================================

    def _build_retrieval_context(self, card: RetrievalCard | None) -> str:
        """Compress retrieval guidance into a compact promptable context block."""
        if card is None:
            return ""

        parts = [f"Recommended pattern: {card.recommended_pattern}"]
        if card.key_symbols:
            parts.append("Key symbols: " + ", ".join(card.key_symbols[:5]))
        if card.key_files:
            parts.append(
                "Key files: " + ", ".join(str(path) for path in card.key_files[:5])
            )
        if card.example_usage:
            parts.append("Example usage: " + " | ".join(card.example_usage[:2]))
        if card.anti_patterns:
            parts.append("Avoid: " + " | ".join(card.anti_patterns[:2]))
        return "\n".join(parts)

    def _build_context(self, task: ManagerTask) -> DecisionContext:
        """Build decision context for a task."""
        context = build_decision_context(
            task.brief,
            task.state,
            task.state.current_workspace_id,
            None,  # last_run_result
            self.memory_store,
            self.policy_engine,
        )
        context.instance_path = task.state.current_instance_path

        # Add memory stats
        if self.memory_store:
            stats = self.memory_store.get_stats()
            context.active_gaps = stats.active_gaps
            context.relevant_lessons = stats.total_lessons

        # Inject retrieval card content into decision context
        if self.last_retrieval_card:
            card = self.last_retrieval_card
            context.retrieval_recommended_pattern = card.recommended_pattern or ""
            context.retrieval_key_symbols = (
                list(card.key_symbols[:5]) if card.key_symbols else []
            )
            context.retrieval_key_files = (
                [str(p) for p in card.key_files[:5]] if card.key_files else []
            )
            context.retrieval_anti_patterns = (
                list(card.anti_patterns[:3]) if card.anti_patterns else []
            )
            context.retrieval_confidence = getattr(card, "confidence", 0.0)

        # Add last run outcome
        if self.last_run_result:
            if self.last_run_result.status == WorkspaceRunStatus.COMPLETED:
                context.last_run_outcome = "success"
            elif self.last_run_result.status == WorkspaceRunStatus.FAILED:
                context.last_run_outcome = "failure"
            else:
                context.last_run_outcome = "partial"
            if self.last_run_result.errors:
                context.error_signatures = self.last_run_result.errors
            if task.state.retrieval_query:
                context.retrieval_failures = (
                    0 if task.state.retrieval_hit_count > 0 else 1
                )
            else:
                context.retrieval_failures = 0

        if task.state.iteration_count > 1 and context.last_run_outcome != "success":
            context.no_progress_iterations = max(0, task.state.iteration_count - 1)

        # Wire iteration and cost tracking so decision policies see real values
        context.total_iterations = task.state.iteration_count
        if self.last_run_result and self.last_run_result.total_tokens:
            cost_per_1k = 0.002  # conservative estimate
            context.cost_so_far_usd += (
                self.last_run_result.total_tokens / 1000.0
            ) * cost_per_1k

        # Propagate eval score for quality-aware decisions
        if self.last_eval_record:
            context.last_eval_score = self.last_eval_record.overall_score
            context.completion_gate_passed = self._completion_quality_ok()[0]
        context.quality_completion_threshold = (
            self.runtime_config.quality_completion_threshold
        )

        return context

    def _build_context_with_inspection(
        self,
        task: ManagerTask,
        inspection_data: dict[str, Any],
    ) -> DecisionContext:
        """Build context with inspection data."""
        context = self._build_context(task)

        # Add inspection details
        manifest = inspection_data.get("manifest", {})
        context.run_manifest = {
            "status": manifest.get("status", "unknown"),
            "errors": manifest.get("errors", []),
            "warnings": manifest.get("warnings", []),
            "tokens": manifest.get("total_tokens", 0),
            "duration": manifest.get("duration_seconds", 0),
        }

        context.artifact_summary = inspection_data.get("artifacts", {}).get(
            "final_answer"
        )

        error_signatures = inspection_data.get("error_signatures", [])
        context.error_signatures = error_signatures

        return context

    # =======================================================================
    # TASK_MAIN.md Handling
    # =======================================================================

    def _initialize_task_main(
        self, instance: WorkspaceInstance, brief: TaskBrief
    ) -> None:
        """Initialize TASK_MAIN.md from task brief."""
        task_main_path = instance.path / "TASK_MAIN.md"

        content = f"""# {brief.summary}

## Task Details

- **Task ID**: {brief.task_id}
- **Task Type**: {brief.task_class.value}
- **Created**: {time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))}

## Original Request

{brief.original_input}

## Requirements

"""

        if brief.requirements:
            content += "\n".join(f"- {req}" for req in brief.requirements)
            content += "\n"

        content += """
## Success Criteria

"""

        if brief.success_criteria:
            content += "\n".join(
                f"- {criterion}" for criterion in brief.success_criteria
            )
            content += "\n"

        content += f"""
## Constraints

{chr(10).join(f"- {c}" for c in brief.constraints) if brief.constraints else "- None specified"}

## Notes

This document serves as the canonical mission file for this task.
Any material changes to the mission description should be reflected here.
"""

        task_main_path.write_text(content, encoding="utf-8")
        log.info(f"Initialized TASK_MAIN.md at {task_main_path}")

    def update_task_main(self, task: ManagerTask, update: str, reason: str) -> bool:
        """Update TASK_MAIN.md with human checkpoint.

        Args:
            task: The task
            update: The update to apply
            reason: Why this update is being made

        Returns:
            True if updated, False if blocked/rejected
        """
        # Check for human checkpoint
        context = self._build_context(task)
        checkpoint_result = self._check_human_checkpoint(
            task,
            context,
            "task_main_change",
            f"TASK_MAIN.md change requires approval: {reason}",
        )

        if checkpoint_result and checkpoint_result.outcome != "success":
            return False

        # Apply the update
        instance = self.active_instance
        if not instance:
            return False

        task_main_path = instance.path / "TASK_MAIN.md"
        if not task_main_path.exists():
            return False

        current_content = task_main_path.read_text(encoding="utf-8")
        updated_content = (
            current_content
            + f"\n\n## Update ({time.strftime('%Y-%m-%d %H:%M:%S')})\n\n{update}\n"
        )

        task_main_path.write_text(updated_content, encoding="utf-8")
        log.info(f"Updated TASK_MAIN.md: {reason}")
        return True

    # =======================================================================
    # Lesson Recording
    # =======================================================================

    @staticmethod
    def _priority_from_score(score: float) -> int:
        return max(1, min(10, int(round(score * 10))))

    @staticmethod
    def _evidence_summary(*parts: str) -> str:
        cleaned = [part.strip() for part in parts if part and part.strip()]
        return " | ".join(cleaned) if cleaned else "No evidence captured."

    def _record_lesson_from_run(
        self,
        task: ManagerTask,
        inspection_data: dict[str, Any],
        run_result: WorkspaceRunResult,
    ) -> None:
        """Record a lesson from a workspace run."""
        if not self.memory_store:
            return

        lesson_type = (
            "failure_analysis"
            if run_result.status == WorkspaceRunStatus.FAILED
            else "success_pattern"
        )
        manifest = inspection_data.get("manifest", {})
        error_signatures = inspection_data.get("error_signatures", [])
        artifact_paths = [
            str(path) for path in inspection_data.get("artifacts", {}).get("paths", [])
        ]

        lesson = WorkspaceLessonRecord(
            id=generate_lesson_id(),
            task_id=task.id,
            workspace_id=task.state.current_workspace_id or "unknown",
            change_summary=f"{lesson_type} from run {run_result.run_id}",
            expected_effect=f"Fulfill task goal: {task.brief.summary}",
            observed_effect=(
                f"Run status={run_result.status.value}; "
                f"tokens={run_result.total_tokens}; duration={run_result.duration_seconds:.2f}s"
            ),
            conclusion=(
                "Structured workspace run completed successfully"
                if run_result.status == WorkspaceRunStatus.COMPLETED
                else "Workspace run exposed a failure mode that should guide the next patch"
            ),
            evidence_summary=self._evidence_summary(
                f"errors={', '.join(error_signatures[:3])}" if error_signatures else "",
                f"artifact_count={inspection_data.get('artifacts', {}).get('count', 0)}",
                f"retrieval_hits={task.state.retrieval_hit_count}",
            ),
            tags={lesson_type, task.brief.task_class.value},
            repeat_tags=["reuse_current_workspace_pattern"]
            if run_result.status == WorkspaceRunStatus.COMPLETED
            else [],
            avoid_tags=error_signatures[:3]
            if run_result.status == WorkspaceRunStatus.FAILED
            else [],
            priority=self._priority_from_score(
                1.0 if run_result.status == WorkspaceRunStatus.COMPLETED else 0.4
            ),
            files_changed=list(task.state.last_patch_files),
            metadata={
                "run_id": run_result.run_id,
                "run_manifest_status": manifest.get("status"),
                "retrieval_summary": task.state.retrieval_summary,
            },
            workspace_version=run_result.run_id,
        )
        if artifact_paths:
            lesson.metadata["artifact_paths"] = artifact_paths

        # Store lesson
        self.memory_store.add_lesson(lesson)
        log.info(f"Recorded lesson {lesson.id} for task {task.id}")

    def _store_eval_summary_in_memory(
        self,
        task: ManagerTask,
        eval_record: EvaluationRecord,
    ) -> None:
        """Store evaluation summary in memory for future reference.

        Args:
            task: Current task
            eval_record: Evaluation record to store
        """
        if not self.memory_store:
            return

        # Create a lesson from the evaluation
        lesson_type = (
            "eval_success"
            if eval_record.task_success == TaskSuccessRating.COMPLETE
            else "eval_failure"
        )

        lesson = WorkspaceLessonRecord(
            id=generate_lesson_id(),
            task_id=task.id,
            workspace_id=eval_record.workspace_id,
            change_summary=f"Evaluation {lesson_type}: score {eval_record.overall_score:.2f}",
            expected_effect=f"Task should converge on {task.brief.summary}",
            observed_effect=(
                f"success={eval_record.task_success.value}; "
                f"quality={eval_record.output_quality.value}; "
                f"stability={eval_record.stability.value}"
            ),
            conclusion=(
                f"Evaluation score={eval_record.overall_score:.2f}; "
                f"cost=${eval_record.total_cost_usd:.4f}; tokens={eval_record.total_tokens}"
            ),
            evidence_summary=self._evidence_summary(
                "; ".join(eval_record.evidence[:3]),
                f"retrieval_useful={eval_record.retrieval_was_useful}",
                f"retrieval_hits_used={eval_record.retrieval_hits_used}",
            ),
            tags={"evaluation", lesson_type, eval_record.task_success.value},
            priority=self._priority_from_score(eval_record.overall_score),
            files_changed=list(task.state.last_patch_files),
            metadata={
                "run_id": eval_record.run_id,
                "retrieval_was_useful": eval_record.retrieval_was_useful,
                "manager_level_issues": list(eval_record.manager_level_issues),
            },
        )

        # Store in memory
        self.memory_store.add_lesson(lesson)
        log.info(
            f"Stored eval summary {lesson.id} with score {eval_record.overall_score:.2f}"
        )

    def _consider_promotion(
        self,
        task: ManagerTask,
        comparison: ComparisonReport,
        run_result: WorkspaceRunResult,
    ) -> None:
        """Consider whether this improvement should be promoted to seed.

        Args:
            task: Current task
            comparison: Comparison report showing improvement
            run_result: The run result that produced the improvement
        """
        if not self.baseline_eval_record or not self.last_eval_record:
            return

        # Build promotion candidate
        try:
            candidate = build_promotion_candidate(
                baseline=self.baseline_eval_record,
                comparison=self.last_eval_record,
                comparison_report=comparison,
                patch_description=f"Improvement from run {run_result.run_id}",
                changed_files=[
                    Path(path)
                    for path in (
                        self.last_patch_result.changed_files
                        if self.last_patch_result
                        else []
                    )
                ],
            )

            # Emit promotion candidate created event
            emit_event(
                PromotionCandidateCreatedEvent(
                    task_id=task.id,
                    workspace_id=task.state.current_workspace_id
                    or comparison.workspace_id,
                    candidate_id=candidate.id,
                    patch_description=candidate.patch_description,
                    improvement_magnitude=candidate.improvement_magnitude,
                    generalizability_score=candidate.generalizability_score,
                )
            )

            # Decide on promotion
            decision = decide_promotion(candidate, self.promotion_policy)

            # Emit promotion decision event
            emit_event(
                PromotionDecisionEvent(
                    task_id=task.id,
                    workspace_id=task.state.current_workspace_id
                    or comparison.workspace_id,
                    candidate_id=candidate.id,
                    decision=decision.decision.value,
                    reasoning=decision.reasoning,
                    human_review_required=decision.decision
                    == PromotionEligibility.NEEDS_REVIEW,
                )
            )

            # Record in metrics
            self.metrics_registry.increment_counter("promotion_decisions")
            if decision.decision.value == "promote":
                self.metrics_registry.increment_counter("promotions_approved")

            # Store candidate for reference
            task.state.promotion_candidate = candidate

            # If human review required, create checkpoint
            if decision.decision == PromotionEligibility.NEEDS_REVIEW:
                self._create_promotion_checkpoint(task, candidate, decision)

            log.info(
                f"Promotion decision for {candidate.id}: {decision.decision.value} "
                f"(magnitude: {candidate.improvement_magnitude:.2f}, "
                f"generalizability: {candidate.generalizability_score:.2f})"
            )

            # AUTO-APPLY promotion if approved
            if decision.decision == PromotionEligibility.PROMOTE:
                log.info(
                    f"✓ Promotion candidate eligible for auto-apply (eligibility={candidate.eligibility})"
                )
                try:
                    from umbrella.evals.promotion import apply_promotion_decision

                    # Get paths - instance path contains workspace name
                    instance_path = Path(candidate.instance_path)

                    # Seed is in workspaces root with base workspace name
                    # Extract base workspace name from instance name
                    # e.g., "agent_research_instance_e7ed4dac" → "agent_research"
                    workspace_id = candidate.workspace_id
                    if "_instance_" in workspace_id:
                        base_name = workspace_id.split("_instance_")[0]
                    else:
                        base_name = workspace_id

                    seed_path = self.workspaces_root / base_name

                    log.info(f"Auto-applying promotion: {instance_path} → {seed_path}")

                    # Apply the promotion
                    applied = apply_promotion_decision(
                        candidate=candidate,
                        decision=decision,
                        seed_path=seed_path,
                        instance_path=instance_path,
                        changed_files=candidate.changed_files,
                    )

                    if applied:
                        log.info(
                            f"✓ Successfully promoted {len(candidate.changed_files)} files to seed workspace"
                        )
                        # Record successful promotion in metrics
                        self.metrics_registry.increment_counter("promotions_applied")
                        try:
                            update_instance_metadata(
                                instance_path,
                                {
                                    "status": "promoted",
                                    "promoted_to_seed": True,
                                    "promoted_at": datetime.now(
                                        timezone.utc
                                    ).isoformat(),
                                },
                            )
                        except Exception:
                            log.debug(
                                "Failed to mark instance %s as promoted",
                                instance_path,
                                exc_info=True,
                            )
                    else:
                        log.warning("✗ Promotion failed to apply")

                except Exception as e:
                    log.warning(f"Failed to auto-apply promotion: {e}")
            else:
                log.info(
                    f"✗ Promotion not eligible: {candidate.eligibility} (need 'promote')"
                )

        except Exception as e:
            log.warning(f"Promotion consideration failed: {e}")

    def _create_promotion_checkpoint(
        self,
        task: ManagerTask,
        candidate,
        decision,
    ) -> None:
        """Create a human checkpoint for promotion review.

        Args:
            task: Current task
            candidate: Promotion candidate
            decision: Promotion decision
        """
        checkpoint_id = f"{task.id}_promotion_{candidate.id}"
        checkpoint_dir = self.control_state_dir / "checkpoints"

        checkpoint = HumanCheckpoint(
            checkpoint_id=checkpoint_id,
            task_id=task.id,
            checkpoint_type="seed_promotion",  # type: ignore[arg-type]
            description=f"Review promotion candidate: {candidate.patch_description}",
            proposed_change={
                "candidate_id": candidate.id,
                "decision": decision.decision.value,
                "reasoning": decision.reasoning,
                "improvement_magnitude": candidate.improvement_magnitude,
                "generalizability_score": candidate.generalizability_score,
            },
            checkpoint_dir=checkpoint_dir,
        )
        checkpoint._save()

        log.info(f"Created promotion checkpoint {checkpoint_id} for human review")

    def promote_lesson_to_manager(
        self,
        lesson_id: str,
        reason: str,
    ) -> bool:
        """Promote a workspace lesson to manager memory.

        Args:
            lesson_id: ID of lesson to promote
            reason: Why this lesson is being promoted

        Returns:
            True if promoted, False otherwise
        """
        if not self.memory_store:
            return False

        # Get the lesson
        lesson = self.memory_store.get_lesson(lesson_id)
        if not lesson or not isinstance(lesson, WorkspaceLessonRecord):
            return False

        # Check for human checkpoint
        task = self.active_task
        if task:
            context = self._build_context(task)
            checkpoint_result = self._check_human_checkpoint(
                task,
                context,
                "lesson_promotion",
                f"Lesson promotion requires approval: {reason}",
            )
            if checkpoint_result and checkpoint_result.outcome != "success":
                return False

        # Create manager lesson
        manager_lesson = ManagerLessonRecord(
            id=generate_lesson_id(),
            task_id=task.id if task else lesson.task_id,
            change_summary=lesson.change_summary,
            workspace_id=lesson.workspace_id,
            expected_effect=lesson.expected_effect,
            observed_effect=lesson.observed_effect,
            conclusion=f"Promoted from workspace lesson: {reason}",
            evidence_summary=lesson.evidence_summary,
            tags=set(lesson.tags) | {"promoted"},
            affected_capability_area=task.brief.task_class.value if task else None,
            priority=max(lesson.priority, 1),
            repeat_tags=list(lesson.repeat_tags),
            avoid_tags=list(lesson.avoid_tags),
            metadata={"source_workspace_lesson_id": lesson.id},
        )

        self.memory_store.add_lesson(manager_lesson)
        log.info(
            f"Promoted lesson {lesson_id} to manager memory as {manager_lesson.id}"
        )
        return True

    # =======================================================================
    # Human Checkpoint Hooks
    # =======================================================================

    def _check_stage_human_review(
        self,
        task: ManagerTask,
        instance: WorkspaceInstance,
    ) -> ActionResult | None:
        """Scan instance stage notes and gate on configured human review stages.

        For each stage in ``runtime_config.human_review_stages``:
        - If a matching stage note exists and hasn't been reviewed yet,
          either block (timeout > 0) or log and auto-approve (timeout == 0).

        Returns an ``ActionResult`` that halts the loop when blocking review is
        needed, or ``None`` when processing should continue.
        """
        review_stages = self.runtime_config.human_review_stages
        if not review_stages:
            return None

        timeout = self.runtime_config.human_review_timeout_seconds

        notes_dir = instance.path / "stage_notes"
        if not notes_dir.is_dir():
            notes_dir = instance.path / "notes"
        completed_stages: set[str] = set()
        if notes_dir.is_dir():
            completed_stages.update(
                f.stem
                for f in notes_dir.iterdir()
                if f.is_file() and f.stat().st_size > 0
            )

        notifications_path = None
        if self.last_run_result is not None and getattr(
            self.last_run_result, "run_dir", None
        ):
            candidate = Path(self.last_run_result.run_dir) / "human_notifications.jsonl"
            if candidate.is_file():
                notifications_path = candidate
        if notifications_path is not None:
            try:
                for raw_line in notifications_path.read_text(
                    encoding="utf-8"
                ).splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    checkpoint = str(payload.get("checkpoint") or "").strip()
                    if checkpoint:
                        completed_stages.add(checkpoint)
            except Exception as exc:
                log.debug(
                    "Failed to parse human notifications from %s: %s",
                    notifications_path,
                    exc,
                )

        if not completed_stages:
            return None

        review_record_dir = self.control_state_dir / "stage_reviews"
        review_record_dir.mkdir(parents=True, exist_ok=True)

        for stage in review_stages:
            if stage not in completed_stages:
                continue

            review_marker = review_record_dir / f"{task.id}_{stage}.reviewed"
            if review_marker.exists():
                continue

            if timeout > 0:
                log.info("Stage '%s' requires blocking human review", stage)
                context = self._build_context(task)
                checkpoint_result = self._check_human_checkpoint(
                    task,
                    context,
                    f"stage_review_{stage}",
                    f"Stage '{stage}' requires human approval before continuing",
                )
                if checkpoint_result is not None:
                    return checkpoint_result
            else:
                log.info("Stage '%s' completed (auto-approved, non-blocking)", stage)

            review_marker.write_text(
                f"auto-approved at {time.time()}\n"
                if timeout <= 0
                else f"reviewed at {time.time()}\n",
                encoding="utf-8",
            )

        return None

    def _check_human_checkpoint(
        self,
        task: ManagerTask,
        context: DecisionContext,
        checkpoint_type: str,
        description: str,
    ) -> ActionResult | None:
        """Check if human approval is required for a risky operation.

        Args:
            task: Current task
            context: Decision context
            checkpoint_type: Type of checkpoint
            description: What requires approval

        Returns:
            ActionResult if checkpoint created, None if approved
        """
        # Check if there's a pending checkpoint for this
        checkpoint_dir = self.control_state_dir / "checkpoints"
        checkpoint_id = f"{task.id}_{checkpoint_type}"

        existing = HumanCheckpoint.load(checkpoint_id, checkpoint_dir)
        if existing and existing.status == "approved":
            # Already approved, proceed
            return None

        if existing and existing.status == "rejected":
            # Rejected, fail the action
            return ActionResult(
                action=NextAction(
                    action_type=ActionType.ESCALATE_TO_HUMAN,
                    description="Checkpoint rejected",
                ),
                outcome="partial",
                summary=f"Checkpoint rejected: {existing.human_response}",
            )

        # Create new checkpoint
        checkpoint = HumanCheckpoint(
            checkpoint_id=checkpoint_id,
            task_id=task.id,
            checkpoint_type=checkpoint_type,  # type: ignore[arg-type]
            description=description,
            proposed_change={"context": str(context)},
            checkpoint_dir=checkpoint_dir,
        )
        checkpoint._save()

        # Create escalation for the checkpoint
        escalate_to_human(
            self.control_state_dir / "escalations",
            context,
            EscalationReason.HUMAN_REQUESTED,
            f"Human approval required: {description}",
        )

        # Transition to escalated
        if self.state_machine.can_transition_to(ManagerPhase.ESCALATED):
            self.state_machine.transition_to(
                ManagerPhase.ESCALATED, reason="Awaiting human checkpoint approval"
            )

        return ActionResult(
            action=NextAction(
                action_type=ActionType.WAIT_FOR_INPUT,
                description=f"Awaiting approval: {description}",
            ),
            outcome="partial",
            summary=f"Human checkpoint created: {checkpoint_type}",
            details={"checkpoint_id": checkpoint_id},
        )

    def approve_checkpoint(self, checkpoint_id: str, response: str) -> bool:
        """Approve a human checkpoint.

        Args:
            checkpoint_id: ID of checkpoint to approve
            response: Human's approval response

        Returns:
            True if approved, False otherwise
        """
        request = load_human_checkpoint_request(
            checkpoint_id, self.human_checkpoint_dir
        )
        if request is not None:
            record_human_checkpoint_decision(
                checkpoint_id,
                checkpoint_dir=self.human_checkpoint_dir,
                approved=True,
                response=response,
            )
            log.info(f"Approved prompt checkpoint {checkpoint_id}: {response}")
            if self.active_task:
                self.process_task_step()
            return True

        checkpoint = HumanCheckpoint.load(
            checkpoint_id, self.control_state_dir / "checkpoints"
        )
        if not checkpoint:
            return False

        checkpoint.approve(response)
        log.info(f"Approved checkpoint {checkpoint_id}: {response}")

        # Resume task processing if there's an active task
        # Note: process_task_step() result is not returned to maintain bool contract
        if self.active_task:
            self.process_task_step()

        return True

    def reject_checkpoint(self, checkpoint_id: str, response: str) -> bool:
        """Reject a human checkpoint.

        Args:
            checkpoint_id: ID of checkpoint to reject
            response: Human's rejection response

        Returns:
            True if rejected, False otherwise
        """
        request = load_human_checkpoint_request(
            checkpoint_id, self.human_checkpoint_dir
        )
        if request is not None:
            record_human_checkpoint_decision(
                checkpoint_id,
                checkpoint_dir=self.human_checkpoint_dir,
                approved=False,
                response=response,
            )
            log.info(f"Rejected prompt checkpoint {checkpoint_id}: {response}")
            return True

        checkpoint = HumanCheckpoint.load(
            checkpoint_id, self.control_state_dir / "checkpoints"
        )
        if not checkpoint:
            return False

        checkpoint.reject(response)
        log.info(f"Rejected checkpoint {checkpoint_id}: {response}")

        # Update task to reflect rejection
        if self.active_task:
            # Could fail task or request different action
            pass

        return True

    # =======================================================================
    # Escalation Helpers
    # =======================================================================

    def _escalate_for_reason(
        self,
        task: ManagerTask,
        context: DecisionContext,
        short_reason: str,
        detailed_reason: str,
    ) -> ActionResult:
        """Escalate to human for a specific reason."""
        escalation = escalate_to_human(
            self.control_state_dir / "escalations",
            context,
            EscalationReason.BLOCKED_BY_DECISION,
            detailed_reason,
        )

        # Only transition to ESCALATED if valid
        if self.state_machine.can_transition_to(ManagerPhase.ESCALATED):
            self.state_machine.transition_to(ManagerPhase.ESCALATED)

        return ActionResult(
            action=NextAction(
                action_type=ActionType.ESCALATE_TO_HUMAN,
                description=f"Escalated: {short_reason}",
            ),
            outcome="partial",
            summary=f"Escalated: {short_reason}",
        )


# =============================================================================
# Factory Functions
# =============================================================================


def create_engine(
    workspace_registry: WorkspaceRegistry | None = None,
    repo_root: Path | None = None,
    memory_store: MemoryStore | None = None,
    retrieval_service: RetrievalService | None = None,
    policy_engine: Any | None = None,
    workspaces_root: Path | None = None,
    control_state_dir: Path | None = None,
) -> ControlPlaneEngine:
    """Create a control plane engine.

    This is the main factory for creating a fully configured engine.

    Args:
        workspace_registry: Workspace registry
        repo_root: Repository root for GMAS retrieval
        memory_store: Memory store
        policy_engine: Policy engine
        workspaces_root: Root for workspaces
        control_state_dir: State directory for control plane

    Returns:
        Configured control plane engine
    """
    return ControlPlaneEngine(
        workspace_registry=workspace_registry,
        repo_root=repo_root,
        memory_store=memory_store,
        retrieval_service=retrieval_service,
        policy_engine=policy_engine,
        workspaces_root=workspaces_root,
        control_state_dir=control_state_dir,
    )
