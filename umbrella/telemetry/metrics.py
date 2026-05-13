"""
Metrics aggregation and tracking for the manager system.

This module provides utilities for tracking and aggregating metrics
across workspace runs, patches, and system operations.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from umbrella.evals.models import EvaluationRecord, ComparisonReport, PatchOutcome
from umbrella.telemetry.events import TelemetryEvent, EventType


@dataclass
class RunMetrics:
    """Aggregated metrics for workspace runs."""

    # Counts
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    paused_runs: int = 0

    # Outcomes
    complete_tasks: int = 0
    partial_tasks: int = 0
    failed_tasks: int = 0

    # Resources
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0

    # Quality
    average_score: float = 0.0
    scores: list[float] = field(default_factory=list)

    # Iterations
    total_iterations: int = 0
    average_iterations_per_run: float = 0.0

    # Retrieval
    retrieval_hits: int = 0
    retrieval_useful_count: int = 0

    # Patches
    patches_applied: int = 0
    patches_successful: int = 0

    # Timestamps
    first_run_time: float | None = None
    last_run_time: float | None = None

    def add_eval(self, eval_record: EvaluationRecord) -> None:
        """Add an evaluation record to the metrics."""
        self.total_runs += 1

        # Status counts

        if eval_record.task_success.value == "complete":
            self.complete_tasks += 1
            self.successful_runs += 1
        elif eval_record.task_success.value == "partial":
            self.partial_tasks += 1
        elif eval_record.task_success.value == "failed":
            self.failed_tasks += 1
            self.failed_runs += 1

        # Resources
        self.total_tokens += eval_record.total_tokens
        self.total_cost_usd += eval_record.total_cost_usd
        self.total_duration_seconds += eval_record.total_duration_seconds

        # Quality
        self.scores.append(eval_record.overall_score)
        self.average_score = sum(self.scores) / len(self.scores)

        # Iterations
        if eval_record.iterations_to_completion:
            self.total_iterations += eval_record.iterations_to_completion
            self.average_iterations_per_run = (
                self.total_iterations / self.total_runs if self.total_runs > 0 else 0
            )

        # Retrieval
        self.retrieval_hits += eval_record.retrieval_hits_used
        if eval_record.retrieval_was_useful:
            self.retrieval_useful_count += 1

        # Patches
        self.patches_applied += eval_record.patches_applied

        # Timestamps
        if self.first_run_time is None:
            self.first_run_time = time.time()
        self.last_run_time = time.time()


@dataclass
class PatchMetrics:
    """Aggregated metrics for patches."""

    # Counts
    total_patches: int = 0
    improved_patches: int = 0
    regressed_patches: int = 0
    neutral_patches: int = 0
    inconclusive_patches: int = 0

    # Outcomes by type
    outcomes: dict[PatchOutcome, int] = field(default_factory=dict)

    # Average improvement
    average_improvement_magnitude: float = 0.0
    improvement_magnitudes: list[float] = field(default_factory=list)

    # Cost of patches
    total_patch_cost_usd: float = 0.0

    def add_comparison(self, comparison: ComparisonReport) -> None:
        """Add a comparison report to the metrics."""
        self.total_patches += 1

        outcome = comparison.overall_improvement
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

        if outcome == PatchOutcome.IMPROVED:
            self.improved_patches += 1
        elif outcome == PatchOutcome.REGRESSED:
            self.regressed_patches += 1
        elif outcome == PatchOutcome.NEUTRAL:
            self.neutral_patches += 1
        else:
            self.inconclusive_patches += 1

        # Track improvement magnitude
        from umbrella.evals.comparisons import get_improvement_magnitude

        magnitude = get_improvement_magnitude(comparison)
        self.improvement_magnitudes.append(magnitude)
        self.average_improvement_magnitude = (
            sum(self.improvement_magnitudes) / len(self.improvement_magnitudes)
            if self.improvement_magnitudes
            else 0
        )

        # Track cost
        self.total_patch_cost_usd += comparison.comparison_cost_usd

    @property
    def patch_success_rate(self) -> float:
        """Calculate the rate of successful patches (improved / total)."""
        if self.total_patches == 0:
            return 0.0
        return self.improved_patches / self.total_patches


@dataclass
class TelemetrySummary:
    """High-level summary of telemetry data."""

    # Event counts by type
    event_counts: dict[EventType, int] = field(default_factory=dict)

    # Total events
    total_events: int = 0

    # Events by level
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0

    # Timestamps
    first_event_time: float | None = None
    last_event_time: float | None = None

    def add_event(self, event: TelemetryEvent) -> None:
        """Add a telemetry event to the summary."""
        self.total_events += 1

        # Count by type
        self.event_counts[event.event_type] = (
            self.event_counts.get(event.event_type, 0) + 1
        )

        # Count by level
        if event.level == "error":
            self.error_count += 1
        elif event.level == "warning":
            self.warning_count += 1
        else:
            self.info_count += 1

        # Timestamps
        if self.first_event_time is None:
            self.first_event_time = event.timestamp
        self.last_event_time = event.timestamp


class MetricsRegistry:
    """Central registry for tracking metrics across the system."""

    def __init__(self) -> None:
        self._run_metrics: dict[str, RunMetrics] = defaultdict(RunMetrics)
        self._patch_metrics: dict[str, PatchMetrics] = defaultdict(PatchMetrics)
        self._telemetry_summary: dict[str, TelemetrySummary] = defaultdict(
            TelemetrySummary
        )
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._timings: dict[str, list[float]] = defaultdict(list)

    def record_run(self, workspace_id: str, eval_record: EvaluationRecord) -> None:
        """Record metrics for a workspace run."""
        self._run_metrics[workspace_id].add_eval(eval_record)

    def record_patch(self, workspace_id: str, comparison: ComparisonReport) -> None:
        """Record metrics for a patch comparison."""
        self._patch_metrics[workspace_id].add_comparison(comparison)

    def record_event(self, task_id: str, event: TelemetryEvent) -> None:
        """Record a telemetry event."""
        self._telemetry_summary[task_id].add_event(event)

    def increment_counter(self, name: str, value: int = 1) -> None:
        """Increment a named counter."""
        self._counters[name] += value

    def set_gauge(self, name: str, value: float) -> None:
        """Set a named gauge value."""
        self._gauges[name] = value

    def record_timing(self, name: str, duration_seconds: float) -> None:
        """Record a timing measurement."""
        self._timings[name].append(duration_seconds)

    def get_run_metrics(self, workspace_id: str) -> RunMetrics:
        """Get metrics for a specific workspace."""
        return self._run_metrics.get(workspace_id, RunMetrics())

    def get_patch_metrics(self, workspace_id: str) -> PatchMetrics:
        """Get patch metrics for a specific workspace."""
        return self._patch_metrics.get(workspace_id, PatchMetrics())

    def get_telemetry_summary(self, task_id: str) -> TelemetrySummary:
        """Get telemetry summary for a specific task."""
        return self._telemetry_summary.get(task_id, TelemetrySummary())

    def get_counter(self, name: str) -> int:
        """Get a counter value."""
        return self._counters.get(name, 0)

    def get_gauge(self, name: str) -> float | None:
        """Get a gauge value."""
        return self._gauges.get(name)

    def get_timing_stats(self, name: str) -> dict[str, float] | None:
        """Get statistics for a timing measurement."""
        timings = self._timings.get(name)
        if not timings:
            return None

        import statistics

        return {
            "count": len(timings),
            "min": min(timings),
            "max": max(timings),
            "mean": statistics.mean(timings),
            "median": statistics.median(timings),
            "stdev": statistics.stdev(timings) if len(timings) > 1 else 0.0,
        }

    def get_all_metrics(self) -> dict[str, Any]:
        """Get all metrics as a dictionary."""
        return {
            "run_metrics": {
                ws: {
                    "total_runs": m.total_runs,
                    "successful_runs": m.successful_runs,
                    "failed_runs": m.failed_runs,
                    "average_score": m.average_score,
                    "total_cost_usd": m.total_cost_usd,
                }
                for ws, m in self._run_metrics.items()
            },
            "patch_metrics": {
                ws: {
                    "total_patches": m.total_patches,
                    "improved_patches": m.improved_patches,
                    "patch_success_rate": m.patch_success_rate,
                    "average_improvement_magnitude": m.average_improvement_magnitude,
                }
                for ws, m in self._patch_metrics.items()
            },
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
        }


# Global metrics registry
_global_registry: MetricsRegistry | None = None


def get_metrics_registry() -> MetricsRegistry:
    """Get the global metrics registry."""
    global _global_registry
    if _global_registry is None:
        _global_registry = MetricsRegistry()
    return _global_registry
