"""Tests for src/core/gnn.py"""

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import rustworkx as rx
import torch

pytest.importorskip(
    "torch_geometric", reason="torch-geometric is an optional dependency; install with: pip install torch-geometric"
)

from gmas.core.gnn import (
    DefaultFeatureGenerator,
    FeatureConfig,
    FeatureGenerator,
    GATRouter,
    GCNRouter,
    GNNModelType,
    GNNRouterInference,
    GraphSAGERouter,
    RoutingPrediction,
    RoutingStrategy,
    TrainingConfig,
    TrainingResult,
    create_gnn_router,
)

# ═══════════════════════════════════════════════════════════════
#  Enums
# ═══════════════════════════════════════════════════════════════


class TestGNNModelType:
    def test_values(self):
        assert GNNModelType.GCN == "gcn"
        assert GNNModelType.GAT == "gat"
        assert GNNModelType.SAGE == "sage"

    def test_all_members(self):
        assert set(GNNModelType) == {GNNModelType.GCN, GNNModelType.GAT, GNNModelType.SAGE}


class TestRoutingStrategy:
    def test_values(self):
        assert RoutingStrategy.ARGMAX == "argmax"
        assert RoutingStrategy.SOFTMAX_SAMPLE == "softmax_sample"
        assert RoutingStrategy.TOP_K == "top_k"
        assert RoutingStrategy.THRESHOLD == "threshold"


# ═══════════════════════════════════════════════════════════════
#  Pydantic models
# ═══════════════════════════════════════════════════════════════


class TestFeatureConfig:
    def test_defaults(self):
        cfg = FeatureConfig()
        assert cfg.node_feature_dim == 64
        assert cfg.edge_feature_dim == 16
        assert cfg.embedding_dim == 128
        assert cfg.use_embeddings is True
        assert cfg.use_metrics is True
        assert cfg.use_centrality is True
        assert cfg.use_structural is True
        assert cfg.normalize_features is True
        assert cfg.clip_outliers is True
        assert cfg.outlier_std == 3.0

    def test_custom(self):
        cfg = FeatureConfig(node_feature_dim=32, normalize_features=False)
        assert cfg.node_feature_dim == 32
        assert cfg.normalize_features is False


class TestTrainingConfig:
    def test_defaults(self):
        cfg = TrainingConfig()
        assert cfg.learning_rate == 1e-3
        assert cfg.hidden_dim == 128
        assert cfg.num_layers == 3
        assert cfg.epochs == 100
        assert cfg.task == "node_classification"

    def test_custom(self):
        cfg = TrainingConfig(learning_rate=0.01, epochs=50)
        assert cfg.learning_rate == 0.01
        assert cfg.epochs == 50


class TestRoutingPrediction:
    def test_creation(self):
        pred = RoutingPrediction(
            recommended_nodes=["A", "B"],
            scores={"A": 0.8, "B": 0.2},
            confidence=0.8,
            strategy=RoutingStrategy.ARGMAX,
        )
        assert pred.recommended_nodes == ["A", "B"]
        assert pred.scores == {"A": 0.8, "B": 0.2}
        assert pred.confidence == 0.8


class TestTrainingResult:
    def test_creation(self):
        result = TrainingResult(
            train_losses=[1.0, 0.8, 0.6],
            val_losses=[1.1, 0.9, 0.7],
            best_epoch=2,
            best_val_loss=0.7,
        )
        assert result.best_epoch == 2
        assert result.best_val_loss == 0.7


# ═══════════════════════════════════════════════════════════════
#  Helpers — build a minimal mock graph
# ═══════════════════════════════════════════════════════════════


def _make_rx_graph(node_ids: list[str], edges: list[tuple[str, str]]):
    """Create a simple rustworkx digraph with named nodes."""
    g = rx.PyDiGraph()
    idx_map = {}
    for nid in node_ids:
        idx = g.add_node({"id": nid})
        idx_map[nid] = idx
    for src, tgt in edges:
        g.add_edge(idx_map[src], idx_map[tgt], {"weight": 1.0})
    return g, idx_map


class MockRoleGraph:
    """Minimal mock RoleGraph for GNN tests."""

    def __init__(self, node_ids, edges):
        self.node_ids = node_ids
        self.graph, self._idx_map = _make_rx_graph(node_ids, edges)
        self.embeddings: torch.Tensor | None = None
        self.num_nodes: int | None = None

    @property
    def edge_index(self):
        len(self.node_ids)
        idx_to_pos = {self._idx_map[nid]: i for i, nid in enumerate(self.node_ids)}
        rows, cols = [], []
        for src, tgt, _ in self.graph.weighted_edge_list():
            if src in idx_to_pos and tgt in idx_to_pos:
                rows.append(idx_to_pos[src])
                cols.append(idx_to_pos[tgt])
        if not rows:
            return torch.zeros((2, 0), dtype=torch.long)
        return torch.tensor([rows, cols], dtype=torch.long)


