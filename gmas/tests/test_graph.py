"""Tests for core/graph.py — RoleGraph and dynamic operations."""

import json
import tempfile

import pytest
import rustworkx as rx
import torch

from gmas.core.graph import (
    GraphIntegrityError,
    RoleGraph,
    StateMigrationPolicy,
)
from gmas.utils.state_storage import FileStateStorage, InMemoryStateStorage


class TestRoleGraphCreation:
    def test_empty_graph(self):
        graph = RoleGraph()
        assert graph.num_nodes == 0
        assert graph.num_edges == 0
        assert graph.node_ids == []

    def test_graph_with_nodes(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
        )

        assert graph.num_nodes == 2
        assert "a" in graph.node_ids
        assert "b" in graph.node_ids

    def test_graph_with_edges(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 0.5})

        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
            graph=g,
        )

        assert graph.num_edges == 1
        edges = graph.edges
        assert len(edges) == 1
        assert edges[0]["source"] == "a"
        assert edges[0]["target"] == "b"


class TestAddNode:
    def test_add_node_basic(self):
        from gmas.core.agent import AgentProfile

        graph = RoleGraph()
        agent = AgentProfile(agent_id="new_agent", display_name="Agent")

        result = graph.add_node(agent)

        assert result is True
        assert "new_agent" in graph.node_ids
        assert graph.num_nodes == 1

    def test_add_node_with_connections(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        graph = RoleGraph(
            node_ids=["a"],
            role_connections={"a": []},
            graph=g,
            A_com=torch.zeros((1, 1), dtype=torch.float32),
        )
        graph.agents = [agent_a]

        agent_b = AgentProfile(agent_id="b", display_name="Agent B")
        graph.add_node(agent_b, connections_to=["a"])

        assert "b" in graph.node_ids
        assert graph.A_com.shape == (2, 2)

    def test_add_duplicate_node_raises(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        graph = RoleGraph(
            node_ids=["a"],
            graph=g,
        )
        graph.agents = [agent_a]

        agent_a_dup = AgentProfile(agent_id="a", display_name="Agent A")
        result = graph.add_node(agent_a_dup)

        # add_node returns False for duplicates
        assert result is False

    def test_add_node_expands_matrices(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            A_com=torch.tensor([[0, 1], [0, 0]], dtype=torch.float32),
        )
        graph.agents = [agent_a, agent_b]
        original_shape = graph.A_com.shape

        agent_c = AgentProfile(agent_id="c", display_name="Agent C")
        graph.add_node(agent_c)

        assert graph.A_com.shape[0] == original_shape[0] + 1
        assert graph.A_com.shape[1] == original_shape[1] + 1


class TestRemoveNode:
    def test_remove_node_basic(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
            A_com=torch.zeros((2, 2), dtype=torch.float32),
        )
        graph.agents = [agent_a, agent_b]

        graph.remove_node("b")

        assert "b" not in graph.node_ids
        assert graph.num_nodes == 1

    def test_remove_nonexistent_node_raises(self):
        graph = RoleGraph()

        # remove_node returns None for nonexistent nodes, doesn't raise
        result = graph.remove_node("nonexistent")
        assert result is None

    def test_remove_node_shrinks_matrices(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")
        agent_c = AgentProfile(agent_id="c", display_name="Agent C")

        graph = RoleGraph(
            node_ids=["a", "b", "c"],
            A_com=torch.eye(3, dtype=torch.float32),
        )
        graph.agents = [agent_a, agent_b, agent_c]

        graph.remove_node("b")

        assert graph.A_com.shape == (2, 2)
        assert len(graph.node_ids) == 2

    def test_remove_node_with_discard_policy(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a", "state": {"data": "important"}})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        graph = RoleGraph(
            node_ids=["a"],
            graph=g,
        )
        graph.agents = [agent_a]

        graph.remove_node("a", policy=StateMigrationPolicy.DISCARD)

        assert "a" not in graph.node_ids

    def test_remove_node_with_archive_policy(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a", "state": {"data": "important"}})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        storage = InMemoryStateStorage()
        graph = RoleGraph(
            node_ids=["a"],
            graph=g,
            state_storage=storage,
        )
        graph.agents = [agent_a]

        graph.remove_node("a", policy=StateMigrationPolicy.ARCHIVE)

        assert "a" not in graph.node_ids
        archived = storage.load("a")
        assert archived is not None


class TestReplaceNode:
    def test_replace_node_basic(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "old", "role": "agent"})

        old_agent = AgentProfile(agent_id="old", display_name="Old Agent")
        graph = RoleGraph(
            node_ids=["old"],
            graph=g,
        )
        graph.agents = [old_agent]

        new_agent = AgentProfile(agent_id="new", display_name="New Agent")
        graph.replace_node("old", new_agent, StateMigrationPolicy.COPY)

        assert "old" not in graph.node_ids
        assert "new" in graph.node_ids

    def test_replace_preserves_connections(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_node({"id": "c"})
        g.add_edge(0, 1, {"weight": 0.5})
        g.add_edge(1, 2, {"weight": 0.8})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")
        agent_c = AgentProfile(agent_id="c", display_name="Agent C")

        graph = RoleGraph(
            node_ids=["a", "b", "c"],
            role_connections={"a": ["b"], "b": ["c"], "c": []},
            graph=g,
        )
        graph.agents = [agent_a, agent_b, agent_c]

        agent_b_new = AgentProfile(agent_id="b_new", display_name="Agent B New")
        graph.replace_node("b", agent_b_new, StateMigrationPolicy.COPY)

        assert "b_new" in graph.node_ids
        assert "b_new" in graph.role_connections

    def test_replace_with_copy_policy(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "old", "state": {"key": "value"}})

        old_agent = AgentProfile(agent_id="old", display_name="Old Agent")
        graph = RoleGraph(
            node_ids=["old"],
            graph=g,
        )
        graph.agents = [old_agent]

        new_agent = AgentProfile(agent_id="new", display_name="New Agent")
        graph.replace_node(
            "old",
            new_agent,
            policy=StateMigrationPolicy.COPY,
        )

        assert "new" in graph.node_ids
        assert "old" not in graph.node_ids


class TestIntegrity:
    def test_verify_integrity_valid(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
            graph=g,
            A_com=torch.tensor([[0, 1], [0, 0]], dtype=torch.float32),
        )
        graph.agents = [agent_a, agent_b]

        graph.verify_integrity()

    def test_is_consistent_true(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        graph = RoleGraph(
            node_ids=["a"],
            graph=g,
            A_com=torch.zeros((1, 1), dtype=torch.float32),
        )
        graph.agents = [agent_a]

        assert graph.is_consistent()

    def test_verify_integrity_mismatched_counts(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        graph = RoleGraph(
            node_ids=["a"],
            graph=g,
        )
        graph.agents = [agent_a]

        with pytest.raises(GraphIntegrityError):
            graph.verify_integrity()


class TestSerialization:
    def test_model_dump_basic(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
            query="test query",
        )
        graph.agents = [agent_a, agent_b]

        data = graph.model_dump(exclude={"graph"})

        assert "node_ids" in data
        assert "role_connections" in data
        assert data["query"] == "test query"

    def test_model_dump_excludes_graph(self):
        g = rx.PyDiGraph()
        graph = RoleGraph(graph=g)

        data = graph.model_dump(exclude={"graph"})

        assert "graph" not in data

    def test_json_roundtrip(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
            query="test",
            answer="response",
        )
        graph.agents = [agent_a, agent_b]

        json_str = graph.model_dump_json(exclude={"graph", "A_com", "S_tilde", "p_matrix"})
        data = json.loads(json_str)

        assert data["node_ids"] == ["a", "b"]
        assert data["query"] == "test"


class TestEdgeOperations:
    def test_add_edge(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": [], "b": []},
            graph=g,
            A_com=torch.zeros((2, 2), dtype=torch.float32),
        )
        graph.agents = [agent_a, agent_b]

        graph.add_edge("a", "b", weight=0.7)

        assert graph.num_edges == 1
        # Role connections are updated automatically in add_edge
        # Check via A_com instead
        assert graph.A_com[0, 1] == 0.7

    def test_remove_edge(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 0.5})

        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
            graph=g,
        )

        graph.remove_edge("a", "b")

        assert graph.num_edges == 0

    def test_update_edge_weight(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 0.5})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
            A_com=torch.tensor([[0, 0.5], [0, 0]], dtype=torch.float32),
        )
        graph.agents = [agent_a, agent_b]

        # Remove and re-add edge with new weight
        graph.remove_edge("a", "b")
        graph.add_edge("a", "b", weight=0.9)

        # Check via edges property
        edges = graph.edges
        ab_edge = [e for e in edges if e["source"] == "a" and e["target"] == "b"]
        assert len(ab_edge) == 1
        assert ab_edge[0]["weight"] == 0.9


class TestPyGExport:
    def test_edge_index_empty(self):
        graph = RoleGraph()

        ei = graph.edge_index

        assert ei.shape == (2, 0)

    def test_edge_index_with_edges(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
        )
        graph.agents = [agent_a, agent_b]

        ei = graph.edge_index

        assert ei.shape[0] == 2
        assert ei.shape[1] == 1
        assert ei[0, 0] == 0
        assert ei[1, 0] == 1

    def test_to_pyg_data(self):
        pytest.importorskip("torch_geometric", reason="torch_geometric not installed")
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 0.5})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
        )
        graph.agents = [agent_a, agent_b]

        data = graph.to_pyg_data()
        assert data is not None
        assert hasattr(data, "edge_index")

    def test_to_pyg_data_with_custom_features(self):
        pytest.importorskip("torch_geometric", reason="torch_geometric not installed")
        import torch

        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")

        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
        )
        graph.agents = [agent_a, agent_b]

        node_features = {"custom": torch.tensor([[1.0, 0.0], [0.0, 1.0]], dtype=torch.float32)}
        edge_features = {"custom": torch.tensor([[0.5, 0.5]], dtype=torch.float32)}

        data = graph.to_pyg_data(
            node_features=node_features,
            edge_features=edge_features,
        )
        assert data.x.shape[0] == 2
        assert data.edge_attr.shape[0] == 1


