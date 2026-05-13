"""Tests for src/core/metrics.py"""

import pytest

from gmas.core.metrics import (
    EdgeMetrics,
    ExponentialMovingAverage,
    MetricHistory,
    MetricsTracker,
    NodeMetrics,
    SlidingWindowAverage,
    compute_composite_score,
    compute_reliability_score,
)

# ─────────────────────────── MetricHistory ────────────────────────────────────


class TestMetricHistory:
    def test_empty(self):
        h = MetricHistory()
        assert h.mean == 0.0
        assert h.last is None
        assert h.get_values() == []

    def test_add_and_get_values(self):
        h = MetricHistory()
        h.add(1.0)
        h.add(2.0)
        h.add(3.0)
        assert h.get_values() == [1.0, 2.0, 3.0]

    def test_mean(self):
        h = MetricHistory()
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            h.add(v)
        assert abs(h.mean - 3.0) < 1e-5

    def test_std_single_value(self):
        h = MetricHistory()
        h.add(5.0)
        assert h.std == 0.0

    def test_std_multiple_values(self):
        h = MetricHistory()
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            h.add(v)
        assert h.std > 0.0

    def test_last(self):
        h = MetricHistory()
        h.add(10.0)
        h.add(20.0)
        assert h.last == 20.0

    def test_max_size_trim(self):
        h = MetricHistory(max_size=3)
        for v in range(10):
            h.add(float(v))
        assert len(h.snapshots) == 3
        assert h.get_values() == [7.0, 8.0, 9.0]

    def test_get_recent(self):
        h = MetricHistory()
        for v in range(20):
            h.add(float(v))
        recent = h.get_recent(5)
        assert len(recent) == 5
        assert recent[-1].value == 19.0

    def test_get_since(self):
        from datetime import UTC, datetime, timedelta

        h = MetricHistory()
        h.add(1.0)
        h.add(2.0)
        future = datetime.now(UTC) + timedelta(hours=1)
        result = h.get_since(future)
        assert len(result) == 0

    def test_add_with_metadata(self):
        h = MetricHistory()
        h.add(5.0, metadata={"tag": "test"})
        assert h.snapshots[0].metadata == {"tag": "test"}


# ─────────────────────────── NodeMetrics ─────────────────────────────────────


class TestNodeMetrics:
    def setup_method(self):
        self.nm = NodeMetrics(node_id="solver")

    def test_initial_state(self):
        assert self.nm.total_executions == 0
        assert self.nm.reliability == 1.0
        assert self.nm.avg_latency_ms == 0.0

    def test_record_success(self):
        self.nm.record_execution(success=True, latency_ms=100.0, cost_tokens=50)
        assert self.nm.total_executions == 1
        assert self.nm.successful_executions == 1
        assert self.nm.failed_executions == 0
        assert self.nm.reliability == 1.0
        assert self.nm.last_success is not None

    def test_record_failure(self):
        self.nm.record_execution(success=False, latency_ms=50.0)
        assert self.nm.total_executions == 1
        assert self.nm.failed_executions == 1
        assert self.nm.reliability == 0.0
        assert self.nm.last_failure is not None

    def test_reliability_mixed(self):
        self.nm.record_execution(success=True, latency_ms=100.0)
        self.nm.record_execution(success=True, latency_ms=100.0)
        self.nm.record_execution(success=False, latency_ms=100.0)
        # 2/3 ≈ 0.666
        assert abs(self.nm.reliability - 2 / 3) < 1e-5

    def test_avg_latency_updates(self):
        self.nm.record_execution(success=True, latency_ms=100.0)
        self.nm.record_execution(success=True, latency_ms=200.0)
        assert self.nm.avg_latency_ms == 150.0

    def test_avg_quality_updates(self):
        self.nm.record_execution(success=True, latency_ms=0.0, quality=0.8)
        self.nm.record_execution(success=True, latency_ms=0.0, quality=0.6)
        assert abs(self.nm.avg_quality - 0.7) < 1e-3

    def test_get_composite_score(self):
        self.nm.record_execution(success=True, latency_ms=100.0, quality=0.9)
        score = self.nm.get_composite_score()
        assert 0.0 <= score <= 1.0

    def test_composite_score_perfect(self):
        self.nm.record_execution(success=True, latency_ms=0.0, cost_tokens=0, quality=1.0)
        score = self.nm.get_composite_score()
        assert score > 0.8

    def test_to_dict(self):
        self.nm.record_execution(success=True, latency_ms=100.0)
        d = self.nm.to_dict()
        assert d["node_id"] == "solver"
        assert "total_executions" in d
        assert "reliability" in d

    def test_last_execution_updated(self):
        self.nm.record_execution(success=True, latency_ms=10.0)
        assert self.nm.last_execution is not None


