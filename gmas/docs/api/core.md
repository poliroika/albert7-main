# Core API Reference

## Core Module

```python
from gmas.core import (
    # Graph
    RoleGraph,
    GraphIntegrityError,
    StateStorage,

    # Agents
    AgentProfile,
    AgentLLMConfig,
    TaskNode,

    # Builder
    build_property_graph,
    GraphBuilder,
    BuilderConfig,

    # Schema
    GraphSchema,
    AgentNodeSchema,
    TaskNodeSchema,
    EdgeType,
    NodeType,

    # Encoder
    NodeEncoder,

    # Algorithms
    GraphAlgorithms,
    CentralityType,

    # Metrics
    MetricsTracker,
    NodeMetrics,
    EdgeMetrics,

    # Events
    EventBus,
    EventHandler,
    EventType,

    # Visualization
    GraphVisualizer,
    to_mermaid,
    to_dot,
    to_ascii,
    print_graph,
)
```

## RoleGraph

Main graph class for multi-agent systems.

### Properties

- `num_nodes: int` - Number of nodes in graph
- `num_edges: int` - Number of edges in graph
- `agent_ids: list[str]` - List of agent IDs
- `A_com: torch.Tensor` - Adjacency matrix
- `edge_index: torch.Tensor` - PyG edge index

### Methods

```python
graph.add_node(agent, connections_to=None)
graph.add_edge(source, target, **kwargs)
graph.remove_node(agent_id)
graph.remove_edge(source, target)
graph.get_agent(agent_id) -> AgentProfile
graph.update_communication(A_com, **kwargs)
graph.to_pyg_data() -> Data
```

## AgentProfile

Represents an agent in the graph.

### Parameters

- `agent_id: str` - Unique identifier
- `display_name: str` - Human-readable name
- `description: str` - Functional description
- `persona: str | None` - Agent personality
- `tools: list[str]` - Available tools
- `state: dict` - Local state
- `llm_config: AgentLLMConfig | None` - LLM configuration

### Methods

```python
agent.with_state(state_dict) -> AgentProfile
agent.with_hidden_state(tensor) -> AgentProfile
```

## TaskNode

Represents the task/query for the system.

### Parameters

- `query: str` - The main query/task
- `context: str | None` - Additional context

## GraphBuilder

Builder for creating agent graphs.

### Methods

```python
builder.add_agent(agent)
builder.add_edge(source, target, **kwargs)
builder.add_sequence(agent_ids)
builder.build(query) -> RoleGraph
```

## NodeEncoder

Encodes agent descriptions into embeddings.

### Methods

```python
encoder.encode(agent) -> torch.Tensor
encoder.encode_batch(agents) -> torch.Tensor
```

## GraphAlgorithms

Graph analysis algorithms.

### Methods

```python
algo.compute_centrality(centrality_type) -> dict
algo.detect_communities() -> list
algo.find_shortest_path(source, target) -> list
algo.find_cycles() -> list
```

## MetricsTracker

Tracks execution metrics.

### Properties

- `total_tokens: int` - Total tokens used
- `total_time: float` - Total execution time
- `node_metrics: dict[str, NodeMetrics]` - Per-node metrics
- `edge_metrics: dict[tuple, EdgeMetrics]` - Per-edge metrics

## GraphVisualizer

Visualizes agent graphs.

### Methods

```python
visualizer.to_mermaid() -> str
visualizer.to_dot() -> str
visualizer.to_ascii() -> str
visualizer.render(filename)
```

## Utility Functions

```python
# Build graph from configuration
build_property_graph(agents, workflow_edges=None, query="") -> RoleGraph

# Visualization helpers
to_mermaid(graph) -> str
to_dot(graph) -> str
to_ascii(graph) -> str
print_graph(graph)

# Graph conversion
graph.to_pyg_data() -> Data  # PyTorch Geometric
```
