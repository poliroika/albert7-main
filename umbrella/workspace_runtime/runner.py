"""
Unified workspace runner.

Stable entrypoint for prepare / run / inspect without ad-hoc experiment scripts.
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type

from umbrella.workspace_registry.models import SeedWorkspaceProfile, TaskBrief
from umbrella.workspace_registry.task_main import (
    TaskMainDocument,
    build_task_brief_from_task_main,
    load_task_main,
)
from umbrella.workspace_runtime.adapters.agent_research import AgentResearchAdapter
from umbrella.workspace_runtime.adapters.base import BaseWorkspaceAdapter
from umbrella.workspace_runtime.adapters.evaluation import EvaluationAdapter
from umbrella.workspace_runtime.adapters.generic import GenericWorkspaceAdapter
from umbrella.workspace_runtime.adapters.world_prediction import WorldPredictionAdapter
from umbrella.workspace_runtime.instances import (
    archive_instance,
    create_task_instance,
    load_instance_metadata,
    snapshot_instance,
    update_instance_metadata,
)
from umbrella.workspace_runtime.models import (
    PreparedWorkspace,
    WorkspaceInstance,
    WorkspaceInspection,
    WorkspaceRunRequest,
    WorkspaceRunResult,
    WorkspaceRunStatus,
)

_ADAPTER_BY_SEED_ID: dict[str, type[BaseWorkspaceAdapter]] = {
    "agent_research": AgentResearchAdapter,
    "evaluation": EvaluationAdapter,
    "world_prediction": WorldPredictionAdapter,
}


def register_adapter(
    seed_workspace_id: str, adapter_cls: type[BaseWorkspaceAdapter]
) -> None:
    """Register an adapter class for a seed workspace id."""
    _ADAPTER_BY_SEED_ID[seed_workspace_id] = adapter_cls


def _seed_key(instance: WorkspaceInstance) -> str:
    if instance.seed_workspace_id:
        return instance.seed_workspace_id
    return instance.workspace_id


def get_adapter_for_instance(instance: WorkspaceInstance) -> BaseWorkspaceAdapter:
    """Return the adapter for this instance (matched by seed workspace id)."""
    key = _seed_key(instance)
    cls = _ADAPTER_BY_SEED_ID.get(key)
    if cls is not None:
        return cls(instance)
    if (instance.path / "workspace.toml").exists():
        return GenericWorkspaceAdapter(instance)
    known = ", ".join(sorted(_ADAPTER_BY_SEED_ID)) or "(none)"
    raise ValueError(
        f"No workspace runtime adapter registered for seed workspace_id={key!r}. Known: {known}"
    )


def load_task_main_document(instance: WorkspaceInstance) -> TaskMainDocument | None:
    """Load TASK_MAIN.md from an instance root."""
    return load_task_main(instance.path / "TASK_MAIN.md")


def build_task_brief_for_instance(
    instance: WorkspaceInstance,
    *,
    task_id: str | None = None,
) -> TaskBrief | None:
    """Build a TaskBrief from the instance's TASK_MAIN.md."""
    doc = load_task_main_document(instance)
    if doc is None:
        return None
    return build_task_brief_from_task_main(
        doc,
        task_id=task_id,
        preferred_workspace_id=_seed_key(instance),
    )


def prepare_instance(instance: WorkspaceInstance) -> PreparedWorkspace:
    """Lifecycle stage: prepare — validate and load workspace resources."""
    return get_adapter_for_instance(instance).prepare()


def _should_persist_instance_metadata(instance: WorkspaceInstance) -> bool:
    metadata_path = instance.path / "instance_metadata.json"
    if metadata_path.exists():
        return True
    return "instances" in {part.lower() for part in instance.path.parts}


def run_workspace(
    instance: WorkspaceInstance,
    request: WorkspaceRunRequest,
    *,
    prepare: bool = True,
) -> WorkspaceRunResult:
    """
    Run the workspace for this instance using the registered adapter.

    When ``prepare`` is True, calls ``adapter.prepare()`` before ``run`` and aborts if not ready.
    """
    adapter = get_adapter_for_instance(instance)
    if prepare:
        prepared = adapter.prepare()
        if not prepared.ready:
            reason = (
                prepared.not_ready_reason
                or "; ".join(prepared.validation_issues)
                or "not ready"
            )
            tid = request.task_id
            if tid is None:
                meta = load_instance_metadata(instance.path)
                tid = (meta or {}).get("task_id")
            return WorkspaceRunResult(
                workspace_id=instance.workspace_id,
                task_id=tid,
                status=WorkspaceRunStatus.FAILED,
                errors=[reason],
            )
    if request.task_id is None:
        meta = load_instance_metadata(instance.path)
        if meta and meta.get("task_id"):
            request.task_id = str(meta["task_id"])
    result = adapter.run(request)
    if _should_persist_instance_metadata(instance):
        meta = load_instance_metadata(instance.path) or {}
        runs = int(meta.get("run_count", 0)) + 1
        update_instance_metadata(
            instance.path,
            {
                "last_run_id": result.run_id,
                "run_count": runs,
                "status": result.status.value,
            },
        )
    return result


def inspect_run(
    result: WorkspaceRunResult,
    instance: WorkspaceInstance | None = None,
) -> WorkspaceInspection:
    """Lifecycle stage: inspect — structured view of a run result."""
    if instance is not None:
        return get_adapter_for_instance(instance).inspect(result)
    return WorkspaceInspection(
        run_id=result.run_id,
        workspace_id=result.workspace_id,
        status=result.status,
        final_answer=result.final_answer,
        errors=result.errors,
        warnings=result.warnings,
        total_tokens=result.total_tokens,
        duration_seconds=result.duration_seconds,
    )


def create_instance_and_run(
    seed: SeedWorkspaceProfile,
    task: TaskBrief,
    request: WorkspaceRunRequest,
    *,
    instances_root: Path | None = None,
    copy_seed_files: bool = True,
    exclude_patterns: list[str] | None = None,
    prepare_workspace: bool = True,
) -> tuple[WorkspaceInstance, WorkspaceRunResult]:
    """Create a task instance from a seed, then run it."""
    instance = create_task_instance(
        seed,
        task,
        instances_root=instances_root,
        copy_seed_files=copy_seed_files,
        exclude_patterns=exclude_patterns,
    )
    if request.task_id is None:
        request.task_id = task.task_id
    result = run_workspace(instance, request, prepare=prepare_workspace)
    return instance, result


__all__ = [
    "register_adapter",
    "get_adapter_for_instance",
    "load_task_main_document",
    "build_task_brief_for_instance",
    "prepare_instance",
    "run_workspace",
    "inspect_run",
    "create_instance_and_run",
    "snapshot_instance",
    "archive_instance",
]