# ─────────────────────────── EdgeMetrics ─────────────────────────────────────


class TestEdgeMetrics:
    def setup_method(self):
        self.em = EdgeMetrics(source_id="a", target_id="b")

    def test_edge_key(self):
        assert self.em.edge_key == ("a", "b")

    def test_initial_state(self):
        assert self.em.total_transitions == 0
        assert self.em.reliability == 1.0

    def test_record_success(self):
        self.em.record_transition(success=True, latency_ms=50.0)
        assert self.em.total_transitions == 1
        assert self.em.successful_transitions == 1
        assert self.em.reliability == 1.0

    def test_record_failure(self):
        self.em.record_transition(success=False, latency_ms=50.0)
        assert self.em.reliability == 0.0

    def test_get_effective_weight(self):
        self.em.record_transition(success=True, latency_ms=50.0)
        w = self.em.get_effective_weight()
        assert w >= 0.0

    def test_to_dict(self):
        self.em.record_transition(success=True, latency_ms=100.0)
        d = self.em.to_dict()
        assert d["source_id"] == "a"
        assert d["target_id"] == "b"

    def test_data_volume_ema(self):
        self.em.record_transition(success=True, data_volume=100.0)
        assert self.em.avg_data_volume > 0.0


# ─────────────────────────── EMA / SlidingWindow ─────────────────────────────


class TestExponentialMovingAverage:
    def test_initial_value(self):
        ema = ExponentialMovingAverage(alpha=0.5, initial=0.0)
        assert ema.get_value() == 0.0
        assert not ema.initialized

    def test_first_update_sets_value(self):
        ema = ExponentialMovingAverage(alpha=0.5, initial=0.0)
        ema.update(10.0)
        assert ema.get_value() == 10.0
        assert ema.initialized

    def test_second_update(self):
        ema = ExponentialMovingAverage(alpha=0.5)
        ema.update(10.0)
        ema.update(20.0)
        # EMA = 0.5*20 + 0.5*10 = 15
        assert abs(ema.get_value() - 15.0) < 1e-5

    def test_reset(self):
        ema = ExponentialMovingAverage(alpha=0.5)
        ema.update(10.0)
        ema.reset()
        assert ema.get_value() == 0.0
        assert not ema.initialized


class TestSlidingWindowAverage:
    def test_empty(self):
        swa = SlidingWindowAverage(window_size=5)
        assert swa.get_value() == 0.0

    def test_single_value(self):
        swa = SlidingWindowAverage(window_size=5)
        swa.update(10.0)
        assert swa.get_value() == 10.0

    def test_window_average(self):
        swa = SlidingWindowAverage(window_size=3)
        for v in [1.0, 2.0, 3.0]:
            swa.update(v)
        assert abs(swa.get_value() - 2.0) < 1e-5

    def test_window_evicts_old(self):
        swa = SlidingWindowAverage(window_size=2)
        swa.update(10.0)
        swa.update(20.0)
        swa.update(30.0)
        assert abs(swa.get_value() - 25.0) < 1e-5

    def test_reset(self):
        swa = SlidingWindowAverage(window_size=5)
        swa.update(10.0)
        swa.reset()
        assert swa.get_value() == 0.0


# ─────────────────────────── MetricsTracker ──────────────────────────────────