# ═══════════════════════════════════════════════════════════════
#  DefaultFeatureGenerator
# ═══════════════════════════════════════════════════════════════


class TestDefaultFeatureGenerator:
    def setup_method(self):
        self.gen = DefaultFeatureGenerator()
        self.graph = MockRoleGraph(["A", "B", "C"], [("A", "B"), ("B", "C")])

    def test_generate_node_features_shape(self):
        features = self.gen.generate_node_features(self.graph, ["A", "B", "C"])
        assert isinstance(features, torch.Tensor)
        assert features.shape[0] == 3
        assert features.dtype == torch.float32

    def test_generate_node_features_empty_nodes(self):
        # Without any features, should return identity
        gen = DefaultFeatureGenerator(
            FeatureConfig(
                use_embeddings=False,
                use_metrics=False,
                use_structural=False,
                use_centrality=False,
            )
        )
        features = gen.generate_node_features(self.graph, ["A", "B", "C"])
        assert features.shape == (3, 3)  # identity matrix

    def test_generate_node_features_no_normalize(self):
        gen = DefaultFeatureGenerator(FeatureConfig(normalize_features=False))
        features = gen.generate_node_features(self.graph, ["A", "B", "C"])
        assert features.shape[0] == 3

    def test_generate_edge_features_basic(self):
        edges = [("A", "B"), ("B", "C")]
        features = self.gen.generate_edge_features(self.graph, edges)
        assert isinstance(features, torch.Tensor)
        assert features.shape[0] == 2

    def test_generate_edge_features_empty(self):
        features = self.gen.generate_edge_features(self.graph, [])
        assert isinstance(features, torch.Tensor)

    def test_generate_edge_features_no_normalize(self):
        gen = DefaultFeatureGenerator(FeatureConfig(normalize_features=False))
        edges = [("A", "B")]
        features = gen.generate_edge_features(self.graph, edges)
        assert features.shape[0] == 1

    def test_normalize_regular_tensor(self):
        gen = DefaultFeatureGenerator()
        t = torch.tensor([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])
        normalized = gen._normalize(t)
        assert normalized.shape == t.shape

    def test_normalize_empty_tensor(self):
        gen = DefaultFeatureGenerator()
        t = torch.zeros((0, 4))
        result = gen._normalize(t)
        assert result.shape == (0, 4)

    def test_normalize_with_no_clipping(self):
        gen = DefaultFeatureGenerator(FeatureConfig(clip_outliers=False))
        t = torch.tensor([[100.0, -100.0], [0.0, 0.0], [50.0, -50.0]])
        result = gen._normalize(t)
        assert result.shape == t.shape

    def test_generate_node_features_with_embeddings(self):
        """Test when graph has embeddings attribute."""
        graph_with_emb = MockRoleGraph(["A", "B"], [("A", "B")])
        graph_with_emb.embeddings = torch.randn(2, 32)
        gen = DefaultFeatureGenerator(FeatureConfig(use_structural=False, use_centrality=False))
        features = gen.generate_node_features(graph_with_emb, ["A", "B"])
        assert features.shape[0] == 2

    def test_generate_node_features_with_none_embeddings(self):
        """Test when graph has embeddings=None."""
        graph_with_emb = MockRoleGraph(["A", "B"], [("A", "B")])
        graph_with_emb.embeddings = None
        gen = DefaultFeatureGenerator()
        features = gen.generate_node_features(graph_with_emb, ["A", "B"])
        assert features.shape[0] == 2

    def test_get_edge_weight_existing_edge(self):
        gen = DefaultFeatureGenerator()
        weight = gen._get_edge_weight(self.graph, "A", "B")
        assert weight == 1.0

    def test_get_edge_weight_missing_node(self):
        gen = DefaultFeatureGenerator()
        # "UNKNOWN" is not in the graph, so src_idx is None and we don't call get_edge_data
        weight = gen._get_edge_weight(self.graph, "A", "UNKNOWN")
        assert weight == 1.0  # default

    def test_structural_features_node_not_in_graph(self):
        gen = DefaultFeatureGenerator()
        features = gen._get_structural_features(self.graph, ["A", "UNKNOWN"])
        assert features.shape[0] == 2
        # Unknown node should have 0,0
        assert features[1, 0] == 0.0
        assert features[1, 1] == 0.0

    def test_centrality_features_shape(self):
        gen = DefaultFeatureGenerator()
        features = gen._get_centrality_features(self.graph, ["A", "B", "C"])
        assert features.shape == (3, 1)


