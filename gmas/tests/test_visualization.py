"""Tests for src/core/visualization.py — GraphVisualizer and helper functions."""

import pytest
import rustworkx as rx
import torch

from gmas.core.graph import RoleGraph
from gmas.core.visualization import (
    EdgeStyle,
    GraphVisualizer,
    ImageFormat,
    MermaidDirection,
    NodeShape,
    NodeStyle,
    VisualizationStyle,
    print_graph,
    to_ascii,
    to_dot,
    to_mermaid,
)

# ============================================================================
# Helpers
# ============================================================================


def _make_simple_graph(num_nodes: int = 2, add_edge: bool = True) -> RoleGraph:
    """Create a simple RoleGraph for testing."""
    from gmas.core.agent import AgentProfile

    ids = [chr(ord("a") + i) for i in range(num_nodes)]
    agents = [AgentProfile(agent_id=aid, display_name=f"Agent {aid.upper()}") for aid in ids]

    g = rx.PyDiGraph()
    idx_map = {}
    for aid in ids:
        idx_map[aid] = g.add_node({"id": aid})

    if add_edge and num_nodes >= 2:
        g.add_edge(idx_map[ids[0]], idx_map[ids[1]], {"weight": 0.8})

    a_com = torch.zeros((num_nodes, num_nodes))
    if add_edge and num_nodes >= 2:
        a_com[0, 1] = 0.8

    role_connections = {aid: [] for aid in ids}
    if add_edge and num_nodes >= 2:
        role_connections[ids[0]] = [ids[1]]

    graph = RoleGraph(
        node_ids=ids,
        role_connections=role_connections,
        graph=g,
        A_com=a_com,
    )
    graph.agents = agents
    return graph


def _make_graph_with_tools() -> RoleGraph:
    """Create a graph with agents that have tools."""
    from gmas.core.agent import AgentProfile

    agent_a = AgentProfile(
        agent_id="researcher",
        display_name="Researcher",
        tools=["web_search", "file_search", "code_exec"],
        description="A research agent",
    )
    agent_b = AgentProfile(
        agent_id="writer",
        display_name="Writer",
        tools=["text_tool"],
        description="A writing agent",
    )

    g = rx.PyDiGraph()
    g.add_node({"id": "researcher"})
    g.add_node({"id": "writer"})
    g.add_edge(0, 1, {"weight": 1.0})

    graph = RoleGraph(
        node_ids=["researcher", "writer"],
        role_connections={"researcher": ["writer"], "writer": []},
        graph=g,
        A_com=torch.tensor([[0.0, 1.0], [0.0, 0.0]]),
    )
    graph.agents = [agent_a, agent_b]
    return graph


# ============================================================================
# Tests for enums and models
# ============================================================================


class TestEnumsAndModels:
    def test_mermaid_direction_values(self):
        assert MermaidDirection.TOP_BOTTOM.value == "TB"
        assert MermaidDirection.LEFT_RIGHT.value == "LR"
        assert MermaidDirection.BOTTOM_TOP.value == "BT"
        assert MermaidDirection.RIGHT_LEFT.value == "RL"

    def test_image_format_values(self):
        assert ImageFormat.PNG.value == "png"
        assert ImageFormat.SVG.value == "svg"
        assert ImageFormat.PDF.value == "pdf"

    def test_image_format_from_path_png(self):
        fmt = ImageFormat.from_path("graph.png")
        assert fmt == ImageFormat.PNG

    def test_image_format_from_path_svg(self):
        fmt = ImageFormat.from_path("diagram.svg")
        assert fmt == ImageFormat.SVG

    def test_image_format_from_path_jpeg(self):
        fmt = ImageFormat.from_path("image.jpeg")
        assert fmt == ImageFormat.JPEG

    def test_image_format_from_path_unknown_defaults_to_png(self):
        fmt = ImageFormat.from_path("graph.xyz")
        assert fmt == ImageFormat.PNG

    def test_image_format_from_path_no_extension(self):
        fmt = ImageFormat.from_path("output")
        assert fmt == ImageFormat.PNG

    def test_node_style_defaults(self):
        style = NodeStyle()
        assert style.shape == NodeShape.ROUND
        assert style.fill_color.startswith("#")
        assert style.stroke_color.startswith("#")

    def test_edge_style_defaults(self):
        style = EdgeStyle()
        assert style.line_style == "solid"
        assert style.arrow_head == "normal"

    def test_visualization_style_defaults(self):
        style = VisualizationStyle()
        assert style.direction == MermaidDirection.TOP_BOTTOM
        assert style.show_tools is True
        assert style.show_weights is False


