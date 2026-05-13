"""
GNN-based routing pipeline with REAL LLM execution.

Demonstrates:
  1. Building an 8-agent directed graph via GraphBuilder
  2. Running agents through MACPRunner with a real LLM
  3. Collecting execution metrics from real runs
  4. Graph algorithms (k-shortest paths, PageRank, betweenness, communities)
  5. Preparing training data from real metrics
  6. Training a GCN routing model
  7. Online inference (ARGMAX / TOP_K / THRESHOLD)
  8. Adaptive routing suggestions based on observed metrics

Requirements:
    pip install torch torch_geometric rustworkx openai

Environment variables (REQUIRED):
    LLM_API_KEY   - API key for the LLM provider
    LLM_BASE_URL  - Base URL (e.g. http://localhost:8000/v1)
    LLM_MODEL     - Model name (e.g. gpt-4o-mini)

Run:
    python -m examples.gnn_routing
"""

import os
import random
import sys
import time
from pathlib import Path
from typing import Any
from unicodedata import normalize

import torch
from torch_geometric.data import Data

from gmas.builder import BuilderConfig, GraphBuilder
from gmas.core.algorithms import CentralityType, GraphAlgorithms
from gmas.core.gnn import (
    DefaultFeatureGenerator,
    GNNModelType,
    GNNRouterInference,
    GNNTrainer,
    RoutingStrategy,
    TrainingConfig,
    create_gnn_router,
)
from gmas.core.graph import RoleGraph
from gmas.core.metrics import MetricsTracker
from gmas.execution import MACPRunner, RunnerConfig, StreamEventType
from gmas.tools import create_openai_caller
from gmas.utils import configure_console, load_dotenv_file

load_dotenv_file(Path(__file__).resolve().parents[1] / ".env")

# -- Constants ----------------------------------------------------------------

MODEL_PATH = Path(__file__).parent / "gnn_router_model.pt"

# Number of LLM execution rounds for collecting training data
NUM_EXECUTION_ROUNDS = 5

# Task queries for the agents to solve (rotated across rounds)
TASK_QUERIES = [
    "Analyze the impact of artificial intelligence on modern healthcare systems.",
    "Explain the key differences between supervised and unsupervised machine learning.",
    "Describe how neural networks learn to recognize patterns in data.",
    "What are the main challenges in deploying AI systems in production?",
    "Compare transformer architectures with traditional recurrent neural networks.",
]


# -- Helpers ------------------------------------------------------------------


def _safe(text: str) -> str:
    """Sanitize text for Windows cp1252 console — replace non-ASCII chars."""
    return normalize("NFKC", text).encode("ascii", errors="replace").decode("ascii")


def _header(step: int, total: int, title: str) -> None:
    print(f"\n{'-' * 60}")
    print(f"  Step {step}/{total} -- {title}")
    print("-" * 60)


def _validate_llm_config() -> tuple[str, str, str]:
    """Validate that LLM configuration is available. Raise error if not."""
    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL")

    missing = []
    if not api_key:
        missing.append("LLM_API_KEY")
    if not base_url:
        missing.append("LLM_BASE_URL")
    if not model:
        missing.append("LLM_MODEL")

    if missing:
        print("ERROR: Real LLM configuration is required.", file=sys.stderr)
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        print(file=sys.stderr)
        print("Set them before running:", file=sys.stderr)
        print("  export LLM_API_KEY=your-api-key", file=sys.stderr)
        print("  export LLM_BASE_URL=http://localhost:8000/v1", file=sys.stderr)
        print("  export LLM_MODEL=gpt-4o-mini", file=sys.stderr)
        print(file=sys.stderr)
        print("This example does NOT support mock/fake LLMs.", file=sys.stderr)
        sys.exit(1)

    # All three are guaranteed non-None — sys.exit(1) fires above if any is missing.
    return api_key, base_url, model


# -- 1. Graph construction ---------------------------------------------------


