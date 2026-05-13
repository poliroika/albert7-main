"""RoleGraph on rustworkx with dynamic topology support."""

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import rustworkx as rx
import torch
from pydantic import BaseModel, ConfigDict, Field

# Constants for magic values
EDGE_THRESHOLD = 0.5

__all__ = [
    "GraphIntegrityError",
    "RoleGraph",
    "StateMigrationPolicy",
    "StateStorage",
]


class StateMigrationPolicy(StrEnum):
    DISCARD = "discard"
    COPY = "copy"
    ARCHIVE = "archive"


@runtime_checkable
class StateStorage(Protocol):
    def save(self, node_id: str, state: dict[str, Any]) -> None: ...

    def load(self, node_id: str) -> dict[str, Any] | None: ...

    def delete(self, node_id: str) -> None: ...


class GraphIntegrityError(Exception):
    pass


def _get_agent_id(agent: Any) -> str | None:
    """Safely get agent_id from an agent (object or dict)."""
    if hasattr(agent, "agent_id"):
        return agent.agent_id
    if isinstance(agent, dict):
        return agent.get("id") or agent.get("agent_id")
    return None


class RoleGraph(BaseModel):
    """
    Role graph on rustworkx with adjacency matrices and auxiliary data.

    Supports conditional routing via edge_conditions.
    Supports explicit start_node and end_node for execution optimisation.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agents: list[Any] = Field(default_factory=list)
    node_ids: list[str] = Field(default_factory=list)
    role_connections: dict[str, list[str]] = Field(default_factory=dict)
    task_node: str | None = None
    query: str | None = None
    answer: str | None = None
    graph: rx.PyDiGraph = Field(default_factory=rx.PyDiGraph)
    A_com: torch.Tensor = Field(default_factory=lambda: torch.zeros((0, 0), dtype=torch.float32))
    S_tilde: torch.Tensor | None = Field(default=None)
    p_matrix: torch.Tensor | None = Field(default=None)
    state_storage: Any | None = Field(default=None, exclude=True)

    # Explicit start/end nodes for execution path optimisation
    start_node: str | None = Field(default=None)
    end_node: str | None = Field(default=None)

    # Inactive nodes — present in the graph but not executed
    # Saves tokens without removing nodes from the structure
    disabled_nodes: set[str] = Field(default_factory=set)

    # Routing conditions: {(source, target): condition}
    # Callable conditions (not serialized)
    edge_conditions: dict[tuple[str, str], Any] = Field(default_factory=dict, exclude=True)
    # String conditions from the schema
    edge_condition_names: dict[tuple[str, str], str] = Field(default_factory=dict)

    @property
    def role_sequence(self) -> list[str]:
        """Order of roles (agent identifiers)."""
        result = []
        for a in self.agents:
            if hasattr(a, "agent_id"):
                result.append(a.agent_id)
            elif isinstance(a, dict):
                result.append(a.get("id", a.get("agent_id", str(a))))
            else:
                result.append(str(a))
        return result

    @property
    def embeddings(self) -> torch.Tensor:
        """Stack of agent embeddings or an empty tensor."""
        embs = []
        for a in self.agents:
            emb = getattr(a, "embedding", None) if hasattr(a, "embedding") else None
            if emb is not None:
                embs.append(emb)
        return torch.stack(embs) if embs else torch.zeros((0, 0), dtype=torch.float32)

    @property
    def num_nodes(self) -> int:
        """Number of nodes in the graph."""
        return self.graph.num_nodes()

    @property
    def num_edges(self) -> int:
        """Number of edges in the graph."""
        return self.graph.num_edges()

    @property
    def edges(self) -> list[dict[str, Any]]:
        """List of edges with data (source, target, attr, weight...)."""
        result = []
        for i in self.graph.edge_indices():
            s, t = self.graph.get_edge_endpoints_by_index(i)
            d = self.graph.get_edge_data_by_index(i)
            edge = {"source": self._nid(s), "target": self._nid(t)}
            if isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, torch.Tensor):
                        edge[k] = v.tolist()
                    else:
                        edge[k] = v
            result.append(edge)
        return result

    @property
    def edge_index(self) -> torch.Tensor:
        """Edge index in PyG format (2 x E)."""
        if not self.graph.num_edges():
            return torch.zeros((2, 0), dtype=torch.long)
        src, tgt = [], []
        for i in self.graph.edge_indices():
            s, t = self.graph.get_edge_endpoints_by_index(i)
            src.append(s)
            tgt.append(t)
        return torch.tensor([src, tgt], dtype=torch.long)

    @property
    def edge_attr(self) -> torch.Tensor:
        """Edge feature matrix (default: weight + attr fields)."""
        if not self.graph.num_edges():
            return torch.zeros((0, 4), dtype=torch.float32)
        attrs = []
        default_attr = torch.tensor([1.0, 0.0, 0.0, 0.0], dtype=torch.float32)
        for i in self.graph.edge_indices():
            d = self.graph.get_edge_data_by_index(i)
            attr = d.get("attr", default_attr) if isinstance(d, dict) else default_attr
            if isinstance(attr, torch.Tensor):
                attrs.append(attr)
            else:
                attrs.append(torch.tensor(attr, dtype=torch.float32))
        return torch.vstack(attrs).to(torch.float32)

    @property
    def has_conditional_edges(self) -> bool:
        """Whether the graph has conditional edges."""
        return bool(self.edge_conditions) or bool(self.edge_condition_names)

    @property
    def conditional_edges(self) -> list[tuple[str, str]]:
        """List of conditional edges (source, target)."""
        edges = set(self.edge_conditions.keys())
        edges.update(self.edge_condition_names.keys())
        return list(edges)

    def get_edge_condition(self, source: str, target: str) -> Any | str | None:
        """
        Get the condition for an edge (callable or string).

        Returns the callable if present, otherwise the string condition, otherwise None.
        """
        # First check callable
        if (source, target) in self.edge_conditions:
            return self.edge_conditions[(source, target)]
        # Then string
        if (source, target) in self.edge_condition_names:
            return self.edge_condition_names[(source, target)]
        return None

    def get_all_edge_conditions(self) -> dict[tuple[str, str], Any]:
        """Get all edge conditions (union of callable and string conditions)."""
        result: dict[tuple[str, str], Any] = {}
        # First string conditions
        result.update(self.edge_condition_names)
        # Then callable (overwrite string ones if present)
        result.update(self.edge_conditions)
        return result

    def set_edge_condition(
        self,
        source: str,
        target: str,
        condition: Any,
    ) -> bool:
        """
        Set the condition for an edge.

        Args:
            source: Source ID.
            target: Target ID.
            condition: Callable or string condition.

        Returns:
            True if the edge exists and the condition was set.

        """
        # Check that the edge exists
        src_idx = self.get_node_index(source)
        tgt_idx = self.get_node_index(target)
        if src_idx is None or tgt_idx is None:
            return False

        if callable(condition):
            self.edge_conditions[(source, target)] = condition
        elif isinstance(condition, str):
            self.edge_condition_names[(source, target)] = condition
        return True

    def remove_edge_condition(self, source: str, target: str) -> bool:
        """Remove the condition from an edge."""
        removed = False
        if (source, target) in self.edge_conditions:
            del self.edge_conditions[(source, target)]
            removed = True
        if (source, target) in self.edge_condition_names:
            del self.edge_condition_names[(source, target)]
            removed = True
        return removed

    # ------------------------------------------------------------------
    # Task update
    # ------------------------------------------------------------------

    def update_task(self, new_query: str) -> None:
        """
        Replace the task query **without rebuilding** the graph.

        Updates ``self.query``, replaces the frozen ``TaskNode`` in
        ``self.agents`` with a fresh copy, and resets every agent's
        ``state`` so the next run starts clean.

        Args:
            new_query: New task/question string.

        """
        from gmas.core.agent import TaskNode

        self.query = new_query
        for i, agent in enumerate(self.agents):
            if isinstance(agent, TaskNode):
                self.agents[i] = agent.model_copy(
                    update={"query": new_query, "state": []},
                )
            elif hasattr(agent, "state") and agent.state:
                self.agents[i] = agent.model_copy(update={"state": []})
        self.answer = None

    def _nid(self, idx: int) -> str:
        """Return the node identifier by rustworkx index."""
        d = self.graph.get_node_data(idx)
        return d.get("id", str(idx)) if isinstance(d, dict) else str(idx)

    def get_node_index(self, node_id: str) -> int | None:
        """Find the rustworkx index of a node by its ID."""
        for i in self.graph.node_indices():
            d = self.graph.get_node_data(i)
            if isinstance(d, dict) and d.get("id") == node_id:
                return i
        return None

    def get_agent_by_id(self, agent_id: str) -> Any | None:
        """Return the agent object by its identifier."""
        for agent in self.agents:
            aid = getattr(agent, "agent_id", None)
            if aid is None and isinstance(agent, dict):
                aid = agent.get("id", agent.get("agent_id"))
            if aid == agent_id:
                return agent
        return None

    def add_node(
        self,
        agent: Any,
        connections_from: Sequence[str] | None = None,
        connections_to: Sequence[str] | None = None,
    ) -> bool:
        """Add a node/agent and optionally connect it to neighbours."""
        node_id = getattr(agent, "agent_id", None)
        if node_id is None and isinstance(agent, dict):
            node_id = agent.get("id", agent.get("agent_id"))
        if node_id is None or node_id in self.node_ids:
            return False
        node_type = "task" if getattr(agent, "type", None) == "task" else "agent"
        self.graph.add_node({"id": node_id, "type": node_type})
        self.agents.append(agent)
        self.node_ids.append(node_id)
        self._expand_adjacency(1)
        self.role_connections[node_id] = []
        for src_id in connections_from or []:
            if src_id in self.node_ids:
                self.add_edge(src_id, node_id)
        for tgt_id in connections_to or []:
            if tgt_id in self.node_ids:
                self.add_edge(node_id, tgt_id)
        return True

    def remove_node(
        self,
        node_id: str,
        policy: StateMigrationPolicy = StateMigrationPolicy.DISCARD,
    ) -> Any | None:
        """Remove a node, with optional state migration/archiving."""
        if node_id not in self.node_ids:
            return None
        agent_idx = self.node_ids.index(node_id)
        agent = self.agents[agent_idx]
        rx_idx = self.get_node_index(node_id)
        if policy == StateMigrationPolicy.ARCHIVE:
            self._archive_state(agent)
        if rx_idx is not None:
            self.graph.remove_node(rx_idx)
        self.agents.pop(agent_idx)
        self.node_ids.pop(agent_idx)
        self._shrink_adjacency(agent_idx)
        self.role_connections.pop(node_id, None)
        for conns in self.role_connections.values():
            if node_id in conns:
                conns.remove(node_id)
        if self.task_node == node_id:
            object.__setattr__(self, "task_node", None)
        return agent

    def replace_node(
        self,
        node_id: str,
        new_agent: Any,
        policy: StateMigrationPolicy = StateMigrationPolicy.COPY,
    ) -> Any | None:
        """Replace a node with a new agent using the selected state migration policy."""
        if node_id not in self.node_ids:
            return None
        agent_idx = self.node_ids.index(node_id)
        old_agent = self.agents[agent_idx]
        rx_idx = self.get_node_index(node_id)
        if policy == StateMigrationPolicy.COPY:
            new_agent = self._copy_state(old_agent, new_agent)
        elif policy == StateMigrationPolicy.ARCHIVE:
            self._archive_state(old_agent)
        new_id = _get_agent_id(new_agent)
        if new_id is None:
            new_id = str(id(new_agent))
        node_type = "task" if getattr(new_agent, "type", None) == "task" else "agent"
        if rx_idx is not None:
            self.graph[rx_idx] = {"id": new_id, "type": node_type}
        self.agents[agent_idx] = new_agent
        self.node_ids[agent_idx] = new_id
        if node_id != new_id:
            if node_id in self.role_connections:
                self.role_connections[new_id] = self.role_connections.pop(node_id)
            for conns in self.role_connections.values():
                for i, c in enumerate(conns):
                    if c == node_id:
                        conns[i] = new_id
            if self.task_node == node_id:
                object.__setattr__(self, "task_node", new_id)
        return old_agent

    def _copy_state(self, old_agent: Any, new_agent: Any) -> Any:
        """Copy state/hidden_state/embedding from the old agent to the new one."""
        if hasattr(old_agent, "state") and hasattr(new_agent, "with_state"):
            new_agent = new_agent.with_state(list(old_agent.state))
        if (
            hasattr(old_agent, "hidden_state")
            and old_agent.hidden_state is not None
            and hasattr(new_agent, "with_hidden_state")
        ):
            new_agent = new_agent.with_hidden_state(old_agent.hidden_state)
        if hasattr(old_agent, "embedding") and old_agent.embedding is not None and hasattr(new_agent, "with_embedding"):
            new_agent = new_agent.with_embedding(old_agent.embedding)
        return new_agent

    def _archive_state(self, agent: Any) -> None:
        """Save the agent state to external storage if it is configured."""
        if self.state_storage is None:
            return

        state_data = {
            "state": list(getattr(agent, "state", [])),
            "hidden_state": (
                agent.hidden_state.cpu().tolist()
                if hasattr(agent, "hidden_state") and agent.hidden_state is not None
                else None
            ),
            "embedding": (
                agent.embedding.cpu().tolist() if hasattr(agent, "embedding") and agent.embedding is not None else None
            ),
        }
        self.state_storage.save(_get_agent_id(agent) or "", state_data)

    def _expand_adjacency(self, count: int = 1) -> None:
        """Expand the adjacency/probability matrices when adding nodes."""
        n = self.A_com.shape[0] if self.A_com.numel() > 0 else 0
        new_n = n + count
        new_a = torch.zeros((new_n, new_n), dtype=torch.float32)
        if n > 0:
            new_a[:n, :n] = self.A_com
        object.__setattr__(self, "A_com", new_a)

        if self.S_tilde is not None:
            new_s = torch.zeros((new_n, new_n), dtype=torch.float32)
            new_s[:n, :n] = self.S_tilde
            object.__setattr__(self, "S_tilde", new_s)

        if self.p_matrix is not None:
            new_p = torch.zeros((new_n, new_n), dtype=torch.float32)
            new_p[:n, :n] = self.p_matrix
            object.__setattr__(self, "p_matrix", new_p)

    def _shrink_adjacency(self, idx: int) -> None:
        """Remove a row/column from the matrices when removing a node."""
        if self.A_com.numel() == 0:
            return

        mask = torch.ones(self.A_com.shape[0], dtype=torch.bool)
        mask[idx] = False
        object.__setattr__(self, "A_com", self.A_com[mask][:, mask])

        if self.S_tilde is not None:
            object.__setattr__(self, "S_tilde", self.S_tilde[mask][:, mask])

        if self.p_matrix is not None:
            object.__setattr__(self, "p_matrix", self.p_matrix[mask][:, mask])

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        weight: float = 1.0,
        **edge_attrs,
    ) -> bool:
        """Add a directed edge and update the adjacency matrix."""
        src_idx = self.get_node_index(source_id)
        tgt_idx = self.get_node_index(target_id)
        if src_idx is None or tgt_idx is None:
            return False
        self.graph.add_edge(src_idx, tgt_idx, {"weight": weight, **edge_attrs})
        src_list_idx = self.node_ids.index(source_id)
        tgt_list_idx = self.node_ids.index(target_id)
        if self.A_com.numel() > 0:
            self.A_com[src_list_idx, tgt_list_idx] = weight
        return True

    def remove_edge(self, source_id: str, target_id: str) -> bool:
        """Remove an edge and zero out the weight in the matrix."""
        src_idx = self.get_node_index(source_id)
        tgt_idx = self.get_node_index(target_id)
        if src_idx is None or tgt_idx is None:
            return False
        for eid in self.graph.edge_indices():
            s, t = self.graph.get_edge_endpoints_by_index(eid)
            if s == src_idx and t == tgt_idx:
                self.graph.remove_edge_from_index(eid)
                src_list_idx = self.node_ids.index(source_id)
                tgt_list_idx = self.node_ids.index(target_id)
                if self.A_com.numel() > 0:
                    self.A_com[src_list_idx, tgt_list_idx] = 0.0
                return True
        return False

    def get_neighbors(self, node_id: str, direction: str = "out") -> list[str]:
        """Return neighbouring nodes (out/in/both)."""
        idx = self.get_node_index(node_id)
        if idx is None:
            return []
        neighbors = set()
        for eid in self.graph.edge_indices():
            s, t = self.graph.get_edge_endpoints_by_index(eid)
            if direction in ("out", "both") and s == idx:
                neighbors.add(self._nid(t))
            if direction in ("in", "both") and t == idx:
                neighbors.add(self._nid(s))
        return list(neighbors)

    def update_communication(
        self,
        a_com: torch.Tensor,
        s_tilde: torch.Tensor | None = None,
        p_matrix: torch.Tensor | None = None,
    ) -> None:
        """Fully replace the communication matrix and graph edges."""
        a_tensor = a_com.detach().cpu() if a_com.requires_grad else a_com.cpu()
        for eid in list(self.graph.edge_indices()):
            self.graph.remove_edge_from_index(eid)

        n_nodes = a_tensor.shape[0]
        node_indices = list(self.graph.node_indices())

        for i in range(n_nodes):
            for j in range(n_nodes):
                if a_tensor[i, j].item() > EDGE_THRESHOLD and i < len(node_indices) and j < len(node_indices):
                    edge_data = {"weight": float(a_tensor[i, j].item()), "from_update": True}
                    if s_tilde is not None:
                        s_tensor = s_tilde.detach().cpu() if s_tilde.requires_grad else s_tilde.cpu()
                        edge_data["score"] = float(s_tensor[i, j].item())
                    if p_matrix is not None:
                        p_tensor = p_matrix.detach().cpu() if p_matrix.requires_grad else p_matrix.cpu()
                        edge_data["p_ij"] = float(p_tensor[i, j].item())
                    self.graph.add_edge(node_indices[i], node_indices[j], edge_data)

        object.__setattr__(self, "A_com", a_tensor.to(torch.float32))
        if s_tilde is not None:
            s_tensor = s_tilde.detach().cpu() if s_tilde.requires_grad else s_tilde.cpu()
            object.__setattr__(self, "S_tilde", s_tensor.to(torch.float32))
        if p_matrix is not None:
            p_tensor = p_matrix.detach().cpu() if p_matrix.requires_grad else p_matrix.cpu()
            object.__setattr__(self, "p_matrix", p_tensor.to(torch.float32))

    def verify_integrity(self, raise_on_error: bool = True) -> list[str]:
        """Check consistency of the agent list, nodes, and matrices."""
        errors: list[str] = []
        n_agents = len(self.agents)
        n_ids = len(self.node_ids)
        n_rx = self.graph.num_nodes()
        n_matrix = self.A_com.shape[0] if self.A_com.numel() > 0 else 0
        if n_agents != n_ids:
            errors.append(f"agents ({n_agents}) != node_ids ({n_ids})")

        if n_agents != n_rx:
            errors.append(f"agents ({n_agents}) != rustworkx nodes ({n_rx})")

        if n_agents != n_matrix:
            errors.append(f"agents ({n_agents}) != matrix size ({n_matrix})")

        role_seq = set(self.role_sequence)
        node_ids_set = set(self.node_ids)

        if role_seq != node_ids_set:
            diff = role_seq.symmetric_difference(node_ids_set)
            errors.append(f"role_sequence != node_ids, diff: {diff}")

        rx_ids = set()
        for i in self.graph.node_indices():
            data = self.graph.get_node_data(i)
            if isinstance(data, dict) and "id" in data:
                rx_ids.add(data["id"])

        if rx_ids != node_ids_set:
            diff = rx_ids.symmetric_difference(node_ids_set)
            errors.append(f"rustworkx IDs != node_ids, diff: {diff}")

        for src, targets in self.role_connections.items():
            if src not in node_ids_set:
                errors.append(f"connection source '{src}' not in nodes")
            errors.extend(f"connection target '{t}' not in nodes" for t in targets if t not in node_ids_set)

        if self.task_node is not None and self.task_node not in node_ids_set:
            errors.append(f"task_node '{self.task_node}' not in nodes")

        if errors and raise_on_error:
            raise GraphIntegrityError("; ".join(errors))

        return errors

    def is_consistent(self) -> bool:
        """Quick size consistency check without a detailed report."""
        n = len(self.agents)
        return (
            len(self.node_ids) == n
            and self.graph.num_nodes() == n
            and (self.A_com.shape[0] if self.A_com.numel() > 0 else 0) == n
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the graph to a dict (for saving or debugging)."""
        emb = self.embeddings
        return {
            "role_sequence": list(self.role_sequence),
            "node_ids": list(self.node_ids),
            "role_connections": {k: list(v) for k, v in self.role_connections.items()},
            "task_node": self.task_node,
            "query": self.query,
            "answer": self.answer,
            "agents": [
                {
                    "agent_id": _get_agent_id(a),
                    "display_name": getattr(a, "display_name", None),
                    "persona": getattr(a, "persona", ""),
                    "description": getattr(a, "description", ""),
                    "llm_backbone": getattr(a, "llm_backbone", None),
                    "tools": list(getattr(a, "tools", [])),
                    "embedding": a.embedding.cpu().tolist() if a.embedding is not None else None,
                    "state": list(getattr(a, "state", [])),
                }
                for a in self.agents
            ],
            "edges": self.edges,
            "embeddings": emb.cpu().tolist() if emb.numel() > 0 else [],
            "edge_index": self.edge_index.tolist() if self.edge_index.numel() > 0 else [[], []],
            "edge_attr": self.edge_attr.tolist() if self.edge_attr.numel() > 0 else [],
            "adjacency": self.A_com.tolist() if self.A_com.numel() > 0 else [],
            "num_nodes": self.num_nodes,
            "num_edges": self.num_edges,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        agent_factory: Any = None,
        verify: bool = True,
    ) -> "RoleGraph":
        """Create a RoleGraph from a dict with agents and edges."""
        from gmas.core.agent import AgentProfile

        factory = agent_factory or AgentProfile
        agents = []
        for a_data in data.get("agents", []):
            emb = a_data.get("embedding")
            embedding = torch.tensor(emb, dtype=torch.float32) if emb else None
            aid = a_data.get("agent_id")
            agent = factory(
                agent_id=aid,
                display_name=a_data.get("display_name", aid),
                persona=a_data.get("persona", ""),
                description=a_data.get("description", ""),
                llm_backbone=a_data.get("llm_backbone"),
                tools=a_data.get("tools", []),
                state=a_data.get("state", []),
                embedding=embedding,
            )
            agents.append(agent)

        graph = rx.PyDiGraph()
        idx_map = {}
        for agent in agents:
            aid = _get_agent_id(agent)
            idx_map[aid] = graph.add_node(
                {
                    "id": aid,
                    "type": "agent",
                }
            )
        for edge in data.get("edges", []):
            src_id = edge.get("source")
            tgt_id = edge.get("target")
            if src_id in idx_map and tgt_id in idx_map:
                edge_data = {k: v for k, v in edge.items() if k not in ("source", "target")}
                graph.add_edge(idx_map[src_id], idx_map[tgt_id], edge_data)
        adj = data.get("adjacency", [])
        a_com = (
            torch.tensor(adj, dtype=torch.float32)
            if adj
            else torch.zeros((len(agents), len(agents)), dtype=torch.float32)
        )
        rg = cls(
            agents=agents,
            node_ids=data.get("node_ids", [_get_agent_id(a) for a in agents]),
            role_connections=data.get("role_connections", {}),
            task_node=data.get("task_node"),
            query=data.get("query"),
            answer=data.get("answer"),
            graph=graph,
            A_com=a_com,
        )
        if verify:
            rg.verify_integrity()
        return rg

    @classmethod
    def from_graph(
        cls,
        agents: Sequence[Any],
        graph: rx.PyDiGraph,
        a_com: torch.Tensor,
        connections: Mapping[str, Iterable[str]],
        task_node: str | None = None,
        query: str | None = None,
        answer: str | None = None,
        verify: bool = True,
    ) -> "RoleGraph":
        """Create a RoleGraph from an existing PyDiGraph and adjacency matrix."""
        agents_list = list(agents)
        a_tensor = a_com if isinstance(a_com, torch.Tensor) else torch.tensor(a_com, dtype=torch.float32)
        node_ids_raw = [_get_agent_id(a) for a in agents_list]
        node_ids_filtered = [nid for nid in node_ids_raw if nid is not None]
        rg = cls(
            agents=agents_list,
            node_ids=node_ids_filtered,
            role_connections={k: list(v) for k, v in connections.items()},
            task_node=task_node,
            query=query,
            answer=answer,
            graph=graph,
            A_com=a_tensor.to(torch.float32),
        )
        if verify:
            rg.verify_integrity()
        return rg

    def to_pyg_data(
        self,
        node_features: dict[str, torch.Tensor] | None = None,
        edge_features: dict[str, torch.Tensor] | None = None,
        include_embeddings: bool = True,
        include_default_edge_attr: bool = True,
    ) -> Any:
        """Convert the graph to torch_geometric.data.Data with features."""
        from torch_geometric.data import Data

        n = len(self.role_sequence)
        num_edges = self.num_edges

        x_parts = []

        if include_embeddings:
            emb = self.embeddings
            if emb.numel() > 0:
                x_parts.append(emb)

        if node_features:
            for node_feat in node_features.values():
                if node_feat.shape[0] == n:
                    feat_to_add = node_feat.unsqueeze(1) if node_feat.dim() == 1 else node_feat
                    x_parts.append(feat_to_add)

        x = torch.cat(x_parts, dim=1) if x_parts else torch.zeros((n, 0), dtype=torch.float32)

        ei = self.edge_index if self.edge_index.numel() > 0 else torch.zeros((2, 0), dtype=torch.long)

        ea_parts = []

        if include_default_edge_attr:
            default_ea = self.edge_attr if self.edge_attr.numel() > 0 else None
            if default_ea is not None and default_ea.numel() > 0:
                ea_parts.append(default_ea)

        if edge_features:
            for edge_feat in edge_features.values():
                if edge_feat.shape[0] == num_edges:
                    feat_to_add = edge_feat.unsqueeze(1) if edge_feat.dim() == 1 else edge_feat
                    ea_parts.append(feat_to_add)

        ea = torch.cat(ea_parts, dim=1) if ea_parts else torch.zeros((ei.shape[1], 0), dtype=torch.float32)

        data = Data(x=x, edge_index=ei, edge_attr=ea, num_nodes=n)

        data.node_ids = self.node_ids
        data.role_sequence = self.role_sequence

        if self.p_matrix is not None:
            data.p_matrix = self.p_matrix.clone()

        return data

    def get_edge_features_from_schema(self) -> dict[str, torch.Tensor]:
        """Extract edge feature tensors from the saved schema."""
        features = {
            "weight": [],
            "probability": [],
            "trust": [],
        }

        for eid in self.graph.edge_indices():
            data = self.graph.get_edge_data_by_index(eid)
            if isinstance(data, dict):
                features["weight"].append(data.get("weight", 1.0))
                features["probability"].append(data.get("probability", 1.0))

                schema = data.get("schema", {})
                cost = schema.get("cost", {})
                features["trust"].append(cost.get("trust", 1.0))
            else:
                features["weight"].append(1.0)
                features["probability"].append(1.0)
                features["trust"].append(1.0)

        return {name: torch.tensor(values, dtype=torch.float32) for name, values in features.items()}

    def get_node_features_from_schema(self) -> dict[str, torch.Tensor]:
        """Extract node feature tensors from the rustworkx data schema."""
        features = {
            "trust_score": [],
            "quality_score": [],
        }

        for node_id in self.node_ids:
            idx = self.get_node_index(node_id)
            if idx is not None:
                data = self.graph.get_node_data(idx)
                if isinstance(data, dict):
                    schema = data.get("schema", {})
                    features["trust_score"].append(schema.get("trust_score", 1.0))
                    features["quality_score"].append(schema.get("quality_score", 1.0))
                else:
                    features["trust_score"].append(1.0)
                    features["quality_score"].append(1.0)
            else:
                features["trust_score"].append(1.0)
                features["quality_score"].append(1.0)

        return {name: torch.tensor(values, dtype=torch.float32) for name, values in features.items()}

    def subgraph(self, node_ids: list[str]) -> "RoleGraph":
        """Build a subgraph containing only the selected nodes and their connections."""
        agents = [a for a in self.agents if _get_agent_id(a) in node_ids]
        id_set = set(node_ids)
        new_graph = rx.PyDiGraph()
        idx_map = {}
        for agent in agents:
            agent_id = _get_agent_id(agent)
            if agent_id is None:
                continue
            old_idx = self.get_node_index(agent_id)
            if old_idx is not None:
                node_data = self.graph.get_node_data(old_idx)
                new_idx = new_graph.add_node(node_data)
                idx_map[old_idx] = new_idx
        for eid in self.graph.edge_indices():
            s, t = self.graph.get_edge_endpoints_by_index(eid)
            if s in idx_map and t in idx_map:
                edge_data = self.graph.get_edge_data_by_index(eid)
                new_graph.add_edge(idx_map[s], idx_map[t], edge_data)
        indices = [self.node_ids.index(nid) for nid in node_ids if nid in self.node_ids]
        if indices and self.A_com.numel() > 0:
            indices_tensor = torch.tensor(indices)
            new_a = self.A_com[indices_tensor][:, indices_tensor]
        else:
            new_a = torch.zeros((len(agents), len(agents)), dtype=torch.float32)
        new_connections = {k: [v for v in vs if v in id_set] for k, vs in self.role_connections.items() if k in id_set}
        node_ids_raw = [_get_agent_id(a) for a in agents]
        node_ids_filtered = [nid for nid in node_ids_raw if nid is not None]
        return RoleGraph(
            agents=agents,
            node_ids=node_ids_filtered,
            role_connections=new_connections,
            task_node=self.task_node if self.task_node in id_set else None,
            query=self.query,
            answer=self.answer,
            graph=new_graph,
            A_com=new_a,
            start_node=self.start_node if self.start_node in id_set else None,
            end_node=self.end_node if self.end_node in id_set else None,
        )

    def set_start_node(self, node_id: str) -> bool:
        """
        Set the start node for execution.

        Args:
            node_id: ID of the node from which execution starts.

        Returns:
            True if the node exists and was set.

        """
        if node_id not in self.node_ids:
            return False
        object.__setattr__(self, "start_node", node_id)
        return True

    def set_end_node(self, node_id: str) -> bool:
        """
        Set the end node for execution.

        Args:
            node_id: ID of the node at which execution ends.

        Returns:
            True if the node exists and was set.

        """
        if node_id not in self.node_ids:
            return False
        object.__setattr__(self, "end_node", node_id)
        return True

    def set_execution_bounds(self, start_node: str | None, end_node: str | None) -> bool:
        """
        Set start and end nodes simultaneously.

        Args:
            start_node: ID of the start node (None for auto-detection).
            end_node: ID of the end node (None for auto-detection).

        Returns:
            True if both nodes are valid (or None).

        """
        if start_node is not None and start_node not in self.node_ids:
            return False
        if end_node is not None and end_node not in self.node_ids:
            return False
        object.__setattr__(self, "start_node", start_node)
        object.__setattr__(self, "end_node", end_node)
        return True

    # =========================================================================
    # INACTIVE NODES (disabled nodes)
    # =========================================================================

    def disable(self, node_ids: str | list[str]) -> int:
        """
        Deactivate nodes — they remain in the graph but will not be executed.

        Args:
            node_ids: Node ID or list of node IDs to deactivate.

        Returns:
            Number of successfully deactivated nodes.

        Example:
            graph.disable("agent1")           # Single node
            graph.disable(["a1", "a2", "a3"]) # Multiple nodes

        """
        if isinstance(node_ids, str):
            node_ids = [node_ids]

        count = 0
        for node_id in node_ids:
            if node_id in self.node_ids:
                self.disabled_nodes.add(node_id)
                count += 1
        return count

    def enable(self, node_ids: str | list[str] | None = None) -> int:
        """
        Activate nodes.

        Args:
            node_ids: Node ID, list of node IDs, or None to activate all.

        Returns:
            Number of activated nodes.

        Example:
            graph.enable("agent1")           # Single node
            graph.enable(["a1", "a2"])       # Multiple nodes
            graph.enable()                   # All nodes

        """
        if node_ids is None:
            count = len(self.disabled_nodes)
            self.disabled_nodes.clear()
            return count

        if isinstance(node_ids, str):
            node_ids = [node_ids]

        count = 0
        for node_id in node_ids:
            if node_id in self.disabled_nodes:
                self.disabled_nodes.remove(node_id)
                count += 1
        return count

    def is_enabled(self, node_id: str) -> bool:
        """Check whether a node is active."""
        return node_id in self.node_ids and node_id not in self.disabled_nodes

    def get_enabled(self) -> list[str]:
        """Get the list of active nodes."""
        return [nid for nid in self.node_ids if nid not in self.disabled_nodes]

    def get_disabled(self) -> list[str]:
        """Get the list of deactivated nodes."""
        return list(self.disabled_nodes)

    def get_reachable_from(self, source_id: str, threshold: float = EDGE_THRESHOLD) -> set[str]:
        """
        Get all nodes reachable from source_id (forward BFS).

        Args:
            source_id: ID of the start node.
            threshold: Minimum edge weight to consider a connection.

        Returns:
            Set of reachable node IDs (including source_id).

        """
        if source_id not in self.node_ids:
            return set()

        reachable = {source_id}
        queue = deque([source_id])

        while queue:
            current = queue.popleft()
            current_idx = self.node_ids.index(current)

            for j, node_id in enumerate(self.node_ids):
                if node_id in reachable:
                    continue
                if self.A_com.numel() > 0 and self.A_com[current_idx, j].item() > threshold:
                    reachable.add(node_id)
                    queue.append(node_id)

        return reachable

    def get_nodes_reaching(self, target_id: str, threshold: float = EDGE_THRESHOLD) -> set[str]:
        """
        Get all nodes from which target_id is reachable (backward BFS).

        Args:
            target_id: ID of the target node.
            threshold: Minimum edge weight to consider a connection.

        Returns:
            Set of node IDs from which target_id is reachable (including target_id itself).

        """
        if target_id not in self.node_ids:
            return set()

        reaching = {target_id}
        queue = deque([target_id])

        while queue:
            current = queue.popleft()
            current_idx = self.node_ids.index(current)

            for i, node_id in enumerate(self.node_ids):
                if node_id in reaching:
                    continue
                if self.A_com.numel() > 0 and self.A_com[i, current_idx].item() > threshold:
                    reaching.add(node_id)
                    queue.append(node_id)

        return reaching

    def get_relevant_nodes(
        self,
        start_node: str | None = None,
        end_node: str | None = None,
        threshold: float = EDGE_THRESHOLD,
    ) -> set[str]:
        """
        Get nodes that lie on paths from start to end.

        This is the intersection of:
        - Nodes reachable from start_node
        - Nodes from which end_node is reachable

        Nodes not in this set are isolated and not needed for execution.

        Args:
            start_node: ID of the start node (or self.start_node, or the first by order).
            end_node: ID of the end node (or self.end_node, or the last by order).
            threshold: Minimum edge weight.

        Returns:
            Set of relevant node IDs.

        """
        # Determine start
        effective_start = start_node or self.start_node
        if effective_start is None and self.node_ids:
            # First node with no incoming edges
            for node_id in self.node_ids:
                idx = self.node_ids.index(node_id)
                if self.A_com.numel() > 0:
                    in_degree = (self.A_com[:, idx] > threshold).sum().item()
                    if in_degree == 0:
                        effective_start = node_id
                        break
            if effective_start is None:
                effective_start = self.node_ids[0]

        # Determine end
        effective_end = end_node or self.end_node
        if effective_end is None and self.node_ids:
            # Last node with no outgoing edges
            for node_id in reversed(self.node_ids):
                idx = self.node_ids.index(node_id)
                if self.A_com.numel() > 0:
                    out_degree = (self.A_com[idx, :] > threshold).sum().item()
                    if out_degree == 0:
                        effective_end = node_id
                        break
            if effective_end is None:
                effective_end = self.node_ids[-1]

        if effective_start is None or effective_end is None:
            return set()

        # Intersection of nodes reachable from start and leading to end
        reachable_from_start = self.get_reachable_from(effective_start, threshold)
        reaching_end = self.get_nodes_reaching(effective_end, threshold)

        return reachable_from_start & reaching_end

    def get_isolated_nodes(
        self,
        start_node: str | None = None,
        end_node: str | None = None,
        threshold: float = EDGE_THRESHOLD,
    ) -> set[str]:
        """
        Get isolated nodes that do not participate in the start->end path.

        These nodes can be excluded from gmas.execution to save tokens.

        Args:
            start_node: ID of the start node.
            end_node: ID of the end node.
            threshold: Minimum edge weight.

        Returns:
            Set of isolated node IDs.

        """
        relevant = self.get_relevant_nodes(start_node, end_node, threshold)
        all_nodes = set(self.node_ids)
        return all_nodes - relevant

    def get_optimized_execution_order(
        self,
        start_node: str | None = None,
        end_node: str | None = None,
        threshold: float = EDGE_THRESHOLD,
    ) -> list[str]:
        """
        Get the optimised execution order, excluding isolated nodes.

        Args:
            start_node: ID of the start node.
            end_node: ID of the end node.
            threshold: Minimum edge weight.

        Returns:
            List of node IDs in topological order (relevant nodes only).

        """
        relevant = self.get_relevant_nodes(start_node, end_node, threshold)

        # Topological sort of relevant nodes only
        # Build in-degree for relevant nodes
        in_degree: dict[str, int] = dict.fromkeys(relevant, 0)

        for i, src in enumerate(self.node_ids):
            if src not in relevant:
                continue
            for j, tgt in enumerate(self.node_ids):
                if tgt not in relevant:
                    continue
                if self.A_com.numel() > 0 and self.A_com[i, j].item() > threshold:
                    in_degree[tgt] += 1

        # Kahn's algorithm
        queue = deque([node_id for node_id in relevant if in_degree[node_id] == 0])
        result: list[str] = []

        while queue:
            current = queue.popleft()
            result.append(current)

            current_idx = self.node_ids.index(current)
            for j, tgt in enumerate(self.node_ids):
                if tgt not in relevant or tgt in result:
                    continue
                if self.A_com.numel() > 0 and self.A_com[current_idx, j].item() > threshold:
                    in_degree[tgt] -= 1
                    if in_degree[tgt] == 0:
                        queue.append(tgt)

        # Add remaining nodes (in case of cycles)
        for node_id in relevant:
            if node_id not in result:
                result.append(node_id)

        return result

    # =========================================================================
    # DATA VALIDATION (input/output schema validation)
    # =========================================================================

    def get_agent_schema(self, agent_id: str) -> Any | None:
        """
        Get the agent schema from the node data.

        Args:
            agent_id: Agent ID.

        Returns:
            AgentNodeSchema or None if not found.

        """
        idx = self.get_node_index(agent_id)
        if idx is None:
            return None

        data = self.graph.get_node_data(idx)
        if not isinstance(data, dict):
            return None

        schema_dict = data.get("schema")
        if schema_dict is None:
            return None

        # Restore AgentNodeSchema
        from gmas.core.schema import AgentNodeSchema, NodeType

        if schema_dict.get("type") == NodeType.AGENT.value or schema_dict.get("type") == "agent":
            return AgentNodeSchema.model_validate(schema_dict)
        return None

    def validate_agent_input(
        self,
        agent_id: str,
        data: dict[str, Any] | str,
    ) -> Any:
        """
        Validate input data for an agent against its input_schema.

        Args:
            agent_id: Agent ID.
            data: Data to validate (dict or JSON string).

        Returns:
            SchemaValidationResult with the validation result.

        Example:
            result = graph.validate_agent_input("solver", {"question": "2+2=?"})
            if not result.valid:
                print(f"Validation failed: {result.errors}")

        """
        from gmas.core.schema import SchemaValidationResult

        schema = self.get_agent_schema(agent_id)
        if schema is None:
            return SchemaValidationResult(
                valid=True,
                schema_type="input",
                message=f"No schema found for agent '{agent_id}'",
            )

        return schema.validate_input(data)

    def validate_agent_output(
        self,
        agent_id: str,
        data: dict[str, Any] | str,
    ) -> Any:
        """
        Validate output data for an agent against its output_schema.

        Args:
            agent_id: Agent ID.
            data: Data to validate (dict or JSON string).

        Returns:
            SchemaValidationResult with the validation result.

        Example:
            result = graph.validate_agent_output("solver", response)
            if result.valid:
                parsed = result.validated_data

        """
        from gmas.core.schema import SchemaValidationResult

        schema = self.get_agent_schema(agent_id)
        if schema is None:
            return SchemaValidationResult(
                valid=True,
                schema_type="output",
                message=f"No schema found for agent '{agent_id}'",
            )

        return schema.validate_output(data)

    def has_input_schema(self, agent_id: str) -> bool:
        """Check whether the agent has an input_schema."""
        schema = self.get_agent_schema(agent_id)
        return schema is not None and schema.has_input_schema()

    def has_output_schema(self, agent_id: str) -> bool:
        """Check whether the agent has an output_schema."""
        schema = self.get_agent_schema(agent_id)
        return schema is not None and schema.has_output_schema()

    def get_input_schema_json(self, agent_id: str) -> dict[str, Any] | None:
        """
        Get the JSON Schema for the agent's input data.

        Useful for generating prompts describing the expected format.
        """
        schema = self.get_agent_schema(agent_id)
        if schema is None:
            return None
        return schema.input_schema_json

    def get_output_schema_json(self, agent_id: str) -> dict[str, Any] | None:
        """
        Get the JSON Schema for the agent's output data.

        Useful for generating prompts describing the expected response format.
        """
        schema = self.get_agent_schema(agent_id)
        if schema is None:
            return None
        return schema.output_schema_json
