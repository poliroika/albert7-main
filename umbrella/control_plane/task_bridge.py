"""
Task-brief adapters between the control plane and workspace runtime.

Selection hints are charter/discovery seeds only — no keyword inference gates.
"""

from pathlib import Path

from umbrella.control_plane.models import TaskBrief as ControlTaskBrief
from umbrella.control_plane.models import TaskClass
from umbrella.workspace_registry.charter import charter_capability_slugs, load_workspace_charter
from umbrella.workspace_registry.models import TaskBrief as WorkspaceTaskBrief


def _map_task_class(task: ControlTaskBrief) -> str | None:
    """Map explicit manager TaskClass to workspace task_class slug."""

    if task.task_class == TaskClass.RESEARCH:
        return "research"
    if task.task_class == TaskClass.CODE_FROM_ARTICLE:
        return "code_generation"
    if task.task_class == TaskClass.SYSTEM_DESIGN:
        return "design"
    if task.task_class == TaskClass.DATA_PROCESSING:
        return "data_processing"
    if task.task_class == TaskClass.EVALUATION:
        return "evaluation"
    return None


def _charter_seed_capabilities(workspace_root: Path | None) -> list[str]:
    if workspace_root is None:
        return []
    return charter_capability_slugs(load_workspace_charter(workspace_root))


def to_workspace_task_brief(
    task: ControlTaskBrief,
    *,
    preferred_workspace_id: str | None = None,
    workspace_root: Path | None = None,
) -> WorkspaceTaskBrief:
    """Convert the manager task brief into the runtime selection contract."""

    seeds = _charter_seed_capabilities(workspace_root)
    return WorkspaceTaskBrief(
        description=task.original_input,
        task_id=task.task_id,
        task_class=_map_task_class(task),
        domains=[],
        required_capabilities=seeds,
        preferred_workspace_id=preferred_workspace_id,
        constraints={
            "requirements": list(task.requirements),
            "constraints": list(task.constraints),
            "success_criteria": list(task.success_criteria),
            "estimated_iterations": task.estimated_iterations,
            "estimated_cost_usd": task.estimated_cost_usd,
        },
        metadata={
            "manager_summary": task.summary,
            "manager_task_class": task.task_class.value,
            "capability_seeds_hint_only": True,
        },
    )