# ═══════════════════════════════════════════════════════════════
#  GNN Router models
# ═══════════════════════════════════════════════════════════════


class TestGCNRouter:
    def test_init(self):
        model = GCNRouter(
            in_channels=8,
            hidden_channels=16,
            out_channels=4,
            num_layers=2,
        )
        assert model.in_channels == 8
        assert model.out_channels == 4

    def test_forward_pass(self):
        model = GCNRouter(
            in_channels=4,
            hidden_channels=8,
            out_channels=2,
            num_layers=2,
        )
        model.eval()
        x = torch.randn(3, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)
        out = model(x, edge_index)
        assert out.shape == (3, 2)

    def test_reset_parameters(self):
        model = GCNRouter(
            in_channels=4,
            hidden_channels=8,
            out_channels=2,
        )
        model.reset_parameters()  # Should not raise


class TestGATRouter:
    def test_init(self):
        model = GATRouter(
            in_channels=8,
            hidden_channels=16,
            out_channels=4,
            heads=2,
        )
        assert model.heads == 2

    def test_forward_pass(self):
        model = GATRouter(
            in_channels=4,
            hidden_channels=8,
            out_channels=2,
            heads=2,
        )
        model.eval()
        x = torch.randn(3, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)
        out = model(x, edge_index)
        assert out.shape == (3, 2)


class TestGraphSAGERouter:
    def test_init(self):
        model = GraphSAGERouter(
            in_channels=8,
            hidden_channels=16,
            out_channels=4,
        )
        assert model.in_channels == 8

    def test_forward_pass(self):
        model = GraphSAGERouter(
            in_channels=4,
            hidden_channels=8,
            out_channels=2,
        )
        model.eval()
        x = torch.randn(3, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)
        out = model(x, edge_index)
        assert out.shape == (3, 2)


# ═══════════════════════════════════════════════════════════════
#  create_gnn_router factory
# ═══════════════════════════════════════════════════════════════


class TestCreateGNNRouter:
    def test_create_gcn(self):
        model = create_gnn_router(GNNModelType.GCN, in_channels=8, out_channels=4)
        assert isinstance(model, GCNRouter)

    def test_create_gat(self):
        model = create_gnn_router(GNNModelType.GAT, in_channels=8, out_channels=4)
        assert isinstance(model, GATRouter)

    def test_create_sage(self):
        model = create_gnn_router(GNNModelType.SAGE, in_channels=8, out_channels=4)
        assert isinstance(model, GraphSAGERouter)

    def test_create_with_config(self):
        config = TrainingConfig(hidden_dim=32, num_layers=2)
        model = create_gnn_router(GNNModelType.GCN, in_channels=8, out_channels=2, config=config)
        assert isinstance(model, GCNRouter)
        assert model.hidden_channels == 32

    def test_create_unknown_type_raises(self):
        class FakeType(str):
            __slots__ = ()

        with pytest.raises((ValueError, Exception)):
            create_gnn_router(cast("GNNModelType", FakeType("unknown")), in_channels=8, out_channels=4)


# ═══════════════════════════════════════════════════════════════
#  GNNRouterInference
# ═══════════════════════════════════════════════════════════════


def _make_inference_setup():
    """
    Create a minimal GCN router inference setup.

    structural features = 2 (in_deg, out_deg)
    embeddings/metrics/centrality disabled → in_channels=2
    """
    model = GCNRouter(
        in_channels=2,
        hidden_channels=8,
        out_channels=1,
        num_layers=1,
    )
    graph = MockRoleGraph(["A", "B", "C"], [("A", "B"), ("B", "C")])
    return model, graph