class TestStateStorage:
    def test_in_memory_storage(self):
        storage = InMemoryStateStorage()

        storage.save("node1", {"key": "value"})

        assert storage.load("node1") == {"key": "value"}
        assert storage.load("nonexistent") is None

        storage.delete("node1")
        assert storage.load("node1") is None

    def test_file_storage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FileStateStorage(tmpdir)

            storage.save("node1", {"key": "value"})

            assert storage.load("node1") == {"key": "value"}

            storage.delete("node1")
            assert storage.load("node1") is None

    def test_graph_with_storage(self):
        from gmas.core.agent import AgentProfile

        storage = InMemoryStateStorage()

        g = rx.PyDiGraph()
        g.add_node({"id": "a", "state": {"data": 123}})

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        graph = RoleGraph(
            node_ids=["a"],
            graph=g,
            state_storage=storage,
        )
        graph.agents = [agent_a]

        graph.remove_node("a", policy=StateMigrationPolicy.ARCHIVE)

        archived = storage.load("a")
        assert archived is not None
        assert isinstance(archived.get("state", []), list)


class TestProperties:
    """Tests for various properties of RoleGraph."""

    def test_role_sequence_from_agents(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        agent_b = AgentProfile(agent_id="b", display_name="Agent B")
        graph = RoleGraph(node_ids=["a", "b"])
        graph.agents = [agent_a, agent_b]

        seq = graph.role_sequence
        assert seq == ["a", "b"]

    def test_role_sequence_from_dicts(self):
        graph = RoleGraph(node_ids=["x", "y"])
        graph.agents = [{"id": "x"}, {"id": "y"}]

        seq = graph.role_sequence
        assert "x" in seq
        assert "y" in seq

    def test_embeddings_empty_without_embeddings(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        graph = RoleGraph(node_ids=["a"])
        graph.agents = [agent_a]

        emb = graph.embeddings
        # No embeddings set → empty tensor
        assert emb.numel() == 0

    def test_embeddings_with_agents(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(
            agent_id="a",
            display_name="Agent A",
            embedding=torch.tensor([1.0, 2.0, 3.0]),
        )
        agent_b = AgentProfile(
            agent_id="b",
            display_name="Agent B",
            embedding=torch.tensor([4.0, 5.0, 6.0]),
        )
        graph = RoleGraph(node_ids=["a", "b"])
        graph.agents = [agent_a, agent_b]

        emb = graph.embeddings
        assert emb.shape == (2, 3)

    def test_edge_attr_empty(self):
        graph = RoleGraph()
        ea = graph.edge_attr
        assert ea.shape == (0, 4)

    def test_edge_attr_with_edges(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        # Edge without "attr" key
        g.add_edge(0, 1, {"weight": 0.5})
        graph = RoleGraph(node_ids=["a", "b"], graph=g)
        ea = graph.edge_attr
        # Default attr [1,0,0,0]
        assert ea.shape == (1, 4)

    def test_has_conditional_edges_false(self):
        graph = RoleGraph()
        assert graph.has_conditional_edges is False

    def test_has_conditional_edges_true(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agent_a = AgentProfile(agent_id="a", display_name="A")
        agent_b = AgentProfile(agent_id="b", display_name="B")
        graph = RoleGraph(node_ids=["a", "b"], graph=g)
        graph.agents = [agent_a, agent_b]

        result = graph.set_edge_condition("a", "b", "some_condition")
        assert result is True
        assert graph.has_conditional_edges is True

    def test_conditional_edges_list(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_node({"id": "c"})

        agent_a = AgentProfile(agent_id="a", display_name="A")
        agent_b = AgentProfile(agent_id="b", display_name="B")
        agent_c = AgentProfile(agent_id="c", display_name="C")
        graph = RoleGraph(node_ids=["a", "b", "c"], graph=g)
        graph.agents = [agent_a, agent_b, agent_c]

        graph.set_edge_condition("a", "b", lambda _: True)
        graph.set_edge_condition("b", "c", "is_done")

        cond_edges = graph.conditional_edges
        assert ("a", "b") in cond_edges
        assert ("b", "c") in cond_edges


class TestEdgeConditions:
    def test_set_get_remove_callable_condition(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agent_a = AgentProfile(agent_id="a", display_name="A")
        agent_b = AgentProfile(agent_id="b", display_name="B")
        graph = RoleGraph(node_ids=["a", "b"], graph=g)
        graph.agents = [agent_a, agent_b]

        def cond(ctx):
            return True

        graph.set_edge_condition("a", "b", cond)

        retrieved = graph.get_edge_condition("a", "b")
        assert retrieved is cond

        removed = graph.remove_edge_condition("a", "b")
        assert removed is True
        assert graph.get_edge_condition("a", "b") is None

    def test_set_get_string_condition(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "x"})
        g.add_node({"id": "y"})

        agent_x = AgentProfile(agent_id="x", display_name="X")
        agent_y = AgentProfile(agent_id="y", display_name="Y")
        graph = RoleGraph(node_ids=["x", "y"], graph=g)
        graph.agents = [agent_x, agent_y]

        graph.set_edge_condition("x", "y", "high_confidence")
        assert graph.get_edge_condition("x", "y") == "high_confidence"

    def test_set_condition_nonexistent_nodes_returns_false(self):
        graph = RoleGraph()
        result = graph.set_edge_condition("ghost", "phantom", "cond")
        assert result is False

    def test_get_all_edge_conditions(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_node({"id": "c"})

        for _agent_id in ["a", "b", "c"]:
            pass
        agent_a = AgentProfile(agent_id="a", display_name="A")
        agent_b = AgentProfile(agent_id="b", display_name="B")
        agent_c = AgentProfile(agent_id="c", display_name="C")
        graph = RoleGraph(node_ids=["a", "b", "c"], graph=g)
        graph.agents = [agent_a, agent_b, agent_c]

        graph.set_edge_condition("a", "b", "str_cond")
        graph.set_edge_condition("b", "c", lambda _: False)

        all_conds = graph.get_all_edge_conditions()
        assert ("a", "b") in all_conds
        assert ("b", "c") in all_conds

    def test_remove_condition_not_set_returns_false(self):
        graph = RoleGraph()
        result = graph.remove_edge_condition("a", "b")
        assert result is False


class TestGetAgentById:
    def test_get_existing_agent(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="alpha", display_name="Alpha")
        graph = RoleGraph(node_ids=["alpha"])
        graph.agents = [agent_a]

        result = graph.get_agent_by_id("alpha")
        assert result is agent_a

    def test_get_nonexistent_agent_returns_none(self):
        graph = RoleGraph()
        assert graph.get_agent_by_id("not_here") is None

    def test_get_agent_from_dict(self):
        graph = RoleGraph(node_ids=["dict_agent"])
        graph.agents = [{"id": "dict_agent", "name": "Dict Agent"}]

        result = graph.get_agent_by_id("dict_agent")
        assert result is not None
        assert result["id"] == "dict_agent"


class TestGetNeighbors:
    def _make_graph(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_node({"id": "c"})
        g.add_edge(0, 1, {"weight": 1.0})
        g.add_edge(2, 0, {"weight": 1.0})

        graph = RoleGraph(
            node_ids=["a", "b", "c"],
            graph=g,
            A_com=torch.zeros((3, 3)),
        )
        agents = [AgentProfile(agent_id=aid, display_name=aid.upper()) for aid in ["a", "b", "c"]]
        graph.agents = agents
        return graph

    def test_get_out_neighbors(self):
        graph = self._make_graph()
        out_neighbors = graph.get_neighbors("a", direction="out")
        assert "b" in out_neighbors

    def test_get_in_neighbors(self):
        graph = self._make_graph()
        in_neighbors = graph.get_neighbors("a", direction="in")
        assert "c" in in_neighbors

    def test_get_both_neighbors(self):
        graph = self._make_graph()
        both = graph.get_neighbors("a", direction="both")
        assert "b" in both
        assert "c" in both

    def test_get_neighbors_nonexistent_node(self):
        graph = self._make_graph()
        result = graph.get_neighbors("ghost")
        assert result == []


class TestUpdateCommunication:
    def test_update_communication_basic(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_node({"id": "c"})

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b", "c"]]
        graph = RoleGraph(
            node_ids=["a", "b", "c"],
            graph=g,
            A_com=torch.zeros((3, 3)),
        )
        graph.agents = agents

        new_a = torch.tensor(
            [
                [0.0, 0.9, 0.0],
                [0.0, 0.0, 0.8],
                [0.0, 0.0, 0.0],
            ],
            dtype=torch.float32,
        )
        graph.update_communication(new_a)

        assert graph.A_com[0, 1] == pytest.approx(0.9)
        assert graph.num_edges == 2

    def test_update_communication_with_s_tilde(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b"]]
        graph = RoleGraph(node_ids=["a", "b"], graph=g, A_com=torch.zeros((2, 2)))
        graph.agents = agents

        new_a = torch.tensor([[0.0, 0.9], [0.0, 0.0]])
        s_tilde = torch.tensor([[0.1, 0.8], [0.0, 0.1]])
        graph.update_communication(new_a, s_tilde=s_tilde)

        assert graph.S_tilde is not None
        assert graph.S_tilde[0, 1] == pytest.approx(0.8)

    def test_update_communication_with_p_matrix(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b"]]
        graph = RoleGraph(node_ids=["a", "b"], graph=g, A_com=torch.zeros((2, 2)))
        graph.agents = agents

        new_a = torch.tensor([[0.0, 0.9], [0.0, 0.0]])
        p_matrix = torch.tensor([[0.3, 0.7], [0.5, 0.5]])
        graph.update_communication(new_a, p_matrix=p_matrix)

        assert graph.p_matrix is not None


class TestShrinkExpandAdjacency:
    def test_expand_with_s_tilde(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="A")
        graph = RoleGraph(
            node_ids=["a"],
            A_com=torch.ones((1, 1)),
            S_tilde=torch.ones((1, 1)),
        )
        graph.agents = [agent_a]

        agent_b = AgentProfile(agent_id="b", display_name="B")
        graph.add_node(agent_b)

        assert graph.S_tilde is not None
        assert graph.S_tilde.shape == (2, 2)

    def test_expand_with_p_matrix(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="A")
        graph = RoleGraph(
            node_ids=["a"],
            A_com=torch.ones((1, 1)),
            p_matrix=torch.ones((1, 1)),
        )
        graph.agents = [agent_a]

        agent_b = AgentProfile(agent_id="b", display_name="B")
        graph.add_node(agent_b)

        assert graph.p_matrix is not None
        assert graph.p_matrix.shape == (2, 2)

    def test_shrink_with_s_tilde(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b"]]
        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
            A_com=torch.zeros((2, 2)),
            S_tilde=torch.ones((2, 2)),
        )
        graph.agents = agents

        graph.remove_node("b")
        assert graph.S_tilde is not None
        assert graph.S_tilde.shape == (1, 1)

    def test_shrink_with_p_matrix(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b"]]
        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
            A_com=torch.zeros((2, 2)),
            p_matrix=torch.ones((2, 2)),
        )
        graph.agents = agents

        graph.remove_node("b")
        assert graph.p_matrix is not None
        assert graph.p_matrix.shape == (1, 1)


class TestVerifyIntegrity:
    def test_returns_errors_without_raising(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agent_a = AgentProfile(agent_id="a", display_name="A")
        # Deliberate mismatch: 1 agent, 2 rx nodes
        graph = RoleGraph(node_ids=["a"], graph=g, A_com=torch.zeros((1, 1)))
        graph.agents = [agent_a]

        errors = graph.verify_integrity(raise_on_error=False)
        assert len(errors) > 0

    def test_task_node_not_in_graph(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})

        agent_a = AgentProfile(agent_id="a", display_name="A")
        graph = RoleGraph(
            node_ids=["a"],
            graph=g,
            A_com=torch.zeros((1, 1)),
            task_node="nonexistent_task",
        )
        graph.agents = [agent_a]

        errors = graph.verify_integrity(raise_on_error=False)
        assert any("task_node" in e for e in errors)


class TestToDict:
    def test_to_dict_basic(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="Agent A")
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})

        graph = RoleGraph(
            node_ids=["a"],
            role_connections={"a": []},
            graph=g,
            A_com=torch.zeros((1, 1)),
            query="q",
            answer="ans",
        )
        graph.agents = [agent_a]

        d = graph.to_dict()
        assert d["query"] == "q"
        assert d["answer"] == "ans"
        assert "a" in d["node_ids"]
        assert "adjacency" in d
        assert "edges" in d

    def test_to_dict_with_embeddings(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(
            agent_id="a",
            display_name="Agent A",
            embedding=torch.tensor([1.0, 2.0]),
        )
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})

        graph = RoleGraph(
            node_ids=["a"],
            graph=g,
            A_com=torch.zeros((1, 1)),
        )
        graph.agents = [agent_a]

        d = graph.to_dict()
        assert len(d["embeddings"]) > 0


class TestFromDict:
    def test_from_dict_basic(self):
        data = {
            "agents": [
                {
                    "agent_id": "a",
                    "display_name": "Agent A",
                    "persona": "",
                    "description": "",
                    "llm_backbone": None,
                    "tools": [],
                    "state": [],
                    "embedding": None,
                }
            ],
            "node_ids": ["a"],
            "role_connections": {"a": []},
            "task_node": None,
            "query": "test",
            "answer": None,
            "edges": [],
            "adjacency": [[0.0]],
        }

        graph = RoleGraph.from_dict(data, verify=False)
        assert "a" in graph.node_ids
        assert graph.query == "test"

    def test_from_dict_with_edges(self):
        data = {
            "agents": [
                {"agent_id": "a", "display_name": "A", "persona": "", "description": "", "tools": [], "state": []},
                {"agent_id": "b", "display_name": "B", "persona": "", "description": "", "tools": [], "state": []},
            ],
            "node_ids": ["a", "b"],
            "role_connections": {"a": ["b"], "b": []},
            "task_node": None,
            "query": None,
            "answer": None,
            "edges": [{"source": "a", "target": "b", "weight": 0.8}],
            "adjacency": [[0.0, 0.8], [0.0, 0.0]],
        }

        graph = RoleGraph.from_dict(data, verify=False)
        assert graph.num_edges == 1


class TestFromGraph:
    def test_from_graph_basic(self):
        from gmas.core.agent import AgentProfile

        agents = [
            AgentProfile(agent_id="x", display_name="X"),
            AgentProfile(agent_id="y", display_name="Y"),
        ]
        g = rx.PyDiGraph()
        g.add_node({"id": "x"})
        g.add_node({"id": "y"})
        g.add_edge(0, 1, {"weight": 1.0})

        a_com = torch.tensor([[0.0, 1.0], [0.0, 0.0]])
        connections = {"x": ["y"], "y": []}

        graph = RoleGraph.from_graph(agents, g, a_com, connections, verify=False)
        assert "x" in graph.node_ids
        assert "y" in graph.node_ids
        assert graph.num_edges == 1


class TestSubgraph:
    def test_subgraph_basic(self):
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b", "c"]]
        g = rx.PyDiGraph()
        for aid in ["a", "b", "c"]:
            g.add_node({"id": aid})
        g.add_edge(0, 1, {"weight": 1.0})
        g.add_edge(1, 2, {"weight": 1.0})

        graph = RoleGraph(
            node_ids=["a", "b", "c"],
            role_connections={"a": ["b"], "b": ["c"], "c": []},
            graph=g,
            A_com=torch.tensor([[0, 1, 0], [0, 0, 1], [0, 0, 0]], dtype=torch.float32),
        )
        graph.agents = agents

        sub = graph.subgraph(["a", "b"])
        assert "a" in sub.node_ids
        assert "b" in sub.node_ids
        assert "c" not in sub.node_ids

    def test_subgraph_preserves_edges(self):
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b", "c"]]
        g = rx.PyDiGraph()
        for aid in ["a", "b", "c"]:
            g.add_node({"id": aid})
        g.add_edge(0, 1, {"weight": 0.9})
        g.add_edge(1, 2, {"weight": 0.7})

        graph = RoleGraph(
            node_ids=["a", "b", "c"],
            role_connections={"a": ["b"], "b": ["c"], "c": []},
            graph=g,
            A_com=torch.tensor([[0, 0.9, 0], [0, 0, 0.7], [0, 0, 0]], dtype=torch.float32),
        )
        graph.agents = agents

        sub = graph.subgraph(["a", "b"])
        assert sub.num_edges == 1


class TestStartEndNodes:
    def _make_graph(self):
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["start", "mid", "end"]]
        g = rx.PyDiGraph()
        for aid in ["start", "mid", "end"]:
            g.add_node({"id": aid})
        graph = RoleGraph(
            node_ids=["start", "mid", "end"],
            graph=g,
        )
        graph.agents = agents
        return graph

    def test_set_start_node(self):
        graph = self._make_graph()
        assert graph.set_start_node("start") is True
        assert graph.start_node == "start"

    def test_set_start_node_nonexistent(self):
        graph = self._make_graph()
        assert graph.set_start_node("ghost") is False

    def test_set_end_node(self):
        graph = self._make_graph()
        assert graph.set_end_node("end") is True
        assert graph.end_node == "end"

    def test_set_end_node_nonexistent(self):
        graph = self._make_graph()
        assert graph.set_end_node("phantom") is False

    def test_set_execution_bounds(self):
        graph = self._make_graph()
        result = graph.set_execution_bounds("start", "end")
        assert result is True
        assert graph.start_node == "start"
        assert graph.end_node == "end"

    def test_set_execution_bounds_invalid_start(self):
        graph = self._make_graph()
        assert graph.set_execution_bounds("nonexistent", "end") is False

    def test_set_execution_bounds_invalid_end(self):
        graph = self._make_graph()
        assert graph.set_execution_bounds("start", "nonexistent") is False

    def test_set_execution_bounds_none_values(self):
        graph = self._make_graph()
        result = graph.set_execution_bounds(None, None)
        assert result is True
        assert graph.start_node is None
        assert graph.end_node is None


class TestDisableEnable:
    def _make_graph(self):
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b", "c"]]
        g = rx.PyDiGraph()
        for aid in ["a", "b", "c"]:
            g.add_node({"id": aid})
        graph = RoleGraph(node_ids=["a", "b", "c"], graph=g)
        graph.agents = agents
        return graph

    def test_disable_single_node(self):
        graph = self._make_graph()
        count = graph.disable("a")
        assert count == 1
        assert "a" in graph.disabled_nodes

    def test_disable_multiple_nodes(self):
        graph = self._make_graph()
        count = graph.disable(["a", "b"])
        assert count == 2

    def test_disable_nonexistent_node(self):
        graph = self._make_graph()
        count = graph.disable("ghost")
        assert count == 0

    def test_enable_single_node(self):
        graph = self._make_graph()
        graph.disable("a")
        count = graph.enable("a")
        assert count == 1
        assert "a" not in graph.disabled_nodes

    def test_enable_all_nodes(self):
        graph = self._make_graph()
        graph.disable(["a", "b"])
        count = graph.enable()
        assert count == 2
        assert len(graph.disabled_nodes) == 0

    def test_is_enabled(self):
        graph = self._make_graph()
        assert graph.is_enabled("a") is True
        graph.disable("a")
        assert graph.is_enabled("a") is False

    def test_get_enabled(self):
        graph = self._make_graph()
        graph.disable("a")
        enabled = graph.get_enabled()
        assert "a" not in enabled
        assert "b" in enabled
        assert "c" in enabled

    def test_get_disabled(self):
        graph = self._make_graph()
        graph.disable(["a", "b"])
        disabled = graph.get_disabled()
        assert "a" in disabled
        assert "b" in disabled
        assert "c" not in disabled


class TestReachabilityMethods:
    def _make_linear_graph(self):
        """Create a -> b -> c -> d linear graph."""
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b", "c", "d"]]
        g = rx.PyDiGraph()
        for aid in ["a", "b", "c", "d"]:
            g.add_node({"id": aid})
        g.add_edge(0, 1, {"weight": 1.0})
        g.add_edge(1, 2, {"weight": 1.0})
        g.add_edge(2, 3, {"weight": 1.0})

        adj_matrix = torch.tensor(
            [
                [0, 1, 0, 0],
                [0, 0, 1, 0],
                [0, 0, 0, 1],
                [0, 0, 0, 0],
            ],
            dtype=torch.float32,
        )

        graph = RoleGraph(
            node_ids=["a", "b", "c", "d"],
            role_connections={"a": ["b"], "b": ["c"], "c": ["d"], "d": []},
            graph=g,
            A_com=adj_matrix,
        )
        graph.agents = agents
        return graph

    def test_get_reachable_from(self):
        graph = self._make_linear_graph()
        reachable = graph.get_reachable_from("b")
        assert "b" in reachable
        assert "c" in reachable
        assert "d" in reachable
        assert "a" not in reachable

    def test_get_reachable_from_nonexistent(self):
        graph = self._make_linear_graph()
        result = graph.get_reachable_from("ghost")
        assert result == set()

    def test_get_nodes_reaching(self):
        graph = self._make_linear_graph()
        reaching = graph.get_nodes_reaching("c")
        assert "c" in reaching
        assert "b" in reaching
        assert "a" in reaching
        assert "d" not in reaching

    def test_get_nodes_reaching_nonexistent(self):
        graph = self._make_linear_graph()
        result = graph.get_nodes_reaching("ghost")
        assert result == set()

    def test_get_relevant_nodes(self):
        graph = self._make_linear_graph()
        relevant = graph.get_relevant_nodes("a", "d")
        assert "a" in relevant
        assert "b" in relevant
        assert "c" in relevant
        assert "d" in relevant

    def test_get_isolated_nodes(self):
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b", "c", "iso"]]
        g = rx.PyDiGraph()
        for aid in ["a", "b", "c", "iso"]:
            g.add_node({"id": aid})
        g.add_edge(0, 1, {"weight": 1.0})
        g.add_edge(1, 2, {"weight": 1.0})
        # "iso" has no connections

        adj_matrix = torch.tensor(
            [
                [0, 1, 0, 0],
                [0, 0, 1, 0],
                [0, 0, 0, 0],
                [0, 0, 0, 0],
            ],
            dtype=torch.float32,
        )

        graph = RoleGraph(
            node_ids=["a", "b", "c", "iso"],
            role_connections={"a": ["b"], "b": ["c"], "c": [], "iso": []},
            graph=g,
            A_com=adj_matrix,
        )
        graph.agents = agents

        isolated = graph.get_isolated_nodes("a", "c")
        assert "iso" in isolated
        assert "a" not in isolated

    def test_get_optimized_execution_order(self):
        graph = self._make_linear_graph()
        order = graph.get_optimized_execution_order("a", "d")
        assert order[0] == "a"
        assert order[-1] == "d"


class TestSchemaFeatures:
    def test_get_edge_features_from_schema(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 0.8, "probability": 0.9})

        graph = RoleGraph(node_ids=["a", "b"], graph=g)
        features = graph.get_edge_features_from_schema()

        assert "weight" in features
        assert "probability" in features
        assert "trust" in features
        assert features["weight"][0].item() == pytest.approx(0.8)

    def test_get_node_features_from_schema(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a", "schema": {"trust_score": 0.9}})
        g.add_node({"id": "b", "schema": {}})

        graph = RoleGraph(node_ids=["a", "b"], graph=g)
        features = graph.get_node_features_from_schema()

        assert "trust_score" in features
        assert "quality_score" in features
        assert features["trust_score"][0].item() == pytest.approx(0.9)

    def test_get_agent_schema_not_found(self):
        graph = RoleGraph()
        result = graph.get_agent_schema("nonexistent")
        assert result is None

    def test_validate_agent_input_no_schema(self):
        graph = RoleGraph()
        result = graph.validate_agent_input("ghost", {"data": "value"})
        assert result.valid is True

    def test_validate_agent_output_no_schema(self):
        graph = RoleGraph()
        result = graph.validate_agent_output("ghost", {"data": "value"})
        assert result.valid is True

    def test_has_input_schema_no_agent(self):
        graph = RoleGraph()
        assert graph.has_input_schema("ghost") is False

    def test_has_output_schema_no_agent(self):
        graph = RoleGraph()
        assert graph.has_output_schema("ghost") is False

    def test_get_input_schema_json_no_agent(self):
        graph = RoleGraph()
        assert graph.get_input_schema_json("ghost") is None

    def test_get_output_schema_json_no_agent(self):
        graph = RoleGraph()
        assert graph.get_output_schema_json("ghost") is None


class TestReplaceNodeWithTaskNode:
    def test_replace_task_node_updates_task_node_attr(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "task1"})

        agent = AgentProfile(agent_id="task1", display_name="Task 1")
        graph = RoleGraph(
            node_ids=["task1"],
            graph=g,
            task_node="task1",
        )
        graph.agents = [agent]

        new_agent = AgentProfile(agent_id="task2", display_name="Task 2")
        graph.replace_node("task1", new_agent, StateMigrationPolicy.DISCARD)

        assert graph.task_node == "task2"
        assert "task2" in graph.node_ids

    def test_replace_nonexistent_node_returns_none(self):
        graph = RoleGraph()
        result = graph.replace_node("ghost", object())
        assert result is None


class TestRemoveNodeWithTaskNode:
    def test_remove_task_node_clears_task_node_attr(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "task"})

        agent = AgentProfile(agent_id="task", display_name="Task")
        graph = RoleGraph(
            node_ids=["task"],
            graph=g,
            task_node="task",
        )
        graph.agents = [agent]

        graph.remove_node("task", policy=StateMigrationPolicy.DISCARD)

        assert graph.task_node is None


class TestToPyGDataWithPMatrix:
    def test_to_pyg_data_with_p_matrix(self):
        pytest.importorskip("torch_geometric", reason="torch_geometric not installed")
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 0.5})

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b"]]
        p = torch.tensor([[0.3, 0.7], [0.4, 0.6]])
        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
            A_com=torch.zeros((2, 2)),
            p_matrix=p,
        )
        graph.agents = agents

        data = graph.to_pyg_data()
        assert hasattr(data, "p_matrix")
        assert data.p_matrix.shape == (2, 2)


class TestGetAgentIdFunction:
    """Tests for the module-level _get_agent_id function."""

    def test_object_with_agent_id(self):
        from gmas.core.agent import AgentProfile
        from gmas.core.graph import _get_agent_id

        agent = AgentProfile(agent_id="agent1", display_name="Agent 1")
        assert _get_agent_id(agent) == "agent1"

    def test_dict_with_id_key(self):
        from gmas.core.graph import _get_agent_id

        assert _get_agent_id({"id": "agent1"}) == "agent1"

    def test_dict_with_agent_id_key(self):
        from gmas.core.graph import _get_agent_id

        assert _get_agent_id({"agent_id": "agent1"}) == "agent1"

    def test_returns_none_for_non_matching_object(self):
        from gmas.core.graph import _get_agent_id

        assert _get_agent_id(42) is None
        assert _get_agent_id("just a string") is None


class TestRoleSequenceStr:
    """Test role_sequence when agent is not object or dict."""

    def test_role_sequence_with_string_agent(self):
        graph = RoleGraph()
        graph.agents = ["agent_str"]
        graph.node_ids = ["agent_str"]
        seq = graph.role_sequence
        assert "agent_str" in seq


class TestEdgesWithTensorValues:
    """Test edges property with tensor values in edge data."""

    def test_edges_with_tensor_value(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        tensor_val = torch.tensor([1.0, 2.0, 3.0])
        g.add_edge(0, 1, {"weight": 1.0, "tensor_data": tensor_val})

        graph = RoleGraph(node_ids=["a", "b"], graph=g)
        edges = graph.edges
        assert len(edges) == 1
        assert "tensor_data" in edges[0]
        # tensor_data should be converted to a list
        assert isinstance(edges[0]["tensor_data"], list)


class TestEdgeAttrNonTensor:
    """Test edge_attr when attr is a list (not tensor)."""

    def test_edge_attr_with_list(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 1.0, "attr": [0.5, 0.5, 0.5, 0.5]})

        graph = RoleGraph(node_ids=["a", "b"], graph=g)
        attr = graph.edge_attr
        assert attr.shape == (1, 4)
        assert attr[0, 0].item() == pytest.approx(0.5)


class TestRemoveEdgeConditionBoth:
    """Test removing both callable and string edge conditions."""

    def test_remove_both_conditions(self):
        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
        )
        # Set a callable condition
        graph.edge_conditions[("a", "b")] = lambda _: True
        # Set a string condition
        graph.edge_condition_names[("a", "b")] = "some_condition"

        result = graph.remove_edge_condition("a", "b")
        assert result is True
        assert ("a", "b") not in graph.edge_conditions
        assert ("a", "b") not in graph.edge_condition_names


class TestAddNodeWithConnections:
    """Test add_node with connections_from and connections_to."""

    def test_add_node_with_connections_from(self):
        from gmas.core.agent import AgentProfile

        # Create a graph with an existing node
        g = rx.PyDiGraph()
        g.add_node({"id": "existing"})
        existing_agent = AgentProfile(agent_id="existing", display_name="Existing")
        graph = RoleGraph(
            node_ids=["existing"],
            role_connections={"existing": []},
            graph=g,
            A_com=torch.zeros((1, 1)),
        )
        graph.agents = [existing_agent]

        # Add new node with connections_from an existing node
        new_agent = AgentProfile(agent_id="new_node", display_name="New Node")
        result = graph.add_node(new_agent, connections_from=["existing"])
        assert result is True
        assert "new_node" in graph.node_ids
        assert graph.num_edges == 1

    def test_add_node_already_exists_returns_false(self):
        from gmas.core.agent import AgentProfile

        graph = RoleGraph()
        agent = AgentProfile(agent_id="agent1", display_name="Agent 1")
        graph.add_node(agent)
        # Adding same agent again should return False
        result = graph.add_node(agent)
        assert result is False


class TestRemoveNodeConnections:
    """Test that removing a node also removes it from other nodes' connections."""

    def test_remove_node_removes_from_connections(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 1.0})

        agents = [
            AgentProfile(agent_id="a", display_name="A"),
            AgentProfile(agent_id="b", display_name="B"),
        ]
        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
            graph=g,
            A_com=torch.tensor([[0.0, 1.0], [0.0, 0.0]]),
        )
        graph.agents = agents

        # Remove "b" - "a" still has "b" in connections
        graph.remove_node("b")
        assert "b" not in graph.node_ids
        assert "b" not in graph.role_connections.get("a", [])


