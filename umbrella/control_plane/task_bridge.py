"""
Task-brief adapters between the control plane and workspace runtime.

This module keeps the manager-facing brief and the workspace-facing brief
explicitly connected instead of relying on duck-typing between unrelated models.
"""

from umbrella.control_plane.models import TaskBrief as ControlTaskBrief
from umbrella.control_plane.models import TaskClass
from umbrella.workspace_registry.models import TaskBrief as WorkspaceTaskBrief


def _infer_domains(text: str) -> list[str]:
    lowered = text.lower()
    domains: list[str] = []

    domain_keywords = {
        "software_engineering": (
            "code",
            "api",
            "framework",
            "repo",
            "workspace",
            "agent",
        ),
        "technology": ("technology", "ai", "llm", "automation", "system"),
        "science": ("science", "research", "paper", "article", "experiment"),
        "data_science": ("data", "analysis", "etl", "pipeline"),
    }

    for domain, keywords in domain_keywords.items():
        if any(keyword in lowered for keyword in keywords):
            domains.append(domain)

    return domains


def _map_task_class(task: ControlTaskBrief) -> str | None:
    lowered = task.original_input.lower()

    if task.task_class == TaskClass.RESEARCH:
        if any(
            keyword in lowered for keyword in ("article", "write", "writing", "paper")
        ):
            return "article_research"
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


def _infer_required_capabilities(task: ControlTaskBrief) -> list[str]:
    lowered = task.original_input.lower()
    capabilities: list[str] = []

    if task.task_class == TaskClass.RESEARCH:
        capabilities.extend(["article_writing", "multi_agent_research"])
        if "web" in lowered or "search" in lowered:
            capabilities.append("web_search")

    if task.task_class == TaskClass.CODE_FROM_ARTICLE:
        capabilities.extend(["code_generation", "file_search", "code_interpreter"])

    if task.task_class == TaskClass.SYSTEM_DESIGN:
        capabilities.extend(["multi_agent_research", "file_search"])

    if task.task_class == TaskClass.DATA_PROCESSING:
        capabilities.extend(["code_interpreter", "file_search"])

    if task.task_class == TaskClass.EVALUATION:
        capabilities.extend(["evaluation", "file_search"])

    if "human" in lowered or "approval" in lowered or "checkpoint" in lowered:
        capabilities.append("human_gates")
    if "draft" in lowered or "revise" in lowered or "rewrite" in lowered:
        capabilities.append("iterative_drafting")

    seen: set[str] = set()
    ordered: list[str] = []
    for capability in capabilities:
        if capability not in seen:
            ordered.append(capability)
            seen.add(capability)
    return ordered


def to_workspace_task_brief(
    task: ControlTaskBrief,
    *,
    preferred_workspace_id: str | None = None,
) -> WorkspaceTaskBrief:
    """Convert the manager task brief into the runtime selection contract."""
    return WorkspaceTaskBrief(
        description=task.original_input,
        task_id=task.task_id,
        task_class=_map_task_class(task),
        domains=_infer_domains(task.original_input),
        required_capabilities=_infer_required_capabilities(task),
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
        },
    )
