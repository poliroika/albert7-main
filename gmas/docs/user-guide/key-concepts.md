# Key Concepts

## RoleGraph

The core data structure - a directed graph based on rustworkx.

```python
from gmas.core import RoleGraph, AgentProfile
from gmas.builder import build_property_graph

agents = [AgentProfile(agent_id="agent1", display_name="Agent 1")]
graph = build_property_graph(agents, query="Task")

# Access graph properties
print(graph.num_nodes)  # Number of nodes
print(graph.num_edges)  # Number of edges
print(graph.A_com)      # Adjacency matrix as torch.Tensor
```

## AgentProfile

Represents an agent in the graph.

```python
from gmas.core import AgentProfile

agent = AgentProfile(
    agent_id="unique_id",
    display_name="Human Readable Name",
    description="What this agent does",
    persona="You are a helpful assistant",
    tools=["tool1", "tool2"],
)
```

Key attributes:
- `agent_id` - Unique identifier
- `display_name` - Human-readable name
- `description` - Functional description
- `state` - Local agent state (decentralized memory)
- `embedding` - Encoded representation

## TaskNode

Represents the task/query for the agent system.

```python
from gmas.core import TaskNode

task = TaskNode(
    query="What is the capital of France?",
    context="Additional context if needed",
)
```

## Execution Flow

1. **Build Graph** - Create agents and define connections
2. **Schedule** - Determine execution order (topological sort)
3. **Execute** - Run agents in order, passing messages
4. **Result** - Get final answer and metrics

## Graph Topology

Edges define information flow between agents:

```python
from gmas.builder import build_property_graph

# Linear chain: A -> B -> C
edges = [("agent_a", "agent_b"), ("agent_b", "agent_c")]

# Parallel: A -> B, A -> C
edges = [("agent_a", "agent_b"), ("agent_a", "agent_c")]

# Star: All connect to center
edges = [("agent_1", "center"), ("agent_2", "center")]
```

## Dynamic Topology

Modify graph structure at runtime:

```python
# Add new agent
new_agent = AgentProfile(agent_id="new", display_name="New Agent")
graph.add_node(new_agent, connections_to=["existing_agent"])

# Add edge
graph.add_edge("agent_a", "agent_b", weight=0.8)

# Remove edge
graph.remove_edge("agent_a", "agent_b")
```

## Memory

Each agent has its own state:

```python
# Access agent state
agent = graph.get_agent("agent_id")
current_state = agent.state

# Update state
agent.state["key"] = "value"
```

## Next Steps

- [RoleGraph](core/rolegraph.md) - Deep dive into graph operations
- [AgentProfile](core/agent-profile.md) - Agent configuration
- [MACPRunner](core/macp-runner.md) - Execution engine
