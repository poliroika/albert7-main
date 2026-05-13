"""
Workspace lineage tracking.

Enables tracing task instances back to their seeds
and understanding the evolution of workspaces.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from umbrella.workspace_registry.models import (
    WorkspaceLineageRecord,
    TaskInstanceProfile,
    SeedWorkspaceProfile,
    TaskBrief,
    WorkspaceRef,
    WorkspaceType,
)


def create_task_instance_record(
    seed: SeedWorkspaceProfile,
    task: TaskBrief,
    task_id: str | None = None,
    parent_instance_id: str | None = None,
    creation_reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> TaskInstanceProfile:
    """
    Create a task instance record from a seed profile.

    Args:
        seed: The seed profile to base the instance on
        task: The task brief for this instance
        task_id: Optional task ID for tracking
        parent_instance_id: Optional parent instance ID for nested tasks
        creation_reason: Reason for creating this instance
        metadata: Optional additional metadata

    Returns:
        TaskInstanceProfile ready for use
    """
    # Create lineage record
    lineage = WorkspaceLineageRecord(
        lineage_id=str(uuid4())[:8],
        seed_workspace_id=seed.workspace_id,
        parent_instance_id=parent_instance_id,
        creation_timestamp=datetime.now(timezone.utc),
        creation_reason=creation_reason or f"Task: {task.description[:100]}",
        task_id=task.task_id or task_id,
        task_brief_summary=task.description[:500] if task.description else "",
        promotion_eligible=False,
    )

    # Create workspace ref (copy from seed)
    ref = WorkspaceRef(
        workspace_id=f"{seed.workspace_id}_instance_{lineage.lineage_id}",
        name=f"Instance of {seed.ref.name}",
        description=f"Task instance for: {task.description[:200]}",
        path=seed.ref.path.parent
        / "instances"
        / f"{seed.workspace_id}_instance_{lineage.lineage_id}",
        task_main_file=seed.ref.task_main_file,
        graph_file=seed.ref.graph_file,
        agents_dir=seed.ref.agents_dir,
        prompts_dir=seed.ref.prompts_dir,
        tools_allowlist_file=seed.ref.tools_allowlist_file,
        models_file=seed.ref.models_file,
        policies_file=seed.ref.policies_file,
        evals_dir=seed.ref.evals_dir,
        experiments_dir=seed.ref.experiments_dir,
        runs_dir=seed.ref.runs_dir,
        snapshots_dir=seed.ref.snapshots_dir,
        reports_dir=seed.ref.reports_dir,
        mutable_paths=list(seed.ref.mutable_paths),
    )

    # Create profile
    profile = TaskInstanceProfile(
        ref=ref,
        workspace_type=WorkspaceType.INSTANCE,
        lineage=lineage,
        seed_profile=seed,
        task_brief=task.description,
        task_class=task.task_class or "",
    )

    return profile


class LineageTracker:
    """
    Tracks lineage records for task instances.

    Manages persistence and retrieval of lineage information.
    """

    def __init__(self, storage_path: Path | None = None):
        """
        Initialize lineage tracker.

        Args:
            storage_path: Optional path to store lineage records
        """
        self._records: dict[str, WorkspaceLineageRecord] = {}
        self._storage_path = storage_path

        if storage_path and storage_path.exists():
            self._load_records()

    def _load_records(self) -> None:
        """Load existing records from storage."""
        if not self._storage_path:
            return

        lineage_file = self._storage_path / "lineage.json"
        if lineage_file.exists():
            try:
                data = json.loads(lineage_file.read_text(encoding="utf-8"))
                for record_data in data.get("records", []):
                    record = self._deserialize_record(record_data)
                    if record:
                        self._records[record.lineage_id] = record
            except Exception:
                pass

    def _deserialize_record(
        self, data: dict[str, Any]
    ) -> WorkspaceLineageRecord | None:
        """Deserialize a record from JSON data."""
        try:
            return WorkspaceLineageRecord(
                lineage_id=data.get("lineage_id", ""),
                seed_workspace_id=data.get("seed_workspace_id", ""),
                parent_instance_id=data.get("parent_instance_id"),
                creation_timestamp=datetime.fromisoformat(data["creation_timestamp"]),
                creation_reason=data.get("creation_reason", ""),
                task_id=data.get("task_id"),
                task_brief_summary=data.get("task_brief_summary", ""),
                promotion_eligible=data.get("promotion_eligible", False),
                promotion_candidate=data.get("promotion_candidate", False),
                promotion_score=data.get("promotion_score", 0.0),
            )
        except Exception:
            return None

    def _serialize_record(self, record: WorkspaceLineageRecord) -> dict[str, Any]:
        """Serialize a record to JSON-compatible dict."""
        return {
            "lineage_id": record.lineage_id,
            "seed_workspace_id": record.seed_workspace_id,
            "parent_instance_id": record.parent_instance_id,
            "creation_timestamp": record.creation_timestamp.isoformat(),
            "creation_reason": record.creation_reason,
            "task_id": record.task_id,
            "task_brief_summary": record.task_brief_summary,
            "promotion_eligible": record.promotion_eligible,
            "promotion_candidate": record.promotion_candidate,
            "promotion_score": record.promotion_score,
        }

    def save_record(self, record: WorkspaceLineageRecord) -> None:
        """Save a lineage record."""
        self._records[record.lineage_id] = record

        if self._storage_path:
            self._persist_records()

    def _persist_records(self) -> None:
        """Persist all records to storage."""
        if not self._storage_path:
            return

        self._storage_path.mkdir(parents=True, exist_ok=True)
        lineage_file = self._storage_path / "lineage.json"

        data = {
            "records": [self._serialize_record(r) for r in self._records.values()],
        }

        lineage_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_record(self, lineage_id: str) -> WorkspaceLineageRecord | None:
        """Get a lineage record by ID."""
        return self._records.get(lineage_id)

    def get_records_for_seed(self, seed_id: str) -> list[WorkspaceLineageRecord]:
        """Get all lineage records for a specific seed."""
        return [r for r in self._records.values() if r.seed_workspace_id == seed_id]

    def get_promotion_candidates(self) -> list[WorkspaceLineageRecord]:
        """Get all records that are candidates for promotion."""
        return [r for r in self._records.values() if r.promotion_candidate]

    def mark_promotion_candidate(
        self,
        lineage_id: str,
        score: float,
        reason: str = "",
    ) -> bool:
        """
        Mark a lineage record as a promotion candidate.

        Args:
            lineage_id: ID of the lineage record
            score: Promotion score (0.0 to 1.0)
            reason: Reason for marking as candidate

        Returns:
            True if successful, False if record not found
        """
        record = self._records.get(lineage_id)
        if not record:
            return False

        record.promotion_candidate = True
        record.promotion_score = score
        record.promotion_eligible = score >= 0.7  # Default threshold

        self.save_record(record)
        return True

    def get_lineage_history(self, lineage_id: str) -> list[WorkspaceLineageRecord]:
        """
        Get the full lineage history for a record.

        Returns all ancestor records up to the seed.
        """
        history = []
        current = self._records.get(lineage_id)

        while current:
            history.append(current)
            if current.parent_instance_id:
                current = self._records.get(current.parent_instance_id)
            else:
                break

        return history