def create_demo_graph(query: str) -> RoleGraph:
    """Build an 8-agent directed graph using GraphBuilder with a real task."""
    builder = GraphBuilder(BuilderConfig(include_task_node=True, validate=True))

    builder.add_task(query=query, description="Multi-agent analysis task")

    builder.add_agent(
        agent_id="coordinator",
        display_name="Coordinator",
        persona="You are a project coordinator who plans and delegates tasks.",
        description="Break down the task into subtasks and coordinate the team.",
    )
    builder.add_agent(
        agent_id="researcher",
        display_name="Researcher",
        persona="You are a thorough researcher who gathers information.",
        description="Research the topic and provide factual information.",
    )
    builder.add_agent(
        agent_id="analyst",
        display_name="Analyst",
        persona="You are a data analyst who finds patterns and insights.",
        description="Analyze the information and identify key patterns.",
    )
    builder.add_agent(
        agent_id="writer",
        display_name="Writer",
        persona="You are a skilled writer who creates clear content.",
        description="Write clear and engaging content based on the analysis.",
    )
    builder.add_agent(
        agent_id="reviewer",
        display_name="Reviewer",
        persona="You are a quality reviewer who ensures accuracy.",
        description="Review the content for accuracy and completeness.",
    )
    builder.add_agent(
        agent_id="expert_1",
        display_name="Domain Expert A",
        persona="You are a domain expert specializing in technical details.",
        description="Provide expert-level technical insights.",
    )
    builder.add_agent(
        agent_id="expert_2",
        display_name="Domain Expert B",
        persona="You are a domain expert specializing in practical applications.",
        description="Provide practical application insights.",
    )
    builder.add_agent(
        agent_id="aggregator",
        display_name="Aggregator",
        persona="You are a synthesis specialist who combines multiple viewpoints.",
        description="Aggregate and synthesize all expert inputs into a final answer.",
    )

    # Connect task node to coordinator
    builder.connect_task_to_agents(agent_ids=["coordinator"], bidirectional=False)

    # Workflow edges: coordinator fans out, experts feed aggregator, writer -> reviewer
    builder.add_workflow_edge("coordinator", "researcher", weight=0.9)
    builder.add_workflow_edge("coordinator", "analyst", weight=0.8)
    builder.add_workflow_edge("researcher", "expert_1", weight=0.7)
    builder.add_workflow_edge("researcher", "expert_2", weight=0.6)
    builder.add_workflow_edge("analyst", "expert_1", weight=0.75)
    builder.add_workflow_edge("analyst", "writer", weight=0.85)
    builder.add_workflow_edge("expert_1", "aggregator", weight=0.8)
    builder.add_workflow_edge("expert_2", "aggregator", weight=0.7)
    builder.add_workflow_edge("writer", "reviewer", weight=0.95)
    builder.add_workflow_edge("aggregator", "reviewer", weight=0.9)

    return builder.build()


# -- 2. LLM Execution --------------------------------------------------------


def run_llm_round(
    runner: MACPRunner,
    query: str,
    tracker: MetricsTracker,
    round_num: int,
) -> dict[str, str]:
    """Execute one round of agents via real LLM and collect metrics."""
    graph = create_demo_graph(query)

    print(f"\n  Round {round_num}: '{query[:60]}...'")

    agent_messages: dict[str, str] = {}
    execution_order: list[str] = []

    start = time.time()

    for event in runner.stream(graph, final_agent_id="reviewer"):
        etype = event.event_type

        if etype == StreamEventType.AGENT_START:
            aid = getattr(event, "agent_id", "")
            name = getattr(event, "agent_name", aid)
            print(f"    -> {name} starting...", flush=True)

        elif etype == StreamEventType.AGENT_OUTPUT:
            aid = getattr(event, "agent_id", "")
            content = getattr(event, "content", "")
            agent_messages[aid] = content
            execution_order.append(aid)
            print(f"    <- {aid}: {_safe(content[:80])}...")

        elif etype == StreamEventType.AGENT_ERROR:
            aid = getattr(event, "agent_id", "")
            err = getattr(event, "error_message", "unknown")
            execution_order.append(aid)
            print(f"    !! {aid}: ERROR - {err}")

        elif etype == StreamEventType.RUN_END:
            total_tokens = getattr(event, "total_tokens", 0)
            total_time = getattr(event, "total_time", 0.0)
            print(f"    == Round done: {total_tokens} tokens, {total_time:.2f}s")

    elapsed = time.time() - start

    # Record metrics from this execution round
    for i, aid in enumerate(execution_order):
        content = agent_messages.get(aid, "")
        success = len(content) > 0
        latency = (elapsed / max(len(execution_order), 1)) * 1000  # approx per-agent ms
        tokens = len(content.split()) * 2  # rough token estimate

        tracker.record_node_execution(
            node_id=aid,
            success=success,
            latency_ms=latency + random.gauss(0, latency * 0.1),
            cost_tokens=tokens,
            quality=min(1.0, len(content) / 500) if success else 0.0,
        )

        # Record edge transitions
        if i > 0:
            prev = execution_order[i - 1]
            tracker.record_edge_transition(
                source_id=prev,
                target_id=aid,
                success=success,
                latency_ms=random.gauss(50, 10),
            )

    return agent_messages