class TestMetricsTracker:
    def setup_method(self):
        self.tracker = MetricsTracker()

    def test_empty_node_metrics(self):
        assert self.tracker.get_node_metrics("nonexistent") is None

    def test_empty_edge_metrics(self):
        assert self.tracker.get_edge_metrics("a", "b") is None

    def test_record_node_execution(self):
        self.tracker.record_node_execution(
            "solver",
            success=True,
            latency_ms=100.0,
            cost_tokens=50,
            quality=0.9,
        )
        nm = self.tracker.get_node_metrics("solver")
        assert nm is not None
        assert nm.total_executions == 1

    def test_record_edge_transition(self):
        self.tracker.record_edge_transition("a", "b", success=True, latency_ms=50.0)
        em = self.tracker.get_edge_metrics("a", "b")
        assert em is not None
        assert em.total_transitions == 1

    def test_get_node_reliability_default(self):
        assert self.tracker.get_node_reliability("unknown") == 1.0

    def test_get_node_reliability_after_failures(self):
        self.tracker.record_node_execution("solver", success=False, latency_ms=100.0)
        assert self.tracker.get_node_reliability("solver") == 0.0

    def test_get_edge_reliability_default(self):
        assert self.tracker.get_edge_reliability("x", "y") == 1.0

    def test_set_edge_weight(self):
        self.tracker.set_edge_weight("a", "b", 2.5)
        em = self.tracker.get_edge_metrics("a", "b")
        assert em is not None
        assert em.weight == 2.5

    def test_add_node_tag(self):
        self.tracker.add_node_tag("solver", "reliable")
        nm = self.tracker.get_node_metrics("solver")
        assert nm is not None
        assert "reliable" in nm.tags

    def test_set_node_custom_metric(self):
        self.tracker.set_node_custom_metric("solver", "my_score", 0.95)
        nm = self.tracker.get_node_metrics("solver")
        assert nm is not None
        assert nm.custom_metrics["my_score"] == 0.95

    def test_set_edge_custom_metric(self):
        self.tracker.set_edge_custom_metric("a", "b", "load", 0.7)
        em = self.tracker.get_edge_metrics("a", "b")
        assert em is not None
        assert em.custom_metrics["load"] == 0.7

    def test_get_all_node_metrics(self):
        self.tracker.record_node_execution("a", success=True, latency_ms=100.0)
        self.tracker.record_node_execution("b", success=True, latency_ms=100.0)
        all_metrics = self.tracker.get_all_node_metrics()
        assert "a" in all_metrics
        assert "b" in all_metrics

    def test_get_all_edge_metrics(self):
        self.tracker.record_edge_transition("a", "b", success=True)
        self.tracker.record_edge_transition("b", "c", success=True)
        all_edges = self.tracker.get_all_edge_metrics()
        assert ("a", "b") in all_edges

    def test_get_routing_weights(self):
        self.tracker.record_edge_transition("a", "b", success=True, latency_ms=50.0)
        weights = self.tracker.get_routing_weights()
        assert ("a", "b") in weights
        assert weights[("a", "b")] >= 0.0

    def test_get_node_scores(self):
        self.tracker.record_node_execution("solver", success=True, latency_ms=100.0)
        scores = self.tracker.get_node_scores()
        assert "solver" in scores
        assert 0.0 <= scores["solver"] <= 1.0

    def test_get_unreliable_nodes(self):
        self.tracker.record_node_execution("bad_agent", success=False, latency_ms=100.0)
        self.tracker.record_node_execution("good_agent", success=True, latency_ms=100.0)
        unreliable = self.tracker.get_unreliable_nodes(threshold=0.5)
        assert "bad_agent" in unreliable
        assert "good_agent" not in unreliable

    def test_get_unreliable_edges(self):
        self.tracker.record_edge_transition("a", "b", success=False)
        unreliable = self.tracker.get_unreliable_edges(threshold=0.5)
        assert ("a", "b") in unreliable

    def test_suggest_pruning(self):
        self.tracker.record_node_execution("bad", success=False, latency_ms=100.0)
        self.tracker.record_node_execution("slow", success=True, latency_ms=9000.0)
        suggestions = self.tracker.suggest_pruning(
            node_reliability_threshold=0.5,
            max_latency_ms=5000.0,
        )
        assert "bad" in suggestions["prune_nodes"]
        assert "slow" in suggestions["slow_nodes"]

    def test_get_node_features(self):
        self.tracker.record_node_execution("n1", success=True, latency_ms=100.0)
        features = self.tracker.get_node_features(["n1", "n2"])
        assert features.shape == (2, 5)

    def test_get_edge_features(self):
        self.tracker.record_edge_transition("a", "b", success=True)
        features = self.tracker.get_edge_features([("a", "b"), ("b", "c")])
        assert features.shape == (2, 4)

    def test_to_dict(self):
        self.tracker.record_node_execution("n1", success=True, latency_ms=100.0)
        d = self.tracker.to_dict()
        assert "nodes" in d
        assert "edges" in d
        assert "global" in d

    def test_reset(self):
        self.tracker.record_node_execution("n1", success=True, latency_ms=100.0)
        self.tracker.reset()
        assert self.tracker.get_node_metrics("n1") is None

    def test_global_ema_updates(self):
        self.tracker.record_node_execution("n1", success=True, latency_ms=200.0, quality=0.8)
        d = self.tracker.to_dict()
        assert d["global"]["avg_latency_ms"] == 200.0