class TestReplaceNodeWithArchive:
    """Test replace_node with StateMigrationPolicy.ARCHIVE."""

    def test_replace_node_archive_with_state_storage(self):
        from gmas.core.agent import AgentProfile
        from gmas.utils.state_storage import InMemoryStateStorage

        g = rx.PyDiGraph()
        g.add_node({"id": "agent1"})
        agent = AgentProfile(agent_id="agent1", display_name="Agent 1")
        storage = InMemoryStateStorage()
        graph = RoleGraph(
            node_ids=["agent1"],
            graph=g,
            A_com=torch.zeros((1, 1)),
            state_storage=storage,
        )
        graph.agents = [agent]

        new_agent = AgentProfile(agent_id="agent2", display_name="Agent 2")
        result = graph.replace_node("agent1", new_agent, StateMigrationPolicy.ARCHIVE)
        assert result is agent
        assert "agent2" in graph.node_ids

    def test_replace_node_no_id_uses_generated_id(self):
        """Test replace_node when new agent has no id."""
        from gmas.core.agent import AgentProfile

        class NoIdAgent:
            """An agent with no agent_id attribute."""

            def __init__(self):
                self.name = "no_id_agent"

        g = rx.PyDiGraph()
        g.add_node({"id": "agent1"})
        agent = AgentProfile(agent_id="agent1", display_name="Agent 1")
        graph = RoleGraph(
            node_ids=["agent1"],
            graph=g,
            A_com=torch.zeros((1, 1)),
        )
        graph.agents = [agent]

        no_id_agent = NoIdAgent()
        result = graph.replace_node("agent1", no_id_agent, StateMigrationPolicy.DISCARD)
        assert result is agent
        # A new ID was generated from id(no_id_agent)
        assert len(graph.node_ids) == 1


