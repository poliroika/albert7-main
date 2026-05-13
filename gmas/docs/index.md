# gMAS Documentation

A modern framework for building multi-agent systems based on rustworkx.

## Overview

gMAS is a flexible, high-performance alternative to LangGraph with:

- **Dynamic topology** - Modify graph structure at runtime
- **Decentralized memory** - Each agent maintains its own state
- **Full graph access** - Complete control over adjacency matrices, edge attributes, and node data
- **Multi-model support** - Different LLMs per agent
- **Streaming API** - Real-time output during execution
- **PyTorch Geometric integration** - GNN routing support

## Installation

```bash
pip install frontier-ai-gmas
```

For development:

```bash
git clone https://github.com/frontier-ai-next/gmas.git
cd gmas
uv sync
```

## Quick Example

```python
from gmas.core import AgentProfile
from gmas.builder import build_property_graph
from gmas.execution import MACPRunner

# Create agents
agents = [
    AgentProfile(
        agent_id="researcher",
        display_name="Researcher",
        description="Gathers information on the topic",
    ),
    AgentProfile(
        agent_id="writer",
        display_name="Writer",
        description="Synthesizes research into a final answer",
    ),
]

# Build graph
graph = build_property_graph(
    agents,
    workflow_edges=[("researcher", "writer")],
    query="What are the latest advances in AI?",
)

# Execute
runner = MACPRunner(llm_caller=my_llm_function)
result = runner.run_round(graph)

print(result.final_answer)
```

## Documentation Sections

- [Getting Started](getting-started/installation.md)
- [User Guide](user-guide/key-concepts.md)
- [API Reference](api/core.md)
- [Examples](examples/basic-usage.md)
- [Contributing](contributing/index.md)