class TestGNNRouterInference:
    def setup_method(self):
        self.model, self.graph = _make_inference_setup()
        # Use only structural features (2-dim)
        gen = DefaultFeatureGenerator(
            FeatureConfig(
                use_embeddings=False,
                use_metrics=False,
                use_centrality=False,
                normalize_features=False,
            )
        )
        self.inference = GNNRouterInference(self.model, feature_generator=gen)

    def test_init(self):
        assert self.inference.device in ("cpu", "cuda")
        assert self.inference.model is not None

    def test_predict_returns_routing_prediction(self):
        pred = self.inference.predict(self.graph)
        assert isinstance(pred, RoutingPrediction)
        assert isinstance(pred.recommended_nodes, list)
        assert isinstance(pred.scores, dict)

    def test_predict_argmax_single_result(self):
        pred = self.inference.predict(self.graph, strategy=RoutingStrategy.ARGMAX)
        assert len(pred.recommended_nodes) == 1
        assert pred.strategy == RoutingStrategy.ARGMAX

    def test_predict_top_k(self):
        pred = self.inference.predict(self.graph, strategy=RoutingStrategy.TOP_K, top_k=2)
        assert len(pred.recommended_nodes) <= 2

    def test_predict_threshold(self):
        pred = self.inference.predict(self.graph, strategy=RoutingStrategy.THRESHOLD, threshold=0.0)
        assert isinstance(pred.recommended_nodes, list)

    def test_predict_softmax_sample(self):
        pred = self.inference.predict(self.graph, strategy=RoutingStrategy.SOFTMAX_SAMPLE)
        assert len(pred.recommended_nodes) == 1

    def test_predict_with_candidates_filter(self):
        pred = self.inference.predict(self.graph, candidates=["A", "B"])
        assert all(n in ["A", "B"] for n in pred.recommended_nodes)

    def test_predict_with_source_excluded(self):
        pred = self.inference.predict(self.graph, source="A")
        assert "A" not in pred.recommended_nodes

    def test_apply_strategy_empty_scores(self):
        result = self.inference._apply_strategy({}, RoutingStrategy.ARGMAX, 3, 0.5)
        assert result == []

    def test_apply_strategy_argmax(self):
        scores = {"A": 0.1, "B": 0.9, "C": 0.5}
        result = self.inference._apply_strategy(scores, RoutingStrategy.ARGMAX, 3, 0.5)
        assert result == ["B"]

    def test_apply_strategy_top_k(self):
        scores = {"A": 0.1, "B": 0.9, "C": 0.5}
        result = self.inference._apply_strategy(scores, RoutingStrategy.TOP_K, 2, 0.5)
        assert len(result) == 2
        assert "B" in result

    def test_apply_strategy_threshold(self):
        scores = {"A": 0.1, "B": 0.9, "C": 0.5}
        result = self.inference._apply_strategy(scores, RoutingStrategy.THRESHOLD, 3, 0.6)
        assert "B" in result
        assert "A" not in result

    def test_apply_strategy_softmax_sample(self):
        scores = {"A": 0.3, "B": 0.7}
        result = self.inference._apply_strategy(scores, RoutingStrategy.SOFTMAX_SAMPLE, 3, 0.5)
        assert len(result) == 1
        assert result[0] in ["A", "B"]

    def test_apply_strategy_unknown_returns_empty(self):
        scores = {"A": 0.5}
        # Pass an unknown strategy value
        result = self.inference._apply_strategy(scores, cast("RoutingStrategy", "unknown_strategy"), 3, 0.5)
        assert result == []


# ═══════════════════════════════════════════════════════════════
#  GNNTrainer (basic tests without actual training)
# ═══════════════════════════════════════════════════════════════