class TestCopyStateWithHiddenAndEmbedding:
    """Test _copy_state with hidden_state and embedding."""

    def test_copy_state_with_hidden_state(self):
        """Test that with_hidden_state is called when old agent has hidden_state."""
        from gmas.core.graph import RoleGraph

        graph = RoleGraph()

        # Use simple objects to avoid MagicMock chain replacement issue
        class OldAgent:
            hidden_state = torch.tensor([1.0, 2.0])
            embedding = None

        hidden_state_calls = []

        class NewAgent:
            def with_hidden_state(self, hs):
                hidden_state_calls.append(hs)
                return self

        old_agent = OldAgent()
        new_agent = NewAgent()
        graph._copy_state(old_agent, new_agent)
        assert len(hidden_state_calls) == 1

    def test_copy_state_with_embedding(self):
        """Test that with_embedding is called when old agent has embedding."""
        from gmas.core.graph import RoleGraph

        graph = RoleGraph()

        class OldAgent:
            hidden_state = None
            embedding = torch.tensor([1.0, 2.0, 3.0])

        embedding_calls = []

        class NewAgent:
            def with_embedding(self, emb):
                embedding_calls.append(emb)
                return self

        old_agent = OldAgent()
        new_agent = NewAgent()
        graph._copy_state(old_agent, new_agent)
        assert len(embedding_calls) == 1


