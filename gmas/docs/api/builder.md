# Builder API

## Builder Module

```python
from gmas.builder import (
    # Main builder
    GraphBuilder,
    build_property_graph,
    build_from_schema,
    build_from_adjacency,

    # Configuration
    BuilderConfig,
    AutoBuilderConfig,
    EmbeddingBuilderConfig,

    # Auto builder
    AutoGraphBuilder,
    EmbeddingGraphBuilder,

    # Utilities
    default_edges,
    default_sequence,
)
```

## GraphBuilder

Builder for creating agent graphs.

### Methods

```python
builder = GraphBuilder()
builder.add_agent(agent)
builder.add_edge(source, target, **kwargs)
builder.add_sequence(agent_ids)
builder.build(query) -> RoleGraph
```

## build_property_graph

Quick graph building function.

```python
graph = build_property_graph(
    agents=[...],
    workflow_edges=[("a", "b")],
    query="Task description",
    include_task_node=True,
)
```

## AutoGraphBuilder

Automatically build graph from task.

```python
from gmas.builder import AutoGraphBuilder, AutoBuilderConfig

config = AutoBuilderConfig(
    max_agents=5,
    link_threshold=0.7,
)

auto_builder = AutoGraphBuilder(config)
graph = auto_builder.build(query="Your task here")
```

## EmbeddingGraphBuilder

Build graph using embeddings.

```python
from gmas.builder import EmbeddingGraphBuilder, EmbeddingBuilderConfig

config = EmbeddingBuilderConfig(
    link_strategy=LinkStrategy.THRESHOLD,
    threshold=0.8,
)

builder = EmbeddingGraphBuilder(config)
graph = builder.build(agents, query)
```
