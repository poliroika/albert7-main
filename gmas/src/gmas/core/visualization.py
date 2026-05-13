"""
Visualisation of agent graphs.

Supports:
- Mermaid (for Markdown/GitHub/documentation)
- ASCII art (for the terminal)
- Graphviz DOT (for external tools)
- Rich Console (coloured terminal output)

Usage:
    from gmas.core.visualization import GraphVisualizer

    viz = GraphVisualizer(graph)
    print(viz.to_mermaid())
    print(viz.to_ascii())
    viz.print_colored()  # Rich console output
"""

import contextlib
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

# Constants for magic values
MAX_TOOLS_PREVIEW = 3
MAX_SHORT_NAME_LENGTH = 8
SHORT_NAME_PREFIX_LENGTH = 6
MAX_DESCRIPTION_LENGTH = 60
MAX_EDGES_DISPLAY = 15

__all__ = [
    "EdgeStyle",
    "GraphVisualizer",
    "ImageFormat",
    "MermaidDirection",
    "NodeStyle",
    "VisualizationStyle",
    "print_graph",
    "render_to_image",
    "show_graph_interactive",
    "to_ascii",
    "to_dot",
    "to_mermaid",
]

if TYPE_CHECKING:
    from gmas.core.graph import RoleGraph


class MermaidDirection(StrEnum):
    """Graph direction in Mermaid."""

    TOP_BOTTOM = "TB"
    BOTTOM_TOP = "BT"
    LEFT_RIGHT = "LR"
    RIGHT_LEFT = "RL"


class ImageFormat(StrEnum):
    """
    Supported image formats for Graphviz.

    Used in render_image() / render_to_image().
    The format can be omitted — it will be inferred from the file extension.
    """

    PNG = "png"
    SVG = "svg"
    PDF = "pdf"
    JPEG = "jpg"

    @classmethod
    def from_path(cls, path: "str | Path") -> "ImageFormat":
        """Determine format from the file extension, default PNG."""
        suffix = Path(path).suffix.lstrip(".").lower()
        if suffix == "jpeg":
            suffix = "jpg"
        with contextlib.suppress(ValueError):
            return cls(suffix)
        return cls.PNG


class NodeShape(StrEnum):
    """Node shapes in Mermaid."""

    RECTANGLE = "rect"
    ROUND = "round"
    STADIUM = "stadium"
    CIRCLE = "circle"
    DIAMOND = "diamond"
    HEXAGON = "hexagon"
    PARALLELOGRAM = "parallelogram"
    TRAPEZOID = "trapezoid"


class NodeStyle(BaseModel):
    """Node display style."""

    shape: NodeShape = NodeShape.ROUND
    fill_color: str = "#e1f5fe"
    stroke_color: str = "#01579b"
    text_color: str = "#000000"
    icon: str = ""  # Emoji or symbol


class EdgeStyle(BaseModel):
    """Edge display style."""

    line_style: str = "solid"  # solid, dashed, dotted
    arrow_head: str = "normal"  # normal, none, diamond
    color: str = "#666666"
    label_color: str = "#333333"


class VisualizationStyle(BaseModel):
    """General visualisation style."""

    direction: MermaidDirection = MermaidDirection.TOP_BOTTOM
    agent_style: NodeStyle = Field(
        default_factory=lambda: NodeStyle(
            shape=NodeShape.ROUND,
            fill_color="#e3f2fd",
            stroke_color="#1976d2",
            icon="🤖",
        )
    )
    task_style: NodeStyle = Field(
        default_factory=lambda: NodeStyle(
            shape=NodeShape.DIAMOND,
            fill_color="#fff3e0",
            stroke_color="#f57c00",
            icon="📋",
        )
    )
    workflow_edge_style: EdgeStyle = Field(
        default_factory=lambda: EdgeStyle(
            line_style="solid",
            color="#1976d2",
        )
    )
    task_edge_style: EdgeStyle = Field(
        default_factory=lambda: EdgeStyle(
            line_style="dashed",
            color="#f57c00",
        )
    )
    show_weights: bool = False
    show_probabilities: bool = False
    show_tools: bool = True
    show_descriptions: bool = False
    max_label_length: int = 30