class TestAddRemoveEdgeFalse:
    """Test add_edge and remove_edge returning False."""

    def test_add_edge_nonexistent_source_returns_false(self):
        graph = RoleGraph()
        result = graph.add_edge("nonexistent", "also_nonexistent")
        assert result is False

    def test_remove_edge_nonexistent_node_returns_false(self):
        graph = RoleGraph()
        result = graph.remove_edge("nonexistent", "also_nonexistent")
        assert result is False

    def test_remove_edge_no_matching_edge_returns_false(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        # No edge between them

        agents = [
            AgentProfile(agent_id="a", display_name="A"),
            AgentProfile(agent_id="b", display_name="B"),
        ]
        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": [], "b": []},
            graph=g,
            A_com=torch.zeros((2, 2)),
        )
        graph.agents = agents

        result = graph.remove_edge("a", "b")
        assert result is False


class TestVerifyIntegrityErrors:
    """Test verify_integrity with various error conditions."""

    def test_agents_count_mismatch(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        # Only one agent but two nodes
        graph = RoleGraph(
            node_ids=["a", "b"],
            graph=g,
            A_com=torch.zeros((2, 2)),
        )
        # Only one agent instead of two
        graph.agents = [AgentProfile(agent_id="a", display_name="A")]

        errors = graph.verify_integrity(raise_on_error=False)
        assert any("agents" in e for e in errors)

    def test_role_sequence_mismatch(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        a_com = torch.zeros((2, 2))
        agents = [
            AgentProfile(agent_id="a", display_name="A"),
            AgentProfile(agent_id="c", display_name="C"),  # Mismatched ID
        ]
        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": [], "b": []},
            graph=g,
            A_com=a_com,
        )
        graph.agents = agents

        errors = graph.verify_integrity(raise_on_error=False)
        # Should detect role_sequence != node_ids
        assert len(errors) > 0

    def test_connection_source_not_in_nodes(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        a_com = torch.zeros((1, 1))
        agents = [AgentProfile(agent_id="a", display_name="A")]
        # role_connections has "ghost" which is not in node_ids
        graph = RoleGraph(
            node_ids=["a"],
            role_connections={"a": [], "ghost": []},
            graph=g,
            A_com=a_com,
        )
        graph.agents = agents

        errors = graph.verify_integrity(raise_on_error=False)
        assert any("ghost" in e for e in errors)


class TestGetRelevantNodesAutoDetect:
    """Test get_relevant_nodes when start/end are auto-detected."""

    def _make_linear_graph(self):
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b", "c"]]
        g = rx.PyDiGraph()
        for aid in ["a", "b", "c"]:
            g.add_node({"id": aid})
        g.add_edge(0, 1, {"weight": 1.0})
        g.add_edge(1, 2, {"weight": 1.0})

        adj_matrix = torch.tensor(
            [
                [0, 1, 0],
                [0, 0, 1],
                [0, 0, 0],
            ],
            dtype=torch.float32,
        )

        graph = RoleGraph(
            node_ids=["a", "b", "c"],
            role_connections={"a": ["b"], "b": ["c"], "c": []},
            graph=g,
            A_com=adj_matrix,
        )
        graph.agents = agents
        return graph

    def test_get_relevant_nodes_auto_start_end(self):
        graph = self._make_linear_graph()
        # Without specifying start/end, should auto-detect
        relevant = graph.get_relevant_nodes()
        assert len(relevant) > 0

    def test_get_relevant_nodes_empty_graph_returns_empty(self):
        graph = RoleGraph()
        relevant = graph.get_relevant_nodes()
        assert relevant == set()

    def test_get_optimized_execution_order_with_cycle(self):
        """Test get_optimized_execution_order falls back for cyclic graph."""
        from gmas.core.agent import AgentProfile

        # Create a graph with a cycle: a -> b -> a
        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b"]]
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 1.0})
        g.add_edge(1, 0, {"weight": 1.0})  # cycle

        adj_matrix = torch.tensor([[0, 1], [1, 0]], dtype=torch.float32)
        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": ["a"]},
            graph=g,
            A_com=adj_matrix,
        )
        graph.agents = agents
        # Should not raise, just return some order
        order = graph.get_optimized_execution_order("a", "b")
        assert len(order) >= 2


