"""
Base adapter protocol for workspace adapters.

This module defines the abstract interface that workspace adapters
must implement to support different workspace types
without hardcoding assumptions about specific workspaces.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from umbrella.workspace_runtime.models import (
    PreparedWorkspace,
    WorkspaceInstance,
    WorkspaceRunRequest,
    WorkspaceRunResult,
    WorkspaceInspection,
    WorkspaceSnapshot,
    WorkspaceRunReport,
    RunReportNode,
    ArtifactRef,
)


class WorkspaceAdapter(ABC):
    """
    Abstract protocol for workspace adapters.

    Each workspace type (e.g., agent_research) should have its own adapter
    that implements this protocol.
    """

    @property
    @abstractmethod
    def workspace_type(self) -> str:
        """Return the type of workspace this adapter handles."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Return a description of this workspace type."""
        pass

    @abstractmethod
    def prepare(
        self,
        instance: WorkspaceInstance,
    ) -> PreparedWorkspace:
        """
        Prepare a workspace for execution.

        This is the first lifecycle stage. It should:
        - Validate configuration
        - Load agent profiles
        - Build tool registry
        - Set up directories
        - Prepare any workspace-specific resources
        """
        pass

    @abstractmethod
    def retrieve_context(self, instance: WorkspaceInstance) -> dict[str, Any]:
        """
        Retrieve context for the workspace.

        This could include:
        - Loading relevant documentation
        - Loading relevant code patterns
        - Loading similar past runs
        """
        pass

    @abstractmethod
    def run(
        self,
        instance: WorkspaceInstance,
        request: WorkspaceRunRequest,
    ) -> WorkspaceRunResult:
        """
        Run a workspace.

        This is the main execution stage. It should:
        - Execute the workspace
        - Capture artifacts
        - Record metrics
        - Handle errors
        - Return structured results
        """
        pass

    @abstractmethod
    def inspect(
        self,
        result: WorkspaceRunResult,
    ) -> WorkspaceInspection:
        """
        Inspect a run result.

        This should provide structured access to run state
        without reading raw logs.
        """
        pass

    @abstractmethod
    def list_artifacts(
        self,
        result: WorkspaceRunResult,
    ) -> list[ArtifactRef]:
        """List artifacts from a run result."""
        pass

    @abstractmethod
    def snapshot(
        self,
        instance: WorkspaceInstance,
        label: str,
        include_artifacts: bool = True,
    ) -> WorkspaceSnapshot:
        """
        Create a snapshot of a workspace instance.

        Snapshots enable checkpointing and rollback.
        """
        pass

    @abstractmethod
    def get_run_manifest(self, result: WorkspaceRunResult) -> dict[str, Any]:
        """Get the run manifest for a result."""
        pass

    @abstractmethod
    def get_log_summary(self, result: WorkspaceRunResult) -> str:
        """Get a log summary for a result."""
        pass

    @abstractmethod
    def supports_workspace(self, workspace_id: str) -> bool:
        """
        Check if this adapter supports a specific workspace.

        Args:
            workspace_id: Workspace ID to check

        Returns:
            True if supported, False otherwise
        """
        pass

    @abstractmethod
    def get_supported_workspace_types(self) -> list[str]:
        """Get list of supported workspace types."""
        pass