class GraphVisualizer:
    """RoleGraph visualiser in various formats."""

    def __init__(
        self,
        graph: "RoleGraph",
        style: VisualizationStyle | None = None,
    ):
        """
        Create a visualiser for the graph.

        Args:
            graph: RoleGraph to visualise
            style: Visualisation style (a new one is created by default)

        """
        self.graph = graph
        self.style = style or VisualizationStyle()

    def to_mermaid(
        self,
        direction: MermaidDirection | None = None,
        title: str | None = None,
    ) -> str:
        """
        Export the graph to Mermaid format.

        Args:
            direction: Graph direction (TB, LR, etc.)
            title: Diagram title

        Returns:
            Mermaid diagram code

        Example:
            ```mermaid
            flowchart TB
                researcher[🤖 Researcher]
                analyzer[🤖 Analyzer]
                researcher --> analyzer
            ```

        """
        direction = direction or self.style.direction
        lines = []

        # Title
        if title:
            lines.append("---")
            lines.append(f"title: {title}")
            lines.append("---")

        lines.append(f"flowchart {direction.value}")

        # Nodes
        for agent in self.graph.agents:
            node_id = self._safe_id(agent.agent_id)
            is_task = getattr(agent, "type", None) == "task"
            style = self.style.task_style if is_task else self.style.agent_style

            label = self._format_node_label(agent, style)

            if is_task:
                # Diamond shape for task: {label}
                lines.append(f"    {node_id}{{{label}}}")
            else:
                # Round rectangle for agents: (label)
                lines.append(f"    {node_id}({label})")

        lines.append("")

        # Edges
        edges_added = set()
        for edge in self.graph.edges:
            src = self._safe_id(edge.get("source", ""))
            tgt = self._safe_id(edge.get("target", ""))

            if not src or not tgt:
                continue

            edge_key = (src, tgt)
            if edge_key in edges_added:
                continue
            edges_added.add(edge_key)

            edge_type = edge.get("type", "workflow")
            weight = edge.get("weight", 1.0)

            # Determine line style
            arrow = "-.->" if "task" in edge_type.lower() else "-->"

            # Edge label
            if self.style.show_weights and weight != 1.0:
                lines.append(f"    {src} {arrow}|w={weight:.2f}| {tgt}")
            else:
                lines.append(f"    {src} {arrow} {tgt}")

        # Styles
        lines.append("")
        lines.append("    %% Styles")

        # Style for agents
        agent_ids = [self._safe_id(a.agent_id) for a in self.graph.agents if getattr(a, "type", None) != "task"]
        if agent_ids:
            s = self.style.agent_style
            lines.append(f"    classDef agent fill:{s.fill_color},stroke:{s.stroke_color},stroke-width:2px")
            lines.append(f"    class {','.join(agent_ids)} agent")

        # Style for task nodes
        task_ids = [self._safe_id(a.agent_id) for a in self.graph.agents if getattr(a, "type", None) == "task"]
        if task_ids:
            s = self.style.task_style
            lines.append(f"    classDef task fill:{s.fill_color},stroke:{s.stroke_color},stroke-width:2px")
            lines.append(f"    class {','.join(task_ids)} task")

        return "\n".join(lines)

    def to_ascii(
        self,
        show_edges: bool = True,
        box_width: int = 20,
    ) -> str:
        """
        Export the graph to ASCII art.

        Args:
            show_edges: Whether to show the edge list
            box_width: Width of node blocks

        Returns:
            ASCII representation of the graph

        """
        lines = []

        # Title
        title = f" Graph: {len(self.graph.agents)} nodes, {self.graph.num_edges} edges "
        border = "═" * (box_width + 4)
        lines.append(f"╔{border}╗")
        lines.append(f"║{title:^{box_width + 4}}║")
        lines.append(f"╠{border}╣")

        # Nodes
        for agent in self.graph.agents:
            is_task = getattr(agent, "type", None) == "task"
            icon = "📋" if is_task else "🤖"
            name = agent.display_name or agent.agent_id

            # Trim long names
            if len(name) > box_width - 4:
                name = name[: box_width - 7] + "..."

            node_line = f"{icon} {name}"
            lines.append(f"║  {node_line:<{box_width + 2}}║")

            # Tools
            if self.style.show_tools and hasattr(agent, "tools") and agent.tools:
                tools_str = ", ".join(agent.tools[:MAX_TOOLS_PREVIEW])
                if len(agent.tools) > MAX_TOOLS_PREVIEW:
                    tools_str += f" (+{len(agent.tools) - MAX_TOOLS_PREVIEW})"
                if len(tools_str) > box_width - 2:
                    tools_str = tools_str[: box_width - 5] + "..."
                lines.append(f"║    🔧 {tools_str:<{box_width}}║")

        lines.append(f"╠{border}╣")

        # Edges
        if show_edges:
            lines.append(f"║{'  Edges:':<{box_width + 4}}║")

            edges_shown = 0
            max_edges = 10

            for edge in self.graph.edges:
                if edges_shown >= max_edges:
                    remaining = len(self.graph.edges) - max_edges
                    lines.append(f"║    ... +{remaining} more{' ' * (box_width - 10)}║")
                    break

                src = edge.get("source", "?")
                tgt = edge.get("target", "?")
                edge_type = edge.get("type", "")

                # Shorten names if needed
                if len(src) > MAX_SHORT_NAME_LENGTH:
                    src = src[:SHORT_NAME_PREFIX_LENGTH] + ".."
                if len(tgt) > MAX_SHORT_NAME_LENGTH:
                    tgt = tgt[:SHORT_NAME_PREFIX_LENGTH] + ".."

                arrow = "⤳" if "task" in edge_type.lower() else "→"
                edge_str = f"{src} {arrow} {tgt}"
                lines.append(f"║    {edge_str:<{box_width}}║")

        lines.append(f"╚{border}╝")

        return "\n".join(lines)

    def to_dot(
        self,
        graph_name: str = "AgentGraph",
        rankdir: str = "TB",
        dpi: int | None = None,
    ) -> str:
        """
        Export the graph to Graphviz DOT format.

        Args:
            graph_name: Graph name
            rankdir: Direction (TB, LR, BT, RL)
            dpi: DPI for raster formats (None — use Graphviz default)

        Returns:
            DOT code for Graphviz

        """
        lines = [
            f"digraph {graph_name} {{",
            f"    rankdir={rankdir};",
        ]
        if dpi is not None:
            lines.append(f"    dpi={dpi};")
        lines += [
            '    node [fontname="Helvetica", fontsize=12];',
            '    edge [fontname="Helvetica", fontsize=10];',
            "",
        ]

        # Nodes
        for agent in self.graph.agents:
            node_id = self._safe_id(agent.agent_id)
            is_task = getattr(agent, "type", None) == "task"

            label = agent.display_name or agent.agent_id
            if self.style.show_tools and hasattr(agent, "tools") and agent.tools:
                tools = ", ".join(agent.tools[:3])
                label = f"{label}\\n[{tools}]"

            if is_task:
                style = self.style.task_style
                shape = "diamond"
            else:
                style = self.style.agent_style
                shape = "box"

            lines.append(
                f"    {node_id} ["
                f'label="{label}", '
                f"shape={shape}, "
                f"style=filled, "
                f'fillcolor="{style.fill_color}", '
                f'color="{style.stroke_color}"'
                f"];"
            )

        lines.append("")

        # Edges
        for edge in self.graph.edges:
            src = self._safe_id(edge.get("source", ""))
            tgt = self._safe_id(edge.get("target", ""))

            if not src or not tgt:
                continue

            edge_type = edge.get("type", "workflow")
            weight = edge.get("weight", 1.0)

            attrs = []
            if "task" in edge_type.lower():
                attrs.append("style=dashed")
                attrs.append(f'color="{self.style.task_edge_style.color}"')
            else:
                attrs.append(f'color="{self.style.workflow_edge_style.color}"')

            if self.style.show_weights and weight != 1.0:
                attrs.append(f'label="{weight:.2f}"')

            attr_str = ", ".join(attrs) if attrs else ""
            lines.append(f"    {src} -> {tgt} [{attr_str}];")

        lines.append("}")
        return "\n".join(lines)

    def to_adjacency_matrix(self, show_labels: bool = True) -> str:
        """
        Show the adjacency matrix in text form.

        Args:
            show_labels: Whether to show node labels

        Returns:
            Text representation of the matrix

        """
        a_com = self.graph.A_com
        if a_com.size == 0:
            return "Empty adjacency matrix"

        lines = []
        n = a_com.shape[0]

        # Short labels
        labels = []
        for agent in self.graph.agents[:n]:
            name = agent.agent_id[:6]
            labels.append(name)

        # Title
        if show_labels:
            header = "       " + " ".join(f"{label:>6}" for label in labels)
            lines.append(header)
            lines.append("       " + "-" * (7 * n))

        # Matrix rows
        for i in range(n):
            row_label = f"{labels[i]:>6} |" if show_labels else ""
            row_values = " ".join(f"{a_com[i, j]:>6.2f}" if a_com[i, j] != 0 else "     ." for j in range(n))
            lines.append(f"{row_label}{row_values}")

        return "\n".join(lines)

    def print_colored(self) -> None:
        """Print the graph to the console with colours (requires rich)."""
        try:
            from rich.console import Console
            from rich.table import Table
            from rich.tree import Tree
        except ImportError:
            # Fallback to ASCII if rich not available
            return

        console = Console()

        # Build tree
        tree = Tree(f"[bold blue]🌐 Graph[/bold blue] ({len(self.graph.agents)} nodes, {self.graph.num_edges} edges)")

        # Group agents and tasks
        agents_branch = tree.add("[bold cyan]🤖 Agents[/bold cyan]")
        tasks_branch = tree.add("[bold yellow]📋 Tasks[/bold yellow]")

        for agent in self.graph.agents:
            is_task = getattr(agent, "type", None) == "task"
            branch = tasks_branch if is_task else agents_branch

            name = agent.display_name or agent.agent_id
            node = branch.add(f"[bold]{name}[/bold] ({agent.agent_id})")

            if hasattr(agent, "description") and agent.description:
                desc = agent.description[:MAX_DESCRIPTION_LENGTH]
                if len(agent.description) > MAX_DESCRIPTION_LENGTH:
                    desc += "..."
                node.add(f"[dim]{desc}[/dim]")

            if hasattr(agent, "tools") and agent.tools:
                tools_str = ", ".join(agent.tools)
                node.add(f"[green]🔧 {tools_str}[/green]")

            # Show connections
            neighbors = self.graph.get_neighbors(agent.agent_id, direction="out")
            if neighbors:
                conns = ", ".join(neighbors)
                node.add(f"[blue]→ {conns}[/blue]")

        console.print(tree)

        # Edge table
        if self.graph.num_edges > 0:
            console.print()
            table = Table(title="Edges", show_header=True)
            table.add_column("Source", style="cyan")
            table.add_column("Target", style="green")
            table.add_column("Type", style="yellow")
            table.add_column("Weight", style="magenta")

            for edge in self.graph.edges[:MAX_EDGES_DISPLAY]:
                table.add_row(
                    str(edge.get("source", "")),
                    str(edge.get("target", "")),
                    str(edge.get("type", "workflow")),
                    f"{edge.get('weight', 1.0):.2f}",
                )

            if len(self.graph.edges) > MAX_EDGES_DISPLAY:
                table.add_row("...", "...", "...", f"+{len(self.graph.edges) - MAX_EDGES_DISPLAY} more")

            console.print(table)

    def save_mermaid(self, filepath: "str | Path", title: str | None = None) -> None:
        """
        Save the Mermaid diagram to a file.

        Args:
            filepath: Path to the file (.md or .mmd)
            title: Diagram title

        """
        filepath = Path(filepath)
        content = self.to_mermaid(title=title)

        # Wrap in markdown code block if .md file
        if filepath.suffix == ".md":
            content = f"```mermaid\n{content}\n```"

        filepath.write_text(content, encoding="utf-8")

    def save_dot(self, filepath: "str | Path", graph_name: str = "AgentGraph") -> None:
        """
        Save the DOT file for Graphviz.

        Args:
            filepath: Path to the file (.dot or .gv)
            graph_name: Graph name

        """
        content = self.to_dot(graph_name=graph_name)
        Path(filepath).write_text(content, encoding="utf-8")

    def render_image(
        self,
        filepath: "str | Path",
        image_format: ImageFormat | None = None,
        dpi: int | None = None,
        graph_name: str = "AgentGraph",
    ) -> None:
        """
        Render the graph to an image using Graphviz.

        Args:
            filepath: Path to the output file. The extension is used for
                      automatic format detection if image_format is not set.
            image_format: Image format. If None — determined from the extension of
                          filepath (png/svg/pdf/jpg). Without extension — PNG.
            dpi: DPI for raster formats (png, jpg). None — Graphviz default.
                 Ignored for vector formats (svg, pdf).
            graph_name: Graph name

        Raises:
            ImportError: If graphviz is not installed
            RuntimeError: If rendering failed

        Example:
            viz = GraphVisualizer(graph)
            viz.render_image("my_graph.png")            # format from extension
            viz.render_image("output", ImageFormat.SVG)  # explicit format
            viz.render_image("report.png", dpi=300)

        """
        try:
            import graphviz
        except ImportError:
            msg = "Graphviz is not installed. Install with: pip install graphviz"
            raise ImportError(msg) from None

        filepath = Path(filepath)

        # Determine format: explicit > from extension > PNG default
        fmt = image_format if image_format is not None else ImageFormat.from_path(filepath)

        # DPI is only meaningful for raster formats
        raster_formats = {ImageFormat.PNG, ImageFormat.JPEG}
        effective_dpi = dpi if fmt in raster_formats else None

        dot_source = self.to_dot(graph_name=graph_name, dpi=effective_dpi)
        source = graphviz.Source(dot_source)

        # graphviz.render() adds the extension itself, pass path without it
        output_stem = str(filepath.with_suffix(""))

        try:
            source.render(
                filename=output_stem,
                format=fmt.value,
                cleanup=True,  # removes the intermediate .dot file
            )
        except Exception as e:
            msg = f"Failed to render image: {e}"
            raise RuntimeError(msg) from e

    def show_interactive(self, graph_name: str = "AgentGraph") -> None:
        """
        Show the graph interactively in a window (using Graphviz).

        Args:
            graph_name: Graph name

        Raises:
            ImportError: If graphviz is not installed

        Note:
            Requires Graphviz installed with GUI support

        """
        try:
            import graphviz
        except ImportError:
            msg = "Graphviz is not installed. Install with: pip install graphviz"
            raise ImportError(msg) from None

        dot_source = self.to_dot(graph_name=graph_name)
        source = graphviz.Source(dot_source)

        with contextlib.suppress(Exception):
            source.view(cleanup=True)

    def _safe_id(self, identifier: str) -> str:
        """Convert an identifier to one safe for Mermaid/DOT."""
        # Replace special characters
        safe = identifier.replace("-", "_").replace(" ", "_").replace(".", "_")
        # Remove double underscores
        while "__" in safe:
            safe = safe.replace("__", "_")
        # Remove leading/trailing underscores
        safe = safe.strip("_")
        # If starts with a digit, add a prefix
        if safe and safe[0].isdigit():
            safe = "n_" + safe
        return safe or "unknown"

    def _format_node_label(self, agent: Any, style: NodeStyle) -> str:
        """Format a node label."""
        name = agent.display_name or agent.agent_id

        # Trim long names
        if len(name) > self.style.max_label_length:
            name = name[: self.style.max_label_length - 3] + "..."

        # Add icon
        if style.icon:
            name = f"{style.icon} {name}"

        # Add tools
        max_tools_in_label = 2
        if self.style.show_tools and hasattr(agent, "tools") and agent.tools:
            tools = agent.tools[:max_tools_in_label]
            tools_str = ", ".join(tools)
            if len(agent.tools) > max_tools_in_label:
                tools_str += "..."
            name = f"{name}<br/>🔧 {tools_str}"

        return name


