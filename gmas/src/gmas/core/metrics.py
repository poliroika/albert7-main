"""
Node and edge metrics for adaptive routing.

Tracking and updating:
- Reliability — percentage of successful executions
- Latency — average execution time
- Cost — token/monetary expenditure
- Quality — response quality score
- Throughput
"""

from collections import deque
from datetime import UTC, datetime
from typing import Any

import torch
from pydantic import BaseModel, Field

__all__ = [
    "EdgeMetrics",
    "ExponentialMovingAverage",
    # Aggregators
    "MetricAggregator",
    "MetricHistory",
    "MetricSnapshot",
    # Main tracker
    "MetricsTracker",
    # Data classes
    "NodeMetrics",
    "SlidingWindowAverage",
    "compute_composite_score",
    # Utility
    "compute_reliability_score",
]


class MetricSnapshot(BaseModel):
    timestamp: datetime
    value: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetricHistory(BaseModel):
    max_size: int = 1000
    snapshots: list[MetricSnapshot] = Field(default_factory=list)

    def add(self, value: float, metadata: dict[str, Any] | None = None) -> None:
        """Add a value snapshot, trimming history to max_size."""
        snapshot = MetricSnapshot(
            timestamp=datetime.now(UTC),
            value=value,
            metadata=metadata or {},
        )
        self.snapshots.append(snapshot)
        if len(self.snapshots) > self.max_size:
            self.snapshots = self.snapshots[-self.max_size :]

    def get_recent(self, n: int = 10) -> list[MetricSnapshot]:
        """Get the last n snapshots."""
        return self.snapshots[-n:]

    def get_since(self, since: datetime) -> list[MetricSnapshot]:
        """Return snapshots collected after the given time."""
        return [s for s in self.snapshots if s.timestamp >= since]

    def get_values(self) -> list[float]:
        """Return a list of numeric history values."""
        return [s.value for s in self.snapshots]

    @property
    def mean(self) -> float:
        """Mean of the history or 0.0 if empty."""
        values = self.get_values()
        return float(torch.mean(torch.tensor(values))) if values else 0.0

    @property
    def std(self) -> float:
        """Standard deviation of the history or 0.0 when insufficient data."""
        values = self.get_values()
        return float(torch.std(torch.tensor(values))) if len(values) > 1 else 0.0

    @property
    def last(self) -> float | None:
        """Last value or None if the history is empty."""
        return self.snapshots[-1].value if self.snapshots else None