class TestGNNTrainer:
    def test_init(self):
        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=2)
        trainer = GNNTrainer(model)
        assert trainer.model is not None
        assert trainer.config is not None
        assert trainer.device in ("cpu", "cuda")

    def test_init_with_config(self):
        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=2)
        config = TrainingConfig(learning_rate=0.01)
        trainer = GNNTrainer(model, config=config)
        assert trainer.config.learning_rate == 0.01

    def test_save_and_load(self, tmp_path):
        import torch.serialization

        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=2)
        trainer = GNNTrainer(model)

        checkpoint_path = tmp_path / "model.pt"
        trainer.save(checkpoint_path)

        assert checkpoint_path.exists()

        # Load - allow unpickling of TrainingConfig
        with torch.serialization.safe_globals([TrainingConfig, TrainingResult]):
            trainer.load(checkpoint_path)  # Should not raise

    def test_train_node_classification(self):
        """Test train() with node_classification task."""
        from torch_geometric.data import Data

        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=3, num_layers=1)
        config = TrainingConfig(task="node_classification", epochs=2, batch_size=2)
        trainer = GNNTrainer(model, config=config)

        # Create minimal training data
        x = torch.randn(3, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)
        y = torch.tensor([0, 1, 2], dtype=torch.long)
        data = Data(x=x, edge_index=edge_index, y=y)

        result = trainer.train([data], verbose=False)
        assert len(result.train_losses) == 2
        assert result.best_epoch == 0

    def test_train_path_ranking(self):
        """Test train() with path_ranking task using _compute_ranking_loss."""
        from torch_geometric.data import Data

        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=1, num_layers=1)
        config = TrainingConfig(task="path_ranking", epochs=2, batch_size=2)
        trainer = GNNTrainer(model, config=config)

        x = torch.randn(3, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)
        y = torch.tensor([0.1, 0.5, 0.9], dtype=torch.float)
        data = Data(x=x, edge_index=edge_index, y=y)

        result = trainer.train([data], verbose=False)
        assert len(result.train_losses) == 2

    def test_train_mse_task(self):
        """Test train() with edge_prediction task → hits else/mse_loss branch."""
        from torch_geometric.data import Data

        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=1, num_layers=1)
        config = TrainingConfig(task="edge_prediction", epochs=2, batch_size=2)
        trainer = GNNTrainer(model, config=config)

        x = torch.randn(3, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)
        y = torch.tensor([0.1, 0.5, 0.9], dtype=torch.float)
        data = Data(x=x, edge_index=edge_index, y=y)

        result = trainer.train([data], verbose=False)
        assert len(result.train_losses) == 2

    def test_train_with_val_data_early_stop(self):
        """Test train() with validation data and early stopping."""
        from torch_geometric.data import Data

        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=3, num_layers=1)
        config = TrainingConfig(task="node_classification", epochs=10, batch_size=2, patience=2)
        trainer = GNNTrainer(model, config=config)

        x = torch.randn(3, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)
        y = torch.tensor([0, 1, 2], dtype=torch.long)
        data = Data(x=x, edge_index=edge_index, y=y)

        result = trainer.train([data], val_data=[data], verbose=True)
        assert len(result.train_losses) >= 1
        assert len(result.val_losses) >= 1

    def test_eval_epoch_node_classification(self):
        """Test _eval_epoch directly with node_classification."""
        from torch_geometric.data import Data
        from torch_geometric.loader import DataLoader

        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=3, num_layers=1)
        config = TrainingConfig(task="node_classification", epochs=1)
        trainer = GNNTrainer(model, config=config)

        x = torch.randn(3, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)
        y = torch.tensor([0, 1, 2], dtype=torch.long)
        data = Data(x=x, edge_index=edge_index, y=y)
        loader = DataLoader([data], batch_size=2)

        loss = trainer._eval_epoch(loader)
        assert loss >= 0.0

    def test_eval_epoch_mse(self):
        """Test _eval_epoch with edge_prediction (mse) task."""
        from torch_geometric.data import Data
        from torch_geometric.loader import DataLoader

        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=1, num_layers=1)
        config = TrainingConfig(task="edge_prediction", epochs=1)
        trainer = GNNTrainer(model, config=config)

        x = torch.randn(3, 4)
        edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)
        y = torch.tensor([0.1, 0.5, 0.9], dtype=torch.float)
        data = Data(x=x, edge_index=edge_index, y=y)
        loader = DataLoader([data], batch_size=2)

        loss = trainer._eval_epoch(loader)
        assert loss >= 0.0


# ═══════════════════════════════════════════════════════════════
#  Additional DefaultFeatureGenerator coverage tests
# ═══════════════════════════════════════════════════════════════


def _make_rx_graph_agent_style(node_ids: list[str], edges: list[tuple[str, str]]):
    """Create a rustworkx graph where node data has agent_id attribute."""
    g = rx.PyDiGraph()
    idx_map = {}
    for nid in node_ids:
        # Use an object with agent_id attribute (like real AgentProfile)
        class NodeObj:
            def __init__(self, aid):
                self.agent_id = aid

        idx = g.add_node(NodeObj(nid))
        idx_map[nid] = idx
    for src, tgt in edges:
        g.add_edge(idx_map[src], idx_map[tgt], {"weight": 1.0})
    return g, idx_map


def _make_rx_graph_plain(node_ids: list[str], edges: list[tuple[str, str]]):
    """Create a rustworkx graph where node data is a plain string (else branch)."""
    g = rx.PyDiGraph()
    idx_map = {}
    for _i, nid in enumerate(node_ids):
        idx = g.add_node(nid)  # plain string, not dict, not agent_id object
        idx_map[nid] = idx
    for src, tgt in edges:
        g.add_edge(idx_map[src], idx_map[tgt], {"weight": 1.0})
    return g, idx_map