# -- 3. Graph algorithms ------------------------------------------------------


def demo_algorithms(graph: RoleGraph) -> None:
    alg = GraphAlgorithms(graph)

    print("  k-Shortest paths (coordinator -> reviewer, k=3):")
    for i, path in enumerate(alg.k_shortest_paths("coordinator", "reviewer", k=3), 1):
        print(f"    {i}. {' -> '.join(path.nodes)}")

    print("\n  PageRank -- top 3:")
    for node, score in alg.compute_centrality(CentralityType.PAGERANK).top_k(3):
        print(f"    {node:<15} {score:.4f}")

    print("\n  Betweenness -- top 3:")
    for node, score in alg.compute_centrality(CentralityType.BETWEENNESS).top_k(3):
        print(f"    {node:<15} {score:.4f}")

    communities = alg.detect_communities()
    print(f"\n  Communities: {len(communities.communities)}")
    for i, c in enumerate(communities.communities):
        print(f"    {i + 1}: {c}")

    cycles = alg.detect_cycles(max_length=5)
    if cycles:
        print("\n  Cycles (up to 3):")
        for cyc in cycles[:3]:
            print(f"    {cyc}")
    else:
        print("\n  No cycles detected.")


# -- 4. Training data ---------------------------------------------------------


def prepare_data(
    graph: RoleGraph,
    tracker: MetricsTracker,
    n: int = 200,
) -> tuple[list[Any], list[Any]]:
    """Generate augmented training samples with Gaussian noise."""
    feat_gen = DefaultFeatureGenerator()
    node_ids = graph.node_ids
    features = feat_gen.generate_node_features(graph, node_ids, tracker)
    edge_index = graph.edge_index
    scores = tracker.get_node_scores()

    median = torch.median(torch.tensor(list(scores.values()))).item() if scores else 0.5
    labels = [1 if scores.get(nid, 0.5) >= median else 0 for nid in node_ids]

    split = int(n * 0.8)
    train, val = [], []
    for i in range(n):
        d = Data(
            x=(features + torch.randn_like(features) * 0.1).to(torch.float32),
            edge_index=edge_index.clone().to(torch.long),
            y=torch.tensor(labels, dtype=torch.long),
        )
        (train if i < split else val).append(d)

    print(f"  Train: {len(train)}  Val: {len(val)}  Features: {features.shape[1]}")
    return train, val


# -- 5. Training --------------------------------------------------------------


def train_model(train_data: list[Any], val_data: list[Any], in_ch: int) -> Any:
    cfg = TrainingConfig(
        learning_rate=1e-3,
        hidden_dim=64,
        num_layers=2,
        dropout=0.2,
        epochs=50,
        batch_size=16,
        patience=10,
        task="node_classification",
        num_classes=2,
    )
    model = create_gnn_router(GNNModelType.GCN, in_ch, cfg.num_classes, cfg)
    trainer = GNNTrainer(model, cfg)
    trainer.train(train_data, val_data, verbose=True)
    trainer.save(MODEL_PATH)
    print(f"  Model saved -> {MODEL_PATH}")
    return model


# -- 6. Inference -------------------------------------------------------------


def demo_inference(graph: RoleGraph, model: Any, tracker: MetricsTracker) -> None:
    router = GNNRouterInference(model, DefaultFeatureGenerator())

    for strategy, label, kw in [
        (RoutingStrategy.ARGMAX, "ARGMAX", {"candidates": ["researcher", "analyst"]}),
        (RoutingStrategy.TOP_K, "TOP_K (3)", {"top_k": 3}),
        (RoutingStrategy.THRESHOLD, "THRESHOLD (>=0.1)", {"threshold": 0.1}),
    ]:
        result = router.predict(
            graph,
            source="coordinator",
            metrics_tracker=tracker,
            strategy=strategy,
            **kw,
        )
        print(f"  {label:<20} -> {result}")


# -- 7. Adaptive suggestions --------------------------------------------------