class NodeMetrics(BaseModel):
    node_id: str

    total_executions: int = 0
    successful_executions: int = 0
    failed_executions: int = 0

    reliability: float = 1.0
    avg_latency_ms: float = 0.0
    avg_cost_tokens: float = 0.0
    avg_cost_usd: float = 0.0
    avg_quality: float = 1.0

    latency_history: MetricHistory = Field(default_factory=MetricHistory)
    quality_history: MetricHistory = Field(default_factory=MetricHistory)
    cost_history: MetricHistory = Field(default_factory=MetricHistory)

    last_execution: datetime | None = None
    last_success: datetime | None = None
    last_failure: datetime | None = None

    tags: set[str] = Field(default_factory=set)
    custom_metrics: dict[str, float] = Field(default_factory=dict)

    def record_execution(
        self,
        success: bool,
        latency_ms: float,
        cost_tokens: int = 0,
        cost_usd: float = 0.0,
        quality: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an agent execution and update reliability/latency/cost."""
        now = datetime.now(UTC)
        self.total_executions += 1
        self.last_execution = now

        if success:
            self.successful_executions += 1
            self.last_success = now
        else:
            self.failed_executions += 1
            self.last_failure = now

        self.reliability = self.successful_executions / self.total_executions

        self.latency_history.add(latency_ms, metadata)
        self.quality_history.add(quality, metadata)
        self.cost_history.add(float(cost_tokens), metadata)

        self.avg_latency_ms = self.latency_history.mean
        self.avg_quality = self.quality_history.mean
        self.avg_cost_tokens = self.cost_history.mean

        alpha = 0.1
        self.avg_cost_usd = alpha * cost_usd + (1 - alpha) * self.avg_cost_usd

    def get_composite_score(
        self,
        reliability_weight: float = 0.4,
        latency_weight: float = 0.2,
        cost_weight: float = 0.2,
        quality_weight: float = 0.2,
        latency_baseline_ms: float = 1000.0,
        cost_baseline_tokens: float = 1000.0,
    ) -> float:
        """Combine node metrics into a single score with the given weights."""
        latency_score = max(0.0, 1.0 - self.avg_latency_ms / latency_baseline_ms)

        cost_score = max(0.0, 1.0 - self.avg_cost_tokens / cost_baseline_tokens)

        return (
            reliability_weight * self.reliability
            + latency_weight * latency_score
            + cost_weight * cost_score
            + quality_weight * self.avg_quality
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize node metrics to a dict."""
        return {
            "node_id": self.node_id,
            "total_executions": self.total_executions,
            "successful_executions": self.successful_executions,
            "failed_executions": self.failed_executions,
            "reliability": self.reliability,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_cost_tokens": self.avg_cost_tokens,
            "avg_cost_usd": self.avg_cost_usd,
            "avg_quality": self.avg_quality,
            "last_execution": self.last_execution.isoformat() if self.last_execution else None,
            "tags": list(self.tags),
            "custom_metrics": self.custom_metrics,
        }


class EdgeMetrics(BaseModel):
    source_id: str
    target_id: str

    total_transitions: int = 0
    successful_transitions: int = 0

    weight: float = 1.0
    reliability: float = 1.0
    avg_latency_ms: float = 0.0
    avg_data_volume: float = 0.0

    latency_history: MetricHistory = Field(default_factory=MetricHistory)

    last_transition: datetime | None = None

    custom_metrics: dict[str, float] = Field(default_factory=dict)

    @property
    def edge_key(self) -> tuple[str, str]:
        """Edge key (source, target)."""
        return (self.source_id, self.target_id)

    def record_transition(
        self,
        success: bool,
        latency_ms: float = 0.0,
        data_volume: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an edge transition and update reliability/latency/traffic."""
        now = datetime.now(UTC)
        self.total_transitions += 1
        self.last_transition = now

        if success:
            self.successful_transitions += 1

        self.reliability = self.successful_transitions / self.total_transitions

        self.latency_history.add(latency_ms, metadata)
        self.avg_latency_ms = self.latency_history.mean

        alpha = 0.1
        self.avg_data_volume = alpha * data_volume + (1 - alpha) * self.avg_data_volume

    def get_effective_weight(
        self,
        reliability_factor: float = 0.5,
        latency_factor: float = 0.3,
        base_factor: float = 0.2,
        latency_baseline_ms: float = 100.0,
    ) -> float:
        """Compute the effective edge weight considering reliability and latency."""
        reliability_cost = 1.0 - self.reliability

        latency_cost = self.avg_latency_ms / latency_baseline_ms

        base_cost = 1.0 / max(self.weight, 0.01)

        return reliability_factor * reliability_cost + latency_factor * latency_cost + base_factor * base_cost

    def to_dict(self) -> dict[str, Any]:
        """Serialize edge metrics to a dict."""
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "total_transitions": self.total_transitions,
            "successful_transitions": self.successful_transitions,
            "weight": self.weight,
            "reliability": self.reliability,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_data_volume": self.avg_data_volume,
            "last_transition": self.last_transition.isoformat() if self.last_transition else None,
            "custom_metrics": self.custom_metrics,
        }


class MetricAggregator:
    def update(self, value: float) -> None:
        raise NotImplementedError

    def get_value(self) -> float:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError


class ExponentialMovingAverage(MetricAggregator):
    def __init__(self, alpha: float = 0.1, initial: float = 0.0):
        self.alpha = alpha
        self.value = initial
        self.initialized = False

    def update(self, value: float) -> None:
        """Update the EMA with a new value."""
        if not self.initialized:
            self.value = value
            self.initialized = True
        else:
            self.value = self.alpha * value + (1 - self.alpha) * self.value

    def get_value(self) -> float:
        """Current EMA value."""
        return self.value

    def reset(self) -> None:
        """Reset the EMA state."""
        self.value = 0.0
        self.initialized = False


class SlidingWindowAverage(MetricAggregator):
    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self.values: deque[float] = deque(maxlen=window_size)

    def update(self, value: float) -> None:
        """Add a value and maintain a fixed-size window."""
        self.values.append(value)

    def get_value(self) -> float:
        """Mean of the window values."""
        return float(torch.mean(torch.tensor(self.values))) if self.values else 0.0

    def reset(self) -> None:
        """Clear the window values."""
        self.values.clear()


class MetricsTracker:
    """Stores node/edge metrics and aggregates for routing."""

    def __init__(
        self,
        history_size: int = 1000,
        ema_alpha: float = 0.1,
    ):
        """Create a tracker with the given history size and EMA coefficient."""
        self._history_size = history_size
        self._ema_alpha = ema_alpha

        self._node_metrics: dict[str, NodeMetrics] = {}
        self._edge_metrics: dict[tuple[str, str], EdgeMetrics] = {}

        self._global_latency = ExponentialMovingAverage(alpha=ema_alpha)
        self._global_cost = ExponentialMovingAverage(alpha=ema_alpha)
        self._global_quality = ExponentialMovingAverage(alpha=ema_alpha)

    def _get_or_create_node(self, node_id: str) -> NodeMetrics:
        if node_id not in self._node_metrics:
            self._node_metrics[node_id] = NodeMetrics(
                node_id=node_id,
                latency_history=MetricHistory(max_size=self._history_size),
                quality_history=MetricHistory(max_size=self._history_size),
                cost_history=MetricHistory(max_size=self._history_size),
            )
        return self._node_metrics[node_id]

    def _get_or_create_edge(self, source_id: str, target_id: str) -> EdgeMetrics:
        key = (source_id, target_id)
        if key not in self._edge_metrics:
            self._edge_metrics[key] = EdgeMetrics(
                source_id=source_id,
                target_id=target_id,
                latency_history=MetricHistory(max_size=self._history_size),
            )
        return self._edge_metrics[key]

    def record_node_execution(
        self,
        node_id: str,
        success: bool,
        latency_ms: float,
        cost_tokens: int = 0,
        cost_usd: float = 0.0,
        quality: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a node execution and update global EMA metrics."""
        node = self._get_or_create_node(node_id)
        node.record_execution(
            success=success,
            latency_ms=latency_ms,
            cost_tokens=cost_tokens,
            cost_usd=cost_usd,
            quality=quality,
            metadata=metadata,
        )

        self._global_latency.update(latency_ms)
        self._global_cost.update(float(cost_tokens))
        self._global_quality.update(quality)

    def record_edge_transition(
        self,
        source_id: str,
        target_id: str,
        success: bool,
        latency_ms: float = 0.0,
        data_volume: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an edge transition and update its metrics."""
        edge = self._get_or_create_edge(source_id, target_id)
        edge.record_transition(
            success=success,
            latency_ms=latency_ms,
            data_volume=data_volume,
            metadata=metadata,
        )

    def set_edge_weight(self, source_id: str, target_id: str, weight: float) -> None:
        """Set the edge weight in the tracker (without recording history)."""
        edge = self._get_or_create_edge(source_id, target_id)
        edge.weight = weight

    def add_node_tag(self, node_id: str, tag: str) -> None:
        """Add a tag to a node."""
        node = self._get_or_create_node(node_id)
        node.tags.add(tag)

    def set_node_custom_metric(self, node_id: str, name: str, value: float) -> None:
        """Set an arbitrary custom metric for a node."""
        node = self._get_or_create_node(node_id)
        node.custom_metrics[name] = value

    def set_edge_custom_metric(self, source_id: str, target_id: str, name: str, value: float) -> None:
        """Set a custom metric for an edge."""
        edge = self._get_or_create_edge(source_id, target_id)
        edge.custom_metrics[name] = value

    def get_node_metrics(self, node_id: str) -> NodeMetrics | None:
        """Get node metrics or None."""
        return self._node_metrics.get(node_id)

    def get_edge_metrics(self, source_id: str, target_id: str) -> EdgeMetrics | None:
        """Get edge metrics or None."""
        return self._edge_metrics.get((source_id, target_id))

    def get_all_node_metrics(self) -> dict[str, NodeMetrics]:
        """Copy of the dict of all node metrics."""
        return dict(self._node_metrics)

    def get_all_edge_metrics(self) -> dict[tuple[str, str], EdgeMetrics]:
        """Copy of the dict of all edge metrics."""
        return dict(self._edge_metrics)

    def get_node_reliability(self, node_id: str) -> float:
        """Node reliability or 1.0 if no metrics are available."""
        node = self._node_metrics.get(node_id)
        return node.reliability if node else 1.0

    def get_edge_reliability(self, source_id: str, target_id: str) -> float:
        """Edge reliability or 1.0 if no metrics are available."""
        edge = self._edge_metrics.get((source_id, target_id))
        return edge.reliability if edge else 1.0

    def get_routing_weights(
        self,
        reliability_factor: float = 0.5,
        latency_factor: float = 0.3,
        base_factor: float = 0.2,
    ) -> dict[tuple[str, str], float]:
        """Compute effective edge weights based on their metrics."""
        weights = {}
        for key, edge in self._edge_metrics.items():
            weights[key] = edge.get_effective_weight(
                reliability_factor=reliability_factor,
                latency_factor=latency_factor,
                base_factor=base_factor,
            )
        return weights

    def get_node_scores(
        self,
        reliability_weight: float = 0.4,
        latency_weight: float = 0.2,
        cost_weight: float = 0.2,
        quality_weight: float = 0.2,
    ) -> dict[str, float]:
        """Return composite scoring values for all nodes."""
        scores = {}
        for node_id, node in self._node_metrics.items():
            scores[node_id] = node.get_composite_score(
                reliability_weight=reliability_weight,
                latency_weight=latency_weight,
                cost_weight=cost_weight,
                quality_weight=quality_weight,
            )
        return scores

    def get_unreliable_nodes(self, threshold: float = 0.5) -> list[str]:
        """List of nodes with reliability below the threshold."""
        return [node_id for node_id, node in self._node_metrics.items() if node.reliability < threshold]

    def get_unreliable_edges(self, threshold: float = 0.5) -> list[tuple[str, str]]:
        """List of edges with reliability below the threshold."""
        return [key for key, edge in self._edge_metrics.items() if edge.reliability < threshold]

    def suggest_pruning(
        self,
        node_reliability_threshold: float = 0.3,
        edge_reliability_threshold: float = 0.3,
        max_latency_ms: float = 5000.0,
    ) -> dict[str, Any]:
        """Suggest nodes/edges for removal based on the given thresholds."""
        prune_nodes = []
        prune_edges = []
        slow_nodes = []

        for node_id, node in self._node_metrics.items():
            if node.reliability < node_reliability_threshold:
                prune_nodes.append(node_id)
            elif node.avg_latency_ms > max_latency_ms:
                slow_nodes.append(node_id)

        for key, edge in self._edge_metrics.items():
            if edge.reliability < edge_reliability_threshold:
                prune_edges.append(key)

        return {
            "prune_nodes": prune_nodes,
            "prune_edges": prune_edges,
            "slow_nodes": slow_nodes,
        }

    def get_node_features(self, node_ids: list[str]) -> torch.Tensor:
        """Get the node feature matrix for GNN/routing."""
        features = []
        for node_id in node_ids:
            node = self._node_metrics.get(node_id)
            if node:
                features.append(
                    [
                        node.reliability,
                        node.avg_latency_ms / 1000.0,
                        node.avg_cost_tokens / 1000.0,
                        node.avg_quality,
                        min(node.total_executions / 100.0, 1.0),
                    ]
                )
            else:
                features.append([1.0, 0.0, 0.0, 1.0, 0.0])

        return torch.tensor(features, dtype=torch.float32)

    def get_edge_features(self, edges: list[tuple[str, str]]) -> torch.Tensor:
        """Get the edge feature matrix."""
        features = []
        for src, tgt in edges:
            edge = self._edge_metrics.get((src, tgt))
            if edge:
                features.append(
                    [
                        edge.weight,
                        edge.reliability,
                        edge.avg_latency_ms / 1000.0,
                        edge.avg_data_volume / 1000.0,
                    ]
                )
            else:
                features.append([1.0, 1.0, 0.0, 0.0])

        return torch.tensor(features, dtype=torch.float32)

    def to_dict(self) -> dict[str, Any]:
        """Serialize all metrics to a dict."""
        return {
            "nodes": {k: v.to_dict() for k, v in self._node_metrics.items()},
            "edges": {f"{k[0]}->{k[1]}": v.to_dict() for k, v in self._edge_metrics.items()},
            "global": {
                "avg_latency_ms": self._global_latency.get_value(),
                "avg_cost_tokens": self._global_cost.get_value(),
                "avg_quality": self._global_quality.get_value(),
            },
        }

    def reset(self) -> None:
        """Fully clear all metrics and aggregates."""
        self._node_metrics.clear()
        self._edge_metrics.clear()
        self._global_latency.reset()
        self._global_cost.reset()
        self._global_quality.reset()


def compute_reliability_score(
    successes: int,
    failures: int,
    prior_successes: int = 1,
    prior_failures: int = 1,
) -> float:
    """Beta-posterior reliability estimate with pseudo-counts."""
    alpha = successes + prior_successes
    beta = failures + prior_failures
    return alpha / (alpha + beta)


def compute_composite_score(
    reliability: float,
    latency_ms: float,
    cost: float,
    quality: float,
    weights: tuple[float, float, float, float] = (0.4, 0.2, 0.2, 0.2),
    latency_baseline: float = 1000.0,
    cost_baseline: float = 1000.0,
) -> float:
    """Combine metrics into a single score with normalised latency and cost."""
    w_rel, w_lat, w_cost, w_qual = weights

    lat_score = max(0.0, 1.0 - latency_ms / latency_baseline)
    cost_score = max(0.0, 1.0 - cost / cost_baseline)

    return w_rel * reliability + w_lat * lat_score + w_cost * cost_score + w_qual * quality