# ─────────────────────────── Utility Functions ────────────────────────────────


class TestComputeReliabilityScore:
    def test_all_successes(self):
        score = compute_reliability_score(successes=10, failures=0)
        # (10 + 1) / (10 + 0 + 2) ≈ 0.917
        assert score > 0.85

    def test_all_failures(self):
        score = compute_reliability_score(successes=0, failures=10)
        assert score < 0.2

    def test_equal_counts(self):
        score = compute_reliability_score(successes=5, failures=5, prior_successes=1, prior_failures=1)
        # (5+1)/(12) = 0.5
        assert abs(score - 0.5) < 1e-5

    def test_prior_only(self):
        score = compute_reliability_score(successes=0, failures=0)
        assert abs(score - 0.5) < 1e-5


class TestComputeCompositeScore:
    def test_perfect_metrics(self):
        score = compute_composite_score(
            reliability=1.0,
            latency_ms=0.0,
            cost=0.0,
            quality=1.0,
        )
        assert score == 1.0

    def test_zero_reliability(self):
        score = compute_composite_score(
            reliability=0.0,
            latency_ms=0.0,
            cost=0.0,
            quality=1.0,
        )
        assert score < 1.0

    def test_high_latency_penalizes(self):
        low_lat = compute_composite_score(reliability=1.0, latency_ms=0.0, cost=0.0, quality=1.0)
        high_lat = compute_composite_score(reliability=1.0, latency_ms=9999.0, cost=0.0, quality=1.0)
        assert low_lat > high_lat

    def test_custom_weights(self):
        score = compute_composite_score(
            reliability=1.0,
            latency_ms=0.0,
            cost=0.0,
            quality=1.0,
            weights=(1.0, 0.0, 0.0, 0.0),
        )
        assert abs(score - 1.0) < 1e-5


# ─────────────────────────── MetricAggregator abstract ───────────────────────


class TestMetricAggregatorAbstract:
    def test_update_raises_not_implemented(self):
        """Line 265: MetricAggregator.update raises NotImplementedError."""
        from gmas.core.metrics import MetricAggregator

        agg = MetricAggregator()
        with pytest.raises(NotImplementedError):
            agg.update(1.0)

    def test_get_value_raises_not_implemented(self):
        """Line 268: MetricAggregator.get_value raises NotImplementedError."""
        from gmas.core.metrics import MetricAggregator

        agg = MetricAggregator()
        with pytest.raises(NotImplementedError):
            agg.get_value()

    def test_reset_raises_not_implemented(self):
        """Line 271: MetricAggregator.reset raises NotImplementedError."""
        from gmas.core.metrics import MetricAggregator

        agg = MetricAggregator()
        with pytest.raises(NotImplementedError):
            agg.reset()


# ─────────────────────────── get_optimization_hints edge ─────────────────────


class TestGetOptimizationHintsEdgeReliability:
    def test_prune_edges_with_low_reliability(self):
        """Lines 504-505: get_optimization_hints adds edges with low reliability to prune_edges."""
        tracker = MetricsTracker()
        # Record multiple failed edge transitions to set low reliability
        tracker.record_edge_transition("src", "tgt", success=False, latency_ms=10.0)
        tracker.record_edge_transition("src", "tgt", success=False, latency_ms=10.0)
        tracker.record_edge_transition("src", "tgt", success=False, latency_ms=10.0)

        hints = tracker.suggest_pruning(edge_reliability_threshold=0.9)
        # Edge ("src", "tgt") should be in prune_edges since reliability < 0.9
        assert len(hints["prune_edges"]) > 0
