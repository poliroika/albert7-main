"""
Tests for src/builder/embedding_builder.py.

All tests use the deterministic hash-based encoder (no sentence-transformers
download required) to keep the suite fast and self-contained.
"""

import pytest
import torch
from pydantic import ValidationError

from gmas.builder.embedding_builder import (
    EmbeddingBuilderConfig,
    EmbeddingGraphBuilder,
    LinkStrategy,
    _agent_text,
    _cosine_similarity_matrix,
    _edges_knn,
    _edges_mst,
    _edges_threshold,
    _ensure_dag,
    _infer_direction,
    _pick_start_end,
)
from gmas.core.agent import AgentProfile
from gmas.core.encoder import NodeEncoder

# ─────────────────────────── Helpers ──────────────────────────────────────────


def _hash_encoder() -> NodeEncoder:
    return NodeEncoder(model_name="hash:128")


def _make_agents(*specs: tuple[str, str, str]) -> list[AgentProfile]:
    """Create agents from (id, persona, description) tuples."""
    return [
        AgentProfile(
            agent_id=aid,
            display_name=aid.replace("_", " ").title(),
            persona=persona,
            description=desc,
        )
        for aid, persona, desc in specs
    ]


def _diverse_agents() -> list[AgentProfile]:
    """Four agents with intentionally different semantic profiles."""
    return _make_agents(
        ("researcher", "a web researcher", "Searches the web for information and news"),
        ("analyst", "a data analyst", "Analyzes data, computes statistics"),
        ("writer", "a content writer", "Writes articles and blog posts"),
        ("reviewer", "a quality reviewer", "Reviews and validates outputs"),
    )


def _similar_pair() -> list[AgentProfile]:
    """Two agents with very similar descriptions."""
    return _make_agents(
        ("writer_a", "a content writer", "Writes blog posts about technology"),
        ("writer_b", "a content writer", "Writes blog posts about science"),
    )


# ─────────────────────────── _agent_text ─────────────────────────────────────


class TestAgentText:
    def test_basic(self):
        a = AgentProfile(agent_id="x", display_name="X", persona="solver", description="solves things")
        assert "solver" in _agent_text(a)
        assert "solves things" in _agent_text(a)

    def test_with_tools(self):
        a = AgentProfile(agent_id="x", display_name="X", tools=["web_search", "code_interpreter"])
        text = _agent_text(a)
        assert "web_search" in text
        assert "code_interpreter" in text

    def test_fallback_to_id(self):
        a = AgentProfile(agent_id="my_agent", display_name="My Agent")
        assert _agent_text(a) == "my_agent"


# ───────────────────── _cosine_similarity_matrix ─────────────────────────────


class TestCosineSimilarity:
    def test_identity(self):
        emb = torch.randn(3, 64)
        sim = _cosine_similarity_matrix(emb)
        for i in range(3):
            assert sim[i, i].item() == pytest.approx(1.0, abs=1e-5)

    def test_symmetry(self):
        emb = torch.randn(4, 32)
        sim = _cosine_similarity_matrix(emb)
        assert torch.allclose(sim, sim.T, atol=1e-5)

    def test_range(self):
        emb = torch.randn(5, 16)
        sim = _cosine_similarity_matrix(emb)
        assert sim.min().item() >= -1.0 - 1e-5
        assert sim.max().item() <= 1.0 + 1e-5


# ───────────────────── Edge selection strategies ─────────────────────────────


class TestEdgesKNN:
    def test_k1(self):
        sim = torch.tensor(
            [
                [1.0, 0.9, 0.1],
                [0.9, 1.0, 0.2],
                [0.1, 0.2, 1.0],
            ]
        )
        ids = ["a", "b", "c"]
        edges = _edges_knn(sim, k=1, agent_ids=ids)
        sources = {(s, t) for s, t, _ in edges}
        assert ("a", "b") in sources
        assert ("b", "a") in sources
        assert len(edges) == 3  # a→b, b→a, c→b

    def test_k_exceeds_n(self):
        sim = torch.eye(2)
        sim[0, 1] = sim[1, 0] = 0.5
        edges = _edges_knn(sim, k=10, agent_ids=["a", "b"])
        assert len(edges) == 2  # each connects to the other