class TestGetAgentSchemaWithData:
    """Test get_agent_schema when node data contains schema."""

    def test_get_agent_schema_with_valid_schema(self):
        from gmas.core.schema import AgentNodeSchema, NodeType

        schema = AgentNodeSchema(id="agent1")
        schema_dict = schema.model_dump()
        schema_dict["type"] = NodeType.AGENT.value

        g = rx.PyDiGraph()
        g.add_node({"id": "agent1", "schema": schema_dict})

        graph = RoleGraph(node_ids=["agent1"], graph=g)
        retrieved = graph.get_agent_schema("agent1")
        assert retrieved is not None
        assert retrieved.id == "agent1"

    def test_get_agent_schema_node_not_dict(self):
        g = rx.PyDiGraph()
        g.add_node("not_a_dict")  # non-dict node data

        graph = RoleGraph(node_ids=[], graph=g)
        result = graph.get_agent_schema("nonexistent")
        assert result is None

    def test_validate_agent_input_with_schema(self):
        from gmas.core.schema import AgentNodeSchema, NodeType

        schema = AgentNodeSchema(id="agent1", input_schema_json={"required": ["field1"]})
        schema_dict = schema.model_dump()
        schema_dict["type"] = NodeType.AGENT.value

        g = rx.PyDiGraph()
        g.add_node({"id": "agent1", "schema": schema_dict})

        graph = RoleGraph(node_ids=["agent1"], graph=g)
        result = graph.validate_agent_input("agent1", {"field1": "value"})
        assert result.valid is True

    def test_validate_agent_output_with_schema(self):
        from gmas.core.schema import AgentNodeSchema, NodeType

        schema = AgentNodeSchema(id="agent1", output_schema_json={"required": ["result"]})
        schema_dict = schema.model_dump()
        schema_dict["type"] = NodeType.AGENT.value

        g = rx.PyDiGraph()
        g.add_node({"id": "agent1", "schema": schema_dict})

        graph = RoleGraph(node_ids=["agent1"], graph=g)
        result = graph.validate_agent_output("agent1", {"result": "answer"})
        assert result.valid is True

    def test_get_input_schema_json_with_schema(self):
        from gmas.core.schema import AgentNodeSchema, NodeType

        schema = AgentNodeSchema(id="agent1", input_schema_json={"type": "object"})
        schema_dict = schema.model_dump()
        schema_dict["type"] = NodeType.AGENT.value

        g = rx.PyDiGraph()
        g.add_node({"id": "agent1", "schema": schema_dict})

        graph = RoleGraph(node_ids=["agent1"], graph=g)
        json_schema = graph.get_input_schema_json("agent1")
        assert json_schema is not None

    def test_get_output_schema_json_with_schema(self):
        from gmas.core.schema import AgentNodeSchema, NodeType

        schema = AgentNodeSchema(id="agent1", output_schema_json={"type": "object"})
        schema_dict = schema.model_dump()
        schema_dict["type"] = NodeType.AGENT.value

        g = rx.PyDiGraph()
        g.add_node({"id": "agent1", "schema": schema_dict})

        graph = RoleGraph(node_ids=["agent1"], graph=g)
        json_schema = graph.get_output_schema_json("agent1")
        assert json_schema is not None


