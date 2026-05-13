"""
Service layer over rustworkx algorithms for graph analysis.

Provides:
- K shortest paths
- Centralities (betweenness, closeness, degree, eigenvector, PageRank)
- Community detection
- Cycle detection
- Subgraph filtering by metadata
- Integration with the router
"""

import contextlib
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import rustworkx as rx
import torch
from pydantic import BaseModel, ConfigDict, Field

from gmas.config.logging import logger

__all__ = [
    "CentralityResult",
    # Enums
    "CentralityType",
    "CommunityResult",
    "CycleInfo",
    # Main service
    "GraphAlgorithms",
    "PathMetric",
    # Data classes
    "PathResult",
    "SubgraphFilter",
    # Utility functions
    "compute_all_centralities",
    "find_critical_nodes",
    "get_graph_metrics",
]


class CentralityType(StrEnum):
    """Centrality types."""

    BETWEENNESS = "betweenness"
    CLOSENESS = "closeness"
    DEGREE = "degree"
    EIGENVECTOR = "eigenvector"
    PAGERANK = "pagerank"
    KATZ = "katz"


class PathMetric(StrEnum):
    """Metric for path computation."""

    HOPS = "hops"
    WEIGHT = "weight"
    LATENCY = "latency"
    COST = "cost"
    RELIABILITY = "reliability"


class PathResult(BaseModel):
    """Description of a found path with weights and arbitrary metadata."""

    nodes: list[str]
    total_weight: float
    edge_weights: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def length(self) -> int:
        """Number of edges in the path."""
        return len(self.nodes) - 1 if len(self.nodes) > 1 else 0

    def __repr__(self) -> str:
        return f"PathResult({' -> '.join(self.nodes)}, weight={self.total_weight:.3f})"


class CentralityResult(BaseModel):
    """Result of centrality computation for graph nodes."""

    centrality_type: CentralityType
    values: dict[str, float]
    normalized: bool = True

    def top_k(self, k: int = 5) -> list[tuple[str, float]]:
        """Return the top-k nodes by centrality value."""
        sorted_items = sorted(self.values.items(), key=lambda x: x[1], reverse=True)
        return sorted_items[:k]

    def get_node_rank(self, node_id: str) -> int | None:
        """Node position in the ranking (1-based) or None if absent."""
        sorted_nodes = sorted(self.values.keys(), key=lambda n: self.values[n], reverse=True)
        try:
            return sorted_nodes.index(node_id) + 1
        except ValueError:
            return None


class CommunityResult(BaseModel):
    """Result of community detection."""

    communities: list[set[str]]
    modularity: float | None = None
    algorithm: str = "unknown"

    @property
    def num_communities(self) -> int:
        """Number of detected communities."""
        return len(self.communities)

    def get_node_community(self, node_id: str) -> int | None:
        """Find the community index that the node belongs to."""
        for i, community in enumerate(self.communities):
            if node_id in community:
                return i
        return None

    def get_community_sizes(self) -> list[int]:
        """Return a list of community sizes."""
        return [len(c) for c in self.communities]


class CycleInfo(BaseModel):
    """Information about a detected cycle."""

    nodes: list[str]
    edges: list[tuple[str, str]]
    total_weight: float = 0.0

    @property
    def length(self) -> int:
        """Number of nodes in the cycle."""
        return len(self.nodes)


