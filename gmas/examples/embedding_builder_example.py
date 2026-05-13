"""
Example: building a RoleGraph from agent embeddings.

Three strategies are demonstrated:
  1. k-nearest neighbours (knn)
  2. Cosine-similarity threshold
  3. Minimum spanning tree (mst)

No LLM API key needed -- embeddings are computed locally via the
hash-based encoder (swap for sentence-transformers for production).

Run:
    python -m examples.embedding_builder_example
"""

from gmas.builder.embedding_builder import (
    EmbeddingBuilderConfig,
    EmbeddingGraphBuilder,
    LinkStrategy,
)
from gmas.core.agent import AgentProfile
from gmas.core.encoder import NodeEncoder


def _header(title: str) -> None:
    line = "-" * 60
    print(f"\n{line}\n  {title}\n{line}")


def _print_graph(graph) -> None:
    print(f"  Nodes ({len(graph.node_ids)}): {sorted(graph.node_ids)}")
    edges = graph.edges
    print(f"  Edges ({len(edges)}):")
    for e in edges:
        src, tgt = e["source"], e["target"]
        weight = e.get("weight", "")
        w_str = f"  (w={weight:.3f})" if isinstance(weight, float) else ""
        print(f"    {src} -> {tgt}{w_str}")

    errors = graph.verify_integrity(raise_on_error=False)
    if errors:
        print(f"  [!] Integrity issues: {errors}")
    else:
        print("  [ok] Graph integrity verified")


AGENTS = [
    AgentProfile(
        agent_id="researcher",
        display_name="Researcher",
        persona="a web researcher",
        description="Searches the internet for relevant information, news, and data",
        tools=["web_search"],
    ),
    AgentProfile(
        agent_id="analyst",
        display_name="Data Analyst",
        persona="a data analyst",
        description="Processes and analyzes data, computes statistics, creates charts",
        tools=["code_interpreter"],
    ),
    AgentProfile(
        agent_id="writer",
        display_name="Writer",
        persona="a content writer",
        description="Writes well-structured articles, reports, and summaries",
    ),
    AgentProfile(
        agent_id="reviewer",
        display_name="Reviewer",
        persona="a quality assurance reviewer",
        description="Reviews outputs for accuracy, consistency, and completeness",
    ),
    AgentProfile(
        agent_id="planner",
        display_name="Planner",
        persona="a project planner",
        description="Breaks tasks into subtasks, assigns priorities, coordinates agents",
    ),
]

QUERY = "Research AI market trends, analyze the data, and produce a report"

ENCODER = NodeEncoder(model_name="hash:256")


def example_knn() -> None:
    _header("Strategy: k-nearest neighbours (k=2)")

    builder = EmbeddingGraphBuilder(
        config=EmbeddingBuilderConfig(
            strategy=LinkStrategy.KNN,
            k=2,
            encoder=ENCODER,
        ),
    )

    sim, ids = builder.compute_similarity_matrix(AGENTS)
    print("  Similarity matrix (top-right):")
    for i, aid in enumerate(ids):
        row = "    " + aid.ljust(12)
        for j in range(len(ids)):
            if j >= i:
                row += f" {sim[i, j].item():5.2f}"
            else:
                row += "      "
        print(row)

    graph = builder.build(AGENTS, query=QUERY)
    _print_graph(graph)


def example_threshold() -> None:
    _header("Strategy: threshold (>= 0.3)")

    builder = EmbeddingGraphBuilder(
        config=EmbeddingBuilderConfig(
            strategy=LinkStrategy.THRESHOLD,
            threshold=0.3,
            encoder=ENCODER,
        ),
    )
    graph = builder.build(AGENTS, query=QUERY)
    _print_graph(graph)


def example_mst() -> None:
    _header("Strategy: MST + shortcut threshold 0.4")

    builder = EmbeddingGraphBuilder(
        config=EmbeddingBuilderConfig(
            strategy=LinkStrategy.MST,
            mst_shortcut_threshold=0.4,
            encoder=ENCODER,
        ),
    )
    graph = builder.build(AGENTS, query=QUERY)
    _print_graph(graph)


def example_symmetric() -> None:
    _header("Symmetric (bidirectional) edges with knn")

    builder = EmbeddingGraphBuilder(
        config=EmbeddingBuilderConfig(
            strategy=LinkStrategy.KNN,
            k=1,
            symmetric=True,
            include_task_node=False,
            encoder=ENCODER,
        ),
    )
    graph = builder.build(AGENTS, query=QUERY)
    _print_graph(graph)


if __name__ == "__main__":
    example_knn()
    example_threshold()
    example_mst()
    example_symmetric()
    print("\nAll examples completed successfully.")