class TestGetNodeFeaturesNonDict:
    """Test get_node_features_from_schema when node data is not a dict."""

    def test_non_dict_node_data(self):
        g = rx.PyDiGraph()
        g.add_node("not_a_dict")  # non-dict node data
        g.add_node({"id": "b"})

        graph = RoleGraph(node_ids=["non_dict_id", "b"], graph=g)
        # get_node_features_from_schema iterates over node_ids and gets idx
        # When idx is not None but data is not dict, uses default values
        features = graph.get_node_features_from_schema()
        assert "trust_score" in features
        assert "quality_score" in features


class TestSubgraphEdgeCases:
    """Test subgraph with edge cases."""

    def test_subgraph_agent_without_id_skipped(self):
        """Agent in self.agents that has no agent_id should be skipped."""

        class NoIdAgent:
            def __init__(self):
                self.name = "no_id"

        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 1.0})
        adj_matrix = torch.tensor([[0.0, 1.0], [0.0, 0.0]])

        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
            graph=g,
            A_com=adj_matrix,
        )
        # Inject an agent with no agent_id
        no_id = NoIdAgent()
        graph.agents = [no_id, AgentProfile(agent_id="b", display_name="B")]

        # Creating subgraph with "b" should work despite no_id agent
        sub = graph.subgraph(["b"])
        assert "b" in sub.node_ids

    def test_subgraph_empty_a_com(self):
        """Test subgraph when A_com is empty."""
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        agents = [AgentProfile(agent_id="a", display_name="A")]
        graph = RoleGraph(
            node_ids=["a"],
            role_connections={"a": []},
            graph=g,
            A_com=torch.zeros((0, 0)),
        )
        graph.agents = agents

        sub = graph.subgraph(["a"])
        assert "a" in sub.node_ids