# ============================================================================
# Convenience functions
# ============================================================================


def to_mermaid(
    graph: "RoleGraph",
    direction: MermaidDirection = MermaidDirection.TOP_BOTTOM,
    title: str | None = None,
    style: VisualizationStyle | None = None,
) -> str:
    """
    Quick export of the graph to Mermaid.

    Args:
        graph: RoleGraph to visualise
        direction: Graph direction
        title: Diagram title
        style: Visualisation style

    Returns:
        Mermaid code

    Example:
        mermaid_code = to_mermaid(graph, direction=MermaidDirection.LR)
        print(mermaid_code)

    """
    viz = GraphVisualizer(graph, style)
    return viz.to_mermaid(direction=direction, title=title)


def to_ascii(
    graph: "RoleGraph",
    show_edges: bool = True,
    style: VisualizationStyle | None = None,
) -> str:
    """
    Quick export of the graph to ASCII.

    Args:
        graph: RoleGraph to visualise
        show_edges: Whether to show edges
        style: Visualisation style

    Returns:
        ASCII representation of the graph

    """
    viz = GraphVisualizer(graph, style)
    return viz.to_ascii(show_edges=show_edges)


def to_dot(
    graph: "RoleGraph",
    graph_name: str = "AgentGraph",
    style: VisualizationStyle | None = None,
) -> str:
    """
    Quick export of the graph to Graphviz DOT.

    Args:
        graph: RoleGraph to visualise
        graph_name: Graph name
        style: Visualisation style

    Returns:
        DOT code

    """
    viz = GraphVisualizer(graph, style)
    return viz.to_dot(graph_name=graph_name)