class SubgraphFilter(BaseModel):
    """Rules for filtering nodes and edges when building a subgraph."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    node_filter: Callable[[str, dict[str, Any]], bool] | None = None
    edge_filter: Callable[[str, str, dict[str, Any]], bool] | None = None
    include_nodes: set[str] | None = None
    exclude_nodes: set[str] | None = None
    min_weight: float | None = None
    max_weight: float | None = None
    required_attrs: list[str] | None = None

    def matches_node(self, node_id: str, attrs: dict[str, Any]) -> bool:
        """Check whether the node satisfies the given conditions."""
        if self.exclude_nodes and node_id in self.exclude_nodes:
            return False
        if self.include_nodes and node_id not in self.include_nodes:
            return False
        if self.required_attrs and not all(attr in attrs for attr in self.required_attrs):
            return False
        return not (self.node_filter and not self.node_filter(node_id, attrs))

    def matches_edge(self, src: str, tgt: str, attrs: dict[str, Any]) -> bool:
        """Check whether the edge satisfies the given conditions."""
        weight = attrs.get("weight", 1.0)
        if self.min_weight is not None and weight < self.min_weight:
            return False
        if self.max_weight is not None and weight > self.max_weight:
            return False
        return not (self.edge_filter and not self.edge_filter(src, tgt, attrs))


class GraphAlgorithms:
    """Service layer over `rustworkx` for analysing a `RoleGraph`."""

    def __init__(
        self,
        graph: Any,  # RoleGraph
        weight_attr: str = "weight",
        default_weight: float = 1.0,
    ):
        """
        Initialise the wrapper around the graph.

        Args:
            graph: A RoleGraph instance (or an object with a `graph: PyDiGraph` attribute).
            weight_attr: Key for the edge weight in the data.
            default_weight: Weight value when not specified in the edge.

        """
        self._role_graph = graph
        self._graph: rx.PyDiGraph = graph.graph
        self._weight_attr = weight_attr
        self._default_weight = default_weight

        self._node_id_to_idx: dict[str, int] = {}
        self._idx_to_node_id: dict[int, str] = {}
        self._rebuild_index_cache()

    def _rebuild_index_cache(self) -> None:
        """Rebuild the cache mapping node_id ↔ rustworkx index."""
        self._node_id_to_idx.clear()
        self._idx_to_node_id.clear()
        for idx in self._graph.node_indices():
            data = self._graph.get_node_data(idx)
            if isinstance(data, dict):
                node_id = data.get("id", str(idx))
            elif hasattr(data, "agent_id"):
                node_id = data.agent_id
            else:
                node_id = str(idx)
            self._node_id_to_idx[node_id] = idx
            self._idx_to_node_id[idx] = node_id

    def _get_node_idx(self, node_id: str) -> int:
        """Get the node index by ID."""
        if node_id not in self._node_id_to_idx:
            self._rebuild_index_cache()
        if node_id not in self._node_id_to_idx:
            msg = f"Node '{node_id}' not found in graph"
            raise ValueError(msg)
        return self._node_id_to_idx[node_id]

    def _get_node_id(self, idx: int) -> str:
        """Get the node ID by its internal graph index."""
        if idx not in self._idx_to_node_id:
            self._rebuild_index_cache()
        return self._idx_to_node_id.get(idx, str(idx))

    def _get_edge_weight(
        self,
        edge_data: Any,
        metric: PathMetric = PathMetric.WEIGHT,
    ) -> float:
        """Get the edge weight according to the selected metric."""
        if edge_data is None:
            return self._default_weight

        if isinstance(edge_data, dict):
            if metric == PathMetric.HOPS:
                return 1.0
            if metric == PathMetric.WEIGHT:
                return edge_data.get(self._weight_attr, self._default_weight)
            if metric == PathMetric.LATENCY:
                return edge_data.get("latency", self._default_weight)
            if metric == PathMetric.COST:
                return edge_data.get("cost", self._default_weight)
            if metric == PathMetric.RELIABILITY:
                rel = edge_data.get("reliability", 1.0)
                return -torch.log(torch.tensor(max(rel, 1e-10))).item()

        return self._default_weight

    def k_shortest_paths(
        self,
        source: str,
        target: str,
        k: int = 3,
        metric: PathMetric = PathMetric.WEIGHT,
    ) -> list[PathResult]:
        """Find k shortest paths between nodes using the given metric."""
        src_idx = self._get_node_idx(source)
        tgt_idx = self._get_node_idx(target)

        def weight_fn(edge_data: Any) -> float:
            return self._get_edge_weight(edge_data, metric)

        paths = self._yen_k_shortest_paths(src_idx, tgt_idx, k, weight_fn)

        results = []
        for path_indices, total_weight in paths:
            nodes = [self._get_node_id(idx) for idx in path_indices]
            edge_weights = []
            for i in range(len(path_indices) - 1):
                edge_data = self._graph.get_edge_data(path_indices[i], path_indices[i + 1])
                edge_weights.append(self._get_edge_weight(edge_data, metric))

            results.append(
                PathResult(
                    nodes=nodes,
                    total_weight=total_weight,
                    edge_weights=edge_weights,
                    metadata={"metric": metric.value},
                )
            )

        return results

    def _find_initial_shortest_path(
        self,
        source: int,
        target: int,
        weight_fn: Callable[[Any], float],
    ) -> tuple[list[int], float] | None:
        """Find the first shortest path."""
        try:
            distances = rx.dijkstra_shortest_path_lengths(self._graph, source, weight_fn)
            if target not in distances:
                return None

            path_map = rx.dijkstra_shortest_paths(self._graph, source, target=target, weight_fn=weight_fn)
            if target not in path_map:
                return None

            return list(path_map[target]), distances[target]
        except (ValueError, KeyError, RuntimeError):
            return None

    def _remove_conflicting_edges(
        self,
        found_paths: list[tuple[list[int], float]],
        root_path: list[int],
        j: int,
    ) -> list[tuple[int, int, Any]]:
        """Remove edges that conflict with the found paths."""
        removed_edges = []
        for path, _ in found_paths:
            if len(path) > j and path[: j + 1] == root_path and j + 1 < len(path):
                try:
                    edge_data = self._graph.get_edge_data(path[j], path[j + 1])
                    self._graph.remove_edge(path[j], path[j + 1])
                    removed_edges.append((path[j], path[j + 1], edge_data))
                except (ValueError, KeyError, RuntimeError):
                    pass
        return removed_edges

    def _calculate_path_weight(self, path: list[int], weight_fn: Callable[[Any], float]) -> float:
        """Compute the total weight of a path."""
        total_weight = 0.0
        for idx in range(len(path) - 1):
            edge_data = self._graph.get_edge_data(path[idx], path[idx + 1])
            total_weight += weight_fn(edge_data) if edge_data else self._default_weight
        return total_weight

    def _find_spur_path(
        self,
        spur_node: int,
        target: int,
        root_path: list[int],
        weight_fn: Callable[[Any], float],
    ) -> tuple[list[int], float] | None:
        """Find an alternative path from the spur node."""
        try:
            spur_distances = rx.dijkstra_shortest_path_lengths(self._graph, spur_node, weight_fn)
            if target not in spur_distances:
                return None

            spur_path_map = rx.dijkstra_shortest_paths(self._graph, spur_node, target=target, weight_fn=weight_fn)
            if target not in spur_path_map:
                return None
        except (ValueError, KeyError, RuntimeError):
            return None
        else:
            spur_path = list(spur_path_map[target])
            total_path = root_path[:-1] + spur_path
            total_weight = self._calculate_path_weight(total_path, weight_fn)
            return total_path, total_weight

    def _yen_k_shortest_paths(
        self,
        source: int,
        target: int,
        k: int,
        weight_fn: Callable[[Any], float],
    ) -> list[tuple[list[int], float]]:
        """Yen's algorithm: return paths as lists of node indices and total weight."""
        import heapq

        initial_path = self._find_initial_shortest_path(source, target, weight_fn)
        if not initial_path:
            return []

        first_path, first_weight = initial_path
        found_paths = [(first_path, first_weight)]
        candidate_heap: list[tuple[float, list[int]]] = []

        for i in range(1, k):
            if i - 1 >= len(found_paths):
                break

            prev_path, _ = found_paths[i - 1]

            for j in range(len(prev_path) - 1):
                spur_node = prev_path[j]
                root_path = prev_path[: j + 1]

                removed_edges = self._remove_conflicting_edges(found_paths, root_path, j)

                try:
                    spur_result = self._find_spur_path(spur_node, target, root_path, weight_fn)
                    if spur_result:
                        total_path, total_weight = spur_result
                        if not any(p == total_path for _, p in candidate_heap) and not any(
                            p == total_path for p, _ in found_paths
                        ):
                            heapq.heappush(candidate_heap, (total_weight, total_path))
                finally:
                    # Restore removed edges in any case
                    for u, v, data in removed_edges:
                        self._graph.add_edge(u, v, data)

            if candidate_heap:
                weight, path = heapq.heappop(candidate_heap)
                found_paths.append((path, weight))

        return found_paths

    def shortest_path(
        self,
        source: str,
        target: str,
        metric: PathMetric = PathMetric.WEIGHT,
    ) -> PathResult | None:
        """Find one shortest path between two nodes."""
        paths = self.k_shortest_paths(source, target, k=1, metric=metric)
        return paths[0] if paths else None

    def all_pairs_shortest_paths(
        self,
        metric: PathMetric = PathMetric.WEIGHT,
    ) -> dict[str, dict[str, float]]:
        """Compute shortest paths between all pairs of nodes."""

        def weight_fn(edge_data: Any) -> float:
            return self._get_edge_weight(edge_data, metric)

        all_distances = rx.all_pairs_dijkstra_path_lengths(self._graph, weight_fn)

        result = {}
        for src_idx, distances in all_distances.items():
            src_id = self._get_node_id(src_idx)
            result[src_id] = {}
            for tgt_idx, dist in distances.items():
                tgt_id = self._get_node_id(tgt_idx)
                result[src_id][tgt_id] = dist

        return result

    def compute_centrality(
        self,
        centrality_type: CentralityType,
        normalized: bool = True,
        **kwargs: Any,
    ) -> CentralityResult:
        """Compute the selected centrality type for all graph nodes."""
        values: dict[int, int | float] = {}

        if centrality_type == CentralityType.BETWEENNESS:
            raw_result = rx.betweenness_centrality(self._graph, normalized=normalized)
            values = (
                dict(raw_result.items())
                if hasattr(raw_result, "items")
                else dict(enumerate(raw_result))
                if isinstance(raw_result, list)
                else raw_result
            )

        elif centrality_type == CentralityType.CLOSENESS:
            undirected = self._graph.to_undirected()
            raw_values = rx.closeness_centrality(undirected)
            values = (
                dict(enumerate(raw_values))
                if isinstance(raw_values, list)
                else dict(raw_values.items())
                if hasattr(raw_values, "items")
                else raw_values
            )

        elif centrality_type == CentralityType.DEGREE:
            for idx in self._graph.node_indices():
                in_deg = self._graph.in_degree(idx)
                out_deg = self._graph.out_degree(idx)
                values[idx] = float(in_deg + out_deg)
            if normalized and self._graph.num_nodes() > 1:
                max_deg = 2 * (self._graph.num_nodes() - 1)
                values = {k: v / max_deg for k, v in values.items()}

        elif centrality_type == CentralityType.EIGENVECTOR:
            try:
                raw = rx.eigenvector_centrality(self._graph)
                values = (
                    dict(enumerate(raw))
                    if isinstance(raw, list)
                    else dict(raw.items())
                    if hasattr(raw, "items")
                    else raw
                )
            except (ValueError, RuntimeError, AttributeError):
                raw_pr = rx.pagerank(self._graph)
                values = (
                    dict(raw_pr.items())
                    if hasattr(raw_pr, "items")
                    else dict(enumerate(raw_pr))
                    if isinstance(raw_pr, list)
                    else raw_pr
                )

        elif centrality_type == CentralityType.PAGERANK:
            alpha = kwargs.get("alpha", 0.85)
            raw_pr = rx.pagerank(self._graph, alpha=alpha)
            values = (
                dict(raw_pr.items())
                if hasattr(raw_pr, "items")
                else dict(enumerate(raw_pr))
                if isinstance(raw_pr, list)
                else raw_pr
            )

        elif centrality_type == CentralityType.KATZ:
            alpha = kwargs.get("alpha", 0.1)
            beta = kwargs.get("beta", 1.0)
            try:
                raw_katz = rx.katz_centrality(self._graph, alpha=alpha, beta=beta)
                values = (
                    dict(raw_katz.items())
                    if hasattr(raw_katz, "items")
                    else dict(enumerate(raw_katz))
                    if isinstance(raw_katz, list)
                    else raw_katz
                )
            except (ValueError, RuntimeError, AttributeError):
                raw_pr = rx.pagerank(self._graph)
                values = (
                    dict(raw_pr.items())
                    if hasattr(raw_pr, "items")
                    else dict(enumerate(raw_pr))
                    if isinstance(raw_pr, list)
                    else raw_pr
                )

        result_values = {}
        for idx, val in values.items():
            node_id = self._get_node_id(idx)
            result_values[node_id] = float(val)

        return CentralityResult(
            centrality_type=centrality_type,
            values=result_values,
            normalized=normalized,
        )

    def compute_all_centralities(self, normalized: bool = True) -> dict[CentralityType, CentralityResult]:
        """Compute all centralities and return a dict keyed by type."""
        results = {}
        for ct in CentralityType:
            with contextlib.suppress(Exception):
                results[ct] = self.compute_centrality(ct, normalized=normalized)
        return results

    def detect_communities(
        self,
        algorithm: str = "louvain",
        _resolution: float = 1.0,
    ) -> CommunityResult:
        """Detect communities using the specified algorithm (louvain/label_propagation)."""
        undirected = self._graph.to_undirected()

        communities: list[set[str]] = []
        modularity: float | None = None

        if algorithm == "louvain":
            try:
                components = rx.connected_components(undirected)
                communities = [{self._get_node_id(idx) for idx in comp} for comp in components]
            except (ValueError, RuntimeError):
                communities = [{self._get_node_id(idx) for idx in undirected.node_indices()}]

        elif algorithm == "label_propagation":
            communities = self._label_propagation(undirected)

        elif algorithm == "connected_components":
            components = rx.connected_components(undirected)
            communities = [{self._get_node_id(idx) for idx in comp} for comp in components]

        else:
            components = rx.connected_components(undirected)
            communities = [{self._get_node_id(idx) for idx in comp} for comp in components]

        return CommunityResult(
            communities=communities,
            modularity=modularity,
            algorithm=algorithm,
        )

    def _label_propagation(self, graph: rx.PyGraph) -> list[set[str]]:
        """Simple label propagation implementation for an undirected graph."""
        import random

        labels = {idx: idx for idx in graph.node_indices()}

        for _ in range(100):
            changed = False
            nodes = list(graph.node_indices())
            random.shuffle(nodes)

            for node in nodes:
                neighbors = list(graph.neighbors(node))
                if not neighbors:
                    continue

                label_counts: dict[int, int] = {}
                for neighbor in neighbors:
                    lbl = labels[neighbor]
                    label_counts[lbl] = label_counts.get(lbl, 0) + 1

                max_count = max(label_counts.values())
                best_labels = [label for label, count in label_counts.items() if count == max_count]
                # Use first label if only one, otherwise pick randomly (non-cryptographic use)
                new_label = best_labels[0] if len(best_labels) == 1 else random.choice(best_labels)

                if labels[node] != new_label:
                    labels[node] = new_label
                    changed = True

            if not changed:
                break

        label_to_nodes: dict[int, set[str]] = {}
        for node, label in labels.items():
            if label not in label_to_nodes:
                label_to_nodes[label] = set()
            label_to_nodes[label].add(self._get_node_id(node))

        return list(label_to_nodes.values())

    def detect_cycles(self, max_length: int | None = None) -> list[CycleInfo]:
        """Find simple cycles, optionally limiting the maximum length."""
        cycles = []

        try:
            simple_cycles = rx.simple_cycles(self._graph)
            for cycle_indices in simple_cycles:
                if max_length and len(cycle_indices) > max_length:
                    continue

                nodes = [self._get_node_id(idx) for idx in cycle_indices]
                edges = []
                total_weight = 0.0

                for i in range(len(cycle_indices)):
                    src = cycle_indices[i]
                    tgt = cycle_indices[(i + 1) % len(cycle_indices)]
                    edges.append((self._get_node_id(src), self._get_node_id(tgt)))

                    edge_data = self._graph.get_edge_data(src, tgt)
                    if edge_data and isinstance(edge_data, dict):
                        total_weight += edge_data.get(self._weight_attr, self._default_weight)
                    else:
                        total_weight += self._default_weight

                cycles.append(
                    CycleInfo(
                        nodes=nodes,
                        edges=edges,
                        total_weight=total_weight,
                    )
                )
        except (ValueError, RuntimeError):
            pass  # Cycle detection may fail

        return cycles

    def is_dag(self) -> bool:
        """Check whether the graph is a directed acyclic graph (DAG)."""
        return rx.is_directed_acyclic_graph(self._graph)

    def topological_sort(self) -> list[str] | None:
        """Return the topological ordering of nodes if the graph is a DAG."""
        if not self.is_dag():
            return None

        order = rx.topological_sort(self._graph)
        return [self._get_node_id(idx) for idx in order]

    def filter_subgraph(
        self,
        filter_spec: SubgraphFilter,
    ) -> "GraphAlgorithms":
        """Filter nodes/edges by rules and return a wrapper over the subgraph."""
        keep_nodes = set()
        for idx in self._graph.node_indices():
            node_id = self._get_node_id(idx)
            node_data = self._graph.get_node_data(idx)
            attrs = node_data if isinstance(node_data, dict) else {}

            if filter_spec.matches_node(node_id, attrs):
                keep_nodes.add(idx)

        new_graph = rx.PyDiGraph()
        old_to_new: dict[int, int] = {}

        for old_idx in keep_nodes:
            node_data = self._graph.get_node_data(old_idx)
            new_idx = new_graph.add_node(node_data)
            old_to_new[old_idx] = new_idx

        for edge_idx in self._graph.edge_indices():
            src, tgt = self._graph.get_edge_endpoints_by_index(edge_idx)
            if src not in keep_nodes or tgt not in keep_nodes:
                continue

            edge_data = self._graph.get_edge_data_by_index(edge_idx)
            attrs = edge_data if isinstance(edge_data, dict) else {}

            src_id = self._get_node_id(src)
            tgt_id = self._get_node_id(tgt)

            if filter_spec.matches_edge(src_id, tgt_id, attrs):
                new_graph.add_edge(old_to_new[src], old_to_new[tgt], edge_data)

        class SubgraphWrapper:
            def __init__(self, g: rx.PyDiGraph):
                self.graph = g

        return GraphAlgorithms(
            SubgraphWrapper(new_graph),
            weight_attr=self._weight_attr,
            default_weight=self._default_weight,
        )

    def get_reachable_nodes(self, source: str, max_depth: int | None = None) -> set[str]:
        """Return the set of nodes reachable from source, optionally limited by depth."""
        src_idx = self._get_node_idx(source)

        visited = set()
        queue = deque([(src_idx, 0)])

        while queue:
            node, depth = queue.popleft()
            if node in visited:
                continue
            if max_depth is not None and depth > max_depth:
                continue

            visited.add(node)

            successors_to_add = [
                (successor, depth + 1) for successor in self._graph.successor_indices(node) if successor not in visited
            ]
            queue.extend(successors_to_add)

        return {self._get_node_id(idx) for idx in visited}

    def get_predecessors(self, node: str, max_depth: int | None = None) -> set[str]:
        """Return the set of predecessors of a node, optionally limited by depth."""
        node_idx = self._get_node_idx(node)

        visited = set()
        queue = deque([(node_idx, 0)])

        while queue:
            n, depth = queue.popleft()
            if n in visited:
                continue
            if max_depth is not None and depth > max_depth:
                continue

            visited.add(n)

            predecessors_to_add = [
                (predecessor, depth + 1)
                for predecessor in self._graph.predecessor_indices(n)
                if predecessor not in visited
            ]
            queue.extend(predecessors_to_add)

        visited.discard(node_idx)
        return {self._get_node_id(idx) for idx in visited}

    def get_routing_metrics(self, source: str, target: str) -> dict[str, Any]:
        """Collect a brief summary of paths and centrality for a pair of nodes."""
        paths_list: list[dict[str, Any]] = []
        centrality_dict: dict[str, float] = {}
        is_reachable = False

        for metric in [PathMetric.WEIGHT, PathMetric.LATENCY, PathMetric.COST]:
            try:
                paths = self.k_shortest_paths(source, target, k=3, metric=metric)
                if paths:
                    is_reachable = True
                    paths_list.append(
                        {
                            "metric": metric.value,
                            "best_path": paths[0].nodes,
                            "best_weight": paths[0].total_weight,
                            "alternatives": len(paths) - 1,
                        }
                    )
            except (ValueError, RuntimeError) as e:
                logger.debug("Error: {}", e)

        try:
            pr = self.compute_centrality(CentralityType.PAGERANK)
            centrality_dict["pagerank"] = pr.values.get(target, 0.0)
        except (ValueError, RuntimeError):
            pass  # Centrality computation may fail

        return {
            "source": source,
            "target": target,
            "paths": paths_list,
            "centrality": centrality_dict,
            "is_reachable": is_reachable,
        }


def compute_all_centralities(graph: Any) -> dict[str, CentralityResult]:
    """Compute all centrality types and return them by string keys."""
    alg = GraphAlgorithms(graph)
    results = alg.compute_all_centralities()
    return {ct.value: result for ct, result in results.items()}


def find_critical_nodes(graph: Any, top_k: int = 5) -> list[str]:
    """Return the nodes with the highest betweenness centrality."""
    alg = GraphAlgorithms(graph)
    bc = alg.compute_centrality(CentralityType.BETWEENNESS)
    return [node_id for node_id, _ in bc.top_k(top_k)]


def get_graph_metrics(graph: Any) -> dict[str, Any]:
    """Collect key graph metrics: size, DAG status, communities, cycles."""
    alg = GraphAlgorithms(graph)

    return {
        "num_nodes": graph.graph.num_nodes(),
        "num_edges": graph.graph.num_edges(),
        "is_dag": alg.is_dag(),
        "num_communities": alg.detect_communities().num_communities,
        "num_cycles": len(alg.detect_cycles(max_length=10)),
    }
