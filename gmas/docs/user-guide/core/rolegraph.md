# RoleGraph

The main graph structure for multi-agent systems.

## Creating a Graph

```python
from gmas.core import AgentProfile
from gmas.builder import build_property_graph

agents = [
    AgentProfile(agent_id="a1", display_name="Agent 1"),
    AgentProfile(agent_id="a2", display_name="Agent 2"),
]

graph = build_property_graph(
    agents,
    workflow_edges=[("a1", "a2")],
    query="What is the question?",
)
```

## Graph Properties

```python
# Basic properties
graph.num_nodes      # Total nodes
graph.num_edges      # Total edges
graph.agent_ids      # List of agent IDs

# Adjacency matrix
graph.A_com          # torch.Tensor adjacency matrix
graph.edge_index     # PyTorch Geometric format

# Node access
agent = graph.get_agent("agent_id")
```

## Dynamic Modifications

### Add Node

```python
new_agent = AgentProfile(
    agent_id="new_agent",
    display_name="New Agent",
    description="A new agent",
)

# Add with connections
graph.add_node(new_agent, connections_to=["existing_agent"])
```

### Add Edge

```python
# Simple edge
graph.add_edge("agent_a", "agent_b")

# Edge with attributes
graph.add_edge("agent_a", "agent_b", weight=0.8, edge_type="workflow")
```

### Remove Elements

```python
graph.remove_edge("agent_a", "agent_b")
graph.remove_node("agent_id")
```

## Graph Analysis

```python
from gmas.core import GraphAlgorithms

algo = GraphAlgorithms(graph)

# Centrality measures
centrality = algo.compute_centrality(CentralityType.PAGERANK)

# Community detection
communities = algo.detect_communities()

# Path finding
path = algo.find_shortest_path("agent_a", "agent_b")
```

## Visualization

```python
from gmas.core import to_mermaid, print_graph

# Print ASCII
print_graph(graph)

# Get Mermaid diagram
mermaid_diagram = to_mermaid(graph)
print(mermaid_diagram)
```

## PyTorch Geometric Integration

```python
# Convert to PyG Data object
pyg_data = graph.to_pyg_data()

# Now use with GNN models
import torch_geometric
# ... use pyg_data with your GNN
```
