"""
Embedding-based automatic graph assembly.

Builds a ``RoleGraph`` by computing semantic similarity between agent
descriptions (persona + description + tools) and connecting agents whose
similarity exceeds a configurable threshold.

Strategies:
- **knn**  — each agent connects to its *k* nearest neighbours.
- **threshold** — all pairs whose cosine similarity ≥ *threshold* are connected.
- **mst** — minimum spanning tree ensures a connected graph with minimal total
  distance, optionally augmented with high-similarity shortcuts.

Edge direction is inferred from a topological heuristic (generalist →
specialist, or by input/output role analysis) and can be overridden.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import torch
from pydantic import BaseModel, Field, field_validator

from gmas.builder.graph_builder import BuilderConfig, build_property_graph
from gmas.core.encoder import NodeEncoder

if TYPE_CHECKING:
    from gmas.core.graph import RoleGraph

__all__ = [
    "EmbeddingBuilderConfig",
    "EmbeddingGraphBuilder",
    "LinkStrategy",
]

_MIN_AGENTS = 2

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class LinkStrategy(StrEnum):
    """How to decide which agent pairs get an edge."""

    KNN = "knn"
    THRESHOLD = "threshold"
    MST = "mst"


class EmbeddingBuilderConfig(BaseModel):
    """Settings for :class:`EmbeddingGraphBuilder`."""

    strategy: LinkStrategy | str = LinkStrategy.KNN

    @field_validator("strategy", mode="before")
    @classmethod
    def _coerce_strategy(cls, v: object) -> LinkStrategy:
        if isinstance(v, LinkStrategy):
            return v
        if isinstance(v, str):
            return LinkStrategy(v)
        msg = f"Invalid strategy: {v!r}"
        raise ValueError(msg)

    k: int = Field(default=2, ge=1, description="Neighbours per node (knn strategy)")
    threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Cosine similarity threshold (threshold / mst shortcut)",
    )
    mst_shortcut_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="If set, add extra edges above this similarity on top of MST",
    )
    symmetric: bool = Field(
        default=False,
        description="If True create bidirectional edges; otherwise infer direction",
    )
    include_task_node: bool = True
    encoder: NodeEncoder | None = Field(
        default=None,
        description="Custom NodeEncoder; if None a default one is created",
    )
    builder_config: BuilderConfig | None = None

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent_text(agent: Any) -> str:
    """Build a rich text representation for embedding."""
    parts: list[str] = []
    if getattr(agent, "persona", ""):
        parts.append(agent.persona)
    if getattr(agent, "description", ""):
        parts.append(agent.description)
    tools = getattr(agent, "tools", [])
    if tools:
        tool_names = [t if isinstance(t, str) else getattr(t, "name", str(t)) for t in tools]
        parts.append("tools: " + ", ".join(tool_names))
    return ". ".join(parts) if parts else agent.agent_id


def _cosine_similarity_matrix(embeddings: torch.Tensor) -> torch.Tensor:
    """Compute pairwise cosine similarity (n×n) from (n×d) embeddings."""
    norms = embeddings.norm(dim=1, keepdim=True).clamp(min=1e-8)
    normed = embeddings / norms
    return normed @ normed.T


def _edges_knn(
    sim: torch.Tensor,
    k: int,
    agent_ids: list[str],
) -> list[tuple[str, str, float]]:
    """Return edges connecting each node to its *k* most similar peers."""
    n = sim.size(0)
    effective_k = min(k, n - 1)
    edges: list[tuple[str, str, float]] = []
    mask = sim.clone()
    mask.fill_diagonal_(float("-inf"))

    for i in range(n):
        _, indices = mask[i].topk(effective_k)
        for j in indices.tolist():
            score = sim[i, j].item()
            edges.append((agent_ids[i], agent_ids[j], score))
    return edges


def _edges_threshold(
    sim: torch.Tensor,
    threshold: float,
    agent_ids: list[str],
) -> list[tuple[str, str, float]]:
    """Return edges for all pairs whose similarity ≥ threshold."""
    n = sim.size(0)
    return [
        (agent_ids[i], agent_ids[j], sim[i, j].item())
        for i in range(n)
        for j in range(n)
        if i != j and sim[i, j].item() >= threshold
    ]


def _edges_mst(
    sim: torch.Tensor,
    agent_ids: list[str],
    shortcut_threshold: float | None = None,
) -> list[tuple[str, str, float]]:
    """Kruskal's MST on distance = 1 - similarity, plus optional shortcuts."""
    n = sim.size(0)
    dist_edges: list[tuple[float, int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            d = 1.0 - sim[i, j].item()
            dist_edges.append((d, i, j))
    dist_edges.sort()

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        parent[ra] = rb
        return True

    mst_pairs: set[tuple[int, int]] = set()
    for _, i, j in dist_edges:
        if union(i, j):
            mst_pairs.add((i, j))
            if len(mst_pairs) == n - 1:
                break

    edges: list[tuple[str, str, float]] = []
    for i, j in mst_pairs:
        score = sim[i, j].item()
        edges.append((agent_ids[i], agent_ids[j], score))
        edges.append((agent_ids[j], agent_ids[i], score))

    if shortcut_threshold is not None:
        for i in range(n):
            for j in range(i + 1, n):
                if (i, j) not in mst_pairs and sim[i, j].item() >= shortcut_threshold:
                    score = sim[i, j].item()
                    edges.append((agent_ids[i], agent_ids[j], score))
                    edges.append((agent_ids[j], agent_ids[i], score))

    return edges


def _infer_direction(
    edges: list[tuple[str, str, float]],
    agent_ids: list[str],
    sim: torch.Tensor,
) -> list[tuple[str, str, float]]:
    """
    Heuristic: keep only src → tgt where src has higher average similarity
    (generalists point toward specialists). Guarantees a DAG when possible.
    """
    id_to_idx = {aid: i for i, aid in enumerate(agent_ids)}
    n = sim.size(0)

    avg_sim = torch.zeros(n)
    for i in range(n):
        row = sim[i].clone()
        row[i] = 0.0
        avg_sim[i] = row.sum() / max(n - 1, 1)

    seen: set[tuple[str, str]] = set()
    directed: list[tuple[str, str, float]] = []
    for src, tgt, score in edges:
        pair = (src, tgt) if src < tgt else (tgt, src)
        if pair in seen:
            continue
        seen.add(pair)
        si, ti = id_to_idx[src], id_to_idx[tgt]
        if avg_sim[si] >= avg_sim[ti]:
            directed.append((src, tgt, score))
        else:
            directed.append((tgt, src, score))
    return directed


def _pick_start_end(
    edges: list[tuple[str, str, float]],
    agent_ids: list[str],
) -> tuple[str | None, str | None]:
    """Pick start (node with no incoming) and end (node with no outgoing)."""
    sources = {src for src, _, _ in edges}
    targets = {tgt for _, tgt, _ in edges}
    all_nodes = set(agent_ids)

    no_incoming = all_nodes - targets
    no_outgoing = all_nodes - sources

    start = next(iter(sorted(no_incoming)), None)
    end = next(iter(sorted(no_outgoing)), None)

    if start == end:
        end = None

    return start, end


def _ensure_dag(
    edges: list[tuple[str, str, float]],
    agent_ids: list[str],
) -> list[tuple[str, str, float]]:
    """Remove minimum-weight edges to break any cycles (greedy)."""
    adj: dict[str, set[str]] = {aid: set() for aid in agent_ids}
    for src, tgt, _ in edges:
        adj[src].add(tgt)

    def has_cycle() -> tuple[str, str] | None:
        white, gray, black = 0, 1, 2
        color = dict.fromkeys(agent_ids, white)
        parent: dict[str, str | None] = dict.fromkeys(agent_ids)

        def dfs(u: str) -> tuple[str, str] | None:
            color[u] = gray
            for v in sorted(adj[u]):
                if color[v] == gray:
                    return (u, v)
                if color[v] == white:
                    parent[v] = u
                    result = dfs(v)
                    if result:
                        return result
            color[u] = black
            return None

        for node in sorted(agent_ids):
            if color[node] == white:
                result = dfs(node)
                if result:
                    return result
        return None

    sorted_edges = sorted(edges, key=lambda e: e[2])
    result_edges = list(edges)

    cycle = has_cycle()
    while cycle:
        back_src, back_tgt = cycle
        for i, (s, t, _w) in enumerate(sorted_edges):
            if s == back_src and t == back_tgt:
                adj[s].discard(t)
                sorted_edges.pop(i)
                result_edges = [e for e in result_edges if not (e[0] == s and e[1] == t)]
                break
        else:
            for i, (s, t, _w) in enumerate(sorted_edges):
                if (s, t) == cycle or t == back_tgt:
                    adj[s].discard(t)
                    sorted_edges.pop(i)
                    result_edges = [e for e in result_edges if not (e[0] == s and e[1] == t)]
                    break
        cycle = has_cycle()

    return result_edges


# ---------------------------------------------------------------------------
# EmbeddingGraphBuilder
# ---------------------------------------------------------------------------


class EmbeddingGraphBuilder:
    """
    Build a ``RoleGraph`` by connecting agents based on embedding similarity.

    Example — k-nearest neighbours::

        from gmas.builder import EmbeddingGraphBuilder, EmbeddingBuilderConfig

        emb_builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(strategy="knn", k=2),
        )
        graph = emb_builder.build(agents=agents, query="Research AI trends")

    Example — minimum spanning tree::

        emb_builder = EmbeddingGraphBuilder(
            config=EmbeddingBuilderConfig(strategy="mst"),
        )
        graph = emb_builder.build(agents=agents, query="Multi-step analysis")

    """

    def __init__(self, config: EmbeddingBuilderConfig | None = None):
        self.config = config or EmbeddingBuilderConfig()
        self._encoder = self.config.encoder or NodeEncoder()

    def build(
        self,
        agents: Sequence[Any],
        query: str = "",
        *,
        builder_config: BuilderConfig | None = None,
    ) -> "RoleGraph":
        """
        Build a graph from agents using embedding similarity.

        Args:
            agents: Pre-built ``AgentProfile`` objects (≥ 2).
            query: Task description (placed in the task node).
            builder_config: Override the default ``BuilderConfig``.

        Returns:
            Assembled ``RoleGraph``.

        """
        if len(agents) < _MIN_AGENTS:
            msg = "At least 2 agents are required"
            raise ValueError(msg)

        sim_matrix, agent_ids = self._compute_similarity(agents)
        raw_edges = self._select_edges(sim_matrix, agent_ids)

        if self.config.symmetric:
            workflow_edges = [(s, t) for s, t, _ in raw_edges]
        else:
            directed = _infer_direction(raw_edges, agent_ids, sim_matrix)
            dag_edges = _ensure_dag(directed, agent_ids)
            workflow_edges = [(s, t) for s, t, _ in dag_edges]

        if not workflow_edges:
            workflow_edges = [(agent_ids[i], agent_ids[i + 1]) for i in range(len(agent_ids) - 1)]

        cfg = (
            builder_config
            or self.config.builder_config
            or BuilderConfig(
                include_task_node=self.config.include_task_node,
            )
        )

        return build_property_graph(
            agents,
            workflow_edges,
            query=query,
            encoder=self._encoder,
            config=cfg,
        )

    def compute_similarity_matrix(
        self,
        agents: Sequence[Any],
    ) -> tuple[torch.Tensor, list[str]]:
        """
        Public access to the similarity matrix (useful for inspection).

        Returns:
            ``(similarity_matrix, agent_ids)`` — a square cosine-similarity
            tensor and the corresponding agent ID list.

        """
        return self._compute_similarity(agents)

    def _compute_similarity(
        self,
        agents: Sequence[Any],
    ) -> tuple[torch.Tensor, list[str]]:
        texts = [_agent_text(a) for a in agents]
        agent_ids = [a.agent_id for a in agents]
        embeddings = self._encoder.encode(texts)
        sim = _cosine_similarity_matrix(embeddings)
        return sim, agent_ids

    def _select_edges(
        self,
        sim: torch.Tensor,
        agent_ids: list[str],
    ) -> list[tuple[str, str, float]]:
        strategy = self.config.strategy
        if strategy == LinkStrategy.KNN:
            return _edges_knn(sim, self.config.k, agent_ids)
        if strategy == LinkStrategy.THRESHOLD:
            return _edges_threshold(sim, self.config.threshold, agent_ids)
        if strategy == LinkStrategy.MST:
            return _edges_mst(sim, agent_ids, self.config.mst_shortcut_threshold)
        msg = f"Unknown strategy: {strategy}"
        raise ValueError(msg)