class TestEdgesThreshold:
    def test_basic(self):
        sim = torch.tensor(
            [
                [1.0, 0.8, 0.3],
                [0.8, 1.0, 0.2],
                [0.3, 0.2, 1.0],
            ]
        )
        edges = _edges_threshold(sim, threshold=0.5, agent_ids=["a", "b", "c"])
        pairs = {(s, t) for s, t, _ in edges}
        assert ("a", "b") in pairs
        assert ("b", "a") in pairs
        assert ("a", "c") not in pairs

    def test_zero_threshold_connects_all(self):
        sim = torch.ones(3, 3) * 0.5
        sim.fill_diagonal_(1.0)
        edges = _edges_threshold(sim, threshold=0.0, agent_ids=["a", "b", "c"])
        assert len(edges) == 6  # all pairs both directions

    def test_high_threshold_no_edges(self):
        sim = torch.ones(3, 3) * 0.3
        sim.fill_diagonal_(1.0)
        edges = _edges_threshold(sim, threshold=0.99, agent_ids=["a", "b", "c"])
        assert len(edges) == 0


class TestEdgesMST:
    def test_three_nodes(self):
        sim = torch.tensor(
            [
                [1.0, 0.9, 0.1],
                [0.9, 1.0, 0.5],
                [0.1, 0.5, 1.0],
            ]
        )
        edges = _edges_mst(sim, agent_ids=["a", "b", "c"])
        pairs = {(s, t) for s, t, _ in edges}
        assert ("a", "b") in pairs or ("b", "a") in pairs
        assert ("b", "c") in pairs or ("c", "b") in pairs

    def test_mst_with_shortcuts(self):
        sim = torch.tensor(
            [
                [1.0, 0.9, 0.8],
                [0.9, 1.0, 0.2],
                [0.8, 0.2, 1.0],
            ]
        )
        edges_no_shortcut = _edges_mst(sim, agent_ids=["a", "b", "c"])
        edges_with_shortcut = _edges_mst(
            sim,
            agent_ids=["a", "b", "c"],
            shortcut_threshold=0.7,
        )
        assert len(edges_with_shortcut) >= len(edges_no_shortcut)


# ───────────────────── Direction inference ───────────────────────────────────


class TestInferDirection:
    def test_deduplicates(self):
        edges = [("a", "b", 0.8), ("b", "a", 0.8)]
        sim = torch.tensor([[1.0, 0.8], [0.8, 1.0]])
        directed = _infer_direction(edges, ["a", "b"], sim)
        assert len(directed) == 1

    def test_generalist_to_specialist(self):
        sim = torch.tensor(
            [
                [1.0, 0.9, 0.8],
                [0.9, 1.0, 0.2],
                [0.8, 0.2, 1.0],
            ]
        )
        edges = [("a", "b", 0.9), ("a", "c", 0.8)]
        directed = _infer_direction(edges, ["a", "b", "c"], sim)
        sources = {s for s, _, _ in directed}
        assert "a" in sources


class TestEnsureDag:
    def test_no_cycles(self):
        edges = [("a", "b", 0.9), ("b", "c", 0.8)]
        result = _ensure_dag(edges, ["a", "b", "c"])
        assert len(result) == 2

    def test_breaks_simple_cycle(self):
        edges = [("a", "b", 0.9), ("b", "c", 0.8), ("c", "a", 0.3)]
        result = _ensure_dag(edges, ["a", "b", "c"])
        {(s, t) for s, t, _ in result}
        adj: dict[str, set[str]] = {"a": set(), "b": set(), "c": set()}
        for s, t, _ in result:
            adj[s].add(t)

        def has_cycle(adj: dict[str, set[str]]) -> bool:
            white, gray, black = 0, 1, 2
            color = dict.fromkeys(adj, white)

            def dfs(u: str) -> bool:
                color[u] = gray
                for v in adj[u]:
                    if color[v] == gray:
                        return True
                    if color[v] == white and dfs(v):
                        return True
                color[u] = black
                return False

            return any(color[n] == white and dfs(n) for n in adj)

        assert not has_cycle(adj)


class TestPickStartEnd:
    def test_chain(self):
        edges = [("a", "b", 0.9), ("b", "c", 0.8)]
        start, end = _pick_start_end(edges, ["a", "b", "c"])
        assert start == "a"
        assert end == "c"

    def test_diamond(self):
        edges = [("a", "b", 0.9), ("a", "c", 0.8), ("b", "d", 0.7), ("c", "d", 0.6)]
        start, end = _pick_start_end(edges, ["a", "b", "c", "d"])
        assert start == "a"
        assert end == "d"


# ───────────────────── EmbeddingBuilderConfig ────────────────────────────────