def demo_adaptive(graph: RoleGraph, tracker: MetricsTracker) -> None:
    weights = tracker.get_routing_weights()
    print("  Bottom-5 edge weights:")
    for (s, t), w in sorted(weights.items(), key=lambda x: x[1])[:5]:
        tag = "(in graph)" if s in graph.node_ids and t in graph.node_ids else "(stale)"
        print(f"    {s} -> {t}  w={w:.4f}  {tag}")

    suggestions = tracker.suggest_pruning(node_reliability_threshold=0.5, max_latency_ms=200)
    if suggestions["prune_nodes"]:
        print(f"\n  Prune nodes: {suggestions['prune_nodes']}")
    if suggestions["slow_nodes"]:
        print(f"  Slow nodes : {suggestions['slow_nodes']}")
    if suggestions["prune_edges"]:
        print(f"  Prune edges: {suggestions['prune_edges']}")
    if not any(suggestions.values()):
        print("\n  Graph is healthy -- no pruning suggested.")


# -- Entry point --------------------------------------------------------------


def main():
    configure_console()

    total_steps = 8

    # Step 0: Validate LLM configuration
    _header(1, total_steps, "Validate LLM configuration")
    api_key, base_url, model = _validate_llm_config()
    print(f"  LLM Provider : {base_url}")
    print(f"  Model        : {model}")
    print(f"  API Key      : {api_key[:8]}...{api_key[-4:]}")

    # Create the LLM caller
    llm_caller = create_openai_caller(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=0.7,
    )

    # Verify LLM is actually reachable
    print("\n  Testing LLM connection...")
    try:
        test_response = str(llm_caller("Say 'OK' if you can hear me. Reply with just 'OK'."))
        if not test_response.strip():
            print("ERROR: LLM returned empty response. Check your configuration.", file=sys.stderr)
            sys.exit(1)
        print(f"  LLM responded: {_safe(test_response.strip()[:50])}")
        print("  Connection OK!")
    except Exception as e:
        print(f"ERROR: Cannot connect to LLM: {e}", file=sys.stderr)
        print("Check that your LLM server is running and credentials are correct.", file=sys.stderr)
        sys.exit(1)

    # Create runner
    runner = MACPRunner(
        llm_caller=llm_caller,
        config=RunnerConfig(
            timeout=120.0,
            adaptive=True,
            update_states=True,
            broadcast_task_to_all=False,
        ),
    )

    # Step 1: Build graph
    _header(2, total_steps, "Build agent graph")
    graph = create_demo_graph(TASK_QUERIES[0])
    print(f"  Agents: {[a.agent_id for a in graph.agents]}")

    # Step 2: Execute agents with real LLM
    _header(3, total_steps, f"Execute agents with real LLM ({NUM_EXECUTION_ROUNDS} rounds)")
    tracker = MetricsTracker()
    all_messages: list[dict[str, str]] = []

    for i in range(NUM_EXECUTION_ROUNDS):
        query = TASK_QUERIES[i % len(TASK_QUERIES)]
        messages = run_llm_round(runner, query, tracker, i + 1)
        all_messages.append(messages)

    # Show collected metrics
    print("\n  Collected metrics:")
    for node, score in tracker.get_node_scores().items():
        print(f"    {node:<15} score={score:.4f}")

    # Step 3: Graph algorithms
    _header(4, total_steps, "Graph algorithms")
    demo_algorithms(graph)

    # Step 4: Prepare training data from real metrics
    _header(5, total_steps, "Prepare training data (from real LLM metrics)")
    train_data, val_data = prepare_data(graph, tracker)

    # Step 5: Train GNN routing model
    _header(6, total_steps, "Train GNN routing model")
    in_ch = train_data[0].x.shape[1] if train_data else 4
    model = train_model(train_data, val_data, in_ch)

    # Step 6: Online inference
    _header(7, total_steps, "Online inference & adaptive routing")
    demo_inference(graph, model, tracker)
    demo_adaptive(graph, tracker)

    # Step 7: Summary
    _header(8, total_steps, "Summary")
    print(f"  LLM rounds executed  : {NUM_EXECUTION_ROUNDS}")
    print(f"  Total agents per run : {len(graph.agents)}")
    print(f"  Unique agent outputs : {sum(len(m) for m in all_messages)}")
    if MODEL_PATH.exists():
        print(f"  Model size           : {MODEL_PATH.stat().st_size / 1024:.1f} KB")
    print(f"  Model saved to       : {MODEL_PATH}")

    print("\nDone!")


if __name__ == "__main__":
    main()
