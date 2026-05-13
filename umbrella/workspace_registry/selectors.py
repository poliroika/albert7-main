"""
Workspace selection and scoring.

Provides a simple scoring-based selector for matching task briefs to seed workspaces.
"""

from typing import TYPE_CHECKING, List

from umbrella.workspace_registry.models import (
    TaskBrief,
    SeedWorkspaceProfile,
    WorkspaceMatch,
)

if TYPE_CHECKING:
    from umbrella.workspace_registry.registry import WorkspaceRegistry
else:

    class WorkspaceRegistry:
        """Placeholder for type hints to - avoids circular import  at runtime."""

        pass


def score_workspace_for_task(
    task: TaskBrief,
    profile: SeedWorkspaceProfile,
) -> float:
    """
    Score a workspace for a given task brief.

    Higher scores indicate better matches.

    Args:
        task: The task brief to score against
        profile: The seed workspace profile to evaluate

    Returns:
        Score from 0.0 (poor match) to 1.0+ (excellent match)
    """
    score = 0.0

    # Score based on task class match
    if task.task_class:
        for task_class in profile.primary_task_classes:
            if task_class.lower() == task.task_class.lower():
                score += 0.5
                break

    # Score based on capability match
    if task.required_capabilities:
        for req_cap in task.required_capabilities:
            for cap in profile.capabilities:
                if req_cap.lower() == cap.name.lower():
                    score += 0.3 * cap.weight
                    break

    # Score based on domain match
    if task.domains:
        for domain in task.domains:
            domain_lower = domain.lower()
            if domain_lower in [
                d.lower() for d in profile.selection_hints.preferred_for_domains
            ]:
                score += 0.2
            if domain_lower in [
                d.lower() for d in profile.selection_hints.avoided_for_domains
            ]:
                score -= 0.3

    # Score based on keywords in description
    if task.description:
        desc_lower = task.description.lower()
        for keyword in profile.selection_hints.keywords:
            if keyword.lower() in desc_lower:
                score += 0.1

    # Bonus for preferred workspace
    if task.preferred_workspace_id == profile.workspace_id:
        score += 1.0

    # Clamp score
    return max(0.0, min(score, 2.0))


def match_workspaces(
    task: TaskBrief,
    registry: WorkspaceRegistry,
) -> list[WorkspaceMatch]:
    """
    Find workspaces that match a task brief.

    Args:
        task: The task brief to match
        registry: The workspace registry to search

    Returns:
        List of workspace matches sorted by score (best first)
    """
    matches = []

    for profile in registry.get_all_seed_profiles():
        score = score_workspace_for_task(task, profile)

        if score > 0:
            # Determine matched capabilities
            matched_caps = []
            if task.required_capabilities:
                for req_cap in task.required_capabilities:
                    for cap in profile.capabilities:
                        if req_cap.lower() == cap.name.lower():
                            matched_caps.append(cap.name)
                            break

            # Determine matched task classes
            matched_classes = []
            if task.task_class:
                for tc in profile.primary_task_classes:
                    if tc.lower() == task.task_class.lower():
                        matched_classes.append(tc)
                        break

            # Build match reasons
            reasons = []
            if matched_classes:
                reasons.append(f"Matches task classes: {', '.join(matched_classes)}")
            if matched_caps:
                reasons.append(f"Has required capabilities: {', '.join(matched_caps)}")
            if score >= 0.5:
                reasons.append("High overall compatibility")

            matches.append(
                WorkspaceMatch(
                    profile=profile,
                    score=score,
                    matched_capabilities=matched_caps,
                    matched_task_classes=matched_classes,
                    match_reasons=reasons,
                )
            )

    # Sort by score descending
    matches.sort(key=lambda m: m.score, reverse=True)
    return matches
