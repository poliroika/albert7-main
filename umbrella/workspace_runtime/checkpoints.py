"""
Checkpoint support for workspace runtime.

Enables resumability after interruption.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from umbrella.workspace_runtime.models import (
    WorkspaceCheckpoint,
    WorkspaceLifecycleStage,
)


class CheckpointStore:
    """
    Manages checkpoint storage for workspace runs.

    Provides resumability after interruption.
    """

    def __init__(self, checkpoints_dir: Path):
        """
        Initialize the checkpoint store.

        Args:
            checkpoints_dir: Directory for checkpoint storage
        """
        self._checkpoints_dir = Path(checkpoints_dir)
        self._checkpoints: dict[str, WorkspaceCheckpoint] = {}
        self._index: dict[str, str] = {}

        for checkpoint in self._checkpoints.values():
            self._index[checkpoint.checkpoint_id] = checkpoint

        self._checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self._load_checkpoints()

    @property
    def checkpoints_dir(self) -> Path:
        """Get checkpoints directory."""
        return self._checkpoints_dir

    def create_checkpoint(
        self,
        instance_id: str,
        run_id: str,
        stage: WorkspaceLifecycleStage,
        state: dict[str, Any],
    ) -> WorkspaceCheckpoint:
        """
        Create a new checkpoint.

        Args:
            instance_id: Instance ID for the workspace
            run_id: Run ID for this checkpoint
            stage: Current lifecycle stage
            state: State to save

        Returns:
            WorkspaceCheckpoint
        """
        checkpoint = WorkspaceCheckpoint(
            checkpoint_id=str(uuid4())[:8],
            instance_id=instance_id,
            run_id=run_id,
            stage=stage,
            state=state,
        )
        checkpoint.checkpoint_path = self._get_checkpoint_path(instance_id, run_id)
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint
        self._persist_checkpoint(checkpoint)
        return checkpoint

    def load_checkpoint(self, checkpoint_id: str) -> WorkspaceCheckpoint | None:
        """
        Load a checkpoint by ID.

        Args:
            checkpoint_id: The checkpoint ID

        Returns:
            WorkspaceCheckpoint if found, None otherwise
        """
        return self._checkpoints.get(checkpoint_id)

    def get_latest_checkpoint(self, instance_id: str) -> WorkspaceCheckpoint | None:
        """
        Get the most recent checkpoint for an instance.

        Args:
            instance_id: Instance ID

        Returns:
            Most recent checkpoint or None if no checkpoints
        """
        checkpoints = [
            cp for cp in self._checkpoints.values() if cp.instance_id == instance_id
        ]
        if not checkpoints:
            return None
        checkpoints.sort(key=lambda c: c.created_at, reverse=True)
        return checkpoints[0]

    def list_checkpoints_for_instance(self, instance_id: str) -> list[dict[str, Any]]:
        """
        List checkpoint metadata for an instance.

        Args:
            instance_id: Instance ID

        Returns:
            List of checkpoint info
        """
        return [
            {
                "checkpoint_id": cp.checkpoint_id,
                "run_id": cp.run_id,
                "stage": cp.stage.value,
                "created_at": cp.created_at.isoformat(),
                "checkpoint_path": str(cp.checkpoint_path),
            }
            for cp in self._checkpoints.values()
            if cp.instance_id == instance_id
        ]

    def restore_checkpoint(self, checkpoint_id: str) -> WorkspaceCheckpoint | None:
        """
        Restore a workspace from a checkpoint.

        Args:
            checkpoint_id: The checkpoint ID

        Returns:
            The restored checkpoint, or None if not found
        """
        checkpoint = self._checkpoints.get(checkpoint_id)
        if not checkpoint:
            return None

        return checkpoint

    def _get_checkpoint_path(self, instance_id: str, run_id: str) -> Path:
        """Get the path for a checkpoint file."""
        return self.checkpoints_dir / f"{instance_id}_{run_id}_checkpoint.json"

    def _persist_checkpoint(self, checkpoint: WorkspaceCheckpoint) -> None:
        """Persist a checkpoint to disk."""
        self._checkpoints_dir.mkdir(parents=True, exist_ok=True)

        path = self._get_checkpoint_path(checkpoint.instance_id, checkpoint.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "instance_id": checkpoint.instance_id,
            "run_id": checkpoint.run_id,
            "stage": checkpoint.stage.value,
            "stage_progress": checkpoint.stage_progress,
            "state": checkpoint.state,
            "created_at": checkpoint.created_at.isoformat(),
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_checkpoints(self) -> None:
        """Load existing checkpoints from storage."""
        if not self.checkpoints_dir.exists():
            return

        for checkpoint_file in self.checkpoints_dir.glob("**/*_checkpoint.json"):
            try:
                data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                checkpoint = WorkspaceCheckpoint(
                    checkpoint_id=data["checkpoint_id"],
                    instance_id=data["instance_id"],
                    run_id=data["run_id"],
                    stage=WorkspaceLifecycleStage(data["stage"]),
                    stage_progress=data.get("stage_progress", 0.0),
                    state=data.get("state", {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                checkpoint.checkpoint_path = checkpoint_file
                self._checkpoints[checkpoint.checkpoint_id] = checkpoint
            except Exception:
                pass
