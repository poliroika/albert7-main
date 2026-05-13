"""
Telemetry storage and persistence.

This module provides utilities for storing and retrieving telemetry data,
including event logs and metrics snapshots.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from umbrella.telemetry.events import TelemetryEvent, EventType
from umbrella.telemetry.metrics import MetricsRegistry

log = logging.getLogger(__name__)


class TelemetryStore:
    """Storage backend for telemetry data."""

    def __init__(
        self,
        store_dir: Path,
        max_events_per_file: int = 1000,
    ):
        """Initialize the telemetry store.

        Args:
            store_dir: Directory to store telemetry data
            max_events_per_file: Maximum events per file before rotation
        """
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.max_events_per_file = max_events_per_file

        # Event logs
        self.events_dir = self.store_dir / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)

        # Metrics snapshots
        self.metrics_dir = self.store_dir / "metrics"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        # Current event buffer
        self._event_buffer: list[TelemetryEvent] = []
        self._current_event_file: Path | None = None

    def emit_event(self, event: TelemetryEvent) -> None:
        """Emit a telemetry event to storage.

        Args:
            event: The event to store
        """
        self._event_buffer.append(event)

        # Flush buffer if it's full
        if len(self._event_buffer) >= self.max_events_per_file:
            self.flush_events()

    def flush_events(self) -> None:
        """Flush buffered events to storage."""
        if not self._event_buffer:
            return

        # Create a new event file
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        event_file = self.events_dir / f"events_{timestamp}.jsonl"

        # Write events as JSONL
        with event_file.open("a", encoding="utf-8") as f:
            for event in self._event_buffer:
                f.write(json.dumps(event.to_dict()) + "\n")

        log.info(f"Wrote {len(self._event_buffer)} events to {event_file}")
        self._event_buffer.clear()

    def save_metrics_snapshot(
        self,
        registry: MetricsRegistry,
        name: str = "latest",
    ) -> Path:
        """Save a metrics snapshot to storage.

        Args:
            registry: The metrics registry to snapshot
            name: Name for the snapshot file

        Returns:
            Path to the saved snapshot file
        """
        snapshot_file = self.metrics_dir / f"{name}.json"

        snapshot_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": registry.get_all_metrics(),
        }

        snapshot_file.write_text(json.dumps(snapshot_data, indent=2), encoding="utf-8")

        log.info(f"Saved metrics snapshot to {snapshot_file}")
        return snapshot_file

    def load_metrics_snapshot(self, name: str = "latest") -> dict[str, Any] | None:
        """Load a metrics snapshot from storage.

        Args:
            name: Name of the snapshot file to load

        Returns:
            Snapshot data dictionary, or None if not found
        """
        snapshot_file = self.metrics_dir / f"{name}.json"

        if not snapshot_file.exists():
            log.warning(f"Metrics snapshot not found: {snapshot_file}")
            return None

        return json.loads(snapshot_file.read_text(encoding="utf-8"))

    def get_events(
        self,
        task_id: str | None = None,
        event_type: EventType | None = None,
        limit: int = 100,
    ) -> list[TelemetryEvent]:
        """Retrieve events from storage.

        Args:
            task_id: Filter by task ID
            event_type: Filter by event type
            limit: Maximum number of events to return

        Returns:
            List of matching events
        """
        events = []

        # Read from event files (most recent first)
        event_files = sorted(self.events_dir.glob("events_*.jsonl"), reverse=True)

        for event_file in event_files:
            if len(events) >= limit:
                break

            try:
                for line in event_file.read_text(encoding="utf-8").strip().split("\n"):
                    if not line:
                        continue

                    event_dict = json.loads(line)
                    event_type_str = event_dict.get("event_type")

                    # Parse event type
                    try:
                        parsed_event_type = EventType(event_type_str)
                    except ValueError:
                        continue

                    # Apply filters
                    if task_id and event_dict.get("task_id") != task_id:
                        continue
                    if event_type and parsed_event_type != event_type:
                        continue

                    events.append(
                        TelemetryEvent(
                            event_type=parsed_event_type,
                            timestamp=event_dict.get("timestamp", 0),
                            task_id=event_dict.get("task_id", ""),
                            workspace_id=event_dict.get("workspace_id", ""),
                            run_id=event_dict.get("run_id", ""),
                            instance_id=event_dict.get("instance_id", ""),
                            data=event_dict.get("data", {}),
                            source=event_dict.get("source", "umbrella"),
                            level=event_dict.get("level", "info"),
                        )
                    )

                    if len(events) >= limit:
                        break

            except Exception as e:
                log.error(f"Error reading event file {event_file}: {e}")

        return events

    def get_task_events(self, task_id: str) -> list[TelemetryEvent]:
        """Get all events for a specific task.

        Args:
            task_id: The task ID to filter by

        Returns:
            List of events for the task
        """
        return self.get_events(task_id=task_id, limit=10000)

    def get_event_summary(self, task_id: str) -> dict[str, Any]:
        """Get a summary of events for a task.

        Args:
            task_id: The task ID to summarize

        Returns:
            Summary dictionary with event counts and metadata
        """
        events = self.get_task_events(task_id)

        # Count events by type
        event_counts: dict[str, int] = {}
        level_counts = {"error": 0, "warning": 0, "info": 0}

        first_timestamp = None
        last_timestamp = None

        for event in events:
            # Count by type
            event_type_str = event.event_type.value
            event_counts[event_type_str] = event_counts.get(event_type_str, 0) + 1

            # Count by level
            level_counts[event.level] = level_counts.get(event.level, 0) + 1

            # Track timestamps
            if first_timestamp is None or event.timestamp < first_timestamp:
                first_timestamp = event.timestamp
            if last_timestamp is None or event.timestamp > last_timestamp:
                last_timestamp = event.timestamp

        return {
            "task_id": task_id,
            "total_events": len(events),
            "event_counts": event_counts,
            "level_counts": level_counts,
            "first_event_time": first_timestamp,
            "last_event_time": last_timestamp,
            "duration_seconds": (
                last_timestamp - first_timestamp
                if last_timestamp and first_timestamp
                else 0
            ),
        }

    def cleanup_old_events(self, keep_days: int = 30) -> int:
        """Clean up event files older than the specified number of days.

        Args:
            keep_days: Number of days of events to keep

        Returns:
            Number of files deleted
        """
        import time

        cutoff_time = time.time() - (keep_days * 86400)
        deleted_count = 0

        for event_file in self.events_dir.glob("events_*.jsonl"):
            if event_file.stat().st_mtime < cutoff_time:
                event_file.unlink()
                deleted_count += 1
                log.info(f"Deleted old event file: {event_file}")

        return deleted_count

    def export_events_to_file(
        self,
        output_path: Path,
        task_id: str | None = None,
        event_type: EventType | None = None,
    ) -> None:
        """Export events to a JSON file.

        Args:
            output_path: Path to write the export file
            task_id: Optional task ID filter
            event_type: Optional event type filter
        """
        events = self.get_events(task_id=task_id, event_type=event_type, limit=100000)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        export_data = {
            "export_time": datetime.now(timezone.utc).isoformat(),
            "filter_task_id": task_id,
            "filter_event_type": event_type.value if event_type else None,
            "event_count": len(events),
            "events": [e.to_dict() for e in events],
        }

        output_path.write_text(json.dumps(export_data, indent=2), encoding="utf-8")

        log.info(f"Exported {len(events)} events to {output_path}")


# Global telemetry store
_global_store: TelemetryStore | None = None


def get_telemetry_store(store_dir: Path | None = None) -> TelemetryStore:
    """Get the global telemetry store.

    Args:
        store_dir: Optional directory for the store (used on first call)

    Returns:
        The global telemetry store instance
    """
    global _global_store
    if _global_store is None:
        if store_dir is None:
            store_dir = Path("telemetry_data")
        _global_store = TelemetryStore(store_dir)
    return _global_store


def emit_event(event: TelemetryEvent) -> None:
    """Emit an event to the global telemetry store.

    Args:
        event: The event to emit
    """
    store = get_telemetry_store()
    store.emit_event(event)
