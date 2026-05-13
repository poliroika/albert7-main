"""
Umbrella services bootstrap - central service registry.

This module provides the bootstrap that wires together all subsystems
into a coherent manager workflow.
"""

import logging
from pathlib import Path
from typing import Any

from umbrella.workspace_registry import WorkspaceRegistry
from umbrella.retrieval.service import RetrievalService
from umbrella.memory.store import MemoryStore
from umbrella.memory.models import MemoryConfig
from umbrella.control_plane.engine import ControlPlaneEngine
from umbrella.control_plane.models import ManagerTask
from umbrella.telemetry.store import TelemetryStore
from umbrella.telemetry.metrics import MetricsRegistry

log = logging.getLogger(__name__)


class UmbrellaServices:
    """Central service registry for the Umbrella manager system.

    This class bootstraps and provides access to all subsystems:
    - workspace_registry: catalog of available seed workspaces
    - retrieval: GMAS knowledge retrieval
    - memory: lesson and competency storage
    - control_plane: manager orchestration and decision making
    - telemetry: event tracking and metrics
    - evals: run evaluation and promotion policy
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        control_state_dir: Path | None = None,
        workspaces_root: Path | None = None,
        use_live_llm: bool = False,
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        llm_base_url: str | None = None,
        runtime_config: Any | None = None,
    ):
        """Initialize all Umbrella services.

        Args:
            repo_root: Repository root (defaults to CWD)
            control_state_dir: Directory for control plane state
            workspaces_root: Root directory for workspaces
            use_live_llm: Whether to use live LLM (vs degraded mode)
            llm_model: LLM model name
            llm_api_key: LLM API key
            llm_base_url: LLM base URL
            runtime_config: ``UmbrellaRuntimeConfig`` for budget, thresholds, etc.
        """
        self.runtime_config = runtime_config
        self.repo_root = repo_root or Path.cwd()
        self.control_state_dir = control_state_dir or self.repo_root / ".umbrella"
        self.workspaces_root = workspaces_root or self.repo_root / "workspaces"

        # Store LLM config for workspace runs
        self.use_live_llm = use_live_llm
        self.llm_model = llm_model
        self.llm_api_key = llm_api_key
        self.llm_base_url = llm_base_url

        # Ensure directories exist
        self.control_state_dir.mkdir(parents=True, exist_ok=True)

        # Initialize services in dependency order
        self._init_telemetry()
        self._init_memory()
        self._init_retrieval()
        self._init_registry()
        self._init_control_plane()

        log.info(
            f"Umbrella services initialized: registry={len(self.registry.get_all_workspace_ids())} workspaces, "
            f"retrieval={'gmas' if self.retrieval else 'none'}, "
            f"control_plane={'ready' if self.control_plane else 'none'}"
        )

    def _init_telemetry(self) -> None:
        """Initialize telemetry service."""
        telemetry_dir = self.control_state_dir / "telemetry"
        self.telemetry = TelemetryStore(telemetry_dir)
        self.metrics = MetricsRegistry()

    def _init_memory(self) -> None:
        """Initialize memory service."""
        try:
            from umbrella.memory.migrations import migrate_to_per_workspace

            migrate_to_per_workspace(self.repo_root)
        except Exception as e:
            log.warning("migrate_to_per_workspace skipped: %s", e)

        memory_dir = self.control_state_dir / "memory"
        config = MemoryConfig(memory_root=memory_dir)
        self.memory = MemoryStore(config)

    def _init_retrieval(self) -> None:
        """Initialize retrieval service."""
        try:
            self.retrieval = RetrievalService(self.repo_root)
            log.info("Retrieval service initialized")
        except Exception as e:
            log.warning(f"Retrieval service initialization failed: {e}")
            self.retrieval = None

    def _init_registry(self) -> None:
        """Initialize workspace registry."""
        self.registry = WorkspaceRegistry(
            root=self.repo_root,  # WorkspaceRegistry expects repository root
        )
        # Discover all workspaces
        discovered = self.registry.discover()
        # Try to load seed profiles for discovered workspaces
        from umbrella.workspace_registry.discovery import load_seed_profile

        seed_count = 0
        for ref in discovered:
            try:
                profile = load_seed_profile(ref.path)
                if profile is not None:
                    self.registry.register_workspace(ref, profile)
                    seed_count += 1
                else:
                    self.registry.register_workspace(ref)
            except Exception as e:
                # Not a seed workspace or failed to load - that's okay
                log.debug(f"Could not load seed profile for {ref.workspace_id}: {e}")
        log.info(
            f"Workspace registry initialized with {len(discovered)} workspaces ({seed_count} seed profiles loaded)"
        )

    def _init_control_plane(self) -> None:
        """Initialize control plane engine."""
        self.control_plane = ControlPlaneEngine(
            workspace_registry=self.registry,
            repo_root=self.repo_root,
            memory_store=self.memory,
            retrieval_service=self.retrieval,
            workspaces_root=self.workspaces_root,
            control_state_dir=self.control_state_dir,
            use_live_llm=self.use_live_llm,
            llm_model=self.llm_model,
            llm_api_key=self.llm_api_key,
            llm_base_url=self.llm_base_url,
            runtime_config=self.runtime_config,
        )

    def get_control_plane(self) -> ControlPlaneEngine:
        """Get the control plane engine."""
        return self.control_plane

    def get_registry(self) -> WorkspaceRegistry:
        """Get the workspace registry."""
        return self.registry

    def get_retrieval(self) -> RetrievalService | None:
        """Get the retrieval service."""
        return self.retrieval

    def get_memory(self) -> MemoryStore:
        """Get the memory store."""
        return self.memory

    def get_workspace_memory_store(self, workspace_id: str) -> MemoryStore:
        """Per-workspace :class:`~umbrella.memory.store.MemoryStore` (or manager root when id is empty)."""
        from umbrella.memory.paths import get_workspace_store

        return get_workspace_store(self.repo_root, workspace_id)

    def get_telemetry(self) -> TelemetryStore:
        """Get the telemetry store."""
        return self.telemetry

    def get_metrics(self) -> MetricsRegistry:
        """Get the metrics registry."""
        return self.metrics

    def create_task(
        self,
        task_input: str,
        task_id: str | None = None,
        workspace_id: str | None = None,
    ) -> ManagerTask:
        """Create a new manager task.

        Args:
            task_input: Raw task description
            task_id: Optional task ID (auto-generated if None)
            workspace_id: Optional workspace to use (auto-selected if None)

        Returns:
            Created ManagerTask
        """
        engine = self.get_control_plane()
        return engine.start_task(
            task_input=task_input,
            task_id=task_id,
            workspace_id=workspace_id,
        )

    def shutdown(self) -> None:
        """Shutdown all services gracefully."""
        log.info("Shutting down Umbrella services...")

        # Flush telemetry
        if self.telemetry:
            self.telemetry.flush_events()

        # Save metrics snapshot
        if self.metrics and self.telemetry:
            self.telemetry.save_metrics_snapshot(self.metrics, "final")

        log.info("Umbrella services shutdown complete")


def bootstrap_services(
    repo_root: Path | None = None,
    control_state_dir: Path | None = None,
    workspaces_root: Path | None = None,
    use_live_llm: bool = False,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    runtime_config: Any | None = None,
) -> UmbrellaServices:
    """Bootstrap all Umbrella services.

    This is the main entrypoint for initializing the Umbrella manager system.

    Args:
        repo_root: Repository root
        control_state_dir: Control state directory
        workspaces_root: Workspaces root directory
        use_live_llm: Whether to use live LLM (vs degraded mode)
        llm_model: LLM model name
        llm_api_key: LLM API key
        llm_base_url: LLM base URL
        runtime_config: ``UmbrellaRuntimeConfig`` for budget, thresholds, etc.

    Returns:
        Initialized UmbrellaServices instance
    """
    return UmbrellaServices(
        repo_root=repo_root,
        control_state_dir=control_state_dir,
        workspaces_root=workspaces_root,
        use_live_llm=use_live_llm,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        runtime_config=runtime_config,
    )