class MockRoleGraphAgentStyle:
    """Mock graph with agent_id-style nodes."""

    def __init__(self, node_ids, edges):
        self.node_ids = node_ids
        self.graph, self._idx_map = _make_rx_graph_agent_style(node_ids, edges)

    @property
    def edge_index(self):
        len(self.node_ids)
        idx_to_pos = {self._idx_map[nid]: i for i, nid in enumerate(self.node_ids)}
        rows, cols = [], []
        for src, tgt, _ in self.graph.weighted_edge_list():
            if src in idx_to_pos and tgt in idx_to_pos:
                rows.append(idx_to_pos[src])
                cols.append(idx_to_pos[tgt])
        if not rows:
            return torch.zeros((2, 0), dtype=torch.long)
        return torch.tensor([rows, cols], dtype=torch.long)


class MockRoleGraphPlainNodes:
    """Mock graph with plain string nodes (hits else branch)."""

    def __init__(self, node_ids, edges):
        self.node_ids = node_ids
        self.graph, self._idx_map = _make_rx_graph_plain(node_ids, edges)

    @property
    def edge_index(self):
        idx_to_pos = {self._idx_map[nid]: i for i, nid in enumerate(self.node_ids)}
        rows, cols = [], []
        for src, tgt, _ in self.graph.weighted_edge_list():
            if src in idx_to_pos and tgt in idx_to_pos:
                rows.append(idx_to_pos[src])
                cols.append(idx_to_pos[tgt])
        if not rows:
            return torch.zeros((2, 0), dtype=torch.long)
        return torch.tensor([rows, cols], dtype=torch.long)