def print_graph(
    graph: "RoleGraph",
    output_format: str = "auto",
    style: VisualizationStyle | None = None,
) -> None:
    """
    Print the graph to the console.

    Args:
        graph: RoleGraph to visualise
        output_format: Output format ("auto", "colored", "ascii", "mermaid")
        style: Visualisation style

    """
    viz = GraphVisualizer(graph, style)

    if output_format == "auto":
        # Try rich, fall back to ASCII
        try:
            from rich.console import Console  # noqa: F401

            viz.print_colored()
        except ImportError:
            pass
    elif output_format == "colored":
        viz.print_colored()
    elif output_format in {"ascii", "mermaid"}:
        pass


def render_to_image(
    graph: "RoleGraph",
    filepath: "str | Path",
    image_format: ImageFormat | None = None,
    dpi: int | None = None,
    graph_name: str = "AgentGraph",
    style: VisualizationStyle | None = None,
) -> None:
    """
    Render the graph to an image.

    Args:
        graph: RoleGraph to visualise
        filepath: Path to the output file. Extension determines the format
                  if image_format is not explicitly specified.
        image_format: Image format. If None — inferred from the filepath extension.
        dpi: DPI for raster formats (png, jpg). None — Graphviz default.
        graph_name: Graph name
        style: Visualisation style

    Raises:
        ImportError: If graphviz is not installed

    Example:
        render_to_image(graph, "output.png")              # format from extension
        render_to_image(graph, "diagram", ImageFormat.SVG)
        render_to_image(graph, "report.png", dpi=300)

    """
    viz = GraphVisualizer(graph, style)
    viz.render_image(filepath, image_format=image_format, dpi=dpi, graph_name=graph_name)


def show_graph_interactive(
    graph: "RoleGraph",
    graph_name: str = "AgentGraph",
    style: VisualizationStyle | None = None,
) -> None:
    """
    Show the graph interactively.

    Args:
        graph: RoleGraph to visualise
        graph_name: Graph name
        style: Visualisation style

    Raises:
        ImportError: If graphviz is not installed

    """
    viz = GraphVisualizer(graph, style)
    viz.show_interactive(graph_name=graph_name)