class TestEmbeddingBuilderConfig:
    def test_defaults(self):
        cfg = EmbeddingBuilderConfig()
        assert cfg.strategy == LinkStrategy.KNN
        assert cfg.k == 2
        assert cfg.threshold == 0.5
        assert cfg.symmetric is False
        assert cfg.include_task_node is True

    def test_custom(self):
        cfg = EmbeddingBuilderConfig(
            strategy=LinkStrategy.THRESHOLD,
            threshold=0.7,
            symmetric=True,
        )
        assert cfg.strategy == LinkStrategy.THRESHOLD
        assert cfg.threshold == 0.7
        assert cfg.symmetric is True

    def test_strategy_from_string(self):
        cfg = EmbeddingBuilderConfig(strategy="mst")
        assert cfg.strategy == LinkStrategy.MST

    def test_k_bounds(self):
        with pytest.raises(ValidationError):
            EmbeddingBuilderConfig(k=0)

    def test_threshold_bounds(self):
        with pytest.raises(ValidationError):
            EmbeddingBuilderConfig(threshold=1.5)


# ───────────────────── EmbeddingGraphBuilder ─────────────────────────────────


class TestEmbeddingGraphBuilder:
    def test_build_knn(self):
        agents = _diverse_agents()
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(
                strategy="knn",
                k=1,
                encoder=_hash_encoder(),
            ),
        )
        graph = builder.build(agents, query="Test task")
        assert len(graph.node_ids) >= len(agents)

    def test_build_threshold(self):
        agents = _diverse_agents()
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(
                strategy="threshold",
                threshold=0.01,
                encoder=_hash_encoder(),
            ),
        )
        graph = builder.build(agents, query="Test")
        assert len(graph.node_ids) >= len(agents)

    def test_build_mst(self):
        agents = _diverse_agents()
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(
                strategy="mst",
                encoder=_hash_encoder(),
            ),
        )
        graph = builder.build(agents, query="Test")
        assert len(graph.node_ids) >= len(agents)

    def test_symmetric_edges(self):
        agents = _similar_pair()
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(
                strategy="knn",
                k=1,
                symmetric=True,
                encoder=_hash_encoder(),
                include_task_node=False,
            ),
        )
        graph = builder.build(agents)
        edges = list(graph.edges)
        pairs = {(e["source"], e["target"]) for e in edges}
        assert ("writer_a", "writer_b") in pairs or ("writer_b", "writer_a") in pairs

    def test_too_few_agents(self):
        agents = _make_agents(("solo", "persona", "desc"))
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(encoder=_hash_encoder()),
        )
        with pytest.raises(ValueError, match="At least 2"):
            builder.build(agents)

    def test_default_encoder_created(self):
        builder = EmbeddingGraphBuilder()
        assert builder._encoder is not None

    def test_compute_similarity_matrix(self):
        agents = _diverse_agents()
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(encoder=_hash_encoder()),
        )
        sim, ids = builder.compute_similarity_matrix(agents)
        assert sim.shape == (4, 4)
        assert len(ids) == 4
        for i in range(4):
            assert sim[i, i].item() == pytest.approx(1.0, abs=1e-5)

    def test_no_task_node(self):
        agents = _diverse_agents()
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(
                encoder=_hash_encoder(),
                include_task_node=False,
            ),
        )
        graph = builder.build(agents)
        assert "task" not in graph.node_ids or len(graph.node_ids) == len(agents)

    def test_fallback_chain_when_no_edges(self):
        """When threshold is so high no edges are produced, fallback chain is used."""
        agents = _diverse_agents()
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(
                strategy="threshold",
                threshold=0.999,
                encoder=_hash_encoder(),
            ),
        )
        graph = builder.build(agents)
        assert len(graph.node_ids) >= len(agents)

    def test_agents_with_tools(self):
        agents = [
            AgentProfile(
                agent_id="coder",
                display_name="Coder",
                persona="a programmer",
                tools=["code_interpreter"],
            ),
            AgentProfile(
                agent_id="searcher",
                display_name="Searcher",
                persona="a web researcher",
                tools=["web_search"],
            ),
        ]
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(
                strategy="knn",
                k=1,
                encoder=_hash_encoder(),
            ),
        )
        graph = builder.build(agents, query="Research and code")
        assert "coder" in graph.node_ids
        assert "searcher" in graph.node_ids

    def test_mst_with_shortcut(self):
        agents = _diverse_agents()
        builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(
                strategy="mst",
                mst_shortcut_threshold=0.01,
                encoder=_hash_encoder(),
            ),
        )
        graph = builder.build(agents, query="Test")
        assert len(graph.node_ids) >= len(agents)

    def test_graph_is_valid(self):
        agents = _diverse_agents()
        for strategy in ["knn", "threshold", "mst"]:
            builder = EmbeddingGraphBuilder(
                config=EmbeddingBuilderConfig(
                    strategy=strategy,
                    threshold=0.01,
                    encoder=_hash_encoder(),
                ),
            )
            graph = builder.build(agents, query="Validation test")
            errors = graph.verify_integrity(raise_on_error=False)
            assert not errors, f"Strategy {strategy} produced invalid graph: {errors}"
