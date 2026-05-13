"""
Decision tracing - explainability and audit trail.

Tracks every control-plane decision for:
- Explainability after the fact
- Audit trail of manager reasoning
- Learning from past decisions
- Compliance review
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from umbrella.control_plane.models import (
    DecisionRecord,
    ActionType,
)

log = logging.getLogger(__name__)


# =============================================================================
# Decision Trace
# =============================================================================


@dataclass
class DecisionTrace:
    """Trace of all decisions made during a task."""

    task_id: str
    decisions: list[DecisionRecord] = field(default_factory=list)

    def add_decision(self, decision: DecisionRecord) -> None:
        """Add a decision to the trace."""
        self.decisions.append(decision)

    def get_decision_history(self) -> list[DecisionRecord]:
        """Get all decisions in chronological order."""
        return sorted(self.decisions, key=lambda d: d.created_at)

    def get_decisions_by_type(self, action_type: ActionType) -> list[DecisionRecord]:
        """Get all decisions of a specific type."""
        return [d for d in self.decisions if d.action.action_type == action_type]

    def get_recent_decisions(self, limit: int = 10) -> list[DecisionRecord]:
        """Get most recent decisions."""
        sorted_decisions = sorted(
            self.decisions, key=lambda d: d.created_at, reverse=True
        )
        return sorted_decisions[:limit]

    def summary(self) -> str:
        """Get a human-readable summary of the trace."""
        if not self.decisions:
            return f"No decisions made for task {self.task_id}"

        lines = [f"Decision trace for task {self.task_id}:"]
        lines.append(f"Total decisions: {len(self.decisions)}")

        # Count by action type
        from collections import Counter

        action_counts = Counter(d.action.action_type for d in self.decisions)
        lines.append("\nDecision types:")
        for action_type, count in action_counts.most_common():
            lines.append(f"  {action_type}: {count}")

        # Most recent decisions
        recent = self.get_recent_decisions(3)
        if recent:
            lines.append("\nRecent decisions:")
            for d in recent:
                lines.append(
                    f"  [{d.created_at}] {d.action.action_type}: {d.rationale.reason[:80]}"
                )

        return "\n".join(lines)


# =============================================================================
# Trace Manager
# =============================================================================


class TraceManager:
    """Manages decision traces for all tasks."""

    def __init__(self, trace_dir: Path):
        self.trace_dir = trace_dir
        self.trace_dir.mkdir(parents=True, exist_ok=True)

        # In-memory cache of active traces
        self._active_traces: dict[str, DecisionTrace] = {}

    def get_or_create_trace(self, task_id: str) -> DecisionTrace:
        """Get existing trace or create new one."""
        if task_id not in self._active_traces:
            self._active_traces[task_id] = DecisionTrace(task_id=task_id)

            # Try to load from disk
            loaded = self._load_trace(task_id)
            if loaded:
                self._active_traces[task_id] = loaded

        return self._active_traces[task_id]

    def save_trace(self, task_id: str) -> Path:
        """Save a trace to disk."""
        trace = self._active_traces.get(task_id)
        if not trace:
            raise ValueError(f"No trace found for task {task_id}")

        path = self.trace_dir / f"{task_id}_trace.jsonl"

        # Append all decisions as JSONL
        with open(path, "a", encoding="utf-8") as f:
            for decision in trace.decisions:
                f.write(json.dumps(decision.model_dump(), ensure_ascii=False))
                f.write("\n")

        log.debug(f"Saved trace for task {task_id} to {path}")
        return path

    def load_trace(self, task_id: str) -> DecisionTrace | None:
        """Load a trace from disk."""
        return self._load_trace(task_id)

    def _load_trace(self, task_id: str) -> DecisionTrace | None:
        """Load trace from disk (internal method)."""
        path = self.trace_dir / f"{task_id}_trace.jsonl"

        if not path.exists():
            return None

        trace = DecisionTrace(task_id=task_id)

        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        decision_data = json.loads(line)
                        decision = DecisionRecord(**decision_data)
                        trace.add_decision(decision)
                    except Exception as e:
                        log.warning(f"Failed to load decision from trace: {e}")

            log.info(
                f"Loaded trace for task {task_id} with {len(trace.decisions)} decisions"
            )
            return trace

        except Exception as e:
            log.error(f"Failed to load trace for task {task_id}: {e}")
            return None

    def finalize_trace(self, task_id: str) -> None:
        """Finalize a trace - save to disk and remove from memory."""
        if task_id not in self._active_traces:
            # No trace to finalize, just return
            return

        self.save_trace(task_id)
        if task_id in self._active_traces:
            del self._active_traces[task_id]


# =============================================================================
# Public API Functions
# =============================================================================


# Global trace manager (initialized lazily)
_global_trace_manager: TraceManager | None = None


def _get_trace_manager(trace_dir: Path | None = None) -> TraceManager:
    """Get or create the global trace manager."""
    global _global_trace_manager

    if _global_trace_manager is None:
        if trace_dir is None:
            trace_dir = Path(".umbrella/traces")
        _global_trace_manager = TraceManager(trace_dir)

    return _global_trace_manager


def trace_decision(
    decision: DecisionRecord,
    trace_dir: Path | None = None,
) -> None:
    """Record a decision in the trace.

    Args:
        decision: Decision to record
        trace_dir: Optional trace directory
    """
    manager = _get_trace_manager(trace_dir)
    trace = manager.get_or_create_trace(decision.task_id)
    trace.add_decision(decision)

    log.debug(
        f"Traced decision {decision.id}: {decision.action.action_type} "
        f"for task {decision.task_id}"
    )


def get_decision_history(
    task_id: str,
    trace_dir: Path | None = None,
    limit: int = 50,
) -> list[DecisionRecord]:
    """Get decision history for a task.

    Args:
        task_id: Task to get history for
        trace_dir: Optional trace directory
        limit: Max decisions to return

    Returns:
        List of decisions, newest first
    """
    manager = _get_trace_manager(trace_dir)
    trace = manager.get_or_create_trace(task_id)

    decisions = trace.get_recent_decisions(limit)
    return decisions


def explain_decision(
    decision_id: str,
    task_id: str,
    trace_dir: Path | None = None,
) -> str:
    """Explain a specific decision in human-readable form.

    Args:
        decision_id: Decision to explain
        task_id: Task containing the decision
        trace_dir: Optional trace directory

    Returns:
        Human-readable explanation
    """
    manager = _get_trace_manager(trace_dir)
    trace = manager.get_or_create_trace(task_id)

    for decision in trace.decisions:
        if decision.id == decision_id:
            lines = [
                f"Decision: {decision.action.action_type}",
                f"Task: {decision.task_id}",
                f"Time: {datetime.fromtimestamp(decision.created_at).isoformat()}",
                "",
                f"Reason: {decision.rationale.reason}",
                f"Confidence: {decision.rationale.confidence:.0%}",
                "",
                "Evidence:",
            ]

            for i, evidence in enumerate(decision.rationale.evidence, 1):
                lines.append(f"  {i}. {evidence}")

            if decision.rationale.alternatives_considered:
                lines.append("")
                lines.append("Alternatives considered:")
                for alt in decision.rationale.alternatives_considered:
                    why_not = decision.rationale.why_not_alternatives.get(
                        alt.value, "Not specified"
                    )
                    lines.append(f"  - {alt.value}: {why_not}")

            if decision.requires_approval:
                lines.append("")
                lines.append(f"Approval required: Yes (by {decision.approved_by})")

            return "\n".join(lines)

    return f"Decision {decision_id} not found in trace for task {task_id}"