class TestDefaultFeatureGeneratorExtraBranches:
    """Tests to cover additional branches in DefaultFeatureGenerator."""

    def test_generate_node_features_with_metrics_tracker(self):
        """Cover lines 146-147: metrics_tracker provided with use_metrics=True."""
        graph = MockRoleGraph(["A", "B"], [("A", "B")])

        # Mock metrics_tracker with get_node_features
        mock_tracker = MagicMock()
        mock_tracker.get_node_features.return_value = torch.ones(2, 4)

        gen = DefaultFeatureGenerator(
            FeatureConfig(
                use_embeddings=False,
                use_metrics=True,
                use_structural=False,
                use_centrality=False,
                normalize_features=False,
            )
        )
        features = gen.generate_node_features(graph, ["A", "B"], metrics_tracker=mock_tracker)
        mock_tracker.get_node_features.assert_called_once_with(["A", "B"])
        assert features.shape == (2, 4)

    def test_generate_edge_features_with_metrics_tracker_has_edge_metrics(self):
        """Cover lines 177-185: metrics_tracker with get_edge_metrics returning non-None."""
        graph = MockRoleGraph(["A", "B"], [("A", "B")])

        mock_edge_metrics = MagicMock()
        mock_edge_metrics.reliability = 0.9
        mock_edge_metrics.avg_latency_ms = 50.0
        mock_edge_metrics.avg_data_volume = 1000.0

        mock_tracker = MagicMock()
        mock_tracker.get_edge_metrics.return_value = mock_edge_metrics

        gen = DefaultFeatureGenerator(FeatureConfig(normalize_features=False))
        features = gen.generate_edge_features(graph, [("A", "B")], metrics_tracker=mock_tracker)
        assert features.shape[0] == 1
        # Should have weight + reliability + latency + volume = 4 features
        assert features.shape[1] == 4

    def test_generate_edge_features_with_metrics_tracker_no_edge_metrics(self):
        """Cover lines 186-187: metrics_tracker.get_edge_metrics returns None/falsy."""
        graph = MockRoleGraph(["A", "B"], [("A", "B")])

        mock_tracker = MagicMock()
        mock_tracker.get_edge_metrics.return_value = None  # returns None → extend([1.0, 0.0, 0.0])

        gen = DefaultFeatureGenerator(FeatureConfig(normalize_features=False))
        features = gen.generate_edge_features(graph, [("A", "B")], metrics_tracker=mock_tracker)
        assert features.shape[0] == 1
        # weight + 3 defaults = 4 features
        assert features.shape[1] == 4

    def test_structural_features_agent_style_nodes(self):
        """Cover lines 222-225: node data has agent_id attribute."""
        graph = MockRoleGraphAgentStyle(["A", "B", "C"], [("A", "B"), ("B", "C")])
        gen = DefaultFeatureGenerator()
        features = gen._get_structural_features(graph, ["A", "B", "C"])
        assert features.shape == (3, 2)

    def test_structural_features_plain_string_nodes(self):
        """Cover lines 224-225: node data is neither dict nor has agent_id."""
        graph = MockRoleGraphPlainNodes(["A", "B", "C"], [("A", "B"), ("B", "C")])
        gen = DefaultFeatureGenerator()
        features = gen._get_structural_features(graph, ["A", "B", "C"])
        assert features.shape == (3, 2)

    def test_centrality_features_agent_style_nodes(self):
        """Cover lines 250-253: node data has agent_id in centrality."""
        graph = MockRoleGraphAgentStyle(["A", "B", "C"], [("A", "B"), ("B", "C")])
        gen = DefaultFeatureGenerator()
        features = gen._get_centrality_features(graph, ["A", "B", "C"])
        assert features.shape == (3, 1)

    def test_centrality_features_plain_string_nodes(self):
        """Cover lines 252-253: else branch in centrality."""
        graph = MockRoleGraphPlainNodes(["A", "B", "C"], [("A", "B"), ("B", "C")])
        gen = DefaultFeatureGenerator()
        features = gen._get_centrality_features(graph, ["A", "B", "C"])
        assert features.shape == (3, 1)

    def test_centrality_features_node_not_in_pagerank(self):
        """Cover line 262: node not in pagerank result → append [0.0]."""
        # Create graph where "UNKNOWN" is in node_ids but not in graph's actual nodes
        graph = MockRoleGraph(["A", "B"], [("A", "B")])
        gen = DefaultFeatureGenerator()
        features = gen._get_centrality_features(graph, ["A", "B", "UNKNOWN"])
        assert features.shape == (3, 1)
        assert features[2, 0] == 0.0  # UNKNOWN → 0.0

    def test_centrality_features_exception_fallback(self):
        """Cover lines 265-267: exception during centrality computation."""
        # Create a mock graph where rx.pagerank raises
        from unittest.mock import patch

        graph = MockRoleGraph(["A", "B"], [("A", "B")])
        gen = DefaultFeatureGenerator()

        with patch("rustworkx.pagerank", side_effect=ValueError("pagerank failed")):
            features = gen._get_centrality_features(graph, ["A", "B"])
        assert features.shape == (2, 1)
        assert torch.all(features == 0.0)

    def test_get_edge_weight_agent_style_nodes(self):
        """Cover lines 279-282: node data has agent_id in _get_edge_weight."""
        graph = MockRoleGraphAgentStyle(["A", "B"], [("A", "B")])
        gen = DefaultFeatureGenerator()
        weight = gen._get_edge_weight(graph, "A", "B")
        assert weight == 1.0

    def test_get_edge_weight_plain_nodes(self):
        """Cover lines 281-282: else branch in _get_edge_weight."""
        graph = MockRoleGraphPlainNodes(["A", "B"], [("A", "B")])
        gen = DefaultFeatureGenerator()
        # The node id lookup won't match since data is string "A" not dict/agent
        # but the else branch handles it: nid = str(idx)
        weight = gen._get_edge_weight(graph, "A", "B")
        # With plain string nodes, str(idx) != "A", so both src_idx and tgt_idx are None → return 1.0
        assert weight == 1.0

    def test_get_edge_weight_exception(self):
        """Cover lines 293-294: exception in _get_edge_weight → return 1.0."""
        gen = DefaultFeatureGenerator()

        # Create a mock graph whose inner rx_graph.get_edge_data raises RuntimeError
        class MockGraph:
            node_ids: list[str] = ["A", "B"]  # noqa: RUF012

            class _FakeRxGraph:
                def node_indices(self):
                    return [0, 1]

                def get_node_data(self, idx):
                    return {"id": ["A", "B"][idx]}

                def get_edge_data(self, src, tgt):
                    msg = "edge err"
                    raise RuntimeError(msg)

            graph = _FakeRxGraph()

        weight = gen._get_edge_weight(MockGraph(), "A", "B")
        assert weight == 1.0


# ═══════════════════════════════════════════════════════════════
#  GNNRouterInference extra coverage
# ═══════════════════════════════════════════════════════════════