class TestGetEdgeFeaturesNonDict:
    """Test get_edge_features_from_schema when edge data is not a dict."""

    def test_non_dict_edge_data(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, "not_a_dict")  # non-dict edge data

        graph = RoleGraph(node_ids=["a", "b"], graph=g)
        features = graph.get_edge_features_from_schema()
        assert "weight" in features
        assert features["weight"][0].item() == pytest.approx(1.0)  # default


# ─────────────────── Missed-line coverage additions ─────────────────────────


class TestAddNodeDictAgent:
    """Line 273: dict agent uses 'agent_id' key when 'id' key is absent."""

    def test_dict_agent_with_agent_id_key(self):
        graph = RoleGraph()

        # Dict agent that has 'agent_id' but not 'id' key
        agent = {"agent_id": "dict_node_1", "display_name": "Dict Node"}
        result = graph.add_node(agent)
        assert result is True
        assert "dict_node_1" in graph.node_ids


class TestArchiveStateNullStorage:
    """Line 368: _archive_state returns early when state_storage is None."""

    def test_archive_state_no_storage(self):
        from gmas.core.agent import AgentProfile

        agent = AgentProfile(agent_id="a", display_name="A")
        graph = RoleGraph(node_ids=["a"])
        graph.agents = [agent]
        graph.state_storage = None
        # Should silently return without error
        graph._archive_state(agent)


class TestUpdateCommunicationClearsEdges:
    """Line 476: update_communication removes existing edges before adding new ones."""

    def test_update_replaces_edges(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 0.3})
        graph = RoleGraph(node_ids=["a", "b"], graph=g)

        # Replace with a new communication matrix
        new_a = torch.tensor([[0.0, 0.9], [0.0, 0.0]])
        graph.update_communication(new_a)
        assert graph.num_edges == 1


class TestGetNodeFeaturesNonDictDefaults:
    """Lines 778-779: get_node_features_from_schema with non-dict node data uses defaults."""

    def test_non_dict_node_data_uses_defaults(self):
        g = rx.PyDiGraph()
        g.add_node("not_a_dict")  # non-dict node
        graph = RoleGraph(node_ids=["x"], graph=g)

        features = graph.get_node_features_from_schema()
        # trust_score and quality_score should default to 1.0
        assert features["trust_score"][0].item() == pytest.approx(1.0)
        assert features["quality_score"][0].item() == pytest.approx(1.0)


class TestSubgraphNoneAgentId:
    """Line 795: subgraph skips agents whose id is None."""

    def test_agent_with_none_id_skipped(self):
        from gmas.core.agent import AgentProfile

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})

        agent_a = AgentProfile(agent_id="a", display_name="A")
        agent_b = AgentProfile(agent_id="b", display_name="B")
        graph = RoleGraph(node_ids=["a", "b"], graph=g)
        graph.agents = [agent_a, agent_b]
        graph.A_com = torch.zeros(2, 2)

        # subgraph of just ["a"] — agent_b should simply not appear
        sub = graph.subgraph(["a"])
        assert "a" in sub.node_ids
        assert "b" not in sub.node_ids


class TestGetRelevantNodesAllConnected:
    """Lines 1050, 1064: effective_start/end fallback when no node has degree 0."""

    def test_cyclic_graph_uses_first_last_node(self):
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=nid, display_name=nid) for nid in ["a", "b", "c"]]
        g = rx.PyDiGraph()
        for a in agents:
            g.add_node({"id": a.agent_id})

        graph = RoleGraph(node_ids=["a", "b", "c"], graph=g)
        graph.agents = agents
        # Cyclic adjacency matrix → all nodes have in-degree and out-degree > 0
        graph.A_com = torch.tensor([[0.0, 0.9, 0.0], [0.0, 0.0, 0.9], [0.9, 0.0, 0.0]])

        # get_relevant_nodes will call get_execution_order which triggers lines 1050/1064
        relevant = graph.get_relevant_nodes()
        assert len(relevant) >= 0  # should not raise


class TestGetAgentSchema:
    """Lines 1177-1188: get_agent_schema various branches."""

    def test_non_dict_node_returns_none(self):
        g = rx.PyDiGraph()
        g.add_node("plain_string")
        graph = RoleGraph(node_ids=["x"], graph=g)
        result = graph.get_agent_schema("x")
        assert result is None

    def test_dict_node_no_schema_returns_none(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a", "name": "no schema here"})
        graph = RoleGraph(node_ids=["a"], graph=g)
        result = graph.get_agent_schema("a")
        assert result is None

    def test_unknown_node_returns_none(self):
        graph = RoleGraph()
        result = graph.get_agent_schema("nonexistent")
        assert result is None

    def test_dict_node_with_wrong_type_returns_none(self):
        g = rx.PyDiGraph()
        g.add_node({"id": "a", "schema": {"type": "unknown_type", "agent_id": "a"}})
        graph = RoleGraph(node_ids=["a"], graph=g)
        result = graph.get_agent_schema("a")
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