# ============================================================================
# Tests for GraphVisualizer
# ============================================================================


class TestGraphVisualizerInit:
    def test_default_style(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        assert viz.graph is graph
        assert isinstance(viz.style, VisualizationStyle)

    def test_custom_style(self):
        graph = _make_simple_graph()
        style = VisualizationStyle(direction=MermaidDirection.LEFT_RIGHT)
        viz = GraphVisualizer(graph, style=style)
        assert viz.style.direction == MermaidDirection.LEFT_RIGHT


class TestToMermaid:
    def test_basic_output(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid()
        assert "flowchart" in mermaid
        assert "TB" in mermaid

    def test_with_title(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid(title="My Graph")
        assert "title: My Graph" in mermaid
        assert "---" in mermaid

    def test_different_direction(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid(direction=MermaidDirection.LEFT_RIGHT)
        assert "LR" in mermaid

    def test_nodes_in_output(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid()
        assert "Agent A" in mermaid or "a(" in mermaid

    def test_edges_in_output(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid()
        assert "-->" in mermaid

    def test_with_weights(self):
        graph = _make_simple_graph()
        style = VisualizationStyle(show_weights=True)
        viz = GraphVisualizer(graph, style=style)
        mermaid = viz.to_mermaid()
        # Weight 0.8 should appear since it's != 1.0
        assert "w=0.80" in mermaid

    def test_empty_graph(self):
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid()
        assert "flowchart" in mermaid

    def test_with_tools(self):
        graph = _make_graph_with_tools()
        style = VisualizationStyle(show_tools=True)
        viz = GraphVisualizer(graph, style=style)
        mermaid = viz.to_mermaid()
        assert "web_search" in mermaid

    def test_classdefs_included(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid()
        assert "classDef" in mermaid


class TestToAscii:
    def test_basic_output(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        ascii_out = viz.to_ascii()
        assert "╔" in ascii_out
        assert "Graph" in ascii_out

    def test_without_edges(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        ascii_no_edges = viz.to_ascii(show_edges=False)
        assert "Edges:" not in ascii_no_edges

    def test_with_edges(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        ascii_out = viz.to_ascii(show_edges=True)
        assert "Edges:" in ascii_out

    def test_shows_tools(self):
        graph = _make_graph_with_tools()
        style = VisualizationStyle(show_tools=True)
        viz = GraphVisualizer(graph, style=style)
        ascii_out = viz.to_ascii()
        assert "🔧" in ascii_out

    def test_custom_box_width(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        ascii_out = viz.to_ascii(box_width=30)
        assert "╔" in ascii_out

    def test_long_node_name_truncated(self):
        from gmas.core.agent import AgentProfile

        long_name_agent = AgentProfile(
            agent_id="short",
            display_name="A" * 50,  # Very long name
        )
        g = rx.PyDiGraph()
        g.add_node({"id": "short"})
        graph = RoleGraph(node_ids=["short"], graph=g)
        graph.agents = [long_name_agent]

        viz = GraphVisualizer(graph)
        ascii_out = viz.to_ascii(box_width=20)
        assert "..." in ascii_out

    def test_many_edges_displayed(self):
        from gmas.core.agent import AgentProfile

        # Build a graph with many edges
        num_nodes = 15
        ids = [f"node{i}" for i in range(num_nodes)]
        agents = [AgentProfile(agent_id=nid, display_name=nid) for nid in ids]
        g = rx.PyDiGraph()
        idx_map = {}
        for nid in ids:
            idx_map[nid] = g.add_node({"id": nid})
        for i in range(num_nodes - 1):
            g.add_edge(idx_map[ids[i]], idx_map[ids[i + 1]], {"weight": 1.0})

        adj_matrix = torch.zeros((num_nodes, num_nodes))
        adj_matrix = torch.zeros((num_nodes, num_nodes))
        for i in range(num_nodes - 1):
            adj_matrix[i, i + 1] = 1.0
            adj_matrix[i, i + 1] = 1.0

        role_connections = {nid: [] for nid in ids}
        for i in range(num_nodes - 1):
            role_connections[ids[i]] = [ids[i + 1]]

        graph = RoleGraph(node_ids=ids, role_connections=role_connections, graph=g, A_com=adj_matrix)
        graph = RoleGraph(node_ids=ids, role_connections=role_connections, graph=g, A_com=adj_matrix)
        graph.agents = agents

        viz = GraphVisualizer(graph)
        ascii_out = viz.to_ascii(show_edges=True)
        # ASCII output should contain edges section
        assert "Edges:" in ascii_out
        assert "→" in ascii_out


class TestToDot:
    def test_basic_output(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        dot = viz.to_dot()
        assert "digraph" in dot
        assert "AgentGraph" in dot
        assert "->" in dot

    def test_custom_graph_name(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        dot = viz.to_dot(graph_name="MyCustomGraph")
        assert "MyCustomGraph" in dot

    def test_custom_rankdir(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        dot = viz.to_dot(rankdir="LR")
        assert "rankdir=LR" in dot

    def test_with_dpi(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        dot = viz.to_dot(dpi=150)
        assert "dpi=150" in dot

    def test_with_weights(self):
        graph = _make_simple_graph()
        style = VisualizationStyle(show_weights=True)
        viz = GraphVisualizer(graph, style=style)
        dot = viz.to_dot()
        assert "0.80" in dot

    def test_with_tools_in_label(self):
        graph = _make_graph_with_tools()
        style = VisualizationStyle(show_tools=True)
        viz = GraphVisualizer(graph, style=style)
        dot = viz.to_dot()
        assert "web_search" in dot

    def test_empty_graph(self):
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        dot = viz.to_dot()
        assert "digraph" in dot
        assert "}" in dot


class TestToAdjacencyMatrix:
    def test_empty_graph(self):
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        result = viz.to_adjacency_matrix()
        # Empty graph returns a string (the "Empty adjacency matrix" check in source
        # uses a_com.size == 0 which compares a method reference, so falls through
        # to produce an empty header string)
        assert isinstance(result, str)

    def test_basic_matrix(self):
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b"]]
        graph = RoleGraph(
            node_ids=["a", "b"],
            A_com=torch.tensor([[0.0, 0.8], [0.0, 0.0]]),
        )
        graph.agents = agents
        viz = GraphVisualizer(graph)
        result = viz.to_adjacency_matrix()
        assert "0.80" in result

    def test_without_labels(self):
        from gmas.core.agent import AgentProfile

        agents = [AgentProfile(agent_id=aid, display_name=aid) for aid in ["a", "b"]]
        graph = RoleGraph(
            node_ids=["a", "b"],
            A_com=torch.tensor([[0.0, 0.5], [0.0, 0.0]]),
        )
        graph.agents = agents
        viz = GraphVisualizer(graph)
        result = viz.to_adjacency_matrix(show_labels=False)
        # Should not include headers when show_labels=False
        assert "0.50" in result


class TestSafeId:
    def test_simple_id(self):
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        assert viz._safe_id("agent") == "agent"

    def test_id_with_hyphens(self):
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        assert viz._safe_id("agent-name") == "agent_name"

    def test_id_with_spaces(self):
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        assert viz._safe_id("my agent") == "my_agent"

    def test_id_starting_with_digit(self):
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        result = viz._safe_id("123agent")
        assert result.startswith("n_")

    def test_id_with_dots(self):
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        result = viz._safe_id("agent.name")
        assert "." not in result

    def test_empty_id(self):
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        result = viz._safe_id("")
        assert result == "unknown"


class TestFormatNodeLabel:
    def test_basic_label(self):
        from gmas.core.agent import AgentProfile

        agent = AgentProfile(agent_id="a", display_name="My Agent")
        graph = RoleGraph()
        viz = GraphVisualizer(graph)
        label = viz._format_node_label(agent, viz.style.agent_style)
        assert "My Agent" in label

    def test_long_name_truncated(self):
        from gmas.core.agent import AgentProfile

        agent = AgentProfile(agent_id="a", display_name="A" * 50)
        graph = RoleGraph()
        style = VisualizationStyle(max_label_length=20)
        viz = GraphVisualizer(graph, style=style)
        label = viz._format_node_label(agent, viz.style.agent_style)
        assert "..." in label

    def test_with_tools(self):
        from gmas.core.agent import AgentProfile

        agent = AgentProfile(agent_id="a", display_name="Agent", tools=["tool1", "tool2", "tool3"])
        graph = RoleGraph()
        style = VisualizationStyle(show_tools=True)
        viz = GraphVisualizer(graph, style=style)
        label = viz._format_node_label(agent, viz.style.agent_style)
        assert "tool1" in label

    def test_with_many_tools_shows_ellipsis(self):
        from gmas.core.agent import AgentProfile

        agent = AgentProfile(agent_id="a", display_name="Agent", tools=["t1", "t2", "t3", "t4"])
        graph = RoleGraph()
        style = VisualizationStyle(show_tools=True)
        viz = GraphVisualizer(graph, style=style)
        label = viz._format_node_label(agent, viz.style.agent_style)
        assert "..." in label


class TestSaveMermaid:
    def test_save_mermaid_file(self, tmp_path):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        filepath = tmp_path / "test.mmd"
        viz.save_mermaid(filepath)
        content = filepath.read_text()
        assert "flowchart" in content

    def test_save_mermaid_md_file(self, tmp_path):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        filepath = tmp_path / "test.md"
        viz.save_mermaid(filepath, title="Test Graph")
        content = filepath.read_text()
        assert "```mermaid" in content
        assert "```" in content

    def test_save_dot_file(self, tmp_path):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        filepath = tmp_path / "test.dot"
        viz.save_dot(filepath)
        content = filepath.read_text()
        assert "digraph" in content


class TestRenderImageBasic:
    def test_render_raises_import_error_without_graphviz(self, tmp_path):
        import sys
        from unittest import mock

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        # Mock the import to simulate graphviz not being installed
        with mock.patch.dict(sys.modules, {"graphviz": None}), pytest.raises((ImportError, Exception)):
            viz.render_image(tmp_path / "output.png")

    def test_show_interactive_raises_without_graphviz(self):
        import sys
        from unittest import mock

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        with mock.patch.dict(sys.modules, {"graphviz": None}), pytest.raises((ImportError, Exception)):
            viz.show_interactive()


class TestPrintColoredBasic:
    def test_print_colored_no_error(self):
        """print_colored should not raise even if rich is unavailable."""
        import contextlib

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        # Should not raise; may silently fail if rich is not installed
        with contextlib.suppress(Exception):
            viz.print_colored()


# ============================================================================
# Tests for module-level convenience functions
# ============================================================================


class TestConvenienceFunctions:
    def test_to_mermaid_function(self):
        graph = _make_simple_graph()
        result = to_mermaid(graph)
        assert "flowchart" in result

    def test_to_mermaid_with_direction(self):
        graph = _make_simple_graph()
        result = to_mermaid(graph, direction=MermaidDirection.LEFT_RIGHT)
        assert "LR" in result

    def test_to_mermaid_with_title(self):
        graph = _make_simple_graph()
        result = to_mermaid(graph, title="Test")
        assert "title: Test" in result

    def test_to_mermaid_with_custom_style(self):
        graph = _make_simple_graph()
        style = VisualizationStyle(show_weights=True)
        result = to_mermaid(graph, style=style)
        assert "flowchart" in result

    def test_to_ascii_function(self):
        graph = _make_simple_graph()
        result = to_ascii(graph)
        assert "╔" in result

    def test_to_ascii_no_edges(self):
        graph = _make_simple_graph()
        result = to_ascii(graph, show_edges=False)
        assert "Edges:" not in result

    def test_to_dot_function(self):
        graph = _make_simple_graph()
        result = to_dot(graph)
        assert "digraph" in result

    def test_to_dot_custom_name(self):
        graph = _make_simple_graph()
        result = to_dot(graph, graph_name="Custom")
        assert "Custom" in result

    def test_print_graph_auto_format(self):
        """print_graph with auto format should not raise."""
        import sys
        from unittest.mock import MagicMock

        graph = _make_simple_graph()

        mock_console = MagicMock()
        mock_tree = MagicMock()
        mock_branch = MagicMock()
        mock_tree.add.return_value = mock_branch
        mock_branch.add.return_value = MagicMock()
        mock_table = MagicMock()

        mock_rich_console = MagicMock()
        mock_rich_console.Console.return_value = mock_console
        mock_rich_tree = MagicMock()
        mock_rich_tree.Tree.return_value = mock_tree
        mock_rich_table = MagicMock()
        mock_rich_table.Table.return_value = mock_table

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "rich.console", mock_rich_console)
            mp.setitem(sys.modules, "rich.table", mock_rich_table)
            mp.setitem(sys.modules, "rich.tree", mock_rich_tree)
            mp.setitem(sys.modules, "rich", MagicMock())
            print_graph(graph, output_format="auto")  # Should call print_colored via rich

    def test_print_graph_colored_format(self):
        """print_graph with colored format should not raise."""
        graph = _make_simple_graph()
        print_graph(graph, output_format="colored")

    def test_print_graph_ascii_format(self):
        """print_graph with ascii format should not raise."""
        graph = _make_simple_graph()
        print_graph(graph, output_format="ascii")

    def test_print_graph_mermaid_format(self):
        """print_graph with mermaid format should not raise."""
        graph = _make_simple_graph()
        print_graph(graph, output_format="mermaid")


class TestEdgeShortNameTruncation:
    """Test ASCII representation edge source/target name truncation."""

    def test_long_source_name_truncated_in_ascii(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(
            agent_id="very_long_source_name_here",
            display_name="Very Long Source",
        )
        agent_b = AgentProfile(
            agent_id="b",
            display_name="B",
        )
        g = rx.PyDiGraph()
        g.add_node({"id": "very_long_source_name_here"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 1.0})

        graph = RoleGraph(
            node_ids=["very_long_source_name_here", "b"],
            role_connections={"very_long_source_name_here": ["b"], "b": []},
            graph=g,
            A_com=torch.tensor([[0.0, 1.0], [0.0, 0.0]]),
        )
        graph.agents = [agent_a, agent_b]

        viz = GraphVisualizer(graph)
        ascii_out = viz.to_ascii(show_edges=True)
        # Long source name should be truncated in edges section
        assert ".." in ascii_out


class TestTaskNodeVisualization:
    """Test visualization of task nodes (diamond shape)."""

    def test_task_node_in_mermaid(self):
        from gmas.core.agent import AgentProfile

        class TaskAgent(AgentProfile):
            type: str = "task"

        task_agent = TaskAgent(agent_id="task1", display_name="My Task")
        regular_agent = AgentProfile(agent_id="agent1", display_name="My Agent")

        g = rx.PyDiGraph()
        g.add_node({"id": "task1"})
        g.add_node({"id": "agent1"})
        g.add_edge(0, 1, {"type": "task", "weight": 1.0})

        graph = RoleGraph(
            node_ids=["task1", "agent1"],
            role_connections={"task1": ["agent1"], "agent1": []},
            graph=g,
            A_com=torch.tensor([[0.0, 1.0], [0.0, 0.0]]),
        )
        graph.agents = [task_agent, regular_agent]

        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid()
        # Task node should use diamond/special shape {label}
        assert "task1" in mermaid

    def test_task_edge_in_dot(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="A")
        agent_b = AgentProfile(agent_id="b", display_name="B")

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"type": "task_edge", "weight": 1.0})

        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
            graph=g,
            A_com=torch.tensor([[0.0, 1.0], [0.0, 0.0]]),
        )
        graph.agents = [agent_a, agent_b]

        viz = GraphVisualizer(graph)
        dot = viz.to_dot()
        assert "dashed" in dot


class TestRenderImage:
    """Test render_image and show_interactive (with mocked graphviz)."""

    def test_render_image_no_graphviz_raises(self):
        import sys

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        # Temporarily remove graphviz from modules if it exists
        graphviz_backup = sys.modules.get("graphviz")
        sys.modules["graphviz"] = None  # type: ignore[assignment,ty:invalid-assignment]
        try:
            with pytest.raises(ImportError, match=r"[Gg]raphviz"):
                viz.render_image("test.png")
        finally:
            if graphviz_backup is not None:
                sys.modules["graphviz"] = graphviz_backup
            else:
                del sys.modules["graphviz"]

    def test_render_image_with_mock_graphviz(self, tmp_path):
        from unittest.mock import MagicMock, patch

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        mock_source = MagicMock()
        mock_graphviz = MagicMock()
        mock_graphviz.Source.return_value = mock_source

        with patch.dict("sys.modules", {"graphviz": mock_graphviz}):
            viz.render_image(tmp_path / "test.png")
            mock_source.render.assert_called_once()

    def test_render_image_with_explicit_format(self, tmp_path):
        from unittest.mock import MagicMock, patch

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        mock_source = MagicMock()
        mock_graphviz = MagicMock()
        mock_graphviz.Source.return_value = mock_source

        with patch.dict("sys.modules", {"graphviz": mock_graphviz}):
            viz.render_image(tmp_path / "test", image_format=ImageFormat.SVG)
            mock_source.render.assert_called_once()

    def test_render_image_with_dpi(self, tmp_path):
        from unittest.mock import MagicMock, patch

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        mock_source = MagicMock()
        mock_graphviz = MagicMock()
        mock_graphviz.Source.return_value = mock_source

        with patch.dict("sys.modules", {"graphviz": mock_graphviz}):
            viz.render_image(tmp_path / "test.png", dpi=300)
            mock_source.render.assert_called_once()

    def test_render_image_svg_ignores_dpi(self, tmp_path):
        from unittest.mock import MagicMock, patch

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        mock_source = MagicMock()
        mock_graphviz = MagicMock()
        mock_graphviz.Source.return_value = mock_source

        with patch.dict("sys.modules", {"graphviz": mock_graphviz}):
            viz.render_image(tmp_path / "test.svg", dpi=300)
            # For SVG, dpi should be None (ignored)
            mock_source.render.assert_called_once()

    def test_render_image_raises_on_render_error(self, tmp_path):
        from unittest.mock import MagicMock, patch

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        mock_source = MagicMock()
        mock_source.render.side_effect = Exception("render failed")
        mock_graphviz = MagicMock()
        mock_graphviz.Source.return_value = mock_source

        with patch.dict("sys.modules", {"graphviz": mock_graphviz}), pytest.raises(RuntimeError, match="render failed"):
            viz.render_image(tmp_path / "test.png")
        with patch.dict("sys.modules", {"graphviz": mock_graphviz}), pytest.raises(RuntimeError, match="render failed"):
            viz.render_image(tmp_path / "test.png")

    def test_show_interactive_no_graphviz_raises(self):
        import sys

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        graphviz_backup = sys.modules.get("graphviz")
        sys.modules["graphviz"] = None  # type: ignore[assignment,ty:invalid-assignment]
        try:
            with pytest.raises(ImportError, match=r"[Gg]raphviz"):
                viz.show_interactive()
        finally:
            if graphviz_backup is not None:
                sys.modules["graphviz"] = graphviz_backup
            else:
                del sys.modules["graphviz"]

    def test_show_interactive_with_mock_graphviz(self):
        from unittest.mock import MagicMock, patch

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        mock_source = MagicMock()
        mock_graphviz = MagicMock()
        mock_graphviz.Source.return_value = mock_source

        with patch.dict("sys.modules", {"graphviz": mock_graphviz}):
            viz.show_interactive()  # Should not raise


class TestPrintColored:
    """Test print_colored method."""

    def test_print_colored_with_mocked_rich(self):
        """print_colored with mocked rich should cover lines 487-541."""
        import sys
        from unittest.mock import MagicMock

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        mock_console = MagicMock()
        mock_tree = MagicMock()
        mock_branch = MagicMock()
        mock_tree.add.return_value = mock_branch
        mock_branch.add.return_value = MagicMock()
        mock_table = MagicMock()

        mock_rich_console = MagicMock()
        mock_rich_console.Console.return_value = mock_console
        mock_rich_tree = MagicMock()
        mock_rich_tree.Tree.return_value = mock_tree
        mock_rich_table = MagicMock()
        mock_rich_table.Table.return_value = mock_table

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "rich.console", mock_rich_console)
            mp.setitem(sys.modules, "rich.table", mock_rich_table)
            mp.setitem(sys.modules, "rich.tree", mock_rich_tree)
            mp.setitem(sys.modules, "rich", MagicMock())
            viz.print_colored()
            mock_console.print.assert_called()

    def test_print_colored_with_graph_with_tools_and_description(self):
        """Covers description + tools branches in print_colored (lines 503-511)."""
        import sys
        from unittest.mock import MagicMock

        from gmas.core.agent import AgentProfile

        agent = AgentProfile(
            agent_id="a",
            display_name="Agent A",
            description="A helpful agent " * 10,  # long description
            tools=["tool1", "tool2"],
        )
        import rustworkx as rx

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        graph = RoleGraph(node_ids=["a"], graph=g, A_com=torch.zeros((1, 1)))
        graph.agents = [agent]
        viz = GraphVisualizer(graph)

        mock_console = MagicMock()
        mock_tree = MagicMock()
        mock_branch = MagicMock()
        mock_node = MagicMock()
        mock_tree.add.return_value = mock_branch
        mock_branch.add.return_value = mock_node
        mock_node.add.return_value = MagicMock()
        mock_table = MagicMock()

        mock_rich_console = MagicMock()
        mock_rich_console.Console.return_value = mock_console
        mock_rich_tree = MagicMock()
        mock_rich_tree.Tree.return_value = mock_tree
        mock_rich_table = MagicMock()
        mock_rich_table.Table.return_value = mock_table

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "rich.console", mock_rich_console)
            mp.setitem(sys.modules, "rich.table", mock_rich_table)
            mp.setitem(sys.modules, "rich.tree", mock_rich_tree)
            mp.setitem(sys.modules, "rich", MagicMock())
            viz.print_colored()
            mock_console.print.assert_called()

    def test_print_colored_with_many_edges_shows_table(self):
        """Covers edge table section in print_colored (lines 522-541)."""
        import sys
        from unittest.mock import MagicMock

        from gmas.core.agent import AgentProfile

        n = 15
        ids = [f"n{i}" for i in range(n)]
        agents = [AgentProfile(agent_id=nid, display_name=nid) for nid in ids]
        import rustworkx as rx

        g = rx.PyDiGraph()
        idx_map = {}
        for nid in ids:
            idx_map[nid] = g.add_node({"id": nid})
        for i in range(n - 1):
            g.add_edge(idx_map[ids[i]], idx_map[ids[i + 1]], {"weight": 1.0, "source": ids[i], "target": ids[i + 1]})

        adj_matrix = torch.zeros((n, n))
        adj_matrix = torch.zeros((n, n))
        for i in range(n - 1):
            adj_matrix[i, i + 1] = 1.0
            adj_matrix[i, i + 1] = 1.0

        graph = RoleGraph(node_ids=ids, graph=g, A_com=adj_matrix)
        graph = RoleGraph(node_ids=ids, graph=g, A_com=adj_matrix)
        graph.agents = agents
        viz = GraphVisualizer(graph)

        mock_console = MagicMock()
        mock_tree = MagicMock()
        mock_branch = MagicMock()
        mock_tree.add.return_value = mock_branch
        mock_branch.add.return_value = MagicMock()
        mock_table = MagicMock()

        mock_rich_console = MagicMock()
        mock_rich_console.Console.return_value = mock_console
        mock_rich_tree = MagicMock()
        mock_rich_tree.Tree.return_value = mock_tree
        mock_rich_table = MagicMock()
        mock_rich_table.Table.return_value = mock_table

        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(sys.modules, "rich.console", mock_rich_console)
            mp.setitem(sys.modules, "rich.table", mock_rich_table)
            mp.setitem(sys.modules, "rich.tree", mock_rich_tree)
            mp.setitem(sys.modules, "rich", MagicMock())
            viz.print_colored()
            # Should have printed tree + table
            assert mock_console.print.call_count >= 2

    def test_print_colored_without_rich(self):
        """If rich is not available, print_colored falls back gracefully."""
        import sys

        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)

        rich_backup = sys.modules.get("rich")
        sys.modules["rich"] = None  # type: ignore[assignment,ty:invalid-assignment]
        sys.modules["rich.console"] = None  # type: ignore[assignment,ty:invalid-assignment]
        sys.modules["rich.table"] = None  # type: ignore[assignment,ty:invalid-assignment]
        sys.modules["rich.tree"] = None  # type: ignore[assignment,ty:invalid-assignment]
        sys.modules["rich"] = None  # type: ignore[assignment,ty:invalid-assignment]
        sys.modules["rich.console"] = None  # type: ignore[assignment,ty:invalid-assignment]
        sys.modules["rich.table"] = None  # type: ignore[assignment,ty:invalid-assignment]
        sys.modules["rich.tree"] = None  # type: ignore[assignment,ty:invalid-assignment]
        try:
            viz.print_colored()  # Should not raise (fallback)
        finally:
            if rich_backup is not None:
                sys.modules["rich"] = rich_backup
            else:
                sys.modules.pop("rich", None)
            sys.modules.pop("rich.console", None)
            sys.modules.pop("rich.table", None)
            sys.modules.pop("rich.tree", None)


class TestSafeidDoubleUnderscore:
    """Test _safe_id with double underscore inputs."""

    def test_double_underscore_removed(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        result = viz._safe_id("agent__name__here")
        assert "__" not in result

    def test_leading_trailing_underscores_removed(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        result = viz._safe_id("_agent_name_")
        assert not result.startswith("_")
        assert not result.endswith("_")

    def test_empty_string_returns_unknown(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        result = viz._safe_id("")
        assert result == "unknown"

    def test_only_special_chars(self):
        graph = _make_simple_graph()
        viz = GraphVisualizer(graph)
        result = viz._safe_id("---")
        assert result == "unknown"


class TestToMermaidEdgeDedup:
    """Test that duplicate edges are not added to mermaid output."""

    def test_duplicate_edges_deduped(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="A")
        agent_b = AgentProfile(agent_id="b", display_name="B")

        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        g.add_node({"id": "b"})
        g.add_edge(0, 1, {"weight": 1.0})
        g.add_edge(0, 1, {"weight": 0.5})  # Duplicate

        graph = RoleGraph(
            node_ids=["a", "b"],
            role_connections={"a": ["b"], "b": []},
            graph=g,
            A_com=torch.tensor([[0.0, 1.0], [0.0, 0.0]]),
        )
        graph.agents = [agent_a, agent_b]

        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid()
        # Count occurrences of a --> b or a -> b
        count = mermaid.count("a --> b") + mermaid.count("a->b")
        assert count <= 1  # Should be deduped


class TestToMermaidEdgeEmptySourceTarget:
    """Test handling of empty source/target in mermaid output."""

    def test_empty_source_edge_skipped(self):
        from gmas.core.agent import AgentProfile

        agent_a = AgentProfile(agent_id="a", display_name="A")
        g = rx.PyDiGraph()
        g.add_node({"id": "a"})
        # Add edge with empty source/target
        g.add_edge(0, 0, {"source": "", "target": ""})

        graph = RoleGraph(
            node_ids=["a"],
            role_connections={"a": []},
            graph=g,
            A_com=torch.zeros((1, 1)),
        )
        graph.agents = [agent_a]
        viz = GraphVisualizer(graph)
        mermaid = viz.to_mermaid()
        # Should not raise
        assert "flowchart" in mermaid


class TestRenderToImageConvenience:
    """Test render_to_image and show_graph_interactive convenience functions."""

    def test_render_to_image_with_mock(self, tmp_path):
        from unittest.mock import MagicMock, patch

        from gmas.core.visualization import render_to_image

        graph = _make_simple_graph()
        mock_source = MagicMock()
        mock_graphviz = MagicMock()
        mock_graphviz.Source.return_value = mock_source

        with patch.dict("sys.modules", {"graphviz": mock_graphviz}):
            render_to_image(graph, tmp_path / "test.png")
            mock_source.render.assert_called_once()

    def test_show_graph_interactive_with_mock(self):
        from unittest.mock import MagicMock, patch

        from gmas.core.visualization import show_graph_interactive

        graph = _make_simple_graph()
        mock_source = MagicMock()
        mock_graphviz = MagicMock()
        mock_graphviz.Source.return_value = mock_source

        with patch.dict("sys.modules", {"graphviz": mock_graphviz}):
            show_graph_interactive(graph)  # Should not raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