class TestGNNRouterInferenceExtra:
    """Extra tests for GNNRouterInference to cover missing lines."""

    def _make_inference(self, use_structural_only=True):
        model = GCNRouter(
            in_channels=2,
            hidden_channels=8,
            out_channels=1,
            num_layers=1,
        )
        gen = DefaultFeatureGenerator(
            FeatureConfig(
                use_embeddings=False,
                use_metrics=False,
                use_centrality=False,
                normalize_features=False,
            )
        )
        return GNNRouterInference(model, feature_generator=gen)

    def test_predict_graph_without_node_ids(self):
        """Cover line 628: graph without node_ids attribute uses num_nodes."""
        model = GCNRouter(in_channels=2, hidden_channels=8, out_channels=1, num_layers=1)
        gen = DefaultFeatureGenerator(
            FeatureConfig(
                use_embeddings=False,
                use_metrics=False,
                use_centrality=False,
                normalize_features=False,
            )
        )
        inference = GNNRouterInference(model, feature_generator=gen)

        # Create a mock graph without node_ids
        class GraphWithoutNodeIds:
            num_nodes = 3

            def __init__(self):
                g = rx.PyDiGraph()
                for _ in range(3):
                    g.add_node({})
                g.add_edge(0, 1, {})
                g.add_edge(1, 2, {})
                self.graph = g
                self.edge_index = torch.tensor([[0, 1], [1, 2]], dtype=torch.long)

        graph = GraphWithoutNodeIds()
        pred = inference.predict(graph)
        assert isinstance(pred, RoutingPrediction)
        assert len(pred.scores) == 3

    def test_predict_with_non_tensor_features(self):
        """Cover line 638: node_features is not a Tensor."""
        model = GCNRouter(in_channels=4, hidden_channels=8, out_channels=1, num_layers=1)

        # Mock feature generator that returns a list instead of tensor
        class ListFeatureGenerator(FeatureGenerator):
            def generate_node_features(
                self, graph: Any, node_ids: list[str], metrics_tracker: Any | None = None
            ) -> Any:
                return [[0.1, 0.2, 0.3, 0.4] for _ in node_ids]  # list, not tensor

            def generate_edge_features(
                self, graph: Any, edges: list[tuple[str, str]], metrics_tracker: Any | None = None
            ) -> torch.Tensor:
                return torch.ones(len(edges), 4)

        inference = GNNRouterInference(model, feature_generator=ListFeatureGenerator())
        graph = MockRoleGraph(["A", "B", "C"], [("A", "B"), ("B", "C")])
        pred = inference.predict(graph)
        assert isinstance(pred, RoutingPrediction)

    def test_predict_with_non_tensor_edge_index(self):
        """Cover line 643: edge_index is not a Tensor."""
        model = GCNRouter(in_channels=2, hidden_channels=8, out_channels=1, num_layers=1)
        gen = DefaultFeatureGenerator(
            FeatureConfig(
                use_embeddings=False,
                use_metrics=False,
                use_centrality=False,
                normalize_features=False,
            )
        )
        inference = GNNRouterInference(model, feature_generator=gen)

        # Create graph with list-based edge_index
        class GraphWithListEdgeIndex:
            node_ids: list[str] = ["A", "B", "C"]  # noqa: RUF012

            def __init__(self):
                self.graph, self._idx_map = _make_rx_graph(["A", "B", "C"], [("A", "B"), ("B", "C")])
                self.edge_index = [[0, 1], [1, 2]]  # list, not tensor

        graph = GraphWithListEdgeIndex()
        pred = inference.predict(graph)
        assert isinstance(pred, RoutingPrediction)

    def test_get_all_scores(self):
        """Cover lines 708-714: get_all_scores method."""
        inference = self._make_inference()
        graph = MockRoleGraph(["A", "B", "C"], [("A", "B"), ("B", "C")])
        # Add num_nodes attribute for get_all_scores
        graph.num_nodes = 3

        scores = inference.get_all_scores(graph)
        assert isinstance(scores, dict)
        assert len(scores) == 3
        assert all(isinstance(v, float) for v in scores.values())

    def test_load_classmethod(self, tmp_path):
        """Cover lines 719-732: GNNRouterInference.load classmethod."""
        import torch.serialization

        # Create and save a trainer checkpoint
        from gmas.core.gnn import GNNTrainer

        model = GCNRouter(in_channels=64, hidden_channels=32, out_channels=2, num_layers=2)
        config = TrainingConfig(hidden_dim=32, num_classes=2, num_layers=2)
        trainer = GNNTrainer(model, config=config)
        checkpoint_path = tmp_path / "router.pt"
        trainer.save(checkpoint_path)

        # Load via GNNRouterInference.load
        with torch.serialization.safe_globals([TrainingConfig, TrainingResult]):
            loaded_inference = GNNRouterInference.load(checkpoint_path)

        assert loaded_inference is not None
        assert isinstance(loaded_inference, GNNRouterInference)