class BaseWorkspaceAdapter:
    """
    Base class for workspace adapters.

    Provides common functionality that can be inherited.
    """

    def __init__(self, instance: WorkspaceInstance):
        """
        Initialize the base adapter.

        Args:
            instance: The workspace instance
        """
        self.instance = instance
        self.path = instance.path

    def _log(self, message: str) -> None:
        """Log a message."""
        print(f"[{self.__class__.__name__}] {message}")

    def _error(self, message: str, details: dict[str, Any] | None = None) -> None:
        """Log an error."""
        print(f"[{self.__class__.__name__}] ERROR: {message}")
        if details:
            print(f"  Details: {details}")

    def _warning(self, message: str) -> None:
        """Log a warning."""
        print(f"[{self.__class__.__name__}] WARNING: {message}")

    def prepare(self) -> PreparedWorkspace:
        """
        Prepare a workspace for execution.

        Default implementation validates configuration and creates directories.
        """
        # Check if instance path exists
        if not self.instance.path.exists():
            return PreparedWorkspace(
                instance=self.instance,
                config_valid=False,
                validation_issues=["Instance path does not exist"],
                ready=False,
                not_ready_reason="Instance path does not exist",
            )

        # Create directories
        self._ensure_directories()
        return PreparedWorkspace(
            instance=self.instance,
            config_valid=True,
            validation_issues=[],
            ready=True,
            not_ready_reason=None,
        )

    def _ensure_directories(self) -> None:
        """Ensure required directories exist."""
        for dir_name in ["runs", "snapshots", "reports", "memory"]:
            dir_path = self.instance.path / dir_name
            dir_path.mkdir(parents=True, exist_ok=True)

    def retrieve_context(self) -> dict[str, Any]:
        """
        Retrieve context for this workspace.

        Default implementation returns empty context.
        """
        return {}

    def run(self, request: WorkspaceRunRequest) -> WorkspaceRunResult:
        """
        Run this workspace.

        Default implementation raises NotImplementedError.
        """
        raise NotImplementedError("Subclasses must implement run()")

    def inspect(self, result: WorkspaceRunResult) -> WorkspaceInspection:
        """
        Inspect a run result.

        Default implementation extracts basic information from result.
        """
        return WorkspaceInspection(
            run_id=result.run_id,
            workspace_id=result.workspace_id,
            status=result.status,
            final_answer=result.final_answer,
            errors=result.errors,
            total_tokens=result.total_tokens,
            duration_seconds=result.duration_seconds,
        )

    def list_artifacts(self, result: WorkspaceRunResult) -> list[ArtifactRef]:
        """List artifacts from a run result."""
        return result.artifacts

    def snapshot(
        self,
        instance: WorkspaceInstance,
        label: str,
        include_artifacts: bool = True,
    ) -> WorkspaceSnapshot:
        """
        Create a snapshot of a workspace instance.

        Default implementation creates a snapshot directory and metadata.
        """
        raise NotImplementedError("Subclasses should implement snapshot()")

    def get_run_manifest(self, result: WorkspaceRunResult) -> dict[str, Any]:
        """Get the run manifest for a result."""
        return {
            "run_id": result.run_id,
            "workspace_id": result.workspace_id,
            "status": result.status.value,
            "artifacts": [
                {
                    "id": a.artifact_id,
                    "type": a.artifact_type.value,
                    "path": str(a.path),
                }
                for a in result.artifacts
            ],
            "metrics": result.metrics,
        }

    def get_log_summary(self, result: WorkspaceRunResult) -> str:
        """Get a log summary for a result."""
        return f"Run {result.run_id} completed with status {result.status.value}"

    def supports_workspace(self, workspace_id: str) -> bool:
        """Check if this adapter supports a workspace."""
        return False

    def get_supported_workspace_types(self) -> list[str]:
        """Get list of supported workspace types."""
        return []

    def get_run_report(self, result: WorkspaceRunResult) -> WorkspaceRunReport:
        """
        Build a universal run report from a completed run.

        This default implementation produces a minimal single-node report.
        Subclasses should override to provide richer graph/event data
        specific to their workspace type.
        """
        node = RunReportNode(
            node_id="workspace",
            display_name=result.workspace_id or "workspace",
            status=result.status.value,
            tokens=result.total_tokens,
            duration_ms=result.duration_seconds * 1000,
            output_preview=(result.final_answer or "")[:500],
            error="; ".join(result.errors[:3]) if result.errors else "",
        )

        artifacts = [
            {
                "id": a.artifact_id,
                "type": a.artifact_type.value,
                "path": str(a.path),
                "description": a.description,
            }
            for a in result.artifacts
        ]

        return WorkspaceRunReport(
            run_id=result.run_id,
            workspace_id=result.workspace_id,
            workspace_type=type(self).__name__,
            status=result.status.value,
            duration_seconds=result.duration_seconds,
            total_tokens=result.total_tokens,
            nodes=[node],
            edges=[],
            events=[],
            artifacts=artifacts,
            errors=list(result.errors),
            summary=result.summary,
            final_answer=result.final_answer,
        )
