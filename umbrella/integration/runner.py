"""
Manager task runner - main execution loop for Umbrella manager.

This module provides the run_manager_task function that executes the full
manager workflow: receive task → classify → select workspace → retrieve knowledge
→ run workspace → inspect → evaluate → decide → iterate → complete.
"""

import logging
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from umbrella.config import MANAGER_PROGRESS_TASK_PREVIEW_LIMIT
from umbrella.env import get_llm_env_config, load_env
from umbrella.integration.services import UmbrellaServices, bootstrap_services
from umbrella.control_plane.models import (
    ManagerTask,
    ManagerPhase,
    ActionType,
    HumanCheckpointStatus,
)
from umbrella.workspace_runtime.instances import (
    cleanup_detached_instances,
    prune_instance_storage,
)

log = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0


def _resolve_llm_config(
    use_live_llm: bool,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> tuple[bool, str | None, str | None, str | None]:
    """Resolve LLM configuration from parameters or environment.

    Args:
        use_live_llm: Whether to use live LLM
        model: Model name
        api_key: API key
        base_url: Base URL

    Returns:
        Tuple of (use_live_llm, model, api_key, base_url)
    """
    if not use_live_llm:
        return False, None, None, None

    env_model, env_api_key, env_base_url = get_llm_env_config()
    if api_key is None and env_api_key is None:
        load_env(repo_root=Path(__file__).resolve().parents[2])
        env_model, env_api_key, env_base_url = get_llm_env_config()

    # Load from environment if not provided
    if model is None:
        model = env_model
    if api_key is None:
        api_key = env_api_key
    if base_url is None:
        base_url = env_base_url

    # Only enable live mode if we have the required credentials
    if api_key:
        return True, model, api_key, base_url
    else:
        log.warning(
            "Live LLM requested but no API key found, falling back to degraded mode"
        )
        return False, None, None, None


@dataclass
class ManagerRunResult:
    """Result of a manager task execution."""

    task_id: str
    status: str  # "complete", "failed", "partial"
    iterations: int = 0
    duration_seconds: float = 0.0

    # Outcomes
    workspace_id: str | None = None
    task_success: str = "unknown"  # "complete", "partial", "failed"
    final_artifact_path: Path | None = None
    instance_path: Path | None = None
    run_id: str | None = None

    # Traces
    phases_visited: list[str] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    # Evidence
    evidence: list[str] = field(default_factory=list)
    workspace_changes: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    artifact_paths: list[Path] = field(default_factory=list)
    lessons_recorded: int = 0
    retrieval_summary: str | None = None
    degraded_mode_used: bool = False
    evaluation_score: float | None = None
    evaluation_delta: float | None = None

    # Self-improvement
    self_improvement_considered: bool = False
    self_improvement_applied: bool = False

    # Human interaction
    human_checkpoints_requested: int = 0
    human_checkpoints_approved: int = 0

    # Timestamps
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    @property
    def duration_str(self) -> str:
        """Human-readable duration."""
        if self.duration_seconds < 60:
            return f"{self.duration_seconds:.1f}s"
        minutes = int(self.duration_seconds // 60)
        seconds = self.duration_seconds % 60
        return f"{minutes}m {seconds:.1f}s"


def _limit_is_enabled(limit: int | float | None) -> bool:
    """Return True when a runtime limit is active."""
    return limit is not None and limit > 0


def _runtime_limit_reason(
    *,
    iteration: int,
    elapsed_seconds: float,
    max_iterations: int | None,
    max_duration_seconds: float | None,
) -> str | None:
    """Return a human-readable reason when a runtime limit has been reached."""
    if _limit_is_enabled(max_iterations) and iteration >= int(max_iterations):
        return f"Reached max iterations ({int(max_iterations)})"
    if _limit_is_enabled(max_duration_seconds) and elapsed_seconds > float(
        max_duration_seconds
    ):
        return f"Reached max duration ({float(max_duration_seconds)}s)"
    return None


def _save_task_checkpoint_safely(services: UmbrellaServices, task: Any) -> None:
    """Persist manager task state without breaking the main loop on checkpoint errors."""
    try:
        _save_task_checkpoint(services, task)
    except Exception:
        log.warning(
            "Failed to persist manager checkpoint for %s",
            getattr(task, "id", "unknown"),
            exc_info=True,
        )


def _emit_manager_heartbeat(
    *,
    task: ManagerTask,
    result: ManagerRunResult,
    services: UmbrellaServices,
    start_time: float,
) -> None:
    """Log a concise supervisor heartbeat for long-running manager sessions."""
    elapsed = time.time() - start_time
    run_id = (
        services.control_plane.last_run_result.run_id
        if services.control_plane.last_run_result
        else ""
    )
    log.info(
        "Umbrella heartbeat: task=%s elapsed=%.1fs phase=%s iterations=%d workspace=%s run_id=%s checkpoint=%s",
        task.id,
        elapsed,
        task.state.phase.value,
        result.iterations,
        task.state.current_workspace_id or "unknown",
        run_id or "-",
        task.state.pending_human_checkpoint_id or "-",
    )


def _load_active_instance_paths(control_state_dir: Path) -> set[Path]:
    """Collect instance paths referenced by non-terminal manager checkpoints."""
    checkpoint_dir = control_state_dir / "checkpoints"
    if not checkpoint_dir.is_dir():
        return set()

    active_paths: set[Path] = set()
    for checkpoint_path in checkpoint_dir.glob("*.json"):
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        status = str(payload.get("status") or "")
        state = payload.get("state") or {}
        phase = str(state.get("phase") or "")
        if status in {"complete", "failed", "blocked"}:
            continue
        if phase in {"task_complete", "task_failed", "task_blocked"}:
            continue

        raw_path = state.get("current_instance_path")
        if not raw_path:
            continue
        try:
            active_paths.add(Path(str(raw_path)).resolve())
        except Exception:
            continue

    return active_paths


def _cleanup_instance_storage(
    *,
    services: UmbrellaServices,
    task: ManagerTask,
    result: ManagerRunResult,
) -> None:
    """Prune transient instance artifacts and drop detached stale instances."""
    runtime_cfg = getattr(services.control_plane, "runtime_config", None)
    if runtime_cfg is None or not getattr(
        runtime_cfg, "instance_cleanup_enabled", True
    ):
        return

    current_instance = task.state.current_instance_path
    if current_instance is None:
        return

    instance_path = Path(current_instance)
    if instance_path.exists():
        pruned_entries = prune_instance_storage(
            instance_path,
            keep_run_dirs=getattr(runtime_cfg, "keep_recent_runs_per_instance", 2),
            keep_snapshots=getattr(
                runtime_cfg, "keep_recent_snapshots_per_instance", 1
            ),
            keep_report_files=getattr(
                runtime_cfg, "keep_recent_reports_per_instance", 4
            ),
        )
        if pruned_entries:
            log.info(
                "Pruned %d stale artifact entries from instance %s",
                len(pruned_entries),
                instance_path,
            )

        removed_instances = cleanup_detached_instances(
            instance_path.parent,
            active_instance_paths=_load_active_instance_paths(
                services.control_state_dir
            ),
            keep_latest_detached=getattr(
                runtime_cfg, "keep_latest_detached_instances", 1
            ),
        )
        if removed_instances:
            log.info(
                "Removed %d detached stale instance(s) under %s",
                len(removed_instances),
                instance_path.parent,
            )
            result.evidence.append(
                f"Storage cleanup removed {len(removed_instances)} detached instance(s)"
            )


def _drive_manager_loop(
    *,
    services: UmbrellaServices,
    task: ManagerTask,
    result: ManagerRunResult,
    start_time: float,
    max_iterations: int | None,
    max_duration_seconds: float | None,
    max_budget_usd: float | None = None,
    checkpoint_each_step: bool = True,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    progress_reporter: Any | None = None,
) -> None:
    """Advance a manager task until completion, human wait, or runtime limit."""
    iteration = 0
    accumulated_cost_usd = 0.0
    seen_run_ids: set[str] = set()
    last_heartbeat_at = start_time

    if checkpoint_each_step:
        _save_task_checkpoint_safely(services, task)

    manager_step_idx = 0
    while True:
        now = time.time()
        elapsed = now - start_time
        limit_reason = _runtime_limit_reason(
            iteration=iteration,
            elapsed_seconds=elapsed,
            max_iterations=max_iterations,
            max_duration_seconds=max_duration_seconds,
        )
        if limit_reason is not None:
            result.evidence.append(limit_reason)
            break

        if max_budget_usd is not None and accumulated_cost_usd >= max_budget_usd:
            result.evidence.append(
                f"Budget exhausted (${accumulated_cost_usd:.2f} / ${max_budget_usd:.2f})"
            )
            break

        if (
            heartbeat_interval_seconds > 0
            and (now - last_heartbeat_at) >= heartbeat_interval_seconds
        ):
            _emit_manager_heartbeat(
                task=task,
                result=result,
                services=services,
                start_time=start_time,
            )
            last_heartbeat_at = now

        action_result = services.control_plane.process_task_step(task)
        manager_step_idx += 1
        if progress_reporter is not None:
            progress_reporter.step(
                services=services,
                task=task,
                action_result=action_result,
                manager_result=result,
                step_idx=manager_step_idx,
            )

        if task.state.phase not in result.phases_visited:
            result.phases_visited.append(task.state.phase.value)

        if action_result.action:
            action_type = action_result.action.action_type
            if action_type not in result.actions_taken:
                result.actions_taken.append(action_type)

        last_run = services.control_plane.last_run_result
        if last_run and last_run.run_id and last_run.run_id not in seen_run_ids:
            seen_run_ids.add(last_run.run_id)
            iteration += 1
            result.iterations = iteration

            if services.control_plane.last_eval_record:
                accumulated_cost_usd += (
                    services.control_plane.last_eval_record.total_cost_usd
                )

            _cleanup_instance_storage(
                services=services,
                task=task,
                result=result,
            )

        if checkpoint_each_step:
            _save_task_checkpoint_safely(services, task)

        if action_result.action and action_result.action.action_type in (
            ActionType.COMPLETE_TASK,
            ActionType.FAIL_TASK,
        ):
            result.status = (
                "complete" if action_result.outcome == "success" else "failed"
            )
            result.task_success = (
                "complete" if action_result.outcome == "success" else "failed"
            )
            break

        if (
            action_result.action
            and action_result.action.action_type == ActionType.WAIT_FOR_INPUT
        ):
            result.status = "partial"
            result.task_success = "partial"
            result.evidence.append(f"Awaiting input: {action_result.summary}")
            break

        if (
            action_result.action
            and action_result.action.action_type == ActionType.PATCH_WORKSPACE
        ):
            result.workspace_changes.append(action_result.summary)
            result.evidence.append(f"Workspace patch: {action_result.summary[:80]}")
            changed_files = (
                action_result.details.get("changed_files", [])
                if action_result.details
                else []
            )
            if isinstance(changed_files, list):
                result.changed_files.extend(
                    str(path)
                    for path in changed_files
                    if str(path) not in result.changed_files
                )

        if task.state.phase == ManagerPhase.SELF_IMPROVEMENT_APPROVED:
            result.self_improvement_considered = True
            result.evidence.append("Self-improvement was considered/approved")

        time.sleep(0.1)


def run_manager_task(
    task_input: str,
    *,
    repo_root: Path | None = None,
    control_state_dir: Path | None = None,
    workspaces_root: Path | None = None,
    task_id: str | None = None,
    workspace_id: str | None = None,
    max_iterations: int | None = 10,
    max_duration_seconds: float | None = 300.0,
    use_live_llm: bool = False,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    runtime_config: Any | None = None,
    progress_reporter: Any | None = None,
) -> ManagerRunResult:
    """Run a manager task from start to completion.

    This is the main entrypoint for executing a manager-driven workflow.
    It handles the full lifecycle: task classification, workspace selection,
    instance creation, knowledge retrieval, workspace execution, inspection,
    evaluation, decision making, and iteration.

    Args:
        task_input: Raw task description from user
        repo_root: Repository root
        control_state_dir: Control plane state directory
        workspaces_root: Workspaces root directory
        task_id: Optional task ID
        workspace_id: Optional workspace ID (auto-selected if None)
        max_iterations: Maximum manager loop iterations. Use ``0`` or ``None`` for no manager-side iteration cap.
        max_duration_seconds: Maximum time to spend. Use ``0`` or ``None`` for no manager-side duration cap.
        use_live_llm: Whether to use live LLM (vs degraded mode)
        llm_model: LLM model name
        llm_api_key: LLM API key
        llm_base_url: LLM base URL
        heartbeat_interval_seconds: Log a supervisor heartbeat every N seconds during long runs. Set to ``0`` to disable.
        runtime_config: ``UmbrellaRuntimeConfig`` controlling budget, thresholds, etc.
        progress_reporter: Optional ``DashboardRunReporter`` for live progress (see ``umbrella.run_observer``).

    Returns:
        ManagerRunResult with full execution trace
    """
    if runtime_config is None:
        from umbrella.config import load_runtime_config

        runtime_config = load_runtime_config()

    if max_iterations == 10 and runtime_config.max_iterations is not None:
        max_iterations = runtime_config.max_iterations
    if (
        max_duration_seconds == 300.0
        and runtime_config.max_duration_seconds is not None
    ):
        max_duration_seconds = runtime_config.max_duration_seconds
    if heartbeat_interval_seconds == DEFAULT_HEARTBEAT_INTERVAL_SECONDS:
        heartbeat_interval_seconds = runtime_config.heartbeat_interval_seconds

    start_time = time.time()

    # Resolve LLM configuration
    resolved_live, resolved_model, resolved_api_key, resolved_base_url = (
        _resolve_llm_config(
            use_live_llm=use_live_llm,
            model=llm_model,
            api_key=llm_api_key,
            base_url=llm_base_url,
        )
    )

    if resolved_live:
        log.info(
            f"Using live LLM: model={resolved_model or 'default'}, base_url={resolved_base_url or 'default'}"
        )

    # Bootstrap services
    services = bootstrap_services(
        repo_root=repo_root,
        control_state_dir=control_state_dir,
        workspaces_root=workspaces_root,
        use_live_llm=resolved_live,
        llm_model=resolved_model,
        llm_api_key=resolved_api_key,
        llm_base_url=resolved_base_url,
        runtime_config=runtime_config,
    )

    # Create result object
    result = ManagerRunResult(
        task_id=task_id or f"task_{int(start_time)}",
        status="pending",
        started_at=start_time,
    )

    task: ManagerTask | None = None
    reporter_armed = False
    loop_exc: BaseException | None = None

    try:
        # Create task
        task = services.create_task(
            task_input=task_input,
            task_id=result.task_id,
            workspace_id=workspace_id,
        )
        result.workspace_id = task.state.current_workspace_id
        result.instance_path = task.state.current_instance_path

        log.info(f"Starting manager task {result.task_id}: {task.brief.summary[:100]}")
        result.evidence.append(f"Task classified as: {task.brief.task_class.value}")

        if progress_reporter is not None:
            from umbrella.run_observer import set_active_task

            progress_reporter.start(
                result.task_id, task_input[:MANAGER_PROGRESS_TASK_PREVIEW_LIMIT], task
            )
            set_active_task(task)
            reporter_armed = True

        _cleanup_instance_storage(
            services=services,
            task=task,
            result=result,
        )

        _drive_manager_loop(
            services=services,
            task=task,
            result=result,
            start_time=start_time,
            max_iterations=max_iterations,
            max_duration_seconds=max_duration_seconds,
            max_budget_usd=runtime_config.max_budget_usd,
            checkpoint_each_step=True,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            progress_reporter=progress_reporter if reporter_armed else None,
        )

        # Finalize result
        result.completed_at = time.time()
        result.duration_seconds = result.completed_at - result.started_at

        # Update status if still pending (loop exited without explicit completion)
        if result.status == "pending":
            if result.iterations > 0 and result.task_success != "unknown":
                result.status = result.task_success
            else:
                result.status = "partial"
                result.task_success = "partial"

        # Collect evidence from control plane
        _collect_final_evidence(services, task, result)
        _cleanup_instance_storage(
            services=services,
            task=task,
            result=result,
        )

        log.info(
            f"Manager task {result.task_id} completed: "
            f"status={result.status}, iterations={result.iterations}, "
            f"duration={result.duration_str}, phases={len(result.phases_visited)}"
        )

    except Exception as e:
        loop_exc = e
        log.error(
            f"Manager task {result.task_id} failed with exception: {e}", exc_info=True
        )
        result.status = "failed"
        result.completed_at = time.time()
        result.duration_seconds = result.completed_at - result.started_at
        result.evidence.append(f"Exception: {str(e)}")
        if task is not None:
            _save_task_checkpoint_safely(services, task)

    finally:
        if progress_reporter is not None and reporter_armed:
            from umbrella.run_observer import set_active_task

            progress_reporter.done(result, loop_exc)
            set_active_task(None)
        # Shutdown services
        services.shutdown()

    return result


def _collect_final_evidence(
    services: UmbrellaServices,
    task: ManagerTask,
    result: ManagerRunResult,
) -> None:
    """Collect final evidence from services.

    Args:
        services: Umbrella services
        task: Manager task
        result: Result to populate with evidence
    """
    # Memory stats
    if services.memory:
        stats = services.memory.get_stats()
        result.lessons_recorded = stats.total_lessons
        result.evidence.append(f"Total lessons in memory: {stats.total_lessons}")

    # Metrics
    if services.metrics:
        all_metrics = services.metrics.get_all_metrics()
        run_metrics = all_metrics.get("run_metrics", {})
        if run_metrics:
            ws_id = result.workspace_id or "unknown"
            ws_metrics = run_metrics.get(ws_id, {})
            if ws_metrics:
                result.evidence.append(
                    f"Workspace runs: {ws_metrics.get('total_runs', 0)}, "
                    f"successful: {ws_metrics.get('successful_runs', 0)}"
                )

    control_plane = services.control_plane
    result.instance_path = task.state.current_instance_path
    result.retrieval_summary = task.state.retrieval_summary
    result.changed_files = list(
        dict.fromkeys(result.changed_files + list(task.state.last_patch_files))
    )

    if control_plane.last_run_result:
        result.run_id = control_plane.last_run_result.run_id
        result.final_artifact_path = task.final_artifact_path
        result.artifact_paths = [
            artifact.path for artifact in control_plane.last_run_result.artifacts
        ]
        result.degraded_mode_used = any(
            "degraded" in error.lower()
            for error in control_plane.last_run_result.errors
        )

    if control_plane.last_eval_record:
        result.task_success = control_plane.last_eval_record.task_success.value
        result.evaluation_score = control_plane.last_eval_record.overall_score
        result.evidence.append(
            f"Evaluation score: {control_plane.last_eval_record.overall_score:.2f}"
        )

    # Control plane state
    if task.state.last_comparison:
        comp = task.state.last_comparison
        result.evaluation_delta = comp.score_delta
        result.evidence.append(
            f"Last comparison: {comp.overall_improvement.value} "
            f"(score delta: {comp.score_delta:+.2f})"
        )

    if task.state.retrieval_summary:
        result.evidence.append(
            f"Retrieval informed run: {task.state.retrieval_summary}"
        )
    if task.state.retrieval_key_files:
        result.evidence.append(
            "Retrieval key files: "
            + ", ".join(str(path) for path in task.state.retrieval_key_files[:5])
        )

    # Check for self-improvement
    if result.self_improvement_considered:
        result.evidence.append("Self-improvement path was explored")
        if task.state.phase in (
            ManagerPhase.SELF_IMPROVEMENT_COMPLETE,
            ManagerPhase.SELF_IMPROVEMENT_APPROVED,
        ):
            result.self_improvement_applied = True
            result.evidence.append("Self-improvement was applied")

    # Promotion candidate
    if task.state.promotion_candidate:
        candidate = task.state.promotion_candidate
        result.evidence.append(
            f"Promotion candidate: {candidate.eligibility.value} "
            f"(magnitude: {candidate.improvement_magnitude:.2f})"
        )


def resume_manager_run(
    run_id: str,
    repo_root: Path | None = None,
    control_state_dir: Path | None = None,
    workspaces_root: Path | None = None,
    max_iterations: int | None = 10,
    max_duration_seconds: float | None = 300.0,
    use_live_llm: bool = False,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> ManagerRunResult:
    """Resume a paused manager run.

    Args:
        run_id: ID of the run to resume
        repo_root: Repository root
        control_state_dir: Control state directory
        workspaces_root: Workspaces root directory
        use_live_llm: Whether to use live LLM (vs degraded mode)
        llm_model: LLM model name
        llm_api_key: LLM API key
        llm_base_url: LLM base URL

    Returns:
        ManagerRunResult of the resumed execution
    """
    from umbrella.config import load_runtime_config

    runtime_config = load_runtime_config()
    if max_iterations == 10 and runtime_config.max_iterations is not None:
        max_iterations = runtime_config.max_iterations
    if (
        max_duration_seconds == 300.0
        and runtime_config.max_duration_seconds is not None
    ):
        max_duration_seconds = runtime_config.max_duration_seconds
    if heartbeat_interval_seconds == DEFAULT_HEARTBEAT_INTERVAL_SECONDS:
        heartbeat_interval_seconds = runtime_config.heartbeat_interval_seconds

    start_time = time.time()

    # Resolve LLM configuration
    resolved_live, resolved_model, resolved_api_key, resolved_base_url = (
        _resolve_llm_config(
            use_live_llm=use_live_llm,
            model=llm_model,
            api_key=llm_api_key,
            base_url=llm_base_url,
        )
    )

    if resolved_live:
        log.info(
            f"Using live LLM: model={resolved_model or 'default'}, base_url={resolved_base_url or 'default'}"
        )

    # Bootstrap services
    services = bootstrap_services(
        repo_root=repo_root,
        control_state_dir=control_state_dir,
        workspaces_root=workspaces_root,
        use_live_llm=resolved_live,
        llm_model=resolved_model,
        llm_api_key=resolved_api_key,
        llm_base_url=resolved_base_url,
        runtime_config=runtime_config,
    )

    # Create result object
    result = ManagerRunResult(
        task_id=run_id,
        status="pending",
        started_at=start_time,
    )

    try:
        # Load saved task state
        task = _load_task_from_checkpoint(services, run_id)
        if task is None:
            result.status = "failed"
            result.completed_at = time.time()
            result.duration_seconds = result.completed_at - result.started_at
            result.evidence.append(f"Failed to load checkpoint for run_id {run_id}")
            return result

        result.workspace_id = task.state.current_workspace_id

        log.info(
            f"Resuming manager task {result.task_id} from phase: {task.state.phase.value}"
        )
        result.evidence.append(f"Resumed from phase: {task.state.phase.value}")

        _cleanup_instance_storage(
            services=services,
            task=task,
            result=result,
        )

        # Continue from where we left off
        # Check if we're waiting for input (human checkpoint)
        if task.state.phase == ManagerPhase.ESCALATED:
            # Check if there's a pending checkpoint decision
            from umbrella.control_plane.human_checkpoints import (
                load_human_checkpoint_request,
            )

            checkpoint = load_human_checkpoint_request(
                f"{task.id}_checkpoint",
                services.control_state_dir / "human_checkpoints",
            )

            if checkpoint and checkpoint.status == HumanCheckpointStatus.PENDING:
                result.evidence.append(f"Pending human checkpoint: {checkpoint.id}")
                # For now, continue without waiting for human input
                # In production, this would wait for external input
                services.control_plane.state_machine.transition_to(
                    ManagerPhase.KNOWLEDGE_RETRIEVED, reason="Resuming after checkpoint"
                )

        _drive_manager_loop(
            services=services,
            task=task,
            result=result,
            start_time=start_time,
            max_iterations=max_iterations,
            max_duration_seconds=max_duration_seconds,
            max_budget_usd=runtime_config.max_budget_usd,
            checkpoint_each_step=True,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )

        # Finalize result
        result.completed_at = time.time()
        result.duration_seconds = result.completed_at - result.started_at

        # Update status if still pending
        if result.status == "pending":
            if result.iterations > 0 and result.task_success != "unknown":
                result.status = result.task_success
            else:
                result.status = "partial"

        # Collect evidence
        _collect_final_evidence(services, task, result)
        _cleanup_instance_storage(
            services=services,
            task=task,
            result=result,
        )

        log.info(
            f"Resumed manager task {result.task_id} completed: "
            f"status={result.status}, iterations={result.iterations}, "
            f"duration={result.duration_str}"
        )

    except Exception as e:
        log.error(f"Failed to resume manager task {result.task_id}: {e}", exc_info=True)
        result.status = "failed"
        result.completed_at = time.time()
        result.duration_seconds = result.completed_at - result.started_at
        result.evidence.append(f"Exception: {str(e)}")

    finally:
        # Shutdown services
        services.shutdown()

    return result


def _load_task_from_checkpoint(
    services: UmbrellaServices,
    task_id: str,
) -> Any:
    """Load a task from checkpoint.

    Args:
        services: Umbrella services
        task_id: Task ID to load

    Returns:
        Loaded ManagerTask or None if not found
    """
    try:
        import json
        from umbrella.control_plane.models import ManagerTask

        # Try to load from control state directory
        checkpoint_path = services.control_state_dir / "checkpoints" / f"{task_id}.json"

        if not checkpoint_path.exists():
            log.warning(f"No checkpoint found at {checkpoint_path}")
            return None

        with open(checkpoint_path, encoding="utf-8") as f:
            data = json.load(f)

        # Reconstruct ManagerTask from saved state
        # Note: This is a simplified reconstruction - in production, you'd need to
        # properly reconstruct all task state including the state machine
        task = ManagerTask.model_validate(data)

        log.info(f"Loaded task from checkpoint: {task_id}")
        return task

    except Exception as e:
        log.error(f"Failed to load task from checkpoint: {e}", exc_info=True)
        return None


def _save_task_checkpoint(
    services: UmbrellaServices,
    task: Any,
) -> None:
    """Save task state to checkpoint.

    Args:
        services: Umbrella services
        task: ManagerTask to checkpoint
    """
    try:
        import json

        checkpoint_dir = services.control_state_dir / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

        checkpoint_path = checkpoint_dir / f"{task.id}.json"

        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(
                task.model_dump(mode="json"),
                f,
                ensure_ascii=False,
                indent=2,
            )

        log.info(f"Saved task checkpoint: {task.id}")

    except Exception as e:
        log.error(f"Failed to save task checkpoint: {e}", exc_info=True)
