"""
Workspace registry implementation.

Provides a programmatic interface for workspace discovery, selection, and querying.
"""

from pathlib import Path
from typing import Dict, List, Optional

from umbrella.workspace_registry.models import (
    WorkspaceRef,
    SeedWorkspaceProfile,
    TaskInstanceProfile,
    ValidationIssue,
    ValidationSeverity,
    WorkspaceMatch,
    TaskBrief,
)
from umbrella.workspace_registry.discovery import (
    discover_workspaces,
    load_registry_manifest,
    load_seed_profile,
    load_task_instance_profile,
)
from umbrella.workspace_registry.validation import validate_workspace
from umbrella.workspace_registry.selectors import (
    match_workspaces,
)
from umbrella.workspace_registry.lineage import (
    create_task_instance_record,
    LineageTracker,
)


class WorkspaceRegistry:
    """
    Registry of all workspaces with discovery and query capabilities.

    Supports registration of seed profiles and task instance profiles.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.manifest = load_registry_manifest(self.root)
        self._workspaces: dict[str, WorkspaceRef] = {}
        self._seed_profiles: dict[str, SeedWorkspaceProfile] = {}
        self._task_instances: dict[str, TaskInstanceProfile] = {}
        self._validation_issues: list[ValidationIssue] = []
        self._lineage_tracker = LineageTracker()

    def discover(self) -> list[WorkspaceRef]:
        """Discover all workspaces under the root directory."""
        discovered = discover_workspaces(self.root)
        for ref in discovered:
            self._workspaces[ref.workspace_id] = ref
        return discovered

    def register_workspace(
        self,
        ref: WorkspaceRef,
        profile: SeedWorkspaceProfile | None = None,
    ) -> None:
        """
        Register a workspace with optional seed profile.

        Args:
            ref: Workspace reference
            profile: Optional seed profile for seed workspaces
        """
        self._workspaces[ref.workspace_id] = ref

        if profile:
            self._seed_profiles[ref.workspace_id] = profile

    def register_task_instance(self, profile: TaskInstanceProfile) -> None:
        """Register a materialized task instance profile."""
        self._workspaces[profile.workspace_id] = profile.ref
        self._task_instances[profile.workspace_id] = profile

    def get_workspace(self, workspace_id: str) -> WorkspaceRef | None:
        """Get a workspace by ID."""
        return self._workspaces.get(workspace_id)

    def get_seed_profile(self, workspace_id: str) -> SeedWorkspaceProfile | None:
        """Get a seed profile by workspace ID."""
        return self._seed_profiles.get(workspace_id)

    def get_task_instance(self, workspace_id: str) -> TaskInstanceProfile | None:
        """Get a task instance profile by workspace ID."""
        return self._task_instances.get(workspace_id)

    def get_all_seed_profiles(self) -> list[SeedWorkspaceProfile]:
        """Get all seed profiles from the registry."""
        return list(self._seed_profiles.values())

    def get_all_task_instances(self) -> list[TaskInstanceProfile]:
        """Get all task instance profiles from the registry."""
        return list(self._task_instances.values())

    def get_all_workspace_ids(self) -> list[str]:
        """Get all registered workspace IDs."""
        return list(self._workspaces.keys())

    def get_validation_issues(self) -> list[ValidationIssue]:
        """Get all validation issues from registered workspaces."""
        issues = list(self._validation_issues)
        for profile in self._seed_profiles.values():
            issues.extend(validate_workspace(profile.ref))
        for profile in self._task_instances.values():
            issues.extend(validate_workspace(profile.ref))
        return issues

    def match(self, task: TaskBrief) -> list[WorkspaceMatch]:
        """
        Match workspaces for a task brief.

        Args:
            task: The task brief to match against

        Returns:
            List of workspace matches sorted by score
        """
        return match_workspaces(task, self)

    def select_best(self, task: TaskBrief) -> SeedWorkspaceProfile | None:
        """
        Select the best workspace for a task.

        Args:
            task: The task brief

        Returns:
            Best matching workspace profile or None
        """
        matches = self.match(task)
        if not matches:
            return None
        return matches[0].profile

    def create_task_instance(
        self,
        seed: SeedWorkspaceProfile,
        task: TaskBrief,
        task_id: str | None = None,
    ) -> TaskInstanceProfile:
        """
        Create a task instance from a seed profile.

        Args:
            seed: The seed profile to base the instance on
            task: The task brief for this instance
            task_id: Optional task ID for tracking

        Returns:
            TaskInstanceProfile ready for use
        """
        profile = create_task_instance_record(seed, task, task_id=task_id)
        self._task_instances[profile.workspace_id] = profile
        self._lineage_tracker.save_record(profile.lineage)
        return profile

    @property
    def seed_count(self) -> int:
        """Number of seed workspaces."""
        return len(self._seed_profiles)

    @property
    def instance_count(self) -> int:
        """Number of task instances."""
        return len(self._task_instances)

    @property
    def workspace_count(self) -> int:
        """Total number of workspaces (seeds + instances)."""
        return len(self._workspaces)


def build_registry(root: Path) -> WorkspaceRegistry:
    """
    Build a registry from a root directory.

    Discovers all workspaces and registers them.

    Args:
        root: Root directory of the repository

    Returns:
        Populated workspace registry
    """
    registry = WorkspaceRegistry(root)

    workspaces = registry.discover()
    seed_workspace_ids: set[str] = set()
    for ref in workspaces:
        profile = load_seed_profile(ref.path)
        if profile:
            registry.register_workspace(ref, profile)
            seed_workspace_ids.add(ref.workspace_id)

    for ref in workspaces:
        if ref.workspace_id in seed_workspace_ids:
            continue
        instance_profile = load_task_instance_profile(ref.path)
        if instance_profile:
            instance_profile.seed_profile = registry.get_seed_profile(
                instance_profile.lineage.seed_workspace_id
            )
            registry.register_task_instance(instance_profile)
        else:
            registry.register_workspace(ref)

    if registry.manifest:
        for seed_id in registry.manifest.seeds:
            if seed_id not in registry._workspaces:
                registry._validation_issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"Registry manifest lists seed {seed_id!r} but no "
                            "workspace with that id was discovered"
                        ),
                        field="seeds",
                    )
                )
            elif seed_id not in registry._seed_profiles:
                registry._validation_issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"Declared seed {seed_id!r} has no loadable "
                            "seed_profile.toml"
                        ),
                        field="seeds",
                    )
                )

    return registry
