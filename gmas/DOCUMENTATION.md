#  gMAS — Full Documentation

<p align="center">
  <strong>A modern graph-based framework for multi-agent systems</strong>
</p>

<p align="center">
  <em>A flexible, high-performance alternative to LangGraph with dynamic topology, decentralized memory, and full access to graph structures</em>
</p>

---

## 📋 Table of Contents

- [Introduction](#introduction)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Key Concepts](#key-concepts)
- [Core Components](#core-components)
  - [RoleGraph](#rolegraph)
  - [AgentProfile](#agentprofile)
  - [TaskNode](#tasknode)
  - [NodeEncoder](#nodeencoder)
  - [MACPRunner](#macprunner)
  - [Scheduler](#scheduler)
  - [Memory System](#memory-system)
  - [Streaming API](#streaming-api)
  - [Token Budget](#token-budget-budget-system)
  - [Error Handling](#error-handling-error-handling)
  - [Graph Algorithms](#graph-algorithms-graph-algorithms)
  - [Metrics Tracking](#metrics-tracking-metrics-tracker)
  - [Visualization](#visualization-visualization)
  - [Graph Schemas](#graph-schemas-schema-system)
  - [Builder API](#builder-api-detailed)
  - [Event System](#event-system-event-system)
  - [Callback System (LangChain-like)](#callback-system)
  - [State Storage](#state-storage-state-storage)
  - [Async Utilities](#async-utilities-async-utils)
  - [Conditional Routing](#conditional-routing-conditional-routing)
  - [Agent Tools (Tools)](#agent-tools-tools)
    - [Remote MCP Servers](#remote-mcp-servers)
- [Advanced Features](#advanced-features)
  - [Execution Optimization and Token Savings](#execution-optimization-and-token-savings)
  - [Multi-Model Support](#multi-model-support-multi-model-support)
  - [Structured Prompt — modern chat LLMs (recommended)](#structured-prompt--modern-chat-llms-recommended)
    - [Built-in factory helpers](#built-in-factory-helpers-recommended-zero-boilerplate)
  - [Dynamic Topology](#dynamic-topology)
  - [GNN Routing](#gnn-routing)
  - [Hidden Channels](#hidden-channels)
  - [Adaptive Execution](#adaptive-execution)
- [Configuration](#configuration)
- [Usage Examples](#usage-examples)
- [API Reference](#api-reference)
- [FAQ](#faq)

---

## Introduction

**gMAS** is a framework for building multi-agent systems that uses the `rustworkx` library for high-performance graph operations. It addresses key limitations of existing solutions such as LangGraph:

### Why is gMAS better than LangGraph?

| Feature | LangGraph | gMAS Framework |
|-------------|-----------|----------------|
| **Topology** | Fixed | **Dynamic** (runtime changes via hooks) |
| **Token optimization** | Minimal | **Automatic** (filtering isolated nodes, disabled nodes, early stopping) |
| **Memory** | Centralized | Decentralized (agents’ local state) |
| **Graph** | Hidden from the developer | First-class citizen (full access) |
| **Representations** | Text only | Text + embeddings + hidden states |
| **Typing and validation** | Minimal | **Full Pydantic validation** (type safety) |
| **Data schemas** | Informal | **Pydantic BaseModel** (auto-validation, serialization) |
| **Multi-model** | Limited | Full support for different LLMs per agent |
| **Parallelism** | Limited | Full async/parallel support |
| **ML integration** | None | PyTorch Geometric, GNN routing, RL hooks |
| **Serialization** | Manual | **Automatic** (Pydantic `.model_dump()`) |
| **Runtime adaptation** | None | **Topology hooks, early stopping, disabled nodes** |
| **Callbacks** | BaseCallbackHandler | **Full compatibility** (same methods: on_run_start, on_agent_end, on_tool_start/end/error, etc.) |

---

## Installation

### Requirements
- Python 3.13+
- PyTorch 2.0+
- **rustworkx 0.13**
- **Pydantic 2.0+** (required — the framework is fully built on Pydantic)

---

## Quick Start

### Minimal example

```python
from core import AgentProfile, RoleGraph
from execution import MACPRunner
from builder import build_property_graph

# 1. Define agents
agents = [
    AgentProfile(
        agent_id="solver",
        display_name="Math Solver",
        description="Solves math problems step by step",
        tools=["calculator"],
    ),
    AgentProfile(
        agent_id="checker",
        display_name="Answer Checker",
        description="Checks solutions for correctness",
    ),
]

# 2. Define connections between agents
workflow_edges = [("solver", "checker")]

# 3. Build the graph
graph = build_property_graph(
    agents,
    workflow_edges=workflow_edges,
    query="What is 25 × 17?",
)

# 4. Define an LLM call function
def my_llm_caller(prompt: str) -> str:
    # Integrate your LLM here (OpenAI, Anthropic, local, etc.)
    return call_your_llm(prompt)

# 5. Run execution
runner = MACPRunner(llm_caller=my_llm_caller)
result = runner.run_round(graph)

# 6. Get results
print(f"Answer: {result.final_answer}")
print(f"Execution order: {result.execution_order}")
print(f"Tokens used: {result.total_tokens}")
```

### Quick Start: with monitoring (Callbacks)

```python
from execution import MACPRunner, RunnerConfig
from callbacks import (
    StdoutCallbackHandler,
    MetricsCallbackHandler,
    collect_metrics,
)

# 1. Add callback handlers
config = RunnerConfig(
    callbacks=[
        StdoutCallbackHandler(show_outputs=True),  # Console output
        MetricsCallbackHandler(),                  # Metrics collection
    ]
)

runner = MACPRunner(llm_caller=my_llm_caller, config=config)
result = runner.run_round(graph)

# 2. Or use a context manager
with collect_metrics() as metrics:
    result = runner.run_round(graph)

    print(f"Total tokens: {metrics.total_tokens}")
    print(f"Execution time: {metrics.total_duration_ms}ms")
    print(f"Agent calls: {metrics.get_metrics()['agent_calls']}")
```

### Quick Start: multi-model (different LLM for each agent)

```python
from builder import GraphBuilder
from execution import MACPRunner, LLMCallerFactory

# 1. Create a builder and add agents with different models
builder = GraphBuilder()

# Agent 1: strong model for complex analysis
builder.add_agent(
    agent_id="analyst",
    display_name="Senior Analyst",
    llm_backbone="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.0,
    max_tokens=2000,
)

# Agent 2: smaller model for formatting
builder.add_agent(
    agent_id="formatter",
    display_name="Report Formatter",
    llm_backbone="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.3,
    max_tokens=500,
)

# 2. Define edges
builder.add_workflow_edge("analyst", "formatter")

# 3. Set the query and build the graph
builder.add_task(query="Analyze Q4 sales")
graph = builder.build()

# 4. Create an LLM factory (automatically creates callers for each agent)
factory = LLMCallerFactory.create_openai_factory()

# 5. Run execution
runner = MACPRunner(llm_factory=factory)
result = runner.run_round(graph)

# 6. Get the result
print(f"Final answer: {result.final_answer}")
print("Savings: use gpt-4 only for analysis, gpt-4o-mini for formatting")
```

### Quick Start: token optimization and dynamic topology

```python
from builder import GraphBuilder
from execution import (
    MACPRunner, RunnerConfig, EarlyStopCondition, TopologyAction
)

# 1. Create a graph with explicit boundaries
builder = GraphBuilder()
builder.add_agent("input", persona="Input processor")
builder.add_agent("solver", persona="Problem solver")
builder.add_agent("checker", persona="Solution checker")
builder.add_agent("expert", persona="Expert reviewer (expensive)")
builder.add_agent("output", persona="Output formatter")
builder.add_agent("optional", persona="Optional analyzer")

builder.add_workflow_edge("input", "solver")
builder.add_workflow_edge("solver", "checker")
builder.add_workflow_edge("checker", "output")
# expert is connected dynamically when needed

# Set boundaries (for filtering unreachable nodes)
builder.set_start_node("input")
builder.set_end_node("output")

builder.add_task(query="Solve the problem")
builder.connect_task_to_agents()

graph = builder.build()

# 2. Disable optional nodes
graph.disable("optional")  # Will not run, token savings

# 3. Hook for topology adaptation
def adaptive_hook(ctx, graph):
    # If checker found an error — add expert
    if ctx.agent_id == "checker" and "ERROR" in (ctx.response or ""):
        return TopologyAction(
            add_edges=[("checker", "expert", 1.0), ("expert", "output", 1.0)],
            trigger_rebuild=True
        )

    # If solver is confident — skip checker
    if ctx.agent_id == "solver" and "CONFIDENT" in (ctx.response or ""):
        return TopologyAction(skip_agents=["checker"])

    return None

# 4. Configure runner with optimization
config = RunnerConfig(
    adaptive=True,
    enable_dynamic_topology=True,
    topology_hooks=[adaptive_hook],
    early_stop_conditions=[
        EarlyStopCondition.on_keyword("FINAL_ANSWER"),
        EarlyStopCondition.on_token_limit(5000),
    ],
)

runner = MACPRunner(llm_caller=my_llm, config=config)

# 5. Execute with filtering unreachable nodes
result = runner.run_round(
    graph,
    filter_unreachable=True  # Exclude nodes not on the input->output path
)

# 6. Result
print(f"Executed: {result.execution_order}")
print(f"Pruned: {result.pruned_agents}")          # optional + unreachable
print(f"Early stopped: {result.early_stopped}")
print(f"Topology mods: {result.topology_modifications}")  # was expert added?
print(f"Tokens: {result.total_tokens}")
```

---

## Key Concepts

### Pydantic-oriented architecture

gMAS Framework is **fully built on Pydantic** for type safety, validation, and data serialization. All key models inherit from `pydantic.BaseModel`:

#### Core Pydantic models in the framework

| Model | Purpose | Notes |
|--------|-----------|-------------|
| `AgentProfile` | Agent profile | `frozen=True` (immutable), `arbitrary_types_allowed` for torch.Tensor |
| `AgentLLMConfig` | Agent LLM configuration | Validates model parameters, supports env vars |
| `TaskNode` | Task node | Stores the query and task context |
| `GraphSchema` | Schema of the whole graph | Nodes (dict), edges (list), metadata |
| `AgentNodeSchema` | Agent-node schema | LLM config, tools, metrics, embeddings |
| `TaskNodeSchema` | Task-node schema | Query, status, deadline |
| `BaseEdgeSchema` | Base edge schema | Weight, probability, cost metrics |
| `WorkflowEdgeSchema` | Workflow edge | Conditions, priority, transformations |
| `CostMetrics` | Cost metrics | Tokens, latency, trust, reliability |
| `LLMConfig` | Full LLM configuration | Model name, base URL, API key, generation parameters |
| `VisualizationStyle` | Visualization styles | Settings for colors, shapes, what to show |
| `NodeStyle` | Node style | Shape, colors, icon |
| `EdgeStyle` | Edge style | Line style, arrow, colors |
| `ValidationResult` | Validation result | Errors, warnings |
| `FeatureConfig` | GNN configuration | Feature dimensions |
| `TrainingConfig` | Training configuration | Learning rate, epochs, optimizer |

#### Benefits of Pydantic in gMAS

1. **Automatic type validation**
   ```python
   # Pydantic automatically checks types
   agent = AgentProfile(
       agent_id="test",            # str - OK
       display_name="Test Agent",  # str - OK
       tools=["search", "calc"],   # list[str] - OK
   )

   # Validation error for a wrong type
   agent = AgentProfile(agent_id=123)  # ❌ ValidationError: agent_id must be str
   ```

2. **Default values**
   ```python
   # Pydantic fills fields with default values
   agent = AgentProfile(agent_id="test", display_name="Test")
   print(agent.tools)     # [] (empty list by default)
   print(agent.persona)   # "" (empty string by default)
   ```

3. **Automatic type conversion**
   ```python
   # Pydantic validators can automatically convert types
   schema = AgentNodeSchema(
       id="test",
       embedding=torch.tensor([0.1, 0.2, 0.3])  # torch.Tensor → list[float]
   )
   print(type(schema.embedding))  # <class 'list'>
   ```

4. **Nested models**
   ```python
   # Pydantic validates nested models
   agent = AgentProfile(
       agent_id="test",
       display_name="Test",
       llm_config=AgentLLMConfig(  # Nested Pydantic model
           model_name="gpt-4",
           temperature=0.7,
       )
   )
   ```

5. **Serialization and deserialization**
   ```python
   # Built-in Pydantic methods
   data = agent.model_dump()  # → dict
   json_str = agent.model_dump_json(indent=2)  # → JSON string

   # Load from dict/JSON
   loaded = AgentProfile.model_validate(data)
   loaded_json = AgentProfile.model_validate_json(json_str)
   ```

6. **Immutability**
   ```python
   # frozen=True for AgentProfile
   agent = AgentProfile(agent_id="test", display_name="Test")
   agent.agent_id = "new_id"  # ❌ ValidationError: frozen model

   # Use copy methods for changes
   updated = agent.model_copy(update={"display_name": "New Name"})
   ```

7. **Extensibility**
   ```python
   # extra="allow" enables arbitrary fields
   schema = GraphSchema(
       name="MyGraph",
       custom_field="custom_value",  # Additional field
       another_field=123,            # Another one
   )
   ```
### Declarative typing

Thanks to Pydantic, all types are declarative and are checked both statically (mypy, pyright) and dynamically (at runtime):

```python
from core import AgentProfile
from core.schema import AgentNodeSchema, LLMConfig

# Static typing (IDE autocompletion)
agent: AgentProfile = AgentProfile(...)
config: LLMConfig = LLMConfig(...)
schema: AgentNodeSchema = AgentNodeSchema(...)

# Dynamic validation (runtime)
try:
    bad_agent = AgentProfile(agent_id=None)  # ❌ None instead of str
except ValidationError as e:
    print(e.errors())  # Detailed error information
```

---

### Decentralized data storage

Unlike centralized architectures, gMAS uses a **decentralized** approach:
- **Embeddings** are stored inside `AgentProfile.embedding`
- **Hidden states** are stored inside `AgentProfile.hidden_state`
- **Local memory** is stored inside `AgentProfile.state`
- `RoleGraph.embeddings` is an accessor that gathers embeddings from all agents into a single tensor

This allows each agent to own its representations and ensures node independence.

### System architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       RoleGraph                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │  Agent   │──│  Agent   │──│  Agent   │──│  Agent   │        │
│  │ Profile  │  │ Profile  │  │ Profile  │  │ Profile  │        │
│  │(embedding│  │(embedding│  │(embedding│  │(embedding│        │
│  │  state)  │  │  state)  │  │  state)  │  │  state)  │        │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘        │
│       ↑             ↑             ↑             ↑              │
│       └─────────────┴─────────────┴─────────────┘              │
│                    Adjacency matrix (A_com)                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        MACPRunner                                │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  Scheduler  │  │   Memory    │  │   Budget    │             │
│  │             │  │    Pool     │  │   Tracker   │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │   MACPResult    │
                    │  • messages     │
                    │  • final_answer │
                    │  • metrics      │
                    └─────────────────┘
```

### Data flow

1. **Create agents** → `AgentProfile` describes the role, capabilities, and tools
2. **Build the graph** → `build_property_graph` creates a `RoleGraph` with topology
3. **Planning** → `Scheduler` determines the execution order
4. **Execution** → `MACPRunner` runs agents sequentially/in parallel
5. **Result** → `MACPResult` contains all agents’ responses and metrics

---

## Core Components

### RoleGraph

`RoleGraph` is the central data structure representing the agent graph.

```python
from core import RoleGraph

# === Graph properties ===
graph.num_nodes        # Number of nodes
graph.num_edges        # Number of edges
graph.agents           # List of AgentProfile objects
graph.node_ids         # List of node IDs ["agent1", "agent2", ...]
graph.role_sequence    # Role order (legacy)
graph.A_com            # Adjacency matrix (torch.Tensor, N x N)
graph.edge_index       # Edge index in PyG format (torch.Tensor, 2 x E)
graph.edge_attr        # Edge attributes (torch.Tensor, E x feature_dim)
graph.embeddings       # Accessor: gathers agent embeddings into a tensor (N x dim)
graph.graph            # Internal rustworkx.PyDiGraph object
graph.task_node        # TaskNode if enabled, otherwise None
graph.query            # Task query (string)

# === Node operations ===
# Add a node
graph.add_node(
    agent,                        # AgentProfile
    connections_to=["other"],     # List of IDs for outgoing edges
    connections_from=["prev"],    # List of IDs for incoming edges
    weight=1.0,                   # Default edge weight
)

# Remove a node with a state migration policy
graph.remove_node(
    "agent_id",
    policy=StateMigrationPolicy.ARCHIVE,  # DISCARD, COPY, ARCHIVE
)

# Replace a node
graph.replace_node(
    old_node_id="old",
    new_agent=new_agent_profile,
    policy=StateMigrationPolicy.COPY,     # Copy state
    keep_connections=True,                # Preserve edges
)

# Get an agent
agent = graph.get_agent_by_id("agent_id")

# Get node index in the matrix
idx = graph.get_node_index("agent_id")  # -> int

# Existence check
if "agent_id" in graph.node_ids:
    ...

# === Edge operations ===
# Add an edge
graph.add_edge(
    source="agent1",
    target="agent2",
    weight=0.8,
    edge_type="workflow",          # Edge type (optional)
    metadata={"priority": 1},      # Additional data
)

# Remove an edge
graph.remove_edge("agent1", "agent2")

# Update edge weight
graph.update_edge_weight("agent1", "agent2", new_weight=0.9)

# Get neighbors
out_neighbors = graph.get_neighbors("agent_id", direction="out")   # Outgoing
in_neighbors = graph.get_neighbors("agent_id", direction="in")     # Incoming
all_neighbors = graph.get_neighbors("agent_id", direction="both")  # All

# Check whether an edge exists
has_edge = graph.has_edge("agent1", "agent2")

# Get edge weight
weight = graph.get_edge_weight("agent1", "agent2")

# === Execution bounds (start/end nodes) ===
# Set start and end nodes for optimization
graph.set_start_node("input_agent")
graph.set_end_node("output_agent")

# Or set both at once
graph.set_execution_bounds("input_agent", "output_agent")

# Inspect bounds
print(f"Start: {graph.start_node}, End: {graph.end_node}")

# === Disabled nodes ===
# Disable nodes (they remain in the graph but will not be executed)
graph.disable("agent1")              # One node
graph.disable(["agent2", "agent3"])  # Multiple nodes

# Enable back
graph.enable("agent1")               # One node
graph.enable(["agent2", "agent3"])   # Multiple nodes
graph.enable()                       # All disabled nodes

# Check status
graph.is_enabled("agent1")           # -> bool
graph.get_enabled()                  # -> ["agent1", ...]
graph.get_disabled()                 # -> ["agent2", ...]

# Use case: token savings based on algorithms
if rl_model.predict(graph_state) < threshold:
    graph.disable("expensive_agent")

# === Reachability analysis ===
# Get nodes reachable from start_node
reachable = graph.get_reachable_from("input_agent")

# Get nodes that can reach end_node
reaching = graph.get_nodes_reaching("output_agent")

# Get relevant nodes (on the path start -> end)
relevant = graph.get_relevant_nodes()
# Automatically uses graph.start_node and graph.end_node

# Get isolated nodes (not on the path start -> end)
isolated = graph.get_isolated_nodes()

# Optimized execution order (without isolated nodes)
order = graph.get_optimized_execution_order()

# === Conditional edges ===
# Add an edge with a condition
from execution.scheduler import ConditionContext

def condition_func(context: ConditionContext) -> bool:
    return context.state.get("quality") > 0.8

graph.add_conditional_edge(
    source="writer",
    target="editor",
    condition=condition_func,
    weight=0.9,
)

# === Dynamic topology updates ===
# Full update of the adjacency matrix
graph.update_communication(
    a_new,                    # New adjacency matrix (torch.Tensor)
    s_tilde=scores,          # Quality score matrix (optional)
    p_matrix=probabilities,  # Transition probability matrix (optional)
)

# === Conversion and export ===
# Serialize to a dictionary
data = graph.to_dict()
# {
#   "agents": [...],
#   "adjacency": [[...]],
#   "query": "...",
#   "task_node": {...},
# }

# Convert to PyTorch Geometric Data
pyg_data = graph.to_pyg_data()
# Data(x=node_features, edge_index=edges, edge_attr=weights)

# Extract a subgraph
subgraph = graph.subgraph(["agent1", "agent2", "agent3"])

# Copy the graph
graph_copy = graph.copy()

# === Integrity checks ===
# Verify consistency of internal structures
graph.verify_integrity(raise_on_error=True)

# Quick check
is_valid = graph.is_consistent()

# === Graph analysis ===
# Check whether it is a DAG (directed acyclic graph)
is_dag = graph.is_dag()

# Get topological order (if DAG)
if graph.is_dag():
    topo_order = graph.topological_sort()

# === Agent updates ===
# Update an agent's embedding
agent = graph.get_agent_by_id("solver")
agent = agent.with_embedding(new_embedding)
graph.update_agent("solver", agent)

# Update an agent's state
agent = agent.append_state({"role": "assistant", "content": "Response"})
graph.update_agent("solver", agent)

# === Batch operations ===
# Update multiple agents
updates = {
    "agent1": updated_agent1,
    "agent2": updated_agent2,
}
graph.batch_update_agents(updates)

# Add multiple edges
edges = [
    ("a", "b", 0.8),
    ("b", "c", 0.9),
    ("c", "d", 0.7),
]
graph.batch_add_edges(edges)
```
#### State migration policies

When removing or replacing a node, you can specify a migration policy:

```python
from core.graph import StateMigrationPolicy

# DISCARD — state is removed
graph.remove_node("agent_id", policy=StateMigrationPolicy.DISCARD)

# COPY — state is copied into the new node
graph.replace_node("old_id", new_agent, policy=StateMigrationPolicy.COPY)

# ARCHIVE — state is saved to external storage
graph.remove_node("agent_id", policy=StateMigrationPolicy.ARCHIVE)
```

---

### AgentProfile

`AgentProfile` is an **immutable Pydantic model** (`BaseModel` with `frozen=True`) representing an agent profile with description, tools, state, and LLM configuration.

> **Important**:
> - `AgentProfile` inherits from `pydantic.BaseModel`, providing **automatic type validation** and **type safety**
> - Embeddings and hidden states are stored **at the agent level**, not at the graph level
> - **Multi-model support** — each agent can have its own LLM configuration
> - Immutability (`frozen=True`) — methods return new objects

#### AgentProfile structure (Pydantic model)

| Field | Type | Description |
|------|-----|----------|
| `agent_id` | `str` | Unique agent identifier (required) |
| `display_name` | `str` | Display name (required) |
| `persona` | `str` | Agent role/persona (e.g., "Expert analyst") |
| `description` | `str` | Textual description of agent capabilities |
| `llm_backbone` | `str \| None` | LLM model identifier (legacy; use `llm_config`) |
| `llm_config` | `AgentLLMConfig \| None` | **Pydantic model** for the agent’s LLM configuration |
| `tools` | `list[str]` | List of available tools (shell, code_interpreter, file_search, web_search, custom) |
| `raw` | `Mapping[str, Any]` | Arbitrary extra data |
| `embedding` | `torch.Tensor \| None` | Agent vector representation (arbitrary_types_allowed) |
| `state` | `list[dict[str, Any]]` | Local state / message history |
| `hidden_state` | `torch.Tensor \| None` | Hidden state passed between agents |

#### AgentLLMConfig (Pydantic model)

```python
from core.agent import AgentLLMConfig

# AgentLLMConfig - a Pydantic model for LLM configuration
llm_config = AgentLLMConfig(
    model_name="gpt-4",                         # Model name
    base_url="https://api.openai.com/v1",      # API endpoint
    api_key="$OPENAI_API_KEY",                 # Key (or $ENV_VAR)
    max_tokens=2000,                            # Max tokens
    temperature=0.7,                            # Temperature
    timeout=60.0,                               # Timeout in seconds
    top_p=0.9,                                  # Top-p sampling
    stop_sequences=["END", "STOP"],             # Stop sequences
    extra_params={"frequency_penalty": 0.5},    # Extra parameters
)

# AgentLLMConfig methods
api_key = llm_config.resolve_api_key()      # Resolve $ENV_VAR
is_set = llm_config.is_configured()         # Check whether configured
params = llm_config.to_generation_params()  # Build params for the LLM
```

#### Creating and working with AgentProfile

```python
from core import AgentProfile
from core.agent import AgentLLMConfig

# 1. Basic creation (Pydantic validates types)
agent = AgentProfile(
    agent_id="analyzer",            # Unique ID (str, required)
    display_name="Data Analyzer",   # Display name (str, required)
    persona="Expert data analyst",  # Role/persona (str, default="")
    description="Analyzes data and produces insights",  # Description (str, default="")
    tools=["python", "sql"],        # Available tools (list[str], default=[])
)

# 2. Creation with LLM config (Pydantic model)
llm_config = AgentLLMConfig(
    model_name="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",  # Resolved from environment
    temperature=0.7,
    max_tokens=2000,
)

agent = AgentProfile(
    agent_id="researcher",
    display_name="Researcher",
    llm_config=llm_config,  # Pydantic validates the nested model
    tools=["web_search"],
)

# 3. State operations (immutable — returns a NEW object)
agent = agent.append_state({"role": "user", "content": "Hello!"})
agent = agent.with_state([{"role": "system", "content": "You are helpful"}])
agent = agent.clear_state()

# 4. Embeddings (arbitrary_types_allowed for torch.Tensor)
import torch

embedding = torch.randn(384)
agent = agent.with_embedding(embedding)

hidden_state = torch.randn(768)
agent = agent.with_hidden_state(hidden_state)

# 5. LLM config operations
agent = agent.with_llm_config(llm_config)

# Get the agent model name (priority: llm_config.model_name → llm_backbone)
model_name = agent.get_model_name()  # "gpt-4"

# Check if a custom LLM configuration is set
if agent.has_custom_llm():
    print(f"Agent uses custom LLM: {agent.llm_config.model_name}")
    print(f"Base URL: {agent.llm_config.base_url}")
    print(f"Generation params: {agent.llm_config.to_generation_params()}")

# 6. Serialization (Pydantic methods)
# For encoder (text)
text = agent.to_text()

# For persistence (dict, includes llm_config)
data = agent.to_dict()

# Pydantic serialization methods
agent_dict = agent.model_dump()  # Dict[str, Any]
agent_json = agent.model_dump_json(indent=2)  # JSON string

# 7. Deserialization (Pydantic methods)
loaded_agent = AgentProfile.model_validate(agent_dict)
loaded_from_json = AgentProfile.model_validate_json(agent_json)
```

#### Example: agents with different LLMs

```python
from core import AgentProfile
from core.agent import AgentLLMConfig

# Agent 1: strong model for analysis
analyst = AgentProfile(
    agent_id="analyst",
    display_name="Senior Analyst",
    persona="Expert data analyst with 10 years experience",
    description="Performs deep analysis of complex data",
    llm_config=AgentLLMConfig(
        model_name="gpt-4",
        base_url="https://api.openai.com/v1",
        api_key="$OPENAI_API_KEY",
        temperature=0.0,  # Deterministic for analysis
        max_tokens=2000,
    ),
    tools=["python", "sql", "visualization"],
)

# Agent 2: cheaper model for formatting
formatter = AgentProfile(
    agent_id="formatter",
    display_name="Report Formatter",
    persona="Technical writer",
    description="Formats analysis results into readable reports",
    llm_config=AgentLLMConfig(
        model_name="gpt-4o-mini",  # Cheaper for simple tasks
        base_url="https://api.openai.com/v1",
        api_key="$OPENAI_API_KEY",
        temperature=0.3,
        max_tokens=500,
    ),
    tools=["markdown", "latex"],
)

# Agent 3: local model
local_agent = AgentProfile(
    agent_id="local_llm",
    display_name="Local Assistant",
    llm_config=AgentLLMConfig(
        model_name="llama3:70b",
        base_url="http://localhost:11434/v1",  # Ollama
        temperature=0.5,
    ),
)
```

#### Benefits of Pydantic validation

1. **Automatic type checking** when creating objects
2. **Default values** for optional fields
3. **Immutability** (`frozen=True`) prevents accidental changes
4. **Nested models** (`AgentLLMConfig` is validated automatically)
5. **Serialization/deserialization** via `.model_dump()` and `.model_validate()`
6. **Support for arbitrary types** (`arbitrary_types_allowed`) for torch.Tensor

#### System Prompt Generation ("You are" Logic)

gMAS automatically generates system prompts from the agent's `persona` and `description` fields.

##### How System Prompts Are Built

The `_build_system_prompt_parts()` function in `runner.py` constructs the system message:

1. **Persona/Role identity** — with automatic "You are" prefix when needed
2. **Description** — appended as a second paragraph
3. **Tools hint** — `"Available tools: tool1, tool2, ..."`
4. **Output schema** — `"Respond with JSON matching: {...}"`

##### Persona Injection Logic

The framework intelligently adds "You are" prefix only when the persona doesn't already look like a complete sentence:

| `persona` value | Result |
|-----------------|--------|
| `"a helpful math assistant"` | `"You are a helpful math assistant."` |
| `"You are a mathematician"` | `"You are a mathematician."` (no duplicate) |
| `"Ты — аналитик"` | `"Ты — аналитик."` (no "You are" prefix) |
| `""` (empty) | `"You are a helpful assistant."` (default) |

##### Multi-language Detection

The `_looks_like_sentence()` heuristic checks for sentence-starting patterns in **15 languages**:

- **EN**: "you are", "you're", "i am", "i'm", "we are"
- **RU**: "ты ", "вы ", "я ", "мы ", "он ", "она "
- **DE**: "du bist", "sie sind", "ich bin", "wir sind"
- **FR**: "tu es", "vous êtes", "je suis", "nous sommes"
- **ES**: "tú eres", "usted es", "yo soy", "nosotros somos"
- **PT**: "você é", "eu sou", "nós somos"
- **IT**: "tu sei", "lei è", "io sono", "noi siamo"
- **ZH**: "你是", "我是", "您是", "他是", "她是"
- **JA**: "あなたは", "わたしは"
- **KO**: "당신은", "나는"
- **AR**: "أنت ", "أنا "
- **HI**: "तुम ", "आप ", "मैं "
- **TR**: "sen ", "siz ", "ben "
- **PL**: "ty ", "pan ", "ja "
- **NL**: "jij bent", "u bent", "ik ben"

**Purpose:** Avoid grammatically incorrect duplicates like `"You are Ты — аналитик"`.

##### Example

```python
from core import AgentProfile

agent = AgentProfile(
    agent_id="math_agent",
    display_name="Mathematician",
    persona="an expert mathematician specializing in number theory",
    description="Solves mathematical problems with step-by-step reasoning",
    tools=["calculator"],
)

# Generated system prompt (internal):
# "You are Mathematician. You are an expert mathematician specializing in number theory.
#  Solves mathematical problems with step-by-step reasoning.
#  Available tools: calculator."
```

##### Default Fallback

If neither `persona` nor `role` is defined:

```python
# Default system prompt when no persona/role
"You are a helpful assistant."
```

---

### TaskNode

`TaskNode` is an **immutable Pydantic model** (`BaseModel` with `frozen=True`) representing a virtual task node that stores the task query and can be connected to all agents.

> **Important**: `TaskNode` inherits from `pydantic.BaseModel`, providing automatic type validation and immutability (just like `AgentProfile`).

#### TaskNode structure (Pydantic model)

| Field | Type | Description |
|------|-----|----------|
| `agent_id` (`id`) | `str` | Task node identifier (default `__task__`) |
| `type` | `str` | Node type (`"task"`, automatically) |
| `query` | `str` | Task statement / query |
| `description` | `str` | Additional context description |
| `embedding` | `torch.Tensor \| None` | Task embedding (arbitrary_types_allowed) |
| `display_name` | `str` | Display name (default `"Task"`) |
| `persona` | `str` | Task persona/role (default empty) |
| `llm_backbone` | `str \| None` | Model identifier, if needed |
| `tools` | `list[str]` | Tools available to the task node (default=[]) |
| `state` | `list[dict[str, Any]]` | Local task state / message history (default=[]) |

```python
from core import TaskNode

# Pydantic validates types on creation
task = TaskNode(
    agent_id="__task__",  # can be overridden (str)
    query="Draft a market research plan",  # required (str)
    description="A task for the whole team of agents",  # optional (str, default="")
)

# Task embedding (optional, arbitrary_types_allowed for torch.Tensor)
import torch
task_embedding = torch.randn(384)
task = task.with_embedding(task_embedding)

# TaskNode is immutable (frozen=True), use copy methods
updated_task = task.model_copy(update={"description": "New description"})

# Pydantic serialization
task_dict = task.model_dump()
task_json = task.model_dump_json(indent=2)

# Deserialization
loaded = TaskNode.model_validate(task_dict)
```

> When using `build_property_graph(..., include_task_node=True)`, the task node is created automatically and connected to agents via context/update edges.

#### TaskNode methods (immutable)

```python
# Embedding operations (returns a new object)
task = task.with_embedding(embedding_tensor)

# State operations (returns a new object)
task = task.append_state({"role": "system", "content": "Context"})
task = task.with_state([{"role": "user", "content": "Query"}])
task = task.clear_state()

# Convert to text
task_text = task.to_text()  # For encoder

# Convert to dict
task_data = task.to_dict()  # For persistence
```

---

### NodeEncoder

`NodeEncoder` converts textual agent descriptions into vector representations.

```python
from core import NodeEncoder

# sentence-transformers (recommended)
encoder = NodeEncoder(
    model_name="sentence-transformers/all-MiniLM-L6-v2",
    normalize_embeddings=True,
)

# hash fallback (fast, no model required)
encoder = NodeEncoder(model_name="hash:256")

# Encode texts
texts = [agent.to_text() for agent in agents]
embeddings = encoder.encode(texts)  # torch.Tensor (N x dim)

# Get dimensionality
dim = encoder.embedding_dim
```

---
### MACPRunner

`MACPRunner` is the executor of the Multi-Agent Communication Protocol.

#### Runner Architecture (Modular Design)

The runner is organized into focused submodules for better maintainability:

```
src/execution/runner/
├── __init__.py      # Public facade, re-exports all symbols
├── core.py          # Lifecycle, memory, caller selection, graph helpers
├── execution.py     # Simple/adaptive execution paths
├── batch.py         # run_round(), arun_round() entrypoints
├── stream.py        # stream(), astream() for real-time output
├── topology.py      # Dynamic topology changes, hooks
├── state.py         # RunnerConfig, ExecutionContext, TopologyAction, etc.
├── llm.py           # LLM caller protocols, factories
├── prompting.py     # StructuredPrompt, _strip_tool_metadata()
└── shared.py        # Common imports for all mixins
```

`MACPRunner` is assembled from mixins:
- `RunnerCoreMixin` — initialization, memory, caller selection
- `RunnerExecutionMixin` — simple/adaptive execution logic
- `RunnerBatchMixin` — `run_round()`, `arun_round()`
- `RunnerStreamMixin` — `stream()`, `astream()`
- `RunnerTopologyMixin` — dynamic topology hooks

```python
from execution import MACPRunner, RunnerConfig

# ✅ Recommended for modern chat LLMs (OpenAI, GigaChat, etc.)
# Sends proper system/user roles — no flat-string workaround needed.
from openai import OpenAI
client = OpenAI(api_key="sk-...")

def my_structured_caller(messages: list[dict]) -> str:
    resp = client.chat.completions.create(model="gpt-4o", messages=messages)
    return resp.choices[0].message.content or ""

runner = MACPRunner(structured_llm_caller=my_structured_caller)

# Legacy setup — one flat-string LLM for all agents (still supported)
runner = MACPRunner(
    llm_caller=sync_llm_function,           # Callable[[str], str]
    async_llm_caller=async_llm_function,    # Callable[[str], Awaitable[str]]
    token_counter=my_token_counter,         # Token counting
)

# Multi-model setup (different LLMs for different agents)
from execution import LLMCallerFactory, create_openai_caller

# Option 1: Use a factory (recommended)
factory = LLMCallerFactory.create_openai_factory(
    default_model="gpt-4o-mini",
    default_base_url="https://api.openai.com/v1",
)
runner = MACPRunner(llm_factory=factory)

# Option 2: A dictionary of callers per agent
runner = MACPRunner(
    llm_callers={
        "analyst": create_openai_caller(model="gpt-4", temperature=0.0),
        "writer": create_openai_caller(model="gpt-4o-mini", temperature=0.7),
    },
    async_llm_callers={
        "analyst": create_openai_caller(model="gpt-4", is_async=True),
        "writer": create_openai_caller(model="gpt-4o-mini", is_async=True),
    },
)

# Option 3: Combined (factory + overrides for specific agents)
runner = MACPRunner(
    llm_factory=factory,                                # Default for everyone
    llm_callers={"critical_agent": specialized_caller}, # Override for critical_agent
)

# Advanced configuration
config = RunnerConfig(
    timeout=60.0,                         # Per-agent timeout
    adaptive=True,                        # Adaptive mode
    enable_parallel=True,                 # Parallel execution
    max_parallel_size=5,                  # Max parallel agents
    max_retries=2,                        # Retries on errors
    update_states=True,                   # Update agent states
    enable_memory=True,                   # Enable memory
    callbacks=[StdoutCallbackHandler()],  # Callbacks for logging
)

runner = MACPRunner(llm_caller=my_llm, config=config)

# Synchronous execution
result = runner.run_round(graph)

# With explicit execution bounds and filtering
result = runner.run_round(
    graph,
    start_agent_id="input",          # Start agent (overrides graph.start_node)
    final_agent_id="output",         # Final agent (overrides graph.end_node)
    filter_unreachable=True,         # Exclude isolated nodes (token savings)
    update_states=True,              # Update agent states
)

# Asynchronous execution
result = await runner.arun_round(
    graph,
    start_agent_id="input",
    final_agent_id="output",
    filter_unreachable=True,
)

# Execution with hidden channels
result = runner.run_round_with_hidden(graph, hidden_encoder=encoder)
```

#### RunnerConfig (full specification)

```python
from execution import RunnerConfig, RoutingPolicy, PruningConfig, BudgetConfig, ErrorPolicy, ErrorAction

config = RunnerConfig(
    # === Basic parameters ===
    timeout=60.0,                        # Per-agent timeout (sec)
    max_retries=3,                       # Max attempts on errors
    update_states=True,                  # Update AgentProfile.state

    # === Adaptive mode ===
    # adaptive controls conditional edges, pruning, fallback, and routing
    # policies.  It does NOT affect whether agents run in parallel.
    adaptive=True,                       # Enable conditional routing & pruning
    routing_policy=RoutingPolicy.WEIGHTED_TOPO,  # Routing policy

    # === Parallel execution ===
    # enable_parallel works independently of adaptive: when True,
    # independent agents (those with all predecessors done) are executed
    # concurrently via asyncio.gather.  Works with both astream() and
    # arun_round(), regardless of the adaptive flag.
    enable_parallel=True,                # Parallel group execution
    max_parallel_size=5,                 # Max agents in a parallel group

    # === Pruning ===
    pruning_config=PruningConfig(
        min_weight_threshold=0.1,        # Min edge weight
        min_probability_threshold=0.05,  # Min transition probability
        max_consecutive_errors=3,        # Max consecutive errors
        token_budget=10000,              # Token budget for pruning
        enable_fallback=True,            # Use fallback agents
        max_fallback_attempts=2,         # Max fallback attempts
        quality_scorer=None,             # Quality scoring function
        min_quality_threshold=0.3,       # Min quality to continue
    ),

    # === Budget ===
    budget_config=BudgetConfig(
        total_token_limit=50000,
        node_token_limit=2000,
        max_prompt_length=4000,
        max_response_length=2000,
        warn_at_usage_ratio=0.8,
        total_time_limit_seconds=600,
        total_request_limit=100,
    ),

    # === Memory ===
    enable_memory=True,                  # Enable memory system
    memory_config=MemoryConfig(
        working_max_entries=20,
        long_term_max_entries=100,
        working_default_ttl=3600.0,
        auto_compress=True,
        promote_after_accesses=3,
    ),
    memory_context_limit=5,              # Memory entries injected into the prompt

    # === Hidden channels ===
    enable_hidden_channels=True,         # Passing hidden_state
    hidden_combine_strategy="mean",      # mean, sum, concat, attention
    pass_embeddings=True,                # Pass embeddings

    # === Task query broadcast ===
    broadcast_task_to_all=True,          # True: task query is sent to all agents
                                         # False: only to agents connected to the task node

    # === Dynamic topology (runtime modification) ===
    enable_dynamic_topology=True,        # Enable runtime graph modifications
    topology_hooks=[my_hook_func],       # Sync hooks for topology modification
    async_topology_hooks=[async_hook],   # Async hooks for topology modification
    early_stop_conditions=[              # Early stopping conditions
        EarlyStopCondition.on_keyword("FINAL ANSWER"),
        EarlyStopCondition.on_token_limit(10000),
        EarlyStopCondition.on_custom(lambda ctx: my_logic(ctx)),
    ],

    # === Callbacks (monitoring and logging) ===
    callbacks=[                          # Callback handlers
        StdoutCallbackHandler(           # Console output
            show_prompts=False,
            show_outputs=True,
        ),
        MetricsCallbackHandler(),         # Metrics aggregation
        FileCallbackHandler("run.jsonl"), # File logging
    ],

    # === Error handling ===
    error_policy=ErrorPolicy(
        on_timeout=ErrorAction.RETRY,
        on_retry_exhausted=ErrorAction.PRUNE,
        on_budget_exceeded=ErrorAction.ABORT,
        on_validation_error=ErrorAction.ABORT,
    ),

    # === Streaming ===
    enable_token_streaming=False,        # Enable token-level streaming if LLM supports it
)
```

#### Execution result (MACPResult)

```python
result.messages               # Dict[agent_id -> response]
result.final_answer           # Final agent answer
result.final_agent_id         # Final agent ID
result.execution_order        # Execution order
result.agent_states           # Updated agent states
result.total_tokens           # Total tokens
result.total_time             # Execution time (sec)
result.topology_changed_count # Number of topology changes
result.fallback_count         # Number of fallbacks
result.pruned_agents          # Pruned agents (including disabled and isolated)
result.errors                 # List of errors
result.hidden_states          # Agents' hidden states
result.metrics                # ExecutionMetrics with detailed statistics
# New fields (dynamic topology)
result.early_stopped          # bool: whether early stopping occurred
result.early_stop_reason      # str: early stop reason
result.topology_modifications # int: number of topology modifications
```

---

### Scheduler

The scheduler determines the agent execution order.

```python
from execution import (
    build_execution_order,
    get_parallel_groups,
    AdaptiveScheduler,
    RoutingPolicy,
    PruningConfig,
)

# Simple topological order
order = build_execution_order(graph.A_com, agent_ids)

# Parallel execution groups
groups = get_parallel_groups(graph.A_com, agent_ids)
# Result: [["a", "b"], ["c"], ["d", "e"]]

# Adaptive scheduler
scheduler = AdaptiveScheduler(
    policy=RoutingPolicy.WEIGHTED_TOPO,  # Routing policy
    pruning_config=PruningConfig(
        min_weight_threshold=0.1,        # Min edge weight
        min_probability_threshold=0.05,  # Min probability
        max_consecutive_errors=3,        # Max consecutive errors
        token_budget=10000,              # Token budget
        enable_fallback=True,            # Enable fallback
        max_fallback_attempts=2,         # Max fallback attempts
    ),
    beam_width=3,                        # Beam search width
)

# Build a plan
plan = scheduler.build_plan(
    a_agents,           # Agent adjacency matrix
    agent_ids,          # List of IDs
    p_matrix=probs,     # Probability matrix
    end_agent="final",  # Final agent
)

# Working with the plan
step = plan.get_current_step()
plan.mark_completed("agent_id", tokens=100)
plan.mark_failed("agent_id")
plan.mark_skipped("agent_id")

# === Step-level tracking (new) ===
# Each step has unique identifiers for precise tracking
step = plan.get_current_step()
step.step_id          # Unique step identifier (e.g., "solver_attempt_2")
step.dependency_ids   # Specific step IDs this step depends on

# Track at step level (not just agent level)
plan.completed_step_ids      # Set of completed step IDs
plan.failed_step_ids         # Set of failed step IDs
plan.skipped_step_ids        # Set of skipped step IDs
plan.condition_skipped_step_ids  # Steps skipped by conditions

# Step-level methods
plan.is_step_resolved(step)  # Check if specific step is done
plan.find_pending_step("solver")  # Find next pending step for agent
plan.get_latest_step("solver")    # Get latest step for agent (resolved or pending)

# Conditional skip management
plan.apply_condition_skip("optional_agent")   # Skip all pending steps for agent
plan.clear_condition_skip("optional_agent")   # Re-enable pending steps
plan.is_condition_skipped(step)               # Check if step is condition-skipped
```

#### Routing policies (detailed)

```python
from execution import RoutingPolicy, AdaptiveScheduler

# ========== 1. TOPOLOGICAL (Topological sort) ==========
# Description: Classic topological sort for a DAG
# Use case: Simple pipelines without adaptivity
# Complexity: O(V + E)

scheduler = AdaptiveScheduler(policy=RoutingPolicy.TOPOLOGICAL)
plan = scheduler.build_plan(adjacency, agent_ids)

# Example:
#   A → B → C → D
# Order: [A, B, C, D]

# ========== 2. WEIGHTED_TOPO (Weighted topological) ==========
# Description: Topological sort with priority based on edge weights
# Use case: When you need to account for connection importance
# Complexity: O(V + E log V)

scheduler = AdaptiveScheduler(policy=RoutingPolicy.WEIGHTED_TOPO)
plan = scheduler.build_plan(adjacency, agent_ids)

# Example:
#       ┌─(0.9)→ B ─┐
#   A ──┤          ├→ D
#       └─(0.3)→ C ─┘
# Order: [A, B, C, D]  (B runs before C because 0.9 > 0.3)

# ========== 3. GREEDY (Greedy selection) ==========
# Description: At each step, selects the agent with the maximum edge weight
# Use case: Optimize for connection quality
# Complexity: O(V²)

scheduler = AdaptiveScheduler(policy=RoutingPolicy.GREEDY)
plan = scheduler.build_plan(
    adjacency,
    agent_ids,
    start_node="coordinator",
    end_node="final",
)

# Example:
#   Start → A(0.9) → B(0.8) → End
#   Start → C(0.5) → D(0.7) → End
# Selected: Start → A → B → End (higher total weight)

# ========== 4. BEAM_SEARCH (Beam search) ==========
# Description: Keeps beam_width best paths and selects the optimal one
# Use case: Balance between quality and speed
# Complexity: O(V * beam_width * E)

scheduler = AdaptiveScheduler(
    policy=RoutingPolicy.BEAM_SEARCH,
    beam_width=3,  # Keep 3 best paths
)

plan = scheduler.build_plan(
    adjacency,
    agent_ids,
    p_matrix=probability_matrix,  # Transition probabilities
)

# Example with beam_width=2:
#   Start ─┬→ A(0.8) ─┬→ B(0.9) → End  [path 1: 0.72]
#          │          └→ C(0.6) → End  [path 2: 0.48]
#          └→ D(0.7) ─→ E(0.8) → End   [path 3: 0.56]
# Beam keeps paths 1 and 3, drops path 2
# Final choice: path 1

# ========== 5. K_SHORTEST (K shortest paths) ==========
# Description: Finds K shortest paths and selects the best by a criterion
# Use case: When alternative routes are required
# Complexity: O(K * (V + E) log V)

scheduler = AdaptiveScheduler(
    policy=RoutingPolicy.K_SHORTEST,
    k_paths=5,  # Find 5 shortest paths
)

plan = scheduler.build_plan(
    adjacency,
    agent_ids,
    start_node="input",
    end_node="output",
    path_metric=PathMetric.WEIGHTED,  # HOP_COUNT, WEIGHTED, RELIABILITY
)

# Example:
# Found paths:
#   1. input → A → B → output  (cost=3, hops=3)
#   2. input → C → output      (cost=4, hops=2)
#   3. input → A → D → output  (cost=5, hops=3)
#   4. input → E → F → output  (cost=6, hops=3)
#   5. input → G → output      (cost=7, hops=2)
# Selection by metric: path 1 (minimum cost)

# ========== 6. GNN_BASED (GNN-based) ==========
# Description: Uses a trained GNN to predict the optimal route
# Use case: Adaptive routing based on history
# Requires: A trained GNN model

from core.gnn import GNNRouterInference

scheduler = AdaptiveScheduler(
    policy=RoutingPolicy.GNN_BASED,
    gnn_router=gnn_inference,     # GNNRouterInference object
    gnn_threshold=0.7,            # Min confidence to use the GNN
)

# If confidence < threshold, fallback policy is used
scheduler.set_fallback_policy(RoutingPolicy.WEIGHTED_TOPO)

plan = scheduler.build_plan(
    adjacency,
    agent_ids,
    metrics_tracker=tracker,  # For GNN features
)

# ========== Policy comparison ==========

# | Policy         | Adaptivity     | Complexity     | Quality  | Use case                       |
# |----------------|----------------|----------------|----------|--------------------------------|
# | TOPOLOGICAL    | No             | O(V+E)         | ⭐       | Simple pipelines               |
# | WEIGHTED_TOPO  | Low            | O(V+E·logV)    | ⭐⭐      | Priority-based pipelines       |
# | GREEDY         | Medium         | O(V²)          | ⭐⭐⭐     | Weight-optimized routing       |
# | BEAM_SEARCH    | High           | O(V·k·E)       | ⭐⭐⭐⭐    | Quality/speed balance          |
# | K_SHORTEST     | High           | O(K·V·logV)    | ⭐⭐⭐⭐    | Alternative route search       |
# | GNN_BASED      | Very high      | O(GNN)         | ⭐⭐⭐⭐⭐   | Trained systems                |

# ========== Choosing a policy based on the task ==========

# Simple linear pipeline
config = RunnerConfig(routing_policy=RoutingPolicy.TOPOLOGICAL)

# Graph with different agent priorities
config = RunnerConfig(routing_policy=RoutingPolicy.WEIGHTED_TOPO)

# Optimize route quality
config = RunnerConfig(routing_policy=RoutingPolicy.GREEDY)

# Balance exploration vs exploitation
config = RunnerConfig(
    routing_policy=RoutingPolicy.BEAM_SEARCH,
    adaptive=True,
)
scheduler = AdaptiveScheduler(policy=RoutingPolicy.BEAM_SEARCH, beam_width=3)

# Need fallback alternatives
config = RunnerConfig(routing_policy=RoutingPolicy.K_SHORTEST)
scheduler = AdaptiveScheduler(policy=RoutingPolicy.K_SHORTEST, k_paths=3)

# Advanced trained system
config = RunnerConfig(routing_policy=RoutingPolicy.GNN_BASED)
scheduler = AdaptiveScheduler(
    policy=RoutingPolicy.GNN_BASED,
    gnn_router=trained_router,
)
```
---

### Memory System

A stratified memory system with **working** and **long-term** levels, supporting TTL, tags, priorities, and automatic compression.

#### Memory architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       AgentMemory                           │
│  ┌────────────────────┐     ┌──────────────────────┐       │
│  │   Working Memory    │     │   Long-term Memory   │       │
│  │   (TTL: 1 hour)     │     │   (TTL: ∞)           │       │
│  │   Max: 20 entries   │     │   Max: 100 entries   │       │
│  │                    │     │                      │       │
│  │  - Recent messages │────▶│  - Important facts   │       │
│  │  - Temp context    │     │  - Key insights      │       │
│  │  - Active tasks    │     │  - Historical data   │       │
│  └────────────────────┘     └──────────────────────┘       │
│         ▲                            ▲                      │
│         │ promotion                  │                      │
│         │ (after N accesses)         │                      │
│         └────────────────────────────┘                      │
└─────────────────────────────────────────────────────────────┘
         │
         │ sharing
         ▼
┌─────────────────────────────────────────────────────────────┐
│                     SharedMemoryPool                        │
│  Memory sharing between agents                              │
│  - Broadcast: one → all                                     │
│  - Share: one → selected                                    │
│  - Query: search by tags                                    │
└─────────────────────────────────────────────────────────────┘
```

---

#### Basic usage of AgentMemory

```python
from utils.memory import (
    AgentMemory,
    MemoryConfig,
    MemoryLevel,
    MemoryEntry,
)

# 1. Memory configuration
config = MemoryConfig(
    # Working memory (short-term)
    working_max_entries=20,         # Max entries
    working_default_ttl=3600.0,     # TTL: 1 hour

    # Long-term memory
    long_term_max_entries=100,      # Max entries
    long_term_default_ttl=None,     # No expiration

    # Automatic management
    auto_compress=True,             # Auto-compress on limit overflow
    compress_strategy="truncate",   # truncate, summarize
    promote_after_accesses=3,       # Promote to long-term after N accesses

    # Prioritization
    use_priority=True,              # Consider priorities when evicting
    priority_weight=0.3,            # Priority weight vs recency
)

# 2. Create an agent memory
memory = AgentMemory("researcher", config)

# 3. Add entries
# 3.1. Add messages (the simplest way)
memory.add_message(role="user", content="Analyze the dataset")
memory.add_message(role="assistant", content="I will analyze it")

# 3.2. Add with parameters
memory.add(
    content={"type": "insight", "text": "Pattern detected in data"},
    level=MemoryLevel.WORKING,      # WORKING or LONG_TERM
    priority=5,                     # 0-10 (higher = more important)
    tags={"insight", "data"},       # Tags for search
    ttl=7200.0,                     # Custom TTL (2 hours)
    metadata={"source": "analysis", "confidence": 0.95},
)

# 3.3. Add directly into long-term
memory.add(
    content="Critical finding: correlation coefficient = 0.87",
    level=MemoryLevel.LONG_TERM,
    priority=10,
    tags={"critical", "finding"},
)

# 4. Retrieve entries
# 4.1. Get recent messages
messages = memory.get_messages(limit=5)
for msg in messages:
    print(f"{msg['role']}: {msg['content']}")

# 4.2. Get from working memory
working_entries = memory.get(level=MemoryLevel.WORKING, limit=10)
for entry in working_entries:
    print(f"[{entry.priority}] {entry.content}")

# 4.3. Get from long-term memory
longterm_entries = memory.get(level=MemoryLevel.LONG_TERM)

# 4.4. Search by tags
insights = memory.search_by_tags({"insight"}, level=MemoryLevel.WORKING)
critical = memory.search_by_tags({"critical"}, level=MemoryLevel.LONG_TERM)

# 4.5. Get all entries
all_entries = memory.get_all()

# 5. Memory management
# 5.1. Remove an entry
memory.remove(entry_key)

# 5.2. Clear a level
memory.clear(level=MemoryLevel.WORKING)

# 5.3. Force compression
memory.compress(level=MemoryLevel.WORKING)

# 5.4. Promote an entry to long-term
memory.promote(entry_key)

# 5.5. Update an entry
memory.update(entry_key, new_content={"updated": "data"})

# 6. Stats
stats = memory.get_stats()
print(f"Working: {stats['working_count']}/{stats['working_max']}")
print(f"Long-term: {stats['longterm_count']}/{stats['longterm_max']}")
print(f"Total accesses: {stats['total_accesses']}")
print(f"Promotions: {stats['promotion_count']}")
```

---

#### SharedMemoryPool — memory sharing between agents

```python
from utils.memory import SharedMemoryPool

# 1. Create a pool
pool = SharedMemoryPool(max_shared_entries=1000)

# 2. Register agents
memory_a = AgentMemory("agent_a", config)
memory_b = AgentMemory("agent_b", config)
memory_c = AgentMemory("agent_c", config)

pool.register(memory_a)
pool.register(memory_b)
pool.register(memory_c)

# 3. Broadcast — send to everyone
pool.broadcast(
    from_agent="agent_a",
    entry={
        "content": "Important discovery: X correlates with Y",
        "priority": 8,
        "tags": {"discovery", "shared"},
    },
)

# All agents will receive this entry in working memory

# 4. Share — send to specific agents
pool.share(
    from_agent="agent_a",
    entry={"content": "Secret info", "priority": 9},
    to_agents=["agent_b", "agent_c"],
)

# Only agent_b and agent_c receive the entry

# 5. Query — request information from the pool
results = pool.query(
    tags={"discovery"},
    min_priority=5,
    limit=10,
)

for result in results:
    print(f"From {result['source_agent']}: {result['content']}")

# 6. Subscribe to updates (callback)
def on_shared_entry(entry, from_agent, to_agents):
    print(f"{from_agent} shared: {entry['content']}")

pool.subscribe("agent_b", on_shared_entry)

# 7. Remove from the pool
pool.unregister("agent_c")

# 8. Clear the pool
pool.clear()
```

---

#### Memory compression

```python
from utils.memory import (
    TruncateCompressor,
    SummaryCompressor,
)

# 1. Truncate — simple removal of old entries
compressor = TruncateCompressor(keep_ratio=0.5)  # Keep 50%

memory = AgentMemory("agent", config)
memory.set_compressor(compressor)

# When over the limit, 50% of old entries are removed automatically

# 2. Summary — summarization using an LLM
def summarize_llm(entries: list[MemoryEntry]) -> str:
    texts = [e.content for e in entries]
    combined = "\n".join(texts)
    return my_llm(f"Summarize these entries: {combined}")

compressor = SummaryCompressor(
    summarizer=summarize_llm,
    chunk_size=10,  # Summarize in chunks of 10 entries
)

memory.set_compressor(compressor)

# On compression, 10 entries are replaced with 1 summarized entry

# 3. Custom compressor
from utils.memory import MemoryCompressor

class SmartCompressor(MemoryCompressor):
    def compress(self, entries: list[MemoryEntry], target_count: int) -> list[MemoryEntry]:
        # Remove low-priority and old entries
        sorted_entries = sorted(
            entries,
            key=lambda e: (e.priority, e.timestamp),
            reverse=True,
        )
        return sorted_entries[:target_count]

memory.set_compressor(SmartCompressor())
```

---

#### Integrating memory with the Runner

```python
from execution import MACPRunner, RunnerConfig

# 1. Configuration with memory enabled
config = RunnerConfig(
    enable_memory=True,
    memory_config=MemoryConfig(
        working_max_entries=20,
        long_term_max_entries=100,
        auto_compress=True,
        promote_after_accesses=3,
    ),
    memory_context_limit=5,      # How many entries to inject into the prompt
    enable_shared_memory=True,   # Enable SharedMemoryPool
)

runner = MACPRunner(llm_caller=my_llm, config=config)

# 2. Run — memory is updated automatically
result1 = runner.run_round(graph)

# 3. Access an agent’s memory
memory = runner.get_agent_memory("researcher")

entries = memory.get_messages(limit=10)
print(f"Researcher memory: {entries}")

# 4. Manually add to memory
runner.add_to_memory(
    "researcher",
    content="External knowledge: XYZ",
    level=MemoryLevel.LONG_TERM,
    priority=8,
)

# 5. Second round — agents retain context
graph.query = "Continue analysis from previous round"
result2 = runner.run_round(graph)

# 6. Export memories
memory_export = runner.export_memories()
# {
#   "agent_a": {"working": [...], "long_term": [...]},
#   "agent_b": {"working": [...], "long_term": [...]},
# }

# 7. Import memories (restore state)
runner.import_memories(memory_export)

# 8. Clear memory for all agents
runner.clear_all_memories()
```

---

#### Advanced usage: Semantic memory search

```python
from utils.memory import SemanticMemoryIndex
from core import NodeEncoder

# 1. Create a semantic index
encoder = NodeEncoder(model_name="sentence-transformers/all-MiniLM-L6-v2")

semantic_index = SemanticMemoryIndex(encoder)

# 2. Add entries to the index
memory = AgentMemory("agent", config)

for entry in memory.get_all():
    semantic_index.add(entry.key, entry.content, entry.tags)

# 3. Semantic search
query = "findings about correlation"
results = semantic_index.search(
    query,
    top_k=5,
    min_similarity=0.7,
    filter_tags={"finding"},
)

for result in results:
    print(f"[{result['similarity']:.3f}] {result['content']}")

# 4. Integration with AgentMemory
memory.enable_semantic_search(encoder)

# Now you can search semantically
results = memory.semantic_search(
    query="data patterns",
    top_k=3,
    level=MemoryLevel.LONG_TERM,
)
```

---

#### Practical example: Multi-round conversation with memory

```python
# Create a graph with memory
agents = [
    AgentProfile(agent_id="analyzer", display_name="Data Analyzer"),
    AgentProfile(agent_id="reporter", display_name="Report Writer"),
]

graph = build_property_graph(
    agents,
    workflow_edges=[("analyzer", "reporter")],
    query="Analyze dataset.csv",
)

# Memory-enabled configuration
config = RunnerConfig(
    enable_memory=True,
    memory_config=MemoryConfig(
        working_max_entries=15,
        long_term_max_entries=50,
        auto_compress=True,
        promote_after_accesses=2,
    ),
    memory_context_limit=5,
    enable_shared_memory=True,
)

runner = MACPRunner(llm_caller=my_llm, config=config)

# Round 1: Initial analysis
graph.query = "Analyze the dataset and find key patterns"
result1 = runner.run_round(graph)

print(f"Round 1 answer: {result1.final_answer}")

# Analyzer saved findings to memory
analyzer_memory = runner.get_agent_memory("analyzer")
print(f"Analyzer memory entries: {len(analyzer_memory.get_all())}")

# Round 2: Deeper analysis (agents remember the previous round)
graph.query = "Based on previous findings, analyze correlations"
result2 = runner.run_round(graph)

print(f"Round 2 answer: {result2.final_answer}")

# Round 3: Report generation
graph.query = "Generate final report summarizing all findings"
result3 = runner.run_round(graph)

print(f"Round 3 answer: {result3.final_answer}")

# Reporter used accumulated memory for a complete report
reporter_memory = runner.get_agent_memory("reporter")

# Export full history
history = {
    "round_1": result1.to_dict(),
    "round_2": result2.to_dict(),
    "round_3": result3.to_dict(),
    "memories": runner.export_memories(),
}

import json
with open("conversation_history.json", "w") as f:
    json.dump(history, f, indent=2)
```

---

### Streaming API

LangGraph-like streaming for real-time output.

```python
from execution import (
    MACPRunner,
    StreamEventType,
    StreamBuffer,
    format_event,
    print_stream,
)

runner = MACPRunner(llm_caller=my_llm)

# Synchronous streaming
for event in runner.stream(graph):
    if event.event_type == StreamEventType.AGENT_OUTPUT:
        print(f"{event.agent_id}: {event.content}")
    elif event.event_type == StreamEventType.TOKEN:
        print(event.token, end="", flush=True)

# Asynchronous streaming
async for event in runner.astream(graph):
    print(format_event(event))

# Using a buffer
buffer = StreamBuffer()
for event in runner.stream(graph):
    buffer.add(event)
    # ... handle the event

print(f"Final answer: {buffer.final_answer}")
print(f"Agent outputs: {buffer.agent_outputs}")

# Convenience printing
answer = print_stream(runner.stream(graph), show_tokens=True)
```

#### Event types (full specification)

```python
from execution.streaming import StreamEventType, StreamEvent

# === Execution lifecycle ===
StreamEventType.RUN_START
# Fields: run_id, query, num_agents, config

StreamEventType.RUN_END
# Fields: run_id, success, total_time, total_tokens, execution_order, final_answer

# === Agent events ===
StreamEventType.AGENT_START
# Fields: agent_id, step_index, predecessors, prompt_preview

StreamEventType.AGENT_OUTPUT
# Fields: agent_id, step_index, content, tokens_used, latency_ms

StreamEventType.AGENT_ERROR
# Fields: agent_id, step_index, error_type, error_message, will_retry

# === Token streaming ===
StreamEventType.TOKEN
# Fields: agent_id, token (str), token_index

# === Adaptive execution ===
StreamEventType.TOPOLOGY_CHANGED
# Fields: reason, old_plan, new_plan, remaining_steps

StreamEventType.PRUNE
# Fields: agent_id, reason (low_weight/low_probability/budget/quality)

StreamEventType.FALLBACK
# Fields: original_agent, fallback_agent, reason, attempt

# === Parallel execution ===
StreamEventType.PARALLEL_START
# Fields: group_agents (list), group_index

StreamEventType.PARALLEL_END
# Fields: group_agents, completed_count, failed_count, duration_ms

# === Budget ===
StreamEventType.BUDGET_WARNING
# Fields: budget_type (tokens/requests/time), current, limit, ratio

StreamEventType.BUDGET_EXCEEDED
# Fields: budget_type, current, limit, action_taken

# === Memory ===
StreamEventType.MEMORY_WRITE
# Fields: agent_id, memory_level (working/long_term), entry_key

StreamEventType.MEMORY_READ
# Fields: agent_id, memory_level, entry_key, found

StreamEventType.MEMORY_PROMOTED
# Fields: agent_id, entry_key, from_level, to_level

# === Metrics ===
StreamEventType.METRICS_UPDATE
# Fields: agent_id, metrics (dict with reliability, latency, quality, cost)

# Example: handling all event types
for event in runner.stream(graph):
    match event.event_type:
        case StreamEventType.RUN_START:
            print(f"Starting run {event.run_id} with {event.num_agents} agents")

        case StreamEventType.AGENT_START:
            print(f"Agent {event.agent_id} starting (step {event.step_index})")

        case StreamEventType.AGENT_OUTPUT:
            print(f"Agent {event.agent_id}: {event.content[:100]}...")
            print(f"  Tokens: {event.tokens_used}, Latency: {event.latency_ms}ms")

        case StreamEventType.TOKEN:
            print(event.token, end="", flush=True)

        case StreamEventType.TOPOLOGY_CHANGED:
            print(f"⟳ Topology changed: {event.reason}")
            print(f"  New plan: {event.new_plan}")

        case StreamEventType.PRUNE:
            print(f"✂ Pruned {event.agent_id}: {event.reason}")

        case StreamEventType.FALLBACK:
            print(f"⤷ Fallback: {event.original_agent} → {event.fallback_agent}")

        case StreamEventType.PARALLEL_START:
            print(f"⫸ Starting parallel group: {event.group_agents}")

        case StreamEventType.PARALLEL_END:
            print(f"⫷ Parallel group done: {event.completed_count}/{len(event.group_agents)}")

        case StreamEventType.BUDGET_WARNING:
            print(f"⚠ Budget warning: {event.budget_type} at {event.ratio:.1%}")

        case StreamEventType.BUDGET_EXCEEDED:
            print(f"❌ Budget exceeded: {event.budget_type}")

        case StreamEventType.RUN_END:
            print(f"✓ Execution completed in {event.total_time:.2f}s")
            print(f"  Total tokens: {event.total_tokens}")
            print(f"  Final answer: {event.final_answer[:100]}...")
```

---

## Advanced Features

### Execution optimization and token savings

The framework provides several mechanisms to optimize execution and reduce token usage:

#### 1. Filtering isolated nodes

Automatically exclude nodes that are not on the path from start to end:

```python
# Set execution bounds
graph.set_execution_bounds("input", "output")

# Filter isolated nodes during execution
result = runner.run_round(
    graph,
    filter_unreachable=True  # Exclude nodes not on the input->output path
)

# Nodes unrelated to the input->output path will not be executed
print(f"Agents excluded: {len(result.pruned_agents or [])}")
```

**Example:**

```python
builder = GraphBuilder()
builder.add_agent("a1")
builder.add_agent("a2")
builder.add_agent("a3")
builder.add_agent("isolated")  # Not connected to a1->a3

builder.add_workflow_edge("a1", "a2")
builder.add_workflow_edge("a2", "a3")
builder.set_execution_bounds("a1", "a3")

graph = builder.build()

# Reachability analysis
relevant = graph.get_relevant_nodes()    # {"a1", "a2", "a3"}
isolated = graph.get_isolated_nodes()    # {"isolated"}

result = runner.run_round(graph, filter_unreachable=True)
# "isolated" will not run → token savings
```

#### 2. Node deactivation (Disabled Nodes)

Temporarily deactivate nodes without removing them from the graph:

```python
# Deactivate based on metrics/RL
if quality_score < threshold:
    graph.disable("expensive_agent")

# Or multiple nodes
graph.disable(["agent1", "agent2"])

# Check
if graph.is_enabled("agent1"):
    ...

# Re-enable
graph.enable("agent1")
graph.enable()  # All

result = runner.run_round(graph)
# Deactivated nodes appear in result.pruned_agents
```

**Use case: RL control**

```python
# An RL agent decides which nodes to deactivate
for agent_id in graph.node_ids:
    rl_score = rl_model.predict(graph_state, agent_id)
    if rl_score < 0.3:
        graph.disable(agent_id)

result = runner.run_round(graph)
```

#### 3. Early stopping

Stop execution when a condition is met:

```python
from execution import EarlyStopCondition, RunnerConfig

# By keyword
stop1 = EarlyStopCondition.on_keyword("FINAL ANSWER")

# By token limit
stop2 = EarlyStopCondition.on_token_limit(5000)

# By number of agents
stop3 = EarlyStopCondition.on_agent_count(3)

# By metadata (for RL/metrics)
stop4 = EarlyStopCondition.on_metadata(
    "quality", 0.95,
    comparator=lambda v, t: v > t
)

# Custom logic
stop5 = EarlyStopCondition.on_custom(
    lambda ctx: my_evaluator.is_done(ctx.messages),
    reason="Evaluator decided task is done",
    min_agents_executed=2  # At least 2 agents before checking
)

# Combination (OR)
stop_any = EarlyStopCondition.combine_any([
    EarlyStopCondition.on_keyword("DONE"),
    EarlyStopCondition.on_token_limit(10000),
])

config = RunnerConfig(
    early_stop_conditions=[stop1, stop2, stop5]
)
runner = MACPRunner(llm_caller=my_llm, config=config)
result = runner.run_round(graph)

if result.early_stopped:
    print(f"Reason: {result.early_stop_reason}")
    saved = len(graph.node_ids) - len(result.execution_order)
    print(f"Agents saved: {saved}")
```

#### 4. Runtime topology (Topology Hooks)

Modify the graph **during execution** based on intermediate results:

```python
from execution import TopologyAction, StepContext

def adaptive_topology(ctx: StepContext, graph) -> TopologyAction:
    """Hook is called after each agent."""

    # ctx.agent_id — current agent
    # ctx.response — its response
    # ctx.messages — all responses
    # ctx.execution_order — execution order
    # ctx.remaining_agents — remaining agents
    # ctx.total_tokens — tokens used

    # Add an edge if review is needed
    if "uncertain" in (ctx.response or "").lower():
        return TopologyAction(
            add_edges=[(ctx.agent_id, "reviewer", 1.0)],
            trigger_rebuild=True
        )

    # Remove an edge
    if confident:
        return TopologyAction(
            remove_edges=[("agent1", "checker")]
        )

    # Skip agents
    if ctx.total_tokens > 8000:
        return TopologyAction(
            skip_agents=["expensive_agent"]
        )

    # Early stop
    if "DONE" in (ctx.response or ""):
        return TopologyAction(
            early_stop=True,
            early_stop_reason="Task completed"
        )

    return None

config = RunnerConfig(
    enable_dynamic_topology=True,
    topology_hooks=[adaptive_topology]
)
```

#### 5. Combined optimization

Use all mechanisms together for maximum optimization:

```python
from execution import (
    GraphBuilder, MACPRunner, RunnerConfig,
    EarlyStopCondition, TopologyAction, StepContext
)

# Build a graph
builder = GraphBuilder()
builder.add_agent("input")
builder.add_agent("solver")
builder.add_agent("checker")
builder.add_agent("expert")      # Expensive agent
builder.add_agent("formatter")
builder.add_agent("optional")    # Optional

builder.add_workflow_edge("input", "solver")
builder.add_workflow_edge("solver", "checker")
builder.add_workflow_edge("checker", "formatter")

# Set execution bounds
builder.set_execution_bounds("input", "formatter")

graph = builder.build()

# Disable optional nodes
graph.disable("optional")

# Adaptation hooks
def smart_topology(ctx: StepContext, graph) -> TopologyAction:
    # If solver is confident — skip checker
    if ctx.agent_id == "solver" and ctx.metadata.get("confidence", 0) > 0.95:
        return TopologyAction(skip_agents=["checker"])

    # If checker found an issue — add expert
    if ctx.agent_id == "checker" and "ERROR" in (ctx.response or ""):
        return TopologyAction(
            add_edges=[("checker", "expert", 1.0), ("expert", "formatter", 1.0)],
            trigger_rebuild=True
        )

    return None

# Configure runner with optimization
config = RunnerConfig(
    adaptive=True,
    enable_dynamic_topology=True,
    topology_hooks=[smart_topology],
    early_stop_conditions=[
        EarlyStopCondition.on_keyword("FINAL_ANSWER"),
        EarlyStopCondition.on_token_limit(10000),
    ],
    pruning_config=PruningConfig(token_budget=15000),
)

runner = MACPRunner(llm_caller=my_llm, config=config)
result = runner.run_round(
    graph,
    filter_unreachable=True  # Exclude isolated nodes
)

# Optimization analysis
print(f"Agents executed: {len(result.execution_order)}")
print(f"Pruned: {len(result.pruned_agents or [])}")
print(f"Early stopped: {result.early_stopped}")
print(f"Modifications: {result.topology_modifications}")
print(f"Tokens: {result.total_tokens}")
```

---

### Multi-Model Support (Multi-Model Support)

Each agent in the graph can use its own LLM model with individual settings. This makes it possible to:
- **Optimize costs** — use expensive models only for complex tasks
- **Balance performance** — fast models for simple operations
- **Specialize agents** — models trained for specific domains
- **Hybrid solutions** — combine cloud and local models

#### Multi-model architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         TASK NODE                             │
│                    "Analyze the market"                      │
└────────────────┬────────────────────────────────────────────┘
                 │
        ┌────────┴────────┐
        ▼                 ▼
┌───────────────┐   ┌───────────────┐
│   ANALYST     │   │  COORDINATOR  │
│               │──▶│               │
│ GPT-4         │   │ GPT-4o-mini   │
│ temp: 0.0     │   │ temp: 0.3     │
│ tokens: 4000  │   │ tokens: 1000  │
└───────────────┘   └───────────────┘
```

---

#### Key components

**1. LLMConfig** — an agent’s LLM configuration

```python
from core.schema import LLMConfig

llm_config = LLMConfig(
    model_name="gpt-4",                    # Model name
    base_url="https://api.openai.com/v1",  # API endpoint
    api_key="$OPENAI_API_KEY",             # Key (or $ENV_VAR)
    max_tokens=2000,                       # Max tokens in the response
    temperature=0.7,                       # Generation temperature
    timeout=60.0,                          # Request timeout
    top_p=0.9,                             # Nucleus sampling
    stop_sequences=["END"],                # Stop sequences
)

# Validate configuration
if llm_config.is_configured():
    params = llm_config.to_generation_params()
    print(f"Generation params: {params}")

# Merge configurations (fallback)
default_config = LLMConfig(model_name="gpt-4o-mini", temperature=0.5)
final_config = llm_config.merge_with(default_config)
```

**2. AgentLLMConfig** — an immutable configuration for AgentProfile

```python
from core.agent import AgentLLMConfig

agent_llm_config = AgentLLMConfig(
    model_name="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="sk-...",
    temperature=0.7,
    max_tokens=2000,
)

# Convert to LLMConfig
llm_config = agent_llm_config.to_llm_config()
```

**3. LLMCallerFactory** — a factory for creating LLM callers

```python
from execution import LLMCallerFactory

# Create a factory for OpenAI-compatible APIs
factory = LLMCallerFactory.create_openai_factory(
    default_model="gpt-4o-mini",
    default_base_url="https://api.openai.com/v1",
    default_api_key="sk-...",
    default_temperature=0.7,
    default_max_tokens=2000,
)

# The factory automatically creates callers based on AgentLLMConfig
# when used with MACPRunner
```

**4. Caller factory helpers**

Three ready-made functions cover the most common setups:

| Function | Interface | Use with |
|---|---|---|
| `create_openai_caller()` | `(str) -> str` | Legacy `llm_caller` |
| `create_openai_structured_caller()` | `(list[dict]) -> str` | `structured_llm_caller` ✅ **recommended** |
| `create_openai_async_structured_caller()` | `async (list[dict]) -> str` | `async_structured_llm_caller` ✅ parallel |

```python
from execution import (
    create_openai_caller,
    create_openai_structured_caller,
    create_openai_async_structured_caller,
)

# ── Legacy flat-string caller ────────────────────────────────────────────────
caller = create_openai_caller(
    model="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="sk-...",
    temperature=0.7,
    max_tokens=2000,
)
response = caller("What is 2+2?")  # (str) -> str

# ── Structured sync caller (recommended for chat LLMs) ──────────────────────
sync_caller = create_openai_structured_caller(
    api_key="sk-...",
    model="gpt-4o",
    temperature=0.7,
    max_tokens=1024,
)
# Use as: MACPRunner(structured_llm_caller=sync_caller)

# ── Structured async caller (required for parallel astream) ─────────────────
async_caller = create_openai_async_structured_caller(
    api_key="sk-...",
    model="gpt-4o",
    temperature=0.7,
    max_tokens=1024,
)
# Use as: MACPRunner(async_structured_llm_caller=async_caller)

# ── Full parallel setup ──────────────────────────────────────────────────────
from execution import MACPRunner, RunnerConfig

runner = MACPRunner(
    structured_llm_caller=sync_caller,
    async_structured_llm_caller=async_caller,
    config=RunnerConfig(enable_parallel=True),
)

# Sequential graphs → stream() uses sync_caller
for event in runner.stream(graph):
    ...

# Parallel graphs → astream() uses async_caller for concurrent groups
import asyncio
async def run():
    async for event in runner.astream(graph):
        ...
asyncio.run(run())
```

---

#### Ways to configure multi-model support

##### Method 1: Via GraphBuilder (recommended)

```python
from builder import GraphBuilder
from execution import MACPRunner, LLMCallerFactory

builder = GraphBuilder()

# Agent 1: strong model for analysis
builder.add_agent(
    agent_id="analyst",
    display_name="Senior Analyst",
    persona="Expert data analyst with deep domain knowledge",
    llm_backbone="gpt-4",               # Or model_name
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.0,                    # Strict analysis
    max_tokens=4000,
    timeout=120.0,
)

# Agent 2: weaker model for formatting
builder.add_agent(
    agent_id="formatter",
    display_name="Report Formatter",
    persona="Formats data into readable reports",
    llm_backbone="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.3,
    max_tokens=1000,
    timeout=30.0,
)

# Agent 3: local model for confidential data
builder.add_agent(
    agent_id="privacy_checker",
    display_name="Privacy Checker",
    llm_backbone="llama3:70b",
    base_url="http://localhost:11434/v1",  # Ollama
    api_key="not-needed",
    temperature=0.1,
    max_tokens=500,
)

builder.add_workflow_edge("analyst", "formatter")
builder.add_workflow_edge("analyst", "privacy_checker")

graph = builder.build()

# The factory will automatically create callers for each agent
factory = LLMCallerFactory.create_openai_factory()

runner = MACPRunner(llm_factory=factory)
result = runner.run_round(graph)

print(f"Final answer: {result.final_answer}")
```

##### Method 2: Explicit LLMConfig

```python
from core.schema import LLMConfig

# Predefined configurations
gpt4_config = LLMConfig(
    model_name="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.7,
    max_tokens=2000,
)

gpt4_mini_config = LLMConfig(
    model_name="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.5,
    max_tokens=1000,
)

builder = GraphBuilder()
builder.add_agent(
    "researcher",
    display_name="Researcher",
    llm_config=gpt4_config,  # Pass a ready configuration
)
builder.add_agent(
    "writer",
    display_name="Writer",
    llm_config=gpt4_mini_config,
)

graph = builder.build()
```

##### Method 3: llm_callers dictionary

```python
from execution import create_openai_caller

# Create callers manually
callers = {
    "analyst": create_openai_caller(
        model="gpt-4",
        temperature=0.0,
        max_tokens=4000,
    ),
    "formatter": create_openai_caller(
        model="gpt-4o-mini",
        temperature=0.3,
        max_tokens=1000,
    ),
    "privacy_checker": create_openai_caller(
        model="llama3:70b",
        base_url="http://localhost:11434/v1",
        api_key="not-needed",
    ),
}

# Pass directly into the runner
runner = MACPRunner(llm_callers=callers)
result = runner.run_round(graph)
```

##### Method 4: Combined approach

```python
# Use the factory as default, but override for some agents
factory = LLMCallerFactory.create_openai_factory(
    default_model="gpt-4o-mini",  # Default
)

# Create a custom caller for a specific agent
specialized_caller = create_openai_caller(
    model="gpt-4",
    temperature=0.0,
    max_tokens=4000,
)

runner = MACPRunner(
    llm_factory=factory,                         # For all agents
    llm_callers={"analyst": specialized_caller}, # Override for analyst
)
```

---

#### LLM caller resolution priority

```
1. llm_callers[agent_id]       ← Explicitly provided caller
        ↓
2. llm_factory.get_caller()    ← Factory creates based on agent.llm_config
        ↓
3. llm_caller                  ← Default caller for all agents
        ↓
4. Exception                   ← Error: no caller specified
```

---

#### Usage examples

##### Example 1: Cost optimization

```python
# Cheap model for routine operations, expensive one for complex tasks

builder = GraphBuilder()

# 5 simple analysts (cheap model)
for i in range(5):
    builder.add_agent(
        f"analyst_{i}",
        display_name=f"Junior Analyst {i}",
        llm_backbone="gpt-4o-mini",
        temperature=0.3,
        max_tokens=500,
    )
    builder.add_workflow_edge(f"analyst_{i}", "senior")

# 1 senior analyst (expensive model)
builder.add_agent(
    "senior",
    display_name="Senior Analyst",
    llm_backbone="gpt-4",
    temperature=0.7,
    max_tokens=4000,
)

graph = builder.build()

# Savings: ~80% of tokens use the cheap model
```

##### Example 2: Hybrid solution (cloud + local model)

```python
builder = GraphBuilder()

# Public data → cloud model
builder.add_agent(
    "public_analyzer",
    llm_backbone="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
)

# Confidential data → local model
builder.add_agent(
    "private_analyzer",
    llm_backbone="llama3:70b",
    base_url="http://localhost:11434/v1",
    api_key="not-needed",
)

# Aggregator → cheap cloud model
builder.add_agent(
    "aggregator",
    llm_backbone="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
)

builder.add_workflow_edge("public_analyzer", "aggregator")
builder.add_workflow_edge("private_analyzer", "aggregator")

graph = builder.build()
```

##### Example 3: Specialized models

```python
builder = GraphBuilder()

# Medical expert → a model trained on medical data
builder.add_agent(
    "medical_expert",
    llm_backbone="medical-llm-v2",
    base_url="https://medical-api.example.com/v1",
    api_key="$MEDICAL_API_KEY",
    temperature=0.0,  # Strict medical recommendations
)

# Legal expert → a model trained on legal texts
builder.add_agent(
    "legal_expert",
    llm_backbone="legal-llm-v3",
    base_url="https://legal-api.example.com/v1",
    api_key="$LEGAL_API_KEY",
    temperature=0.0,
)

# Coordinator → general model
builder.add_agent(
    "coordinator",
    llm_backbone="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.5,
)

builder.add_workflow_edge("medical_expert", "coordinator")
builder.add_workflow_edge("legal_expert", "coordinator")

graph = builder.build()
```

##### Example 4: Different temperatures for different styles

```python
builder = GraphBuilder()

# Creative writer (high temperature)
builder.add_agent(
    "creative_writer",
    llm_backbone="gpt-4",
    temperature=0.9,  # Creativity
    max_tokens=2000,
)

# Strict editor (low temperature)
builder.add_agent(
    "strict_editor",
    llm_backbone="gpt-4",
    temperature=0.1,  # Precision
    max_tokens=1500,
)

# Final formatter (medium temperature)
builder.add_agent(
    "formatter",
    llm_backbone="gpt-4o-mini",
    temperature=0.5,  # Balance
    max_tokens=1000,
)

builder.add_workflow_edge("creative_writer", "strict_editor")
builder.add_workflow_edge("strict_editor", "formatter")

graph = builder.build()
```

---

#### Supported providers

The framework supports **any OpenAI-compatible API**:

| Provider | Base URL | Notes |
|----------|----------|-------|
| **OpenAI** | `https://api.openai.com/v1` | GPT-4, GPT-4o-mini, GPT-3.5-turbo |
| **Anthropic** | via wrapper | Claude (requires an adapter) |
| **Ollama** | `http://localhost:11434/v1` | Local models (llama3, mistral, etc.) |
| **vLLM** | custom | Self-hosted models |
| **LiteLLM** | custom | Unified API for all providers |
| **Azure OpenAI** | `https://<resource>.openai.azure.com/` | Azure-hosted models |
| **GigaChat** | custom | Sber models |
| **Cloudflare Tunnels** | custom | Via Cloudflare tunnels |

```python
# Examples for different providers

# OpenAI
builder.add_agent("agent1", llm_backbone="gpt-4",
                  base_url="https://api.openai.com/v1")

# Ollama (local)
builder.add_agent("agent2", llm_backbone="llama3:70b",
                  base_url="http://localhost:11434/v1")

# Azure OpenAI
builder.add_agent("agent3", llm_backbone="gpt-4",
                  base_url="https://myresource.openai.azure.com/")

# GigaChat
builder.add_agent("agent4", llm_backbone="GigaChat-Lightning",
                  base_url="https://gigachat-api.trycloudflare.com/v1")

# vLLM
builder.add_agent("agent5", llm_backbone="./models/Qwen3-80B",
                  base_url="https://my-vllm-server.com/v1")
```

---

#### Async and streaming support

```python
from execution import create_openai_caller

# Async caller per agent
async_callers = {
    "agent1": create_openai_caller(model="gpt-4", is_async=True),
    "agent2": create_openai_caller(model="gpt-4o-mini", is_async=True),
}

runner = MACPRunner(async_llm_callers=async_callers)
result = await runner.arun_round(graph)

# Streaming callers
streaming_callers = {
    "agent1": create_openai_caller(model="gpt-4", is_streaming=True),
    "agent2": create_openai_caller(model="gpt-4o-mini", is_streaming=True),
}

runner = MACPRunner(streaming_llm_callers=streaming_callers)

for event in runner.stream(graph):
    if event.event_type == StreamEventType.TOKEN:
        print(f"[{event.agent_id}] {event.token}", end="")
```

---

#### API key handling

```python
# 1. Direct
builder.add_agent("agent", api_key="sk-...")

# 2. From an environment variable (recommended)
builder.add_agent("agent", api_key="$OPENAI_API_KEY")

# When parsing, it is automatically resolved as os.getenv("OPENAI_API_KEY")

# 3. From a file
import os
os.environ["OPENAI_API_KEY"] = open("keys/openai.key").read().strip()
builder.add_agent("agent", api_key="$OPENAI_API_KEY")
```

---

#### Monitoring multi-model execution

```python
from core.metrics import MetricsTracker

tracker = MetricsTracker()

runner = MACPRunner(
    llm_factory=factory,
    metrics_tracker=tracker,
)

result = runner.run_round(graph)

# Per-model analysis
for agent_id in graph.node_ids:
    agent = graph.get_agent_by_id(agent_id)
    model = agent.llm_config.model_name if agent.llm_config else "default"

    metrics = tracker.get_node_metrics(agent_id)

    print(f"\n{agent_id} ({model}):")
    print(f"  Latency: {metrics.avg_latency_ms:.0f}ms")
    print(f"  Tokens: {metrics.total_cost_tokens}")
    print(f"  Reliability: {metrics.reliability:.2%}")
```

---

#### Backward compatibility

Old code **continues to work** without changes:

```python
# Old approach (one LLM for all agents)
runner = MACPRunner(llm_caller=my_llm)
result = runner.run_round(graph)
# ✅ Works as before

# New approach (multi-model)
runner = MACPRunner(llm_factory=factory)
result = runner.run_round(graph)
# ✅ Uses per-agent models
```

---

### Structured Prompt — modern chat LLMs (recommended)

> **TL;DR** — if you use OpenAI, GigaChat, Anthropic, or any other
> chat-completion API, pass `structured_llm_caller` instead of the
> legacy `llm_caller`. The runner will send proper `system` / `user`
> roles to the LLM instead of one flat string. This produces shorter,
> more focused responses and saves tokens — especially in long agent chains.

#### The problem with the legacy `llm_caller`

The classic `llm_caller: Callable[[str], str]` interface passes the entire
prompt as a **single flat string**, combining persona, description, task and
messages from other agents:

```
"You are a mathematician.\n\nSolve step by step.\n\nTask: ...\n\nMessages from other agents:\n..."
```

Modern chat LLMs (OpenAI GPT-4, GigaChat, Claude, Gemini…) expect messages
to be split into **roles** (`system`, `user`, `assistant`). When everything
arrives in one blob the model has to re-parse it, which leads to:

- 🔴 **Verbose, padded responses** — the model does not know how strictly to
  follow the system instruction
- 🔴 **Token accumulation** — long chains accumulate more and more context
- 🔴 **Lower instruction-following quality** — especially for role-specific behaviour

#### The fix: `structured_llm_caller`

`MACPRunner` now supports a second caller interface that receives a
`list[dict[str, str]]` — exactly what the OpenAI chat completions API expects:

The full message list produced by `_build_prompt` is:

```python
[
    # 1. system — persona, description, tools hint, output_schema instruction
    {"role": "system",    "content": "You are a mathematician. Solve step by step.\n\nAvailable tools: calculator.\n\nRespond with JSON matching: {\"type\":\"object\",...}"},

    # 2..N-1. agent.state — previous conversation turns replayed with correct roles
    {"role": "assistant", "content": "Previous answer turn 1…"},
    {"role": "user",      "content": "Follow-up question turn 2…"},
    # … (as many entries as agent.state contains)

    # N. user — current task, input_schema hint, memory context, incoming agent messages
    {"role": "user",      "content": "Task: 3x² - 7x + 2 = 0\n\nInput format: {...}\n\nMessages from other agents: ..."},
]
```

The runner builds this automatically inside `_build_prompt` → `StructuredPrompt`
and dispatches via `_call_llm`. No parsing, no heuristics, no hacks.

---

#### How it works internally

```
_build_prompt()
    │
    └─► StructuredPrompt
            ├── .text     →  flat string  (used by legacy llm_caller)
            └── .messages →  list[dict]   (used by structured_llm_caller)

MACPRunner._call_llm(caller, prompt)
    ├── if structured_llm_caller is set → calls structured_llm_caller(prompt.messages)
    └── else                            → calls caller(prompt.text)    # backward compat
```

Both representations are always built — switching between interfaces
requires **zero changes** to graph/agent code.

> **What goes where in `messages`:**
>
> | Source field | Role | Note |
> |---|---|---|
> | `persona` + `description` | `system` | Always first message |
> | tool names (`has_tools()`) | `system` | Appended to system content |
> | `output_schema` | `system` | `"Respond with JSON matching: …"` |
> | `agent.state` entries | `assistant`/`user` | Replayed in order between system and final user |
> | query + `input_schema` + memory + incoming msgs | `user` | Always last message |

---

#### Built-in factory helpers (recommended, zero boilerplate)

The framework ships ready-made factory functions so you don't need to write
any boilerplate caller code yourself:

```python
from execution import (
    MACPRunner,
    RunnerConfig,
    create_openai_structured_caller,        # sync  — for stream() / run_round()
    create_openai_async_structured_caller,  # async — for astream() / arun_round()
)

# ── Sequential graphs (chains, single agent) ────────────────────────────────
runner = MACPRunner(
    structured_llm_caller=create_openai_structured_caller(
        api_key="sk-...",
        base_url="https://api.openai.com/v1",
        model="gpt-4o",
        temperature=0.7,
        max_tokens=1024,
    ),
)

for event in runner.stream(graph):
    ...

# ── Parallel graphs (fan-in, fan-out) ──────────────────────────────────────
runner = MACPRunner(
    structured_llm_caller=create_openai_structured_caller(
        api_key="sk-...", model="gpt-4o"
    ),
    async_structured_llm_caller=create_openai_async_structured_caller(
        api_key="sk-...", model="gpt-4o"
    ),
    config=RunnerConfig(enable_parallel=True),
)

async for event in runner.astream(graph):
    ...
```

> **Why two callers for parallel mode?**  `stream()` is synchronous and
> uses `structured_llm_caller`.  `astream()` with `enable_parallel=True`
> runs independent agents concurrently via `asyncio.gather` and therefore
> requires `async_structured_llm_caller`.  For purely sequential graphs
> only the sync caller is needed.

---

#### Quick start (manual caller)

If you need custom logic (retries, logging, token tracking), write the
caller yourself — the interface is a simple function:

```python
from openai import OpenAI
from execution import MACPRunner, RunnerConfig

client = OpenAI(api_key="sk-...")

def my_structured_caller(messages: list[dict[str, str]]) -> str:
    """Drop-in replacement for any str->str llm_caller."""
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,      # passed through as-is
        max_tokens=1024,
        temperature=0.7,
    )
    return resp.choices[0].message.content or ""

runner = MACPRunner(
    structured_llm_caller=my_structured_caller,
    config=RunnerConfig(timeout=60.0),
)
result = runner.run_round(graph)
print(result.final_answer)
```

#### Async variant (manual caller)

```python
import asyncio
from openai import AsyncOpenAI

aclient = AsyncOpenAI(api_key="sk-...")

async def my_async_structured_caller(messages: list[dict[str, str]]) -> str:
    resp = await aclient.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        max_tokens=1024,
    )
    return resp.choices[0].message.content or ""

runner = MACPRunner(async_structured_llm_caller=my_async_structured_caller)
result = await runner.arun_round(graph)
```

---

#### Tracking tokens (benchmark pattern)

When you need to count tokens across many agents (e.g. for benchmarks), wrap
the OpenAI client to intercept `usage`:

```python
from openai import OpenAI

class TrackedLLM:
    def __init__(self, api_key, base_url, model):
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self.total_tokens = 0
        self.call_count = 0

    def reset(self):
        self.total_tokens = 0
        self.call_count = 0

    def chat(self, system: str, user: str, max_tokens: int = 1024) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        resp = self._client.chat.completions.create(
            model=self._model, messages=messages,
            temperature=0.7, max_tokens=max_tokens,
        )
        self.total_tokens += resp.usage.total_tokens if resp.usage else 0
        self.call_count += 1
        return resp.choices[0].message.content or ""

    def as_structured_caller(self, max_tokens: int = 1024):
        """Return a structured_llm_caller for MACPRunner."""
        def _caller(messages: list[dict[str, str]]) -> str:
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            user   = next((m["content"] for m in messages if m["role"] == "user"),   "")
            return self.chat(system, user, max_tokens=max_tokens)
        return _caller

llm = TrackedLLM(api_key="...", base_url="...", model="gpt-4o")

runner = MACPRunner(
    structured_llm_caller=llm.as_structured_caller(max_tokens=1024),
)
result = runner.run_round(graph)
print(f"Tokens used: {llm.total_tokens}, calls: {llm.call_count}")
```

---

#### Caller priority

All caller types can coexist. The resolution priority is:

```
structured_llm_caller   ← Used for ALL plain agent calls when set
        │
        │  (automatic str→str wrapper also registered as llm_caller
        │   for internal checks — no code change needed)
        ▼
llm_callers[agent_id]   ← Per-agent override (always takes precedence)
        ▼
llm_factory             ← Factory by AgentLLMConfig
        ▼
llm_caller              ← Legacy default
```

You can mix `structured_llm_caller` (global default) with per-agent
`llm_callers` overrides — the structured caller will be used for all agents
that don't have an explicit override.

---

#### Providers comparison

| Provider | Recommended interface | Notes |
|---|---|---|
| **OpenAI** (GPT-4o, GPT-4, …) | `structured_llm_caller` ✅ | Native chat completions |
| **GigaChat / Sber** | `structured_llm_caller` ✅ | OpenAI-compatible API |
| **Anthropic Claude** | `structured_llm_caller` ✅ | Via adapter or LiteLLM |
| **Ollama** (local) | `structured_llm_caller` ✅ | OpenAI-compatible `/v1/chat/completions` |
| **vLLM** | `structured_llm_caller` ✅ | OpenAI-compatible server |
| **Azure OpenAI** | `structured_llm_caller` ✅ | Same API, different base URL |
| **Custom / non-chat API** | `llm_caller` (legacy) | Falls back to flat string |

---

#### Benchmark results (gMAS vs LangGraph)

The table below was measured with `examples/benchmark_vs_langgraph.py --runs 10`
using `structured_llm_caller`. LangGraph uses an equivalent explicit
`system` / `user` split on its side.

| Test topology | LangGraph time | gMAS time | Token Δ |
|---|---|---|---|
| Single agent (1) | baseline | ~+10% | ~+10% |
| Chain of 3 (3) | baseline | **−18 %** | **−11 %** |
| Fan-in 2→1 (3) | baseline | **−30 %** | **−22 %** |
| Chain of 7 (7) | baseline | **−10 %** | **−17 %** |
| Fan-out 1→3→1 (5) | baseline | **−19 %** | **−13 %** |

> Single-agent test is slightly slower in gMAS due to protocol overhead;
> this overhead amortises quickly as the number of agents grows.

---

#### Migration from `llm_caller` to `structured_llm_caller`

No changes to graph or agent code are required. Only the runner
instantiation changes:

```python
# Before (legacy)
runner = MACPRunner(llm_caller=lambda prompt: my_model(prompt))

# After (recommended)
runner = MACPRunner(
    structured_llm_caller=lambda messages: my_model_chat(messages)
)
```

Both interfaces are fully supported. The legacy `llm_caller` is not
deprecated and will not be removed.

---

### Dynamic Topology

#### Static graph modification

Modify the graph structure before execution:

```python
# Add a new agent
new_agent = AgentProfile(agent_id="expert", display_name="Expert")
graph.add_node(new_agent, connections_to=["checker"])

# Change connections
graph.add_edge("solver", "expert", weight=0.9)
graph.remove_edge("solver", "checker")

# Disable nodes (without deletion)
graph.disable("expensive_agent")  # Will not run, but remains in the graph

# Full topology update from a matrix
import torch

new_adjacency = torch.tensor([
    [0, 1, 0],
    [0, 0, 1],
    [0, 0, 0],
], dtype=torch.float32)

graph.update_communication(
    new_adjacency,
    s_tilde=score_matrix,       # Connection quality scores
    p_matrix=probability_matrix # Transition probabilities
)
```

#### Runtime modification (during execution)

A powerful feature for modifying the graph **during a round** based on intermediate results:

##### Early stopping (Early Stopping)

```python
from execution import EarlyStopCondition, RunnerConfig

# 1. By keyword in the response
stop_on_answer = EarlyStopCondition.on_keyword(
    "FINAL ANSWER",
    reason="Answer found"
)

# 2. By token limit
stop_on_tokens = EarlyStopCondition.on_token_limit(
    max_tokens=5000,
    reason="Token budget exceeded"
)

# 3. By number of executed agents
stop_on_count = EarlyStopCondition.on_agent_count(
    max_agents=5,
    reason="Sufficient agents executed"
)

# 4. By a metadata value (for RL, metrics)
stop_on_quality = EarlyStopCondition.on_metadata(
    "quality_score",
    0.95,
    comparator=lambda v, threshold: v > threshold,
    reason="Quality threshold reached"
)

# 5. Custom condition
stop_custom = EarlyStopCondition.on_custom(
    condition=lambda ctx: my_rl_agent.should_stop(ctx.messages),
    reason="RL agent decided to stop",
    min_agents_executed=2  # At least 2 agents before checking
)

# 6. Combine conditions (OR)
stop_any = EarlyStopCondition.combine_any([
    EarlyStopCondition.on_keyword("DONE"),
    EarlyStopCondition.on_token_limit(10000),
    stop_on_quality,
])

# 7. Combine conditions (AND)
stop_all = EarlyStopCondition.combine_all([
    EarlyStopCondition.on_keyword("answer"),
    stop_on_quality,
])

# Usage
config = RunnerConfig(
    early_stop_conditions=[stop_on_answer, stop_on_tokens]
)
runner = MACPRunner(llm_caller=my_llm, config=config)
result = runner.run_round(graph)

if result.early_stopped:
    print(f"Stopped: {result.early_stop_reason}")
    print(f"Saved: {len(graph.node_ids) - len(result.execution_order)} agents")
```

##### Topology Hooks (on-the-fly graph modification)

```python
from execution import TopologyAction, StepContext, RunnerConfig

def my_topology_hook(ctx: StepContext, graph) -> TopologyAction:
    """Called after each execution step.

    StepContext contains:
        - agent_id: current agent
        - response: its response
        - messages: all responses so far
        - execution_order: execution order
        - remaining_agents: remaining agents
        - total_tokens: tokens used
        - metadata: arbitrary data
    """

    # 1. Early stopping based on custom logic
    if "TASK_COMPLETE" in (ctx.response or ""):
        return TopologyAction(
            early_stop=True,
            early_stop_reason="Task marked as complete"
        )

    # 2. Add an edge if quality is low
    if ctx.metadata.get("quality", 1.0) < 0.5:
        return TopologyAction(
            add_edges=[
                (ctx.agent_id, "reviewer_agent", 1.0),
            ],
            trigger_rebuild=True  # Re-plan remaining steps
        )

    # 3. Remove an edge
    if some_condition:
        return TopologyAction(
            remove_edges=[
                ("agent1", "agent2"),
            ]
        )

    # 4. Skip upcoming agents
    if ctx.total_tokens > 8000:
        return TopologyAction(
            skip_agents=["expensive_agent1", "expensive_agent2"]
        )

    # 5. Force execution of agents
    if needs_expert_review:
        return TopologyAction(
            force_agents=["expert_reviewer"]
        )

    # 6. Change the final agent
    if early_finish:
        return TopologyAction(
            new_end_agent="quick_finalizer"
        )

    return None  # No changes

# Async hook for integration with RL, APIs, etc.
async def rl_topology_hook(ctx: StepContext, graph) -> TopologyAction:
    """Async hook for more complex logic."""
    # You can call async APIs, RL models, etc.
    decision = await my_rl_agent.get_topology_decision(
        messages=ctx.messages,
        graph_state=graph.to_dict()
    )

    if decision.add_connection:
        return TopologyAction(
            add_edges=[(decision.from_node, decision.to_node, decision.weight)]
        )

    return None

# Usage
config = RunnerConfig(
    enable_dynamic_topology=True,
    topology_hooks=[my_topology_hook],
    async_topology_hooks=[rl_topology_hook],
)

runner = MACPRunner(llm_caller=my_llm, config=config)
result = runner.run_round(graph)

print(f"Topology modifications: {result.topology_modifications}")
```

##### Example: RL-controlled topology

```python
import torch
from your_rl_agent import RLAgent

class TopologyRL:
    def __init__(self):
        self.rl_agent = RLAgent()

    def should_stop(self, ctx: StepContext) -> bool:
        """RL-agent decision for early stopping."""
        state = self.encode_state(ctx)
        action = self.rl_agent.predict(state)
        return action == "STOP"

    def get_topology_action(self, ctx: StepContext) -> TopologyAction | None:
        """RL agent decides how to change topology."""
        state = self.encode_state(ctx)
        action = self.rl_agent.predict(state)

        if action == "ADD_REVIEWER":
            return TopologyAction(
                add_edges=[(ctx.agent_id, "reviewer", 1.0)],
                trigger_rebuild=True
            )
        elif action == "SKIP_EXPENSIVE":
            return TopologyAction(
                skip_agents=["expensive_model"]
            )

        return None

    def encode_state(self, ctx: StepContext) -> torch.Tensor:
        # Encode state for RL
        return torch.tensor([
            len(ctx.messages),
            ctx.total_tokens,
            len(ctx.remaining_agents),
        ])

# Usage
rl_controller = TopologyRL()

config = RunnerConfig(
    enable_dynamic_topology=True,
    early_stop_conditions=[
        EarlyStopCondition.on_custom(
            rl_controller.should_stop,
            reason="RL decided to stop"
        )
    ],
    topology_hooks=[rl_controller.get_topology_action],
)
```

##### Full example: adaptive system

```python
from execution import (
    GraphBuilder, MACPRunner, RunnerConfig,
    EarlyStopCondition, TopologyAction, StepContext
)

# Build the graph
builder = GraphBuilder()
builder.add_agent("input", persona="Input processor")
builder.add_agent("solver", persona="Problem solver")
builder.add_agent("checker", persona="Solution checker")
builder.add_agent("expensive_expert", persona="Expert (expensive)")
builder.add_agent("output", persona="Output formatter")

builder.add_workflow_edge("input", "solver")
builder.add_workflow_edge("solver", "checker")
builder.add_workflow_edge("checker", "output")
# expensive_expert is connected dynamically

builder.set_start_node("input")
builder.set_end_node("output")
builder.add_task(query="Solve the complex problem")
builder.connect_task_to_agents()

graph = builder.build()

# Hooks for adaptation
def adaptive_hook(ctx: StepContext, graph) -> TopologyAction:
    # If checker found an issue — add expert
    if ctx.agent_id == "checker" and "ERROR" in (ctx.response or ""):
        return TopologyAction(
            add_edges=[("checker", "expensive_expert", 1.0),
                      ("expensive_expert", "output", 1.0)],
            trigger_rebuild=True
        )

    # If solver produced a good answer — skip checker
    if ctx.agent_id == "solver" and ctx.metadata.get("confidence", 0) > 0.95:
        return TopologyAction(
            skip_agents=["checker"],
            reason="High confidence, skipping validation"
        )

    return None

# Configure runner
config = RunnerConfig(
    adaptive=True,
    enable_dynamic_topology=True,
    topology_hooks=[adaptive_hook],
    early_stop_conditions=[
        EarlyStopCondition.on_keyword("FINAL_ANSWER"),
        EarlyStopCondition.on_token_limit(10000),
    ],
)

runner = MACPRunner(llm_caller=my_llm, config=config)
result = runner.run_round(
    graph,
    filter_unreachable=True  # Exclude isolated nodes
)

# Result
print(f"Executed: {result.execution_order}")
print(f"Early stopped: {result.early_stopped}")
print(f"Topology mods: {result.topology_modifications}")
print(f"Tokens saved: calculated from pruned_agents")
```

---

### GNN Routing (Graph Neural Networks for Routing)

Using graph neural networks for **learnable** optimal routing based on execution history.

#### Overview of GNN models

| Model | Description | When to use |
|------|-------------|-------------|
| **GCN** (Graph Convolutional Network) | Classic convolution for graphs | Homogeneous graphs, simple tasks |
| **GAT** (Graph Attention Network) | Uses an attention mechanism | Edge importance varies |
| **GraphSAGE** | Neighbor sampling for large graphs | Large graphs, inductive learning |
| **GIN** (Graph Isomorphism Network) | Maximally expressive architecture | Complex patterns, small graphs |

---

#### Full example: training a GNN router

```python
from core.gnn import (
    create_gnn_router,
    GNNTrainer,
    GNNRouterInference,
    GNNModelType,
    TrainingConfig,
    FeatureConfig,
    RoutingStrategy,
    DefaultFeatureGenerator,
)
from core.metrics import MetricsTracker
import torch
from torch_geometric.data import Data

# ========== STEP 1: Collect execution data ==========
tracker = MetricsTracker()

# Run multiple rounds to accumulate metrics
for i in range(100):
    result = runner.run_round(graph)

    # Record per-node metrics
    for agent_id in result.execution_order:
        response = result.messages[agent_id]
        tracker.record_node_execution(
            node_id=agent_id,
            success=True,
            latency_ms=response["latency"],
            cost_tokens=response["tokens"],
            quality=evaluate_quality(response["content"]),
        )

    # Record edge traversal metrics
    for i, agent_id in enumerate(result.execution_order[:-1]):
        next_agent = result.execution_order[i + 1]
        tracker.record_edge_traversal(
            source=agent_id,
            target=next_agent,
            weight=graph.get_edge_weight(agent_id, next_agent),
            success=True,
            latency_ms=50,
        )

# ========== STEP 2: Feature generation ==========
feature_config = FeatureConfig(
    include_degree=True,           # Node degrees
    include_centrality=True,       # Centrality (betweenness, closeness)
    include_embeddings=True,       # Agent embeddings
    include_metrics=True,          # Performance metrics
    include_structural=True,       # Structural features (clustering coef)
    normalize=True,                # Feature normalization
)

feature_gen = DefaultFeatureGenerator(config=feature_config)

node_features = feature_gen.generate_node_features(
    graph,
    graph.node_ids,
    tracker,
)  # Shape: (num_nodes, feature_dim)

edge_features = feature_gen.generate_edge_features(
    graph,
    tracker,
)  # Shape: (num_edges, edge_feature_dim)

print(f"Node features shape: {node_features.shape}")
print(f"Edge features shape: {edge_features.shape}")

# ========== STEP 3: Prepare the dataset ==========
# Create PyTorch Geometric Data objects

train_data_list = []
val_data_list = []

for sample in dataset:  # Your dataset with execution history
    data = Data(
        x=sample['node_features'],          # Node features
        edge_index=sample['edge_index'],    # Edge connections (2, E)
        edge_attr=sample['edge_features'],  # Edge features
        y=sample['labels'],                 # Labels (optimal next node, quality score, etc.)
    )

    if sample['is_train']:
        train_data_list.append(data)
    else:
        val_data_list.append(data)

# ========== STEP 4: Training configuration ==========
training_config = TrainingConfig(
    # Hyperparameters
    learning_rate=1e-3,
    hidden_dim=64,
    num_layers=3,
    dropout=0.2,

    # Training
    epochs=100,
    batch_size=32,
    patience=10,                 # Early stopping

    # Task
    task="node_classification",  # or "link_prediction", "graph_regression"
    num_classes=2,               # For classification

    # Optimization
    optimizer="adam",            # adam, sgd, adamw
    weight_decay=1e-5,
    scheduler="reduce_on_plateau",  # step, cosine, reduce_on_plateau

    # Device
    device="cuda" if torch.cuda.is_available() else "cpu",

    # Logging
    log_interval=10,
    save_best=True,
)

# ========== STEP 5: Create the model ==========

# 5.1. GCN (Graph Convolutional Network)
model_gcn = create_gnn_router(
    model_type=GNNModelType.GCN,
    in_channels=node_features.shape[1],
    out_channels=training_config.num_classes,
    config=training_config,
)

# 5.2. GAT (Graph Attention Network)
model_gat = create_gnn_router(
    model_type=GNNModelType.GAT,
    in_channels=node_features.shape[1],
    out_channels=training_config.num_classes,
    config=training_config,
    heads=4,              # Number of attention heads
    concat=True,          # Concatenate heads or average
)

# 5.3. GraphSAGE
model_sage = create_gnn_router(
    model_type=GNNModelType.GraphSAGE,
    in_channels=node_features.shape[1],
    out_channels=training_config.num_classes,
    config=training_config,
    aggr="mean",          # mean, max, lstm
)

# 5.4. GIN (Graph Isomorphism Network)
model_gin = create_gnn_router(
    model_type=GNNModelType.GIN,
    in_channels=node_features.shape[1],
    out_channels=training_config.num_classes,
    config=training_config,
    train_eps=True,       # Trainable epsilon
)

# ========== STEP 6: Train ==========
trainer = GNNTrainer(model_gat, training_config)

training_result = trainer.train(
    train_data_list,
    val_data_list,
    verbose=True,
)

print(f"Best validation accuracy: {training_result['best_val_acc']:.3f}")
print(f"Best epoch: {training_result['best_epoch']}")
print(f"Training time: {training_result['training_time']:.2f}s")

# Save the model
trainer.save("gnn_router.pt")

# Load the model
trainer.load("gnn_router.pt")

# ========== STEP 7: Inference ==========
router = GNNRouterInference(
    model=model_gat,
    feature_generator=feature_gen,
)

# 7.1. Predict the next node (node selection)
prediction = router.predict(
    graph,
    source="coordinator",
    candidates=["researcher", "analyst", "writer"],
    metrics_tracker=tracker,
    strategy=RoutingStrategy.ARGMAX,  # ARGMAX, TOP_K, SAMPLING, THRESHOLD
)

print(f"Recommended nodes: {prediction.recommended_nodes}")
print(f"Scores: {prediction.scores}")
print(f"Confidence: {prediction.confidence:.3f}")

# 7.2. Top-K prediction
prediction_topk = router.predict(
    graph,
    source="coordinator",
    candidates=["a", "b", "c", "d"],
    strategy=RoutingStrategy.TOP_K,
    k=2,  # Return top 2
)

print(f"Top 2: {prediction_topk.recommended_nodes}")

# 7.3. Probabilistic sampling
prediction_sample = router.predict(
    graph,
    source="coordinator",
    candidates=candidates,
    strategy=RoutingStrategy.SAMPLING,
    temperature=0.8,  # Sampling temperature
)

# 7.4. Threshold filtering
prediction_threshold = router.predict(
    graph,
    source="coordinator",
    candidates=candidates,
    strategy=RoutingStrategy.THRESHOLD,
    threshold=0.7,  # Only nodes with prob > 0.7
)

# ========== STEP 8: Integrate with AdaptiveScheduler ==========
from execution import AdaptiveScheduler, RoutingPolicy

scheduler = AdaptiveScheduler(
    policy=RoutingPolicy.GNN_BASED,
    gnn_router=router,
    gnn_threshold=0.6,                         # Min confidence to use the GNN
    fallback_policy=RoutingPolicy.WEIGHTED_TOPO # Fallback on low confidence
)

plan = scheduler.build_plan(
    graph.A_com,
    graph.node_ids,
    metrics_tracker=tracker,
)

# ========== STEP 9: Monitoring and fine-tuning ==========
# Collect new data after deployment
new_data = []
for i in range(20):
    result = runner.run_round(graph)
    # ... record data ...
    new_data.append(create_data_sample(result))

# Fine-tune
trainer.fine_tune(
    new_data,
    epochs=10,
    learning_rate=1e-4,
)

trainer.save("gnn_router_finetuned.pt")

# ========== Evaluation ==========
from core.gnn import evaluate_router

metrics = evaluate_router(
    router,
    test_data_list,
    metrics=["accuracy", "f1", "precision", "recall"],
)

print(f"Test accuracy: {metrics['accuracy']:.3f}")
print(f"F1 score: {metrics['f1']:.3f}")
```

---

#### Comparing GNN models

```python
# Experiment: compare performance across models

models = {
    "GCN": create_gnn_router(GNNModelType.GCN, in_channels, out_channels, config),
    "GAT": create_gnn_router(GNNModelType.GAT, in_channels, out_channels, config),
    "GraphSAGE": create_gnn_router(GNNModelType.GraphSAGE, in_channels, out_channels, config),
    "GIN": create_gnn_router(GNNModelType.GIN, in_channels, out_channels, config),
}

results = {}

for name, model in models.items():
    trainer = GNNTrainer(model, training_config)
    result = trainer.train(train_data, val_data)
    results[name] = result

# Comparison
import pandas as pd

df = pd.DataFrame([
    {
        "Model": name,
        "Val Acc": res["best_val_acc"],
        "Train Time": res["training_time"],
        "Params": sum(p.numel() for p in models[name].parameters()),
    }
    for name, res in results.items()
])

print(df)

# Output:
# | Model     | Val Acc | Train Time | Params  |
# |-----------|---------|------------|---------|
# | GCN       | 0.853   | 12.5s      | 45123   |
# | GAT       | 0.891   | 18.3s      | 67891   |
# | GraphSAGE | 0.874   | 15.2s      | 52341   |
# | GIN       | 0.867   | 14.8s      | 48976   |
```

---

#### Production usage

```python
# Load a trained model
router = GNNRouterInference.load("gnn_router.pt", feature_gen)

# Integrate with the runner
config = RunnerConfig(
    adaptive=True,
    routing_policy=RoutingPolicy.GNN_BASED,
)

runner = MACPRunner(
    llm_caller=my_llm,
    config=config,
    gnn_router=router,
    metrics_tracker=tracker,
)

# Execute with GNN routing
result = runner.run_round(graph)

# Monitor GNN predictions
print(f"GNN predictions used: {result.gnn_prediction_count}")
print(f"Fallback to heuristic: {result.fallback_to_heuristic_count}")
```

---

### Hidden Channels

Hidden channels allow passing **implicit information** between agents as vector representations, bypassing text prompts. This is especially useful for:
- Passing contextual information without increasing prompt length
- Preserving semantic embeddings for downstream tasks
- Implementing attention mechanisms between agents
- Integrating with a GNN to predict next steps

#### Hidden channel architecture

```
┌─────────────┐     hidden_state     ┌─────────────┐
│   Agent A   │ ──────────────────>  │   Agent B   │
│ (embedding) │     embedding        │ (receives   │
└─────────────┘                      │  combined)  │
                                     └─────────────┘
```

Each agent owns its:
- **`embedding`** — vector representation of the agent description
- **`hidden_state`** — hidden state updated after execution

The runner combines predecessor `hidden_state` and `embedding` and passes them to the next agent.

#### Using hidden channels

```python
from execution import RunnerConfig, MACPRunner, HiddenState
from core import NodeEncoder

# 1. Create an encoder for embeddings
encoder = NodeEncoder(model_name="sentence-transformers/all-MiniLM-L6-v2")

# 2. Hidden-channel configuration
config = RunnerConfig(
    enable_hidden_channels=True,
    hidden_combine_strategy="mean",  # Combine strategy
    pass_embeddings=True,            # Pass embeddings too
    hidden_dim=384,                  # Hidden state dimensionality
)

runner = MACPRunner(llm_caller=my_llm, config=config)

# 3. Compute agent embeddings
texts = [agent.to_text() for agent in graph.agents]
embeddings = encoder.encode(texts)

for agent, emb in zip(graph.agents, embeddings):
    agent = agent.with_embedding(emb)
    graph.update_agent(agent.agent_id, agent)

# 4. Execute with hidden channels
result = runner.run_round_with_hidden(
    graph,
    hidden_encoder=encoder,  # To create hidden_state from responses
)

# 5. Access hidden states after execution
for agent_id, hidden in result.hidden_states.items():
    print(f"{agent_id}:")
    print(f"  Hidden state: {hidden.tensor.shape}")      # (hidden_dim,)
    print(f"  Embedding: {hidden.embedding.shape}")      # (embedding_dim,)
    print(f"  Combined: {hidden.combined.shape}")        # (hidden_dim + embedding_dim,)

# 6. Use hidden states for downstream tasks
hidden_states_matrix = torch.stack([
    result.hidden_states[aid].tensor for aid in graph.node_ids
])  # Shape: (num_agents, hidden_dim)

# For example, cluster agents by semantics
from sklearn.cluster import KMeans
kmeans = KMeans(n_clusters=3)
clusters = kmeans.fit_predict(hidden_states_matrix.cpu().numpy())
```

#### Combine strategies (combine_strategy)

When an agent has multiple predecessors, their hidden states are combined:

```python
# 1. "mean" — average (default)
# hidden_combined = mean([h1, h2, h3])
config.hidden_combine_strategy = "mean"

# 2. "sum" — sum
# hidden_combined = h1 + h2 + h3
config.hidden_combine_strategy = "sum"

# 3. "concat" — concatenation
# hidden_combined = concat([h1, h2, h3])  # dimensionality increases
config.hidden_combine_strategy = "concat"

# 4. "attention" — weighted attention (weights from adjacency)
# hidden_combined = w1*h1 + w2*h2 + w3*h3, where wi = edge_weight(i -> current)
config.hidden_combine_strategy = "attention"

# 5. "max" — elementwise max
# hidden_combined = max(h1, h2, h3)
config.hidden_combine_strategy = "max"
```

#### Advanced: custom hidden-state processing

```python
from utils.memory import HiddenChannel

# Create a custom HiddenChannel
channel = HiddenChannel(
    node_id="agent_id",
    hidden_dim=384,
)

# Set hidden state
import torch
channel.set_hidden(torch.randn(384))
channel.set_embedding(torch.randn(384))

# Get combined representation
combined = channel.get_combined(strategy="attention", edge_weights=torch.tensor([0.8, 0.2]))

# Reset
channel.reset()

# Integration with agent memory
from utils.memory import AgentMemory

memory = AgentMemory("agent_id")
memory.hidden_state = torch.randn(384)
memory.embedding = torch.randn(384)

# Get what to pass to the next agent
hidden_to_pass = memory.hidden_state
embedding_to_pass = memory.embedding
```

#### Using with a GNN

```python
from core.gnn import GNNRouterInference, DefaultFeatureGenerator

# 1. Hidden states as features for a GNN
feature_gen = DefaultFeatureGenerator()

# Include hidden states into node features
node_features = feature_gen.generate_node_features(
    graph,
    graph.node_ids,
    metrics_tracker,
    include_hidden_states=True,  # Add hidden_state to features
)

# 2. GNN predicts the next agent based on hidden states
router = GNNRouterInference(model, feature_gen)

prediction = router.predict(
    graph,
    source="current_agent",
    candidates=["next1", "next2"],
    metrics_tracker=tracker,
    hidden_states=result.hidden_states,  # Pass current hidden states
)

# 3. Update the graph based on GNN predictions
if prediction.confidence > 0.8:
    next_agent = prediction.recommended_nodes[0]
    graph.add_edge("current_agent", next_agent, weight=prediction.confidence)
```

#### Example: multi-hop reasoning with hidden channels

```python
# Task: multi-hop reasoning where each agent accumulates context

agents = [
    AgentProfile(agent_id="reader", display_name="Document Reader"),
    AgentProfile(agent_id="analyzer", display_name="Analyzer"),
    AgentProfile(agent_id="reasoner", display_name="Reasoner"),
    AgentProfile(agent_id="answerer", display_name="Final Answerer"),
]

edges = [
    ("reader", "analyzer"),
    ("analyzer", "reasoner"),
    ("reasoner", "answerer"),
]

graph = build_property_graph(agents, edges, query="Complex question")

# Enable hidden channels for context passing
config = RunnerConfig(
    enable_hidden_channels=True,
    hidden_combine_strategy="attention",
    pass_embeddings=True,
)

encoder = NodeEncoder(model_name="sentence-transformers/all-MiniLM-L6-v2")
runner = MACPRunner(llm_caller=my_llm, config=config)

result = runner.run_round_with_hidden(graph, hidden_encoder=encoder)

# After each step, hidden_state contains the "accumulated context"
# answerer receives a weighted combination of all previous hidden states
```

---

### Adaptive execution

Full control over adaptive execution:

```python
from execution import (
    MACPRunner,
    RunnerConfig,
    RoutingPolicy,
    PruningConfig,
    BudgetConfig,
    ErrorPolicy,
)

config = RunnerConfig(
    adaptive=True,
    enable_parallel=True,
    max_parallel_size=5,

    routing_policy=RoutingPolicy.BEAM_SEARCH,

    pruning_config=PruningConfig(
        min_weight_threshold=0.1,
        token_budget=10000,
        enable_fallback=True,
        max_fallback_attempts=2,
        quality_scorer=lambda response: evaluate_quality(response),
        min_quality_threshold=0.5,
    ),

    budget_config=BudgetConfig(
        total_token_limit=50000,
        max_prompt_length=4000,
        node_token_limit=2000,
    ),

    error_policy=ErrorPolicy(
        on_timeout=ErrorAction.RETRY,
        on_retry_exhausted=ErrorAction.PRUNE,
        on_budget_exceeded=ErrorAction.ABORT,
    ),
)

runner = MACPRunner(llm_caller=my_llm, config=config)
result = runner.run_round(graph)

print(f"Topology changes: {result.topology_changed_count}")
print(f"Fallbacks: {result.fallback_count}")
print(f"Pruned agents: {result.pruned_agents}")
```

---

## Configuration

### Environment variables

```bash
# API key (required)
export RWXF_API_KEY="sk-your-api-key"
# or via file
export RWXF_API_KEY_FILE=/secure/rwxf.key

# LLM service URL
export RWXF_BASE_URL="https://api.openai.com/v1"

# Models
export RWXF_MODEL_NAME="gpt-4o-mini"
export RWXF_EMBEDDING_MODEL="sentence-transformers/all-MiniLM-L6-v2"

# Logging
export RWXF_LOG_LEVEL="INFO"
export RWXF_LOG_FILE="./logs/framework.log"

# Network settings
export RWXF_DEFAULT_TIMEOUT=60
export RWXF_MAX_RETRIES=3
```

### Programmatic configuration

```python
from config import FrameworkSettings, load_settings

# Load from environment
settings = FrameworkSettings()

# Load from a .env file
settings = load_settings(".env")

# Access settings
api_key = settings.resolved_api_key
model = settings.model_name
timeout = settings.default_timeout
```

---

## Usage examples

### Example 1: Simple pipeline

```python
from core import AgentProfile
from execution import MACPRunner
from builder import build_property_graph

agents = [
    AgentProfile(agent_id="researcher", display_name="Researcher"),
    AgentProfile(agent_id="writer", display_name="Writer"),
    AgentProfile(agent_id="editor", display_name="Editor"),
]

graph = build_property_graph(
    agents,
    workflow_edges=[("researcher", "writer"), ("writer", "editor")],
    query="Write an article about quantum computers",
)

runner = MACPRunner(llm_caller=my_llm)
result = runner.run_round(graph)

print(result.final_answer)
```

### Example 2: Parallel processing

```python
# Agents work in parallel, then results are aggregated
agents = [
    AgentProfile(agent_id="analyst_1", display_name="Financial Analyst"),
    AgentProfile(agent_id="analyst_2", display_name="Market Analyst"),
    AgentProfile(agent_id="analyst_3", display_name="Risk Analyst"),
    AgentProfile(agent_id="aggregator", display_name="Report Aggregator"),
]

edges = [
    ("analyst_1", "aggregator"),
    ("analyst_2", "aggregator"),
    ("analyst_3", "aggregator"),
]

graph = build_property_graph(agents, workflow_edges=edges, query="Analyze company X")

config = RunnerConfig(
    enable_parallel=True,
    max_parallel_size=3,
)

runner = MACPRunner(llm_caller=my_llm, config=config)
result = runner.run_round(graph)
```

### Example 3: Streaming with a callback

```python
def on_event(event):
    if event.event_type == StreamEventType.AGENT_OUTPUT:
        save_to_db(event.agent_id, event.content)
        notify_frontend(event)

runner = MACPRunner(llm_caller=my_llm)

for event in runner.stream(graph):
    on_event(event)

    if event.event_type == StreamEventType.TOKEN:
        yield event.token  # For SSE or WebSocket
```

### Example 4: Working with memory

```python
from execution import MACPRunner, RunnerConfig, MemoryConfig

config = RunnerConfig(
    enable_memory=True,
    memory_config=MemoryConfig(
        working_max_entries=20,
        long_term_max_entries=100,
    ),
    memory_context_limit=5,  # Include last 5 entries in the prompt
)

runner = MACPRunner(llm_caller=my_llm, config=config)

# First round
result1 = runner.run_round(graph)

# Second round — agents remember context
graph.query = "Continue the previous task"
result2 = runner.run_round(graph)

# Access agent memory
agent_memory = runner.get_agent_memory("solver")
entries = agent_memory.get_messages()
```

### Example 5: Graph visualization

```python
from core import AgentProfile
from core.visualization import (
    GraphVisualizer,
    VisualizationStyle,
    MermaidDirection,
    NodeStyle,
    NodeShape,
    # Convenience functions
    to_mermaid,
    to_ascii,
    to_dot,
    print_graph,
    render_to_image,
)
from builder import build_property_graph

# Create a graph
agents = [
    AgentProfile(
        agent_id="input",
        display_name="Input Handler",
        tools=["api_reader"],
    ),
    AgentProfile(
        agent_id="processor",
        display_name="Data Processor",
        tools=["pandas", "torch"],
    ),
    AgentProfile(
        agent_id="output",
        display_name="Output Formatter",
        tools=["json", "csv"],
    ),
]

graph = build_property_graph(
    agents,
    workflow_edges=[("input", "processor"), ("processor", "output")],
    query="Process data pipeline",
    include_task_node=True,
)

# Option 1: Quick visualization (convenience functions)
print("=== MERMAID ===")
mermaid = to_mermaid(graph, direction=MermaidDirection.LEFT_RIGHT)
print(mermaid)

print("\n=== ASCII ===")
ascii_art = to_ascii(graph, show_edges=True)
print(ascii_art)

print("\n=== COLORED (if Rich is installed) ===")
print_graph(graph, format="auto")  # Automatically chooses colored or ascii

# Option 2: Advanced visualization with custom styles (Pydantic models)
# Create a style (Pydantic model with validation)
custom_style = VisualizationStyle(
    direction=MermaidDirection.LEFT_RIGHT,
    agent_style=NodeStyle(
        shape=NodeShape.ROUND,
        fill_color="#e3f2fd",
        stroke_color="#1976d2",
        icon="🤖",
    ),
    task_style=NodeStyle(
        shape=NodeShape.DIAMOND,
        fill_color="#fff3e0",
        stroke_color="#f57c00",
        icon="📋",
    ),
    show_weights=True,
    show_tools=True,
    max_label_length=30,
)

# Create a visualizer with the custom style
viz = GraphVisualizer(graph, custom_style)

# Mermaid with a title
mermaid_styled = viz.to_mermaid(title="Data Pipeline")
print("\n=== STYLED MERMAID ===")
print(mermaid_styled)

# Save to files
viz.save_mermaid("pipeline.md", title="Data Pipeline")  # Markdown with ```mermaid```
viz.save_dot("pipeline.dot", graph_name="DataPipeline")

# Render to images (requires system Graphviz)
try:
    render_to_image(graph, "pipeline.png", format="png", dpi=150, style=custom_style)
    render_to_image(graph, "pipeline.svg", format="svg", style=custom_style)
    print("\n✅ Images created: pipeline.png, pipeline.svg")
except Exception as e:
    print(f"\n⚠️  Image rendering failed: {e}")
    print("   Install system Graphviz to render images")

# Adjacency matrix (text representation)
print("\n=== ADJACENCY MATRIX ===")
matrix = viz.to_adjacency_matrix(show_labels=True)
print(matrix)

# Rich Console output with trees and tables
print("\n=== RICH CONSOLE ===")
viz.print_colored()
```

### Example 6: Conditional routing

```python
from builder import GraphBuilder
from execution.scheduler import ConditionContext

# Define conditions
def is_high_quality(context: ConditionContext) -> bool:
    return context.state.get("quality", 0) > 0.8

def needs_review(context: ConditionContext) -> bool:
    return context.state.get("word_count", 0) > 1000

# Build a graph with conditional edges
builder = GraphBuilder()
builder.add_agent(agent_id="writer", display_name="Content Writer")
builder.add_agent(agent_id="editor", display_name="Quick Editor")
builder.add_agent(agent_id="reviewer", display_name="Senior Reviewer")
builder.add_agent(agent_id="publisher", display_name="Publisher")

# Conditional transitions
builder.add_conditional_edge("writer", "editor", condition=is_high_quality)
builder.add_conditional_edge("writer", "reviewer", condition=needs_review)
builder.add_workflow_edge("editor", "publisher")
builder.add_workflow_edge("reviewer", "publisher")

graph = builder.build()

# Run
runner = MACPRunner(llm_caller=my_llm)
result = runner.run_round(graph)
```

### Example 7: Monitoring with events

```python
from core.events import (
    global_event_bus,
    EventType,
    MetricsEventHandler,
)

# Configure event handlers
bus = global_event_bus()
metrics_handler = MetricsEventHandler()

# Subscribe to events
bus.subscribe(None, metrics_handler)  # Listen to all events

@bus.subscribe(EventType.STEP_COMPLETED)
def on_step_completed(event):
    print(f"✅ {event.agent_id} completed in {event.duration_ms:.0f}ms")

@bus.subscribe(EventType.BUDGET_WARNING)
def on_budget_warning(event):
    print(f"⚠️  Budget {event.budget_type}: {event.ratio:.1%}")

# Run with monitoring
runner = MACPRunner(llm_caller=my_llm)
result = runner.run_round(graph)

# Get aggregated metrics
metrics = metrics_handler.get_metrics()
print(f"Total tokens: {metrics['total_tokens']}")
print(f"Errors: {metrics['errors_count']}")
print(f"Avg step duration: {metrics['avg_step_duration_ms']:.1f}ms")
```

### Example 8: GNN routing with training

```python
from core.gnn import (
    create_gnn_router,
    GNNTrainer,
    GNNRouterInference,
    GNNModelType,
    TrainingConfig,
    DefaultFeatureGenerator,
)
from core.metrics import MetricsTracker
import torch

# Collect execution data for training
tracker = MetricsTracker()

# ... run several rounds with different queries ...
for i in range(100):
    result = runner.run_round(graph)
    # Record metrics
    for agent_id, response in result.messages.items():
        tracker.record_node_execution(
            node_id=agent_id,
            success=True,
            latency_ms=response["latency"],
            cost_tokens=response["tokens"],
            quality=evaluate_quality(response["content"]),
        )

# Feature generation
feature_gen = DefaultFeatureGenerator()
node_features = feature_gen.generate_node_features(
    graph,
    graph.node_ids,
    tracker,
)

# Create dataset
# ... prepare train_data, val_data in PyG Data format ...

# Train the model
config = TrainingConfig(
    learning_rate=1e-3,
    hidden_dim=64,
    num_layers=2,
    epochs=50,
    task="node_classification",
)

model = create_gnn_router(
    model_type=GNNModelType.GAT,
    in_channels=node_features.shape[1],
    out_channels=2,
    config=config,
)

trainer = GNNTrainer(model, config)
result = trainer.train(train_data, val_data)

print(f"Best validation accuracy: {result['best_val_acc']:.3f}")
trainer.save("gnn_router.pt")

# Use the trained model for routing
router = GNNRouterInference(model, feature_gen)

prediction = router.predict(
    graph,
    source="coordinator",
    candidates=["agent1", "agent2", "agent3"],
    metrics_tracker=tracker,
)

print(f"Recommended: {prediction.recommended_nodes[0]}")
print(f"Confidence: {prediction.confidence:.3f}")
```

### Example 9: Adaptive execution with a budget

```python
from execution import (
    MACPRunner,
    RunnerConfig,
    RoutingPolicy,
    PruningConfig,
)
from execution.budget import Budget

# Configure adaptive execution
config = RunnerConfig(
    adaptive=True,
    enable_parallel=True,
    max_parallel_size=3,

    routing_policy=RoutingPolicy.WEIGHTED_TOPO,

    pruning_config=PruningConfig(
        min_weight_threshold=0.1,
        token_budget=5000,
        enable_fallback=True,
        max_fallback_attempts=2,
    ),

    budget_config=BudgetConfig(
        total_token_limit=10000,
        node_token_limit=2000,
        max_prompt_length=3000,
        warn_at_usage_ratio=0.8,
    ),

    timeout=60.0,
    max_retries=2,
)

runner = MACPRunner(llm_caller=my_llm, config=config)

# Execute
try:
    result = runner.run_round(graph)

    print(f"Executed agents: {len(result.execution_order)}")
    print(f"Pruned agents: {result.pruned_agents}")
    print(f"Topology changes: {result.topology_changed_count}")
    print(f"Fallback count: {result.fallback_count}")
    print(f"Total tokens: {result.total_tokens}")

except BudgetExceededError as e:
    print(f"Budget exceeded: {e}")
except ExecutionError as e:
    print(f"Execution failed: {e}")
```

### Example 10: Graph analysis with algorithms

```python
from core.algorithms import (
    GraphAlgorithms,
    CentralityType,
    PathMetric,
)

# Create a complex graph
algo = GraphAlgorithms(graph)

# Find critical nodes
centrality = algo.centrality(CentralityType.BETWEENNESS, normalized=True)
print(f"Most critical agents: {centrality.top_nodes[:3]}")

# Find alternative paths
paths = algo.k_shortest_paths(
    source="input",
    target="output",
    k=3,
    metric=PathMetric.WEIGHTED,
)

print(f"Found {len(paths)} alternative paths:")
for i, path in enumerate(paths, 1):
    print(f"  Path {i}: {' -> '.join(path.nodes)} (cost: {path.cost:.2f})")

# Detect communities
communities = algo.detect_communities(algorithm="louvain")
print(f"Communities found: {len(communities.communities)}")
for i, community in enumerate(communities.communities):
    print(f"  Community {i}: {community}")

# Cycle check
cycles = algo.find_cycles(max_length=5)
if cycles.has_cycles:
    print(f"⚠️  Graph has {len(cycles.cycles)} cycles!")
else:
    print("✓ Graph is acyclic (DAG)")
```

### Example 11: Multi-model system with cost optimization

```python
from builder import GraphBuilder
from execution import MACPRunner, LLMCallerFactory

# Build a graph with different models for different tasks
builder = GraphBuilder()

# Stage 1: Data collection (5 parallel agents, cheap model)
for i in range(5):
    builder.add_agent(
        f"collector_{i}",
        display_name=f"Data Collector {i}",
        persona="Collects and formats raw data",
        llm_backbone="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        api_key="$OPENAI_API_KEY",
        temperature=0.2,
        max_tokens=500,
    )
    builder.add_workflow_edge(f"collector_{i}", "analyst")

# Stage 2: Deep analysis (1 agent, strong model)
builder.add_agent(
    "analyst",
    display_name="Senior Data Analyst",
    persona="Expert analyst with deep statistical knowledge",
    llm_backbone="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.0,
    max_tokens=4000,
)
builder.add_workflow_edge("analyst", "privacy_checker")

# Stage 3: Privacy compliance check (local model)
builder.add_agent(
    "privacy_checker",
    display_name="Privacy Compliance Checker",
    persona="Ensures data privacy and compliance",
    llm_backbone="llama3:70b",
    base_url="http://localhost:11434/v1",
    api_key="not-needed",
    temperature=0.0,
    max_tokens=1000,
)
builder.add_workflow_edge("privacy_checker", "reporter")

# Stage 4: Report generation (cheap model)
builder.add_agent(
    "reporter",
    display_name="Report Generator",
    persona="Formats analysis into readable reports",
    llm_backbone="gpt-4o-mini",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.5,
    max_tokens=2000,
)

builder.set_task(
    query="Analyze Q4 sales data and generate a compliance report",
    description="Full pipeline from data collection to the final report",
)

graph = builder.build()

# Print configuration
print("=== Multi-Model Pipeline Configuration ===\n")
for agent in graph.agents:
    if hasattr(agent, 'llm_config') and agent.llm_config:
        config = agent.llm_config
        print(f"{agent.display_name}:")
        print(f"  Model: {config.model_name}")
        print(f"  Endpoint: {config.base_url}")
        print(f"  Temp: {config.temperature}, Max tokens: {config.max_tokens}")
        print()

# Create factory and runner
factory = LLMCallerFactory.create_openai_factory()

config = RunnerConfig(
    enable_parallel=True,
    max_parallel_size=5,  # Collectors run in parallel
    timeout=120.0,
    callbacks=[StdoutCallbackHandler()],  # Execution monitoring
)

runner = MACPRunner(
    llm_factory=factory,
    config=config,
)

# Execute
print("=== Executing Multi-Model Pipeline ===\n")
result = runner.run_round(graph)

print(f"\n=== Results ===")
print(f"Execution order: {' → '.join(result.execution_order)}")
print(f"Total time: {result.total_time:.2f}s")
print(f"Total tokens: {result.total_tokens}")
print(f"\nFinal report:\n{result.final_answer}")

# Token usage analysis by model
from collections import defaultdict

costs_by_model = defaultdict(int)
for agent_id in result.execution_order:
    agent = graph.get_agent_by_id(agent_id)
    model = agent.llm_config.model_name if agent.llm_config else "default"
    tokens = result.messages.get(agent_id, {}).get("tokens", 0)
    costs_by_model[model] += tokens

print(f"\n=== Token Usage by Model ===")
for model, tokens in costs_by_model.items():
    print(f"{model}: {tokens} tokens")

# Savings calculation
# gpt-4: $30/$60 per 1M tokens (input/output)
# gpt-4o-mini: $0.15/$0.60 per 1M tokens
# llama3 (local): $0

gpt4_tokens = costs_by_model.get("gpt-4", 0)
mini_tokens = costs_by_model.get("gpt-4o-mini", 0)
llama_tokens = costs_by_model.get("llama3:70b", 0)

actual_cost = (gpt4_tokens * 45 / 1_000_000) + (mini_tokens * 0.375 / 1_000_000)
if_all_gpt4_cost = (gpt4_tokens + mini_tokens + llama_tokens) * 45 / 1_000_000

print(f"\n=== Cost Analysis ===")
print(f"Actual cost: ${actual_cost:.4f}")
print(f"Cost if all GPT-4: ${if_all_gpt4_cost:.4f}")
print(f"Savings: ${if_all_gpt4_cost - actual_cost:.4f} ({((1 - actual_cost/if_all_gpt4_cost)*100):.1f}%)")
```

---

### Token budget (Budget System)

Resource management for execution (tokens, requests, time).

```python
from execution.budget import (
    Budget,
    BudgetConfig,
    NodeBudget,
    BudgetTracker,
)

# Budget — tracks a single resource (tokens, requests, or time)
token_budget = Budget(limit=50000)
print(f"Available: {token_budget.available}")
print(f"Usage ratio: {token_budget.usage_ratio:.1%}")

can_spend = token_budget.can_spend(100)  # Check before using
token_budget.spend(100)                  # Record usage

# Per-node budget (composed of Budget objects)
node_budget = NodeBudget(
    node_id="solver",
    tokens=Budget(limit=2000),
    requests=Budget(limit=10),
    time_seconds=Budget(limit=60),
)

# Budget tracker — configured via BudgetConfig
config = BudgetConfig(
    total_token_limit=50000,       # Global token limit
    total_request_limit=100,       # Global request limit
    total_time_limit_seconds=600,  # Global time limit (10 min)
    node_token_limit=2000,         # Per-node token limit
    max_prompt_length=4000,        # Max chars in a prompt
    max_response_length=2000,      # Max chars in a response
    warn_at_usage_ratio=0.8,       # Warn at 80%
)

tracker = BudgetTracker(config=config)
tracker.start()  # Start the timer

# Availability check
can_run, reason = tracker.can_execute("solver", estimated_tokens=100)
if can_run:
    # Record usage after execution
    tracker.record_usage(
        node_id="solver",
        prompt_tokens=80,
        completion_tokens=120,
        latency_seconds=1.5,
    )

# Prompt/response truncation when exceeding limits
prompt = "a very long prompt..."
truncated = tracker.truncate_prompt(prompt)

# Budget summary
summary = tracker.get_summary()
print(f"Tokens used: {summary['global']['tokens']['used']}")
print(f"Time elapsed: {summary['global']['elapsed_seconds']:.1f}s")

# Reset
tracker.reset()
```

#### Integration with RunnerConfig

```python
from execution import RunnerConfig, BudgetConfig

config = RunnerConfig(
    budget_config=BudgetConfig(
        total_token_limit=50000,
        node_token_limit=2000,
        max_prompt_length=4000,
        warn_at_usage_ratio=0.8,
    ),
)
```

---

### Error handling (Error Handling)

Structured exceptions and error-handling policies.

```python
from execution.errors import (
    ExecutionError,
    TimeoutError,
    RetryExhaustedError,
    BudgetExceededError,
    AgentNotFoundError,
    ValidationError,
    ErrorPolicy,
    ErrorAction,
    ExecutionMetrics,
)

# Error policy
error_policy = ErrorPolicy(
    on_timeout=ErrorAction.RETRY,           # retry, skip, prune, fallback, rollback, abort
    on_retry_exhausted=ErrorAction.PRUNE,
    on_budget_exceeded=ErrorAction.ABORT,
    on_validation_error=ErrorAction.ABORT,
    on_agent_not_found=ErrorAction.SKIP,
    on_unknown_error=ErrorAction.SKIP,
    max_skipped_agents=5,
    abort_on_critical_path=True,
)

# Apply in configuration
config = RunnerConfig(
    error_policy=error_policy,
    max_retries=3,
    timeout=60.0,
)

# Error handling
try:
    result = runner.run_round(graph)
except TimeoutError as e:
    print(f"Timeout: {e}")
except RetryExhaustedError as e:
    print(f"Retries exhausted: {e}")
except BudgetExceededError as e:
    print(f"Budget exceeded: {e}")
except ExecutionError as e:
    print(f"Execution error: {e}")
    # Access metrics
    metrics: ExecutionMetrics = e.metrics
    print(f"Retries: {metrics.retry_count}")
    print(f"Fallbacks: {metrics.fallback_count}")

# Get metrics from the result
if result.errors:
    for error in result.errors:
        print(f"{error['agent_id']}: {error['type']} - {error['message']}")
```

---

### Graph algorithms (Graph Algorithms)

A service layer for graph analysis using `rustworkx` algorithms.

```python
from core.algorithms import (
    GraphAlgorithms,
    CentralityType,
    PathMetric,
    SubgraphFilter,
)

algo = GraphAlgorithms(graph)

# K shortest paths
paths = algo.k_shortest_paths(
    source="researcher",
    target="writer",
    k=3,
    metric=PathMetric.HOP_COUNT,   # HOP_COUNT, WEIGHTED, RELIABILITY
    edge_weights=None,             # or custom weights
)
for i, path in enumerate(paths):
    print(f"Path {i+1}: {path.nodes} (cost={path.cost:.2f})")

# Node centrality
centrality = algo.centrality(
    centrality_type=CentralityType.BETWEENNESS,  # DEGREE, BETWEENNESS, CLOSENESS, EIGENVECTOR, PAGERANK
    normalized=True,
)
print(f"Most central node: {centrality.top_nodes[0]}")
print(f"Scores: {centrality.scores}")

# Community detection
communities = algo.detect_communities(algorithm="louvain")  # louvain, label_propagation
print(f"Communities found: {len(communities.communities)}")
print(f"Modularity: {communities.modularity:.3f}")

# Cycle search
cycles = algo.find_cycles(max_length=5)
if cycles.has_cycles:
    print(f"Cycles found: {len(cycles.cycles)}")
    for cycle in cycles.cycles:
        print(f"  {cycle}")

# Subgraph filtering
subgraph_filter = SubgraphFilter(
    include_node_ids=["a", "b", "c"],
    min_edge_weight=0.5,
    max_hop_distance=2,
    from_node="a",
)
subgraph = algo.filter_subgraph(subgraph_filter)
print(f"Nodes in subgraph: {len(subgraph.node_ids)}")

# Reachability analysis
reachable = algo.get_reachable_nodes("start", max_distance=3)
print(f"Reachable nodes: {reachable}")

# Topological order
if algo.is_dag():
    topo_order = algo.topological_sort()
    print(f"Topological order: {topo_order}")
```

---

### Metrics Tracker

Collects and aggregates performance metrics for nodes and edges.

```python
from core.metrics import (
    MetricsTracker,
    NodeMetrics,
    EdgeMetrics,
    MetricAggregator,
    ExponentialMovingAverage,
    SlidingWindowAverage,
)

tracker = MetricsTracker()

# Record node metrics
tracker.record_node_execution(
    node_id="solver",
    success=True,
    latency_ms=150,
    cost_tokens=200,
    quality=0.95,
)

# Record edge metrics
tracker.record_edge_traversal(
    source="solver",
    target="checker",
    weight=0.9,
    success=True,
    latency_ms=50,
)

# Get node metrics
metrics: NodeMetrics = tracker.get_node_metrics("solver")
print(f"Reliability: {metrics.reliability:.3f}")
print(f"Avg latency: {metrics.avg_latency_ms:.1f}ms")
print(f"Total cost: {metrics.total_cost_tokens}")
print(f"Avg quality: {metrics.avg_quality:.3f}")
print(f"Executions: {metrics.execution_count}")

# Get edge metrics
edge_metrics: EdgeMetrics = tracker.get_edge_metrics("solver", "checker")
print(f"Edge reliability: {edge_metrics.reliability:.3f}")
print(f"Traversals: {edge_metrics.traversal_count}")

# Snapshot of all metrics
snapshot = tracker.snapshot()
print(f"Timestamp: {snapshot.timestamp}")
print(f"Node metrics: {snapshot.node_metrics}")
print(f"Edge metrics: {snapshot.edge_metrics}")

# Metrics history (if enabled)
tracker = MetricsTracker(keep_history=True, history_window=100)
# ... records ...
history = tracker.get_history(node_id="solver")
for snapshot in history.snapshots:
    print(f"{snapshot.timestamp}: {snapshot.metrics}")

# Custom aggregators
ema = ExponentialMovingAverage(alpha=0.1)
tracker.set_aggregator("solver", "latency", ema)

swa = SlidingWindowAverage(window_size=10)
tracker.set_aggregator("checker", "quality", swa)

# Export metrics
data = tracker.to_dict()
tracker.save("metrics.json")

# Load metrics
tracker = MetricsTracker.load("metrics.json")
```

---

### Visualization

Tools for visualizing graphs in different formats. All visualization styles are based on **Pydantic models** for validation and type safety.

#### Core classes

```python
from core.visualization import (
    GraphVisualizer,
    VisualizationStyle,
    MermaidDirection,
    NodeShape,
    NodeStyle,
    EdgeStyle,
    # Convenience functions
    to_mermaid,
    to_ascii,
    to_dot,
    print_graph,
    render_to_image,
    show_graph_interactive,
)
```

#### 1. Quick usage (convenience functions)

```python
# Simple Mermaid
mermaid_code = to_mermaid(graph, direction=MermaidDirection.LEFT_RIGHT)
print(mermaid_code)

# Simple ASCII
ascii_art = to_ascii(graph, show_edges=True)
print(ascii_art)

# Simple DOT
dot_code = to_dot(graph, graph_name="MyGraph")
print(dot_code)

# Print to console (auto-selects Rich or ASCII)
print_graph(graph, format="auto")  # "auto", "colored", "ascii", "mermaid"

# Render to image (requires system Graphviz)
render_to_image(graph, "output.png", format="png", dpi=300)
render_to_image(graph, "output.svg", format="svg")

# Interactive view (opens in system viewer)
show_graph_interactive(graph, graph_name="MyWorkflow")
```

#### 2. Advanced usage (GraphVisualizer with custom styles)

**VisualizationStyle**, **NodeStyle**, **EdgeStyle** are Pydantic models with field validation.

```python
# Create custom node styles (Pydantic models)
agent_style = NodeStyle(
    shape=NodeShape.ROUND,      # RECTANGLE, ROUND, STADIUM, CIRCLE, DIAMOND, etc.
    fill_color="#e3f2fd",       # Fill color
    stroke_color="#1976d2",     # Border color
    text_color="#000000",       # Text color
    icon="🤖",                  # Emoji icon
)

task_style = NodeStyle(
    shape=NodeShape.DIAMOND,
    fill_color="#fff3e0",
    stroke_color="#f57c00",
    icon="📋",
)

# Edge styles (Pydantic models)
workflow_edge = EdgeStyle(
    line_style="solid",         # solid, dashed, dotted
    arrow_head="normal",        # normal, none, diamond
    color="#1976d2",
    label_color="#333333",
)

task_edge = EdgeStyle(
    line_style="dashed",
    color="#f57c00",
)

# Global visualization style (Pydantic model)
style = VisualizationStyle(
    direction=MermaidDirection.LEFT_RIGHT,  # TOP_BOTTOM, BOTTOM_TOP, LEFT_RIGHT, RIGHT_LEFT
    agent_style=agent_style,
    task_style=task_style,
    workflow_edge_style=workflow_edge,
    task_edge_style=task_edge,
    show_weights=True,          # Show edge weights
    show_probabilities=False,   # Show probabilities
    show_tools=True,            # Show agent tools
    show_descriptions=False,    # Show descriptions
    max_label_length=30,        # Max label length
)

# Create a visualizer with custom style
viz = GraphVisualizer(graph, style)

# Mermaid diagrams
mermaid = viz.to_mermaid(
    direction=MermaidDirection.TOP_BOTTOM,  # Can override style
    title="Agent Workflow",                 # Diagram title
)
print(mermaid)

# Save Mermaid to a file
viz.save_mermaid("graph.md", title="My Workflow")   # Wraps in ```mermaid```
viz.save_mermaid("graph.mmd", title="My Workflow")  # Raw .mmd without wrapper

# ASCII art for terminal
ascii_art = viz.to_ascii(
    show_edges=True,
    box_width=20,
)
print(ascii_art)

# Graphviz DOT
dot = viz.to_dot(
    graph_name="AgentGraph",
    rankdir="LR",  # TB, LR, BT, RL
)
viz.save_dot("graph.dot", graph_name="AgentGraph")

# Render to image (requires installed Graphviz)
viz.render_image(
    "output.png",
    format="png",     # png, svg, pdf, jpg
    dpi=300,          # For raster formats
    graph_name="MyGraph",
)

# Interactive view
viz.show_interactive(graph_name="MyGraph")  # Opens system viewer

# Adjacency matrix (text representation)
matrix = viz.to_adjacency_matrix(show_labels=True)
print(matrix)
```

#### 3. Colored terminal output (Rich Console)

```python
# Automatic colored output (if Rich is installed)
print_graph(graph, format="colored")

# Or via visualizer
viz = GraphVisualizer(graph)
viz.print_colored()  # Pretty output with trees, tables, and colors
```

#### 4. Full configuration example

```python
from core.visualization import (
    GraphVisualizer,
    VisualizationStyle,
    NodeStyle,
    EdgeStyle,
    NodeShape,
    MermaidDirection,
)

# Fully configured style
custom_style = VisualizationStyle(
    direction=MermaidDirection.LEFT_RIGHT,
    agent_style=NodeStyle(
        shape=NodeShape.ROUND,
        fill_color="#bbdefb",
        stroke_color="#0d47a1",
        icon="🤖",
    ),
    task_style=NodeStyle(
        shape=NodeShape.DIAMOND,
        fill_color="#ffe0b2",
        stroke_color="#e65100",
        icon="📋",
    ),
    workflow_edge_style=EdgeStyle(
        line_style="solid",
        color="#1976d2",
    ),
    task_edge_style=EdgeStyle(
        line_style="dashed",
        color="#f57c00",
    ),
    show_weights=True,
    show_tools=True,
    max_label_length=40,
)

viz = GraphVisualizer(graph, custom_style)

# Generate all formats
viz.save_mermaid("docs/graph.md", title="Workflow")
viz.save_dot("docs/graph.dot")
viz.render_image("docs/graph.png", format="png", dpi=150)
viz.render_image("docs/graph.svg", format="svg")

print(viz.to_ascii())
```

#### 5. Installing Graphviz for image rendering

For `render_image()` and `render_to_image()` you need:
1. Python library: `pip install graphviz`
2. System Graphviz:
   - Ubuntu/Debian: `sudo apt install graphviz`
   - macOS: `brew install graphviz`
   - Windows: `winget install graphviz` or https://graphviz.org/download/

---

### Schema System

A complete system of **Pydantic schemas** for type-safe validation, serialization, and migration of graph data. All schemas inherit from `pydantic.BaseModel` and provide automatic type validation, default values, and data conversion.

#### Core schema classes

```python
from core.schema import (
    # Versioning
    SCHEMA_VERSION,
    SchemaVersion,
    # Node and edge types
    NodeType,
    EdgeType,
    # Node schemas (Pydantic BaseModel)
    BaseNodeSchema,
    AgentNodeSchema,
    TaskNodeSchema,
    # Edge schemas (Pydantic BaseModel)
    BaseEdgeSchema,
    WorkflowEdgeSchema,
    CostMetrics,
    # Graph schema (Pydantic BaseModel)
    GraphSchema,
    # LLM configuration (Pydantic BaseModel)
    LLMConfig,
    # Validation (Pydantic BaseModel)
    ValidationResult,
    SchemaValidator,
    # Migrations
    SchemaMigration,
    MigrationRegistry,
    migrate_schema,
)
```

#### 1. Creating node schemas (Pydantic models)

```python
# Agent with a full LLM configuration
agent_node = AgentNodeSchema(
    id="solver",
    type=NodeType.AGENT,
    display_name="Math Solver",
    persona="You are an expert mathematician",
    description="Solves complex math problems step by step",
    tools=["calculator", "wolfram_alpha"],
    # LLM configuration (Pydantic model)
    llm_backbone="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.0,
    max_tokens=2000,
    # Metrics and state
    trust_score=0.95,
    quality_score=0.9,
    success_rate=1.0,
    total_calls=0,
    total_tokens_used=0,
    # Pydantic validates embedding automatically
    embedding=[0.1, 0.2, 0.3],  # Can be a list or torch.Tensor
    embedding_dim=3,            # Auto-filled if None
    # Metadata (arbitrary data)
    metadata={"priority": "high", "category": "math"},
    tags={"solver", "math", "primary"},
)

# Task
task_node = TaskNodeSchema(
    id="main_task",
    type=NodeType.TASK,
    query="Solve: x^2 + 5x + 6 = 0",
    description="Main mathematical task",
    expected_output="Two solutions: x1, x2",
    max_iterations=10,
    status="pending",  # pending, running, completed, failed
)

# Extract LLM configuration from the agent
llm_config: LLMConfig = agent_node.get_llm_config()
print(f"Model: {llm_config.model_name}")
print(f"Configured: {llm_config.is_configured()}")
print(f"Generation params: {llm_config.to_generation_params()}")

# Check whether an LLM configuration exists
if agent_node.has_llm_config():
    print("Agent has LLM configuration")
```

#### 2. Creating edge schemas (Pydantic models)

```python
# Base edge with cost metrics (Pydantic model)
edge = BaseEdgeSchema(
    source="solver",
    target="checker",
    type=EdgeType.WORKFLOW,
    weight=1.0,
    probability=0.95,
    bidirectional=False,
    # Cost metrics (Pydantic model)
    cost=CostMetrics(
        estimated_tokens=500,
        actual_tokens=None,
        latency_ms=150.0,
        timeout_ms=5000.0,
        trust=0.9,
        reliability=0.95,
        cost_usd=0.01,
        custom={"priority": 1.0},
    ),
    # Pydantic validates attr automatically
    attr=[1.0, 0.95, 0.9],  # Can be a list or torch.Tensor
    attr_dim=3,             # Auto-filled if None
    metadata={"route": "primary"},
)

# Workflow edge with conditional routing
conditional_edge = WorkflowEdgeSchema(
    source="solver",
    target="checker",
    type=EdgeType.WORKFLOW,
    weight=0.9,
    probability=1.0,
    # Conditional routing
    condition="source_success",  # Name of a built-in or registered condition
    priority=1,                  # Priority (higher = checked earlier)
    transform="extract_answer",  # Optional data transform
    is_conditional=True,         # Auto-set if condition is provided
)

# Get edge features
feature_vector = edge.get_feature_vector(feature_names=["trust", "reliability"])
print(f"Features: {feature_vector}")

# Convert to torch.Tensor
attr_tensor = edge.to_attr_tensor()
print(f"Attr tensor: {attr_tensor}")
```

#### 3. Full graph schema (Pydantic model)

```python
from datetime import datetime

# GraphSchema - the main Pydantic model
schema = GraphSchema(
    schema_version=SCHEMA_VERSION,  # "2.0.0"
    name="Math Pipeline",
    description="A workflow for solving mathematical problems",
    created_at=datetime.now(),
    updated_at=datetime.now(),
    # nodes is dict[str, BaseNodeSchema], not a list!
    nodes={
        "solver": AgentNodeSchema(
            id="solver",
            display_name="Math Solver",
            description="Solves math problems",
            tools=["calculator"],
            llm_backbone="gpt-4",
            base_url="https://api.openai.com/v1",
            api_key="$OPENAI_API_KEY",
        ),
        "checker": AgentNodeSchema(
            id="checker",
            display_name="Answer Checker",
            description="Validates solutions",
            llm_backbone="gpt-4o-mini",
        ),
        "__task__": TaskNodeSchema(
            id="__task__",
            query="Solve: x^2 + 5x + 6 = 0",
        ),
    },
    edges=[
        WorkflowEdgeSchema(
            source="solver",
            target="checker",
            weight=0.9,
            type=EdgeType.WORKFLOW,
        ),
    ],
    # Feature names for feature extraction
    node_feature_names=["trust_score", "quality_score"],
    edge_feature_names=["trust", "reliability"],
    # Metadata
    metadata={
        "created_by": "user@example.com",
        "purpose": "math_pipeline",
        "version": "1.0",
    },
)

# Add nodes and edges
new_agent = AgentNodeSchema(
    id="reviewer",
    display_name="Reviewer",
)
schema.add_node(new_agent)

new_edge = BaseEdgeSchema(
    source="checker",
    target="reviewer",
)
schema.add_edge(new_edge)

# Retrieve nodes and edges
solver_node = schema.get_node("solver")
edges_from_solver = schema.get_edges(source="solver")
edges_to_checker = schema.get_edges(target="checker")

# Compute feature dimensionalities
schema.compute_feature_dims()
print(f"Node feature dim: {schema.node_feature_dim}")
print(f"Edge feature dim: {schema.edge_feature_dim}")
```

#### 4. Serialization and validation (Pydantic)

```python
# Serialization (Pydantic methods)
schema_dict = schema.model_dump()              # Dict[str, Any]
schema_json = schema.model_dump_json(indent=2) # JSON string

# Or a specialized method
schema_data = schema.to_dict()

# Deserialization (Pydantic methods)
loaded_schema = GraphSchema.model_validate(schema_dict)
loaded_from_json = GraphSchema.model_validate_json(schema_json)

# Schema validation (returns ValidationResult - Pydantic model)
validator = SchemaValidator(
    check_cycles=True,
    check_duplicates=True,
    check_orphans=True,
    check_connectivity=False,
)
result: ValidationResult = validator.validate(schema)

if result.valid:
    print("✓ Schema is valid")
else:
    print("✗ Validation errors:")
    for error in result.errors:
        print(f"  - {error}")

if result.warnings:
    print("⚠ Warnings:")
    for warning in result.warnings:
        print(f"  - {warning}")
```

#### 5. Schema migration between versions

```python
# Automatic migration of legacy data
old_data = {
    "schema_version": "1.0.0",
    "agents": [  # Old format (agents list)
        {"agent_id": "solver", "display_name": "Solver"},
    ],
    "edges": [
        {"source": "solver", "target": "checker"},
    ],
}

# Migrate to the current version (2.0.0)
migrated_data = migrate_schema(old_data)
print(f"Migrated to version: {migrated_data['schema_version']}")

# Create a custom migration
from core.schema import SchemaMigration, register_migration

class MyCustomMigration(SchemaMigration):
    from_version = "1.5.0"
    to_version = "2.0.0"

    def migrate(self, data: dict) -> dict:
        # Your migration logic
        data["new_field"] = "default_value"
        return data

# Register migration
register_migration(MyCustomMigration())
```

#### 6. Versioning

```python
# Check schema version
current_version = SchemaVersion.parse(SCHEMA_VERSION)  # "2.0.0"
print(f"Current: {current_version}")

old_version = SchemaVersion.parse("1.5.0")
print(f"Compatible: {current_version.is_schema_compatible(old_version)}")  # False (different major versions)
print(f"Newer: {current_version > old_version}")  # True
```

#### Benefits of Pydantic schemas

1. **Automatic type validation** — Pydantic checks types when creating objects
2. **Default values** — fields are auto-populated
3. **Type conversion** — automatic conversion (torch.Tensor → list)
4. **Serialization/deserialization** — built-in `.model_dump()`, `.model_validate()`
5. **Extensibility** — `extra="allow"` enables arbitrary fields
6. **Immutability** — `frozen=True` for immutable models
7. **Documentation** — automatic JSON Schema generation

---

#### 7. Agent input/output validation

**New:** Each agent can have **input_schema** and **output_schema** to validate incoming data and outputs. This allows you to:
- 🔒 Guarantee data correctness
- 📝 Automatically parse structured outputs
- 🚫 Catch invalid LLM outputs
- 📋 Generate JSON Schema for prompts

> **Prompt injection:** `_build_prompt` automatically injects schemas into the LLM prompt.
> - `output_schema` → system message: `"Respond with JSON matching: {schema}"`
> - `input_schema`  → user message: `"Input format: {schema}"`
>
> The schemas are serialised as compact JSON (no extra whitespace) to minimise token usage.
> No manual prompt engineering is required.

##### Imports

```python
from pydantic import BaseModel
from core.schema import (
    AgentNodeSchema,
    SchemaValidationResult,  # Validation result
)
from builder import GraphBuilder
```

##### 7.1. Create an agent with Pydantic schemas

```python
# Define input/output schemas as Pydantic models
class SolverInput(BaseModel):
    question: str
    context: str | None = None
    difficulty: int = 1

class SolverOutput(BaseModel):
    answer: str
    confidence: float  # 0.0 - 1.0
    explanation: str | None = None

# Create an agent with validation
builder = GraphBuilder()
builder.add_agent(
    "solver",
    display_name="Math Solver",
    persona="Expert mathematician",
    description="Solves mathematical problems",
    # Schemas for validation
    input_schema=SolverInput,
    output_schema=SolverOutput,
    # LLM configuration
    llm_backbone="gpt-4",
    temperature=0.0,
)

graph = builder.build()
```

##### 7.2. Using JSON Schema (without Pydantic)

You can pass a plain dict with JSON Schema:

```python
# JSON Schema directly (without Pydantic models)
input_schema = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "context": {"type": "string"},
    },
    "required": ["question"]
}

output_schema = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["answer", "confidence"]
}

builder.add_agent(
    "solver",
    input_schema=input_schema,    # JSON Schema dict
    output_schema=output_schema,  # JSON Schema dict
)
```

##### 7.3. Validation via RoleGraph

```python
# Check whether schemas exist
has_input = graph.has_input_schema("solver")    # True
has_output = graph.has_output_schema("solver")  # True

# Validate input data
result: SchemaValidationResult = graph.validate_agent_input(
    "solver",
    {"question": "Solve x^2 + 5x + 6 = 0"}
)

if result.valid:
    print("✅ Input is valid")
    print(f"Validated data: {result.validated_data}")
else:
    print("❌ Input validation failed")
    print(f"Errors: {result.errors}")

# Validate output data (JSON string or dict)
response = '{"answer": "x1=-2, x2=-3", "confidence": 0.95}'
result = graph.validate_agent_output("solver", response)

if result.valid:
    parsed = result.validated_data
    print(f"Answer: {parsed['answer']}")
    print(f"Confidence: {parsed['confidence']}")
else:
    print(f"Invalid output: {result.errors}")
    # You can raise an exception
    result.raise_if_invalid()  # -> ValueError
```

##### 7.4. Getting JSON Schema for prompts

```python
# Get JSON Schema for LLM instructions
input_schema_json = graph.get_input_schema_json("solver")
output_schema_json = graph.get_output_schema_json("solver")

# Use in the prompt
prompt = f"""You are a math solver.

INPUT FORMAT:
{json.dumps(input_schema_json, indent=2)}

You MUST respond in the following JSON format:
{json.dumps(output_schema_json, indent=2)}

Now solve: {{question}}
"""
```

##### 7.5. Validation directly via AgentNodeSchema

```python
# Create an agent with schemas
agent = AgentNodeSchema(
    id="solver",
    display_name="Math Solver",
    input_schema=SolverInput,
    output_schema=SolverOutput,
)

# Validate
result = agent.validate_input({"question": "2+2=?"})
print(f"Valid: {result.valid}")

result = agent.validate_output('{"answer": "4", "confidence": 0.99}')
print(f"Valid: {result.valid}, data: {result.validated_data}")

# Check schema presence
if agent.has_input_schema():
    print("Agent has input schema")
if agent.has_output_schema():
    print("Agent has output schema")
```

##### 7.6. Handling invalid LLM outputs

```python
# Scenario: the LLM responds in the wrong format
response = llm_call(prompt)
result = graph.validate_agent_output("solver", response)

if not result.valid:
    # Option 1: Retry with a stricter prompt
    retry_prompt = f"{prompt}\n\n⚠️ IMPORTANT: You MUST respond with valid JSON!"
    response = llm_call(retry_prompt)
    result = graph.validate_agent_output("solver", response)

    if not result.valid:
        # Option 2: Fallback to default values
        parsed = {
            "answer": response,
            "confidence": 0.5,
            "explanation": "LLM failed to format correctly"
        }
    else:
        parsed = result.validated_data
else:
    parsed = result.validated_data

print(f"Final answer: {parsed['answer']}")
```

##### 7.7. SchemaValidationResult API

```python
class SchemaValidationResult(BaseModel):
    """Schema validation result."""

    valid: bool                               # True if data is valid
    schema_type: str                          # "input" or "output"
    errors: list[str]                         # Validation errors
    warnings: list[str]                       # Validation warnings
    validated_data: dict[str, Any] | None     # Validated data
    message: str                              # Additional message

# Methods
result.raise_if_invalid()  # Raise ValueError if invalid
```

##### 7.8. Serialization support

When saving a graph:
- **Pydantic models** (`input_schema`/`output_schema`) are **NOT** serialized (exclude=True)
- **JSON Schema** (`input_schema_json`/`output_schema_json`) **is** serialized

```python
# When creating an agent with a Pydantic model
agent = AgentNodeSchema(
    id="solver",
    input_schema=SolverInput,     # Not serialized
    output_schema=SolverOutput,   # Not serialized
)

# JSON Schema is extracted automatically
print(agent.input_schema_json)   # {'type': 'object', 'properties': {...}}
print(agent.output_schema_json)  # {'type': 'object', 'properties': {...}}

# When deserializing a graph from JSON
# Pydantic models are lost, but JSON Schema remains
# Validation works via basic type checks
```

##### When should you use input/output schemas?

| Scenario | Recommendation |
|----------|----------------|
| **Structured data** | ✅ Use Pydantic schemas |
| **JSON outputs from an LLM** | ✅ Required! Parsing and validation |
| **Free-form text** | ❌ Not needed |
| **API integration** | ✅ Guarantees correct data |
| **Debugging** | ✅ Quickly surfaces issues |

##### Performance impact

- ✅ **Validation does not consume tokens** — it is pure Python
- ⚠️ **Prompt instructions consume tokens** — embedding JSON Schema into prompts increases token usage
- ⚡ **Validation is fast** — Pydantic is optimized for speed

##### Validation FAQ

**Q: Is this required?**
A: No, it is fully optional. If schemas are not set, validation is skipped.

**Q: What if the LLM cannot respond in the required format?**
A: `validate_output()` returns `valid=False` plus errors. Options: retry/fallback/ignore.

**Q: Can I pass plain JSON Schema?**
A: Yes. Pass a dict with JSON Schema instead of a Pydantic model.

**Q: Does token usage increase?**
A: Validation does not consume tokens. But including JSON Schema in prompts does increase token usage.

---

### Builder API (Detailed)

Different ways to construct graphs.

#### 1. build_property_graph (quick construction)

```python
from builder import build_property_graph

graph = build_property_graph(
    agents=[agent1, agent2, agent3],
    workflow_edges=[("agent1", "agent2"), ("agent2", "agent3")],
    context_edges=[("agent1", "agent3")],  # Additional connections
    query="Solve this task",
    include_task_node=True,                # Add a task node
    task_node_id="__task__",               # Task node ID
    connect_task_to_all=False,             # Connect task to all agents
    edge_weights=None,                     # Custom edge weights
    default_weight=1.0,                    # Default weight
    bidirectional=False,                   # Bidirectional edges
    encoder=None,                          # NodeEncoder for embeddings
    compute_embeddings=False,              # Compute embeddings immediately
)
```

#### 2. GraphBuilder (fluent API)

```python
from builder import GraphBuilder

builder = GraphBuilder()

# Add agents (basic)
builder.add_agent(
    agent_id="researcher",
    display_name="Researcher",
    description="Does research",
    tools=["search", "read"],
)

# Add an agent with multi-model configuration
builder.add_agent(
    agent_id="analyst",
    display_name="Senior Analyst",
    persona="Expert data analyst",
    # LLM configuration
    llm_backbone="gpt-4",               # Model name
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",          # Or $ENV_VAR
    temperature=0.7,
    max_tokens=2000,
    timeout=60.0,
    top_p=0.9,
    stop_sequences=["END", "STOP"],
)

# Or via an LLMConfig object
from core.schema import LLMConfig

llm_config = LLMConfig(
    model_name="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.7,
    max_tokens=2000,
)

builder.add_agent(
    agent_id="writer",
    display_name="Writer",
    llm_config=llm_config,  # Pass a ready configuration
)

# Add edges
builder.add_workflow_edge("researcher", "writer", weight=0.9)
builder.add_context_edge("researcher", "writer", weight=0.5)

# Add a task
builder.set_task(query="Write a report", description="Main task")

# Conditional edges
def quality_check(state: dict) -> bool:
    return state.get("quality_score", 0) > 0.8

builder.add_conditional_edge(
    source="writer",
    target="editor",
    condition=quality_check,
    weight=0.9,
)

# Set execution bounds (new!)
builder.set_start_node("researcher")  # Start node
builder.set_end_node("writer")        # End node
# Or both at once:
builder.set_execution_bounds("researcher", "writer")

# Build the graph
graph = builder.build(compute_embeddings=True, encoder=my_encoder)

# Validate before building
is_valid, errors = builder.validate()
if not is_valid:
    print(f"Errors: {errors}")
```

#### 3. build_from_adjacency (from a matrix)

```python
from builder import build_from_adjacency
import torch

adjacency = torch.tensor([
    [0, 1, 0],
    [0, 0, 1],
    [0, 0, 0],
], dtype=torch.float32)

graph = build_from_adjacency(
    adjacency_matrix=adjacency,
    agents=[agent1, agent2, agent3],
    query="Task",
    threshold=0.1,  # Ignore edges with weight < threshold
)
```

#### 4. build_from_schema (from a schema)

```python
from builder import build_from_schema

graph = build_from_schema(
    schema=my_schema,
    compute_embeddings=True,
    encoder=my_encoder,
    validate=True,  # Validate before building
)
```

#### 5. AutoGraphBuilder (LLM-powered automatic assembly)

Automatically build graphs using an LLM — either from existing agents (topology only) or from scratch (agents + topology).

```python
from builder import AutoGraphBuilder, AutoBuilderConfig
```

**Mode 1: Topology from existing agents**

The LLM receives agent descriptions and proposes the optimal workflow edges, start/end nodes.

```python
from core.agent import AgentProfile
from builder import AutoGraphBuilder

# 1. Define agents as usual
agents = [
    AgentProfile(agent_id="researcher", persona="an expert researcher",
                 description="Searches and analyzes information"),
    AgentProfile(agent_id="writer", persona="a technical writer",
                 description="Writes clear reports from research data"),
    AgentProfile(agent_id="reviewer", persona="a quality reviewer",
                 description="Reviews documents for accuracy"),
]

# 2. Provide an LLM caller (same interface as MACPRunner structured_llm_caller)
def my_llm(messages: list[dict[str, str]]) -> str:
    return openai_client.chat.completions.create(
        model="gpt-4", messages=messages
    ).choices[0].message.content

# 3. Assemble — LLM designs the topology
auto = AutoGraphBuilder(llm_caller=my_llm)
graph = auto.assemble_topology(agents=agents, query="Research and report on AI trends")

print(graph.node_ids)        # ['researcher', 'writer', 'reviewer', ...]
print(graph.start_node)      # 'researcher'
print(graph.end_node)        # 'reviewer'
```

**Mode 2: Full assembly from scratch**

The LLM designs both the agents and the topology.

```python
from builder import AutoGraphBuilder, AutoBuilderConfig

config = AutoBuilderConfig(
    max_agents=5,
    available_tools=["web_search", "code_interpreter"],
    default_llm_backbone="gpt-4o-mini",
)

auto = AutoGraphBuilder(llm_caller=my_llm, config=config)
graph = auto.assemble_full(query="Build a market analysis pipeline")

# The LLM created agents and connected them:
for agent in graph.agents:
    if hasattr(agent, 'persona'):
        print(f"  {agent.agent_id}: {agent.persona}")
```

**Async support:**

```python
graph = await auto.assemble_topology_async(agents=agents, query="...")
graph = await auto.assemble_full_async(query="...")
```

**AutoBuilderConfig options:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_retries` | `int` | `3` | Retry attempts on LLM parse/validation errors |
| `max_agents` | `int` | `10` | Maximum agents when generating from scratch |
| `include_task_node` | `bool` | `True` | Add a virtual task node |
| `default_llm_backbone` | `str \| None` | `None` | Default model for generated agents |
| `default_temperature` | `float \| None` | `None` | Default temperature for generated agents |
| `available_tools` | `list[str] \| None` | `None` | Restrict tools the LLM can assign |
| `builder_config` | `BuilderConfig \| None` | `None` | Override GraphBuilder settings |
| `topology_prompt` | `str \| None` | `None` | Custom system prompt for topology generation |
| `agents_prompt` | `str \| None` | `None` | Custom system prompt for agent generation (supports `{max_agents}` and `{tools_section}` placeholders) |

**Custom prompts:**

You can replace the built-in LLM prompts at three levels (highest priority wins):

1. **Per-call** — pass `system_prompt=` to `assemble_topology()` or `topology_prompt=`/`agents_prompt=` to `assemble_full()`.
2. **Config-level** — set `topology_prompt` / `agents_prompt` on `AutoBuilderConfig`.
3. **Default** — the built-in prompts are used when nothing is specified.

```python
# Custom prompt via config
config = AutoBuilderConfig(
    topology_prompt="You are a workflow architect. Given a set of agents, "
                    "design an efficient DAG. Return JSON with edges, "
                    "start_node, end_node.",
)
auto = AutoGraphBuilder(llm_caller=my_llm, config=config)
graph = auto.assemble_topology(agents=agents, query="...")

# Per-call override (takes priority over config)
graph = auto.assemble_topology(
    agents=agents,
    query="...",
    system_prompt="Build a strictly sequential chain of agents. "
                  "Return JSON with edges, start_node, end_node.",
)

# Full assembly with custom prompts for both stages
graph = auto.assemble_full(
    query="...",
    agents_prompt="Design up to {max_agents} agents.\n{tools_section}\nReturn JSON.",
    topology_prompt="Connect the agents in a fan-out pattern. Return JSON.",
)
```

---

#### 6. EmbeddingGraphBuilder (similarity-based assembly)

Build a graph by computing semantic similarity between agent descriptions.
Agents whose embeddings are close get connected — no LLM required.

```python
from builder import EmbeddingGraphBuilder, EmbeddingBuilderConfig
```

**Quick start — k-nearest neighbours:**

```python
from core.agent import AgentProfile
from builder import EmbeddingGraphBuilder, EmbeddingBuilderConfig

agents = [
    AgentProfile(agent_id="researcher", display_name="Researcher",
                 persona="a web researcher", description="Searches for information"),
    AgentProfile(agent_id="analyst", display_name="Analyst",
                 persona="a data analyst", description="Analyzes data"),
    AgentProfile(agent_id="writer", display_name="Writer",
                 persona="a content writer", description="Writes reports"),
    AgentProfile(agent_id="reviewer", display_name="Reviewer",
                 persona="a reviewer", description="Reviews outputs"),
]

builder = EmbeddingGraphBuilder(
    config=EmbeddingBuilderConfig(strategy="knn", k=2),
)
graph = builder.build(agents, query="Research and report on AI trends")
```

**Minimum spanning tree (ensures connected graph):**

```python
builder = EmbeddingGraphBuilder(
    config=EmbeddingBuilderConfig(
        strategy="mst",
        mst_shortcut_threshold=0.7,  # add extra high-similarity edges
    ),
)
graph = builder.build(agents, query="Multi-step analysis")
```

**Threshold-based (connect all pairs above a similarity score):**

```python
builder = EmbeddingGraphBuilder(
    config=EmbeddingBuilderConfig(strategy="threshold", threshold=0.6),
)
graph = builder.build(agents, query="Collaborative task")
```

**Inspect the similarity matrix:**

```python
sim_matrix, agent_ids = builder.compute_similarity_matrix(agents)
print(sim_matrix)  # (n x n) cosine similarity tensor
```

**EmbeddingBuilderConfig options:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `strategy` | `"knn" \| "threshold" \| "mst"` | `"knn"` | Edge selection strategy |
| `k` | `int` | `2` | Neighbours per node (knn) |
| `threshold` | `float` | `0.5` | Cosine similarity threshold (threshold / mst shortcut) |
| `mst_shortcut_threshold` | `float \| None` | `None` | Extra high-similarity edges on top of MST |
| `symmetric` | `bool` | `False` | If True, create bidirectional edges |
| `include_task_node` | `bool` | `True` | Add a virtual task node |
| `encoder` | `NodeEncoder \| None` | `None` | Custom encoder (default: sentence-transformers) |
| `builder_config` | `BuilderConfig \| None` | `None` | Override GraphBuilder settings |

**Strategies explained:**

- **knn** — each agent connects to its *k* most similar peers. Good default for most workflows.
- **threshold** — all pairs whose similarity ≥ threshold are connected. Good when you want explicit control over density.
- **mst** — minimum spanning tree guarantees connectivity with minimal edges. Use `mst_shortcut_threshold` to add high-similarity shortcuts.

Edge direction is inferred automatically: generalist agents (high average similarity to all) point toward specialist agents. Set `symmetric=True` for bidirectional edges.

---

### Event System

Subscribe to events for monitoring and debugging.

```python
from core.events import (
    EventBus,
    global_event_bus,
    EventType,
    LoggingEventHandler,
    MetricsEventHandler,
    on_event,
    # Events
    NodeAddedEvent,
    EdgeAddedEvent,
    StepCompletedEvent,
    BudgetWarningEvent,
)

# Get the global event bus
bus = global_event_bus()

# 1. Subscribe via a handler
logging_handler = LoggingEventHandler(
    log_level="INFO",
    include_metadata=True,
)
bus.subscribe(EventType.STEP_COMPLETED, logging_handler)

# 2. Subscribe via a function
def on_step_completed(event):
    if isinstance(event, StepCompletedEvent):
        print(f"Agent {event.agent_id} completed: {event.tokens_used} tokens")

bus.subscribe(EventType.STEP_COMPLETED, on_step_completed)

# 3. Subscribe via a decorator
@on_event(EventType.BUDGET_WARNING)
def handle_budget_warning(event: BudgetWarningEvent):
    print(f"⚠️  Budget warning: {event.budget_type} at {event.ratio:.1%}")

# 4. Global subscription (all events)
@on_event(None)
def handle_all_events(event):
    print(f"Event: {event.event_type.value}")

# Disable event handling
bus.disable()

# Enable
bus.enable()

# Clear all handlers
bus.clear()

# Aggregate metrics via events
metrics_handler = MetricsEventHandler()
bus.subscribe(None, metrics_handler)

# After execution
metrics = metrics_handler.get_metrics()
print(f"Total tokens: {metrics['total_tokens']}")
print(f"Errors: {metrics['errors_count']}")
print(f"Budget warnings: {metrics['budget_warnings']}")
```

---

### Callback system

Monitoring and logging execution via callback handlers.

#### Core concepts

- **`BaseCallbackHandler`** — base class for creating callback handlers
- **`AsyncCallbackHandler`** — async version for asynchronous operations
- **`CallbackManager`** — manager that orchestrates and invokes handlers
- **Built-in handlers** — StdoutCallbackHandler, MetricsCallbackHandler, FileCallbackHandler

#### Quick start

```python
from execution import MACPRunner
from callbacks import (
    StdoutCallbackHandler,
    MetricsCallbackHandler,
    FileCallbackHandler,
)

# 1. Callbacks via RunnerConfig
from execution import RunnerConfig

config = RunnerConfig(
    callbacks=[
        StdoutCallbackHandler(show_outputs=True),
        MetricsCallbackHandler(),
    ]
)

runner = MACPRunner(llm_caller=my_llm, config=config)
result = runner.run_round(graph)

# 2. Per-run callbacks (override config)
result = runner.run_round(
    graph,
    callbacks=[FileCallbackHandler("execution_log.jsonl")]
)
```

#### Context Manager

```python
from callbacks import collect_metrics, trace_as_callback

# 1. Collect metrics
with collect_metrics() as metrics:
    runner.run_round(graph)

    print(f"Total tokens: {metrics.total_tokens}")
    print(f"Total duration: {metrics.total_duration_ms}ms")
    print(f"Runs completed: {metrics.runs_completed}")
    print(f"Runs failed: {metrics.runs_failed}")

    # Full statistics
    all_metrics = metrics.get_metrics()
    print(f"Agent calls: {all_metrics['agent_calls']}")
    print(f"Errors: {all_metrics['errors_count']}")

# 2. Tracing with arbitrary handlers
from callbacks import StdoutCallbackHandler

with trace_as_callback(handlers=[StdoutCallbackHandler()]) as manager:
    runner.run_round(graph)
    # Callbacks are automatically applied to this run
```

#### Creating your own CallbackHandler

```python
from callbacks import BaseCallbackHandler
from uuid import UUID

class MySlackAlertHandler(BaseCallbackHandler):
    """Sends Slack alerts on errors."""

    def on_run_start(
        self,
        *,
        run_id: UUID,
        query: str,
        num_agents: int = 0,
        **kwargs,
    ) -> None:
        send_slack(f"🚀 Started run {run_id}: {num_agents} agents")

    def on_agent_end(
        self,
        *,
        run_id: UUID,
        agent_id: str,
        output: str,
        tokens_used: int = 0,
        duration_ms: float = 0.0,
        **kwargs,
    ) -> None:
        print(f"✅ Agent {agent_id}: {tokens_used} tokens, {duration_ms:.0f}ms")

    def on_agent_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        agent_id: str,
        **kwargs,
    ) -> None:
        send_slack_alert(
            f"❌ Agent {agent_id} failed in run {run_id}: {error}",
            severity="high"
        )

    def on_run_end(
        self,
        *,
        run_id: UUID,
        output: str,
        success: bool = True,
        total_tokens: int = 0,
        **kwargs,
    ) -> None:
        if not success:
            send_slack_alert(f"🛑 Run {run_id} failed!")
        else:
            send_slack(f"✅ Run {run_id} completed: {total_tokens} tokens")

# Usage
runner = MACPRunner(
    llm_caller=my_llm,
    config=RunnerConfig(callbacks=[MySlackAlertHandler()])
)
```

#### Async Callbacks

```python
from callbacks import AsyncCallbackHandler
import aiohttp

class AsyncWebhookHandler(AsyncCallbackHandler):
    """Asynchronously sends a webhook on events."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    async def on_run_start(
        self,
        *,
        run_id: UUID,
        query: str,
        **kwargs,
    ) -> None:
        async with aiohttp.ClientSession() as session:
            await session.post(
                self.webhook_url,
                json={"event": "run_start", "run_id": str(run_id), "query": query}
            )

    async def on_agent_end(
        self,
        *,
        run_id: UUID,
        agent_id: str,
        output: str,
        tokens_used: int = 0,
        **kwargs,
    ) -> None:
        async with aiohttp.ClientSession() as session:
            await session.post(
                self.webhook_url,
                json={
                    "event": "agent_end",
                    "run_id": str(run_id),
                    "agent_id": agent_id,
                    "tokens": tokens_used,
                }
            )

# Usage with async runner
runner = MACPRunner(
    async_llm_caller=my_async_llm,
    config=RunnerConfig(callbacks=[AsyncWebhookHandler("https://api.example.com/webhook")])
)

result = await runner.arun_round(graph)
```

#### Built-in handlers

##### 1. StdoutCallbackHandler — console output

```python
from callbacks import StdoutCallbackHandler

handler = StdoutCallbackHandler(
    color=True,                  # Colored output
    show_prompts=False,          # Show prompts
    show_outputs=True,           # Show agent outputs
    truncate_length=200,         # Output truncation length
)

runner = MACPRunner(
    llm_caller=my_llm,
    config=RunnerConfig(callbacks=[handler])
)

# Output example:
# 🚀 Run started: 5 agents
#    Order: researcher → analyst → writer → editor → publisher
#   ▶️  [0] Researcher started
#     🛠️  Tool 'web_search.search' started with args: {query: "market analysis"}
#     ✅ Success Tool 'web_search.search' ended (1200ms, 3500 chars)
#   ✅ [0] Researcher completed: 150 tokens, 1200ms
#      Output: Market analysis shows strong growth...
#   ▶️  [1] Analyst started
#   ✅ [1] Analyst completed: 200 tokens, 1500ms [FINAL]
# ✅ Run completed: 350 tokens, 2700ms
```

##### 2. MetricsCallbackHandler — metrics aggregation

```python
from callbacks import MetricsCallbackHandler

metrics_handler = MetricsCallbackHandler()

runner = MACPRunner(
    llm_caller=my_llm,
    config=RunnerConfig(callbacks=[metrics_handler])
)

result = runner.run_round(graph)

# Retrieve metrics
metrics = metrics_handler.get_metrics()

print(f"Total tokens: {metrics['total_tokens']}")
print(f"Total duration: {metrics['total_duration_ms']}ms")
print(f"Agent calls: {metrics['agent_calls']}")        # {'researcher': 1, 'writer': 1, ...}
print(f"Agent tokens: {metrics['agent_tokens']}")      # {'researcher': 150, ...}
print(f"Errors: {metrics['errors_count']}")
print(f"Retries: {metrics['retries']}")
print(f"Budget warnings: {metrics['budget_warnings']}")
print(f"Runs completed: {metrics['runs_completed']}")

# Averages
print(f"Avg tokens per agent: {metrics['avg_tokens_per_agent']}")

# Tool metrics (WebSearchTool and other tools)
print(f"Tool calls: {metrics['tool_calls']}")            # {'web_search.search': 3, 'web_search.fetch': 1}
print(f"Tool durations: {metrics['tool_durations']}")    # {'web_search.search': 3600.0, ...}
print(f"Tool errors: {metrics['tool_errors_count']}")    # 0

# Last 10 errors
for error in metrics['errors']:
    print(f"Error in {error['agent_id']}: {error['error_message']}")

# Last 10 tool errors
for error in metrics['tool_errors']:
    print(f"Tool error: {error['tool_name']}.{error['action']}: {error['error_message']}")

# Reset metrics
metrics_handler.reset()
```

##### 3. FileCallbackHandler — write to a JSON Lines file

```python
from callbacks import FileCallbackHandler

handler = FileCallbackHandler(
    file_path="execution_log.jsonl",
    append=True,           # Append or overwrite
    flush_every=1,         # Flush after each event
)

runner = MACPRunner(
    llm_caller=my_llm,
    config=RunnerConfig(callbacks=[handler])
)

result = runner.run_round(graph)

# Close the file manually (or it is closed automatically via __del__)
handler.close()

# File format (JSON Lines):
# {"event_type": "run_start", "timestamp": "2024-...", "run_id": "...", "query": "...", "num_agents": 5}
# {"event_type": "agent_start", "timestamp": "...", "run_id": "...", "agent_id": "researcher", ...}
# {"event_type": "agent_end", "timestamp": "...", "run_id": "...", "agent_id": "researcher", "tokens_used": 150, ...}
```

#### Available callback methods

| Method | Description | Parameters |
|-------|-------------|-----------|
| `on_run_start` | Run start | `run_id`, `query`, `num_agents`, `execution_order` |
| `on_run_end` | Run end | `run_id`, `output`, `success`, `error`, `total_tokens`, `total_time_ms`, `executed_agents` |
| `on_agent_start` | Agent started | `run_id`, `agent_id`, `agent_name`, `step_index`, `prompt`, `predecessors` |
| `on_agent_end` | Agent finished | `run_id`, `agent_id`, `output`, `tokens_used`, `duration_ms`, `is_final` |
| `on_agent_error` | Agent error | `error`, `run_id`, `agent_id`, `error_type`, `will_retry`, `attempt` |
| `on_retry` | Retry attempt | `run_id`, `agent_id`, `attempt`, `max_attempts`, `delay_ms`, `error` |
| `on_llm_new_token` | New token (streaming) | `token`, `run_id`, `agent_id`, `token_index`, `is_first`, `is_last` |
| `on_plan_created` | Plan created | `run_id`, `num_steps`, `execution_order` |
| `on_topology_changed` | Topology changed | `run_id`, `reason`, `old_remaining`, `new_remaining`, `change_count` |
| `on_prune` | Agent pruned | `run_id`, `agent_id`, `reason` |
| `on_fallback` | Fallback activated | `run_id`, `failed_agent_id`, `fallback_agent_id`, `reason` |
| `on_parallel_start` | Parallel group start | `run_id`, `agent_ids`, `group_index` |
| `on_parallel_end` | Parallel group end | `run_id`, `agent_ids`, `successful`, `failed` |
| `on_memory_read` | Memory read | `run_id`, `agent_id`, `entries_count`, `keys` |
| `on_memory_write` | Memory write | `run_id`, `agent_id`, `key`, `value_size` |
| `on_budget_warning` | Budget warning | `run_id`, `budget_type`, `current`, `limit`, `ratio` |
| `on_budget_exceeded` | Budget exceeded | `run_id`, `budget_type`, `current`, `limit`, `action_taken` |
| `on_tool_start` | Tool started | `run_id`, `tool_name`, `action`, `arguments` |
| `on_tool_end` | Tool finished | `run_id`, `tool_name`, `action`, `success`, `duration_ms`, `output_size`, `result_summary` |
| `on_tool_error` | Tool error | `run_id`, `tool_name`, `action`, `error_type`, `error_message` |

#### Tool Callback Events

Tools emit events via the callback system. This lets you monitor all tool actions without direct logging.

**Event types:**

| Event | Class | Description |
|------|-------|-------------|
| `TOOL_START` | `ToolStartEvent` | Tool action started |
| `TOOL_END` | `ToolEndEvent` | Tool action successfully completed |
| `TOOL_ERROR` | `ToolErrorEvent` | Tool action failed |

**Example: handling tool events**

```python
from callbacks import BaseCallbackHandler, CallbackManager
from tools import WebSearchTool
from uuid import UUID

class ToolMonitorHandler(BaseCallbackHandler):
    """Monitor all tool actions."""

    def on_tool_start(
        self,
        *,
        run_id: UUID,
        tool_name: str,
        action: str,
        arguments: dict,
        **kwargs,
    ) -> None:
        print(f"[TOOL] {tool_name}.{action} started with {arguments}")

    def on_tool_end(
        self,
        *,
        run_id: UUID,
        tool_name: str,
        action: str,
        success: bool = True,
        duration_ms: float = 0.0,
        output_size: int = 0,
        result_summary: str = "",
        **kwargs,
    ) -> None:
        status = "OK" if success else "FAIL"
        print(f"[TOOL] {tool_name}.{action} {status} ({duration_ms:.0f}ms, {output_size} chars)")

    def on_tool_error(
        self,
        error: BaseException = None,
        *,
        run_id: UUID,
        tool_name: str,
        action: str,
        error_type: str = "",
        error_message: str = "",
        **kwargs,
    ) -> None:
        print(f"[TOOL ERROR] {tool_name}.{action}: {error_type} - {error_message}")

# Usage
cb = CallbackManager(handlers=[ToolMonitorHandler()])
tool = WebSearchTool(callback_manager=cb)
tool.execute(query="Python tutorials")
# [TOOL] web_search.search started with {'query': 'Python tutorials'}
# [TOOL] web_search.search OK (1200ms, 3500 chars)
```

**Built-in handlers already support tool events:**
- `StdoutCallbackHandler` — prints tool events to console with emoji
- `MetricsCallbackHandler` — collects metrics for tool_calls, tool_durations, tool_errors

#### Ignore flags

You can disable specific event types:

```python
class MyMinimalHandler(BaseCallbackHandler):
    # Ignore most events
    ignore_llm = True       # Do not call on_llm_new_token
    ignore_retry = True     # Do not call on_retry
    ignore_budget = True    # Do not call on_budget_*
    ignore_memory = True    # Do not call on_memory_*
    ignore_tool = True      # Do not call on_tool_start/end/error

    # Handle only errors
    def on_agent_error(self, error, *, run_id, agent_id, **kwargs):
        log_critical_error(agent_id, error)
```

#### Combining handlers

```python
from callbacks import (
    StdoutCallbackHandler,
    MetricsCallbackHandler,
    FileCallbackHandler,
)

# You can use multiple handlers at the same time
runner = MACPRunner(
    llm_caller=my_llm,
    config=RunnerConfig(callbacks=[
        StdoutCallbackHandler(show_outputs=False),  # Only status to console
        MetricsCallbackHandler(),                   # Metrics collection
        FileCallbackHandler("debug.jsonl"),         # Full log to file
        MySlackAlertHandler(),                      # Slack alerts
    ])
)
```

---

### State Storage

Persistent storage for node states.

```python
from utils.state_storage import (
    InMemoryStateStorage,
    FileStateStorage,
)

# 1. In-memory storage
storage = InMemoryStateStorage()

storage.save("agent_id", {"messages": [...], "context": {...}})
state = storage.load("agent_id")
storage.delete("agent_id")

all_keys = storage.keys()
storage.clear()

# 2. File-based storage
storage = FileStateStorage(directory="./agent_states")

storage.save("researcher", {
    "messages": [{"role": "user", "content": "Hello"}],
    "iteration": 5,
})

state = storage.load("researcher")
if state:
    print(f"Iteration: {state['iteration']}")

storage.delete("researcher")

# Get all stored IDs
all_agent_ids = storage.keys()

# Clear all states
storage.clear()
```

---

### Async Utils

Helper functions for asynchronous execution.

```python
from utils.async_utils import (
    run_sync,
    gather_with_concurrency,
    timeout_wrapper,
)

# 1. Run a coroutine synchronously
async def my_async_function():
    return "result"

result = run_sync(my_async_function(), context="my_context")

# 2. Parallel execution with a concurrency limit
async def fetch_data(agent_id: str):
    # ... async call ...
    return response

async def main():
    tasks = [fetch_data(f"agent_{i}") for i in range(20)]

    # Run no more than 5 at once
    results = await gather_with_concurrency(5, *tasks)
    return results

# 3. Timeouts
async def slow_operation():
    await asyncio.sleep(10)
    return "done"

async def main():
    try:
        result = await timeout_wrapper(
            slow_operation(),
            timeout=5.0,
            error_message="Operation took too long",
        )
    except TimeoutError as e:
        print(f"Timeout: {e}")
```

---

### Conditional Routing

Dynamic selection of the next agent based on conditions.

```python
from core.graph import ConditionalEdge
from execution.scheduler import ConditionContext, ConditionEvaluator

# 1. Define conditional edges
def quality_above_threshold(context: ConditionContext) -> bool:
    """Go to editor only if quality > 0.8"""
    quality = context.state.get("quality_score", 0)
    return quality > 0.8

def has_errors(context: ConditionContext) -> bool:
    """Go to fixer if there are errors"""
    return "errors" in context.state and len(context.state["errors"]) > 0

# Add conditional edges to the graph
graph.add_conditional_edge(
    source="writer",
    targets={
        "editor": quality_above_threshold,
        "fixer": has_errors,
    },
    default="reviewer",  # Fallback if no condition matches
)

# 2. Use via the builder
from builder import GraphBuilder

builder = GraphBuilder()
builder.add_agent(agent_id="writer", display_name="Writer")
builder.add_agent(agent_id="editor", display_name="Editor")
builder.add_agent(agent_id="fixer", display_name="Fixer")

builder.add_conditional_edge(
    source="writer",
    target="editor",
    condition=quality_above_threshold,
    weight=0.9,
)
builder.add_conditional_edge(
    source="writer",
    target="fixer",
    condition=has_errors,
    weight=0.7,
)

graph = builder.build()

# 3. Evaluate conditions at runtime
evaluator = ConditionEvaluator()

context = ConditionContext(
    current_node="writer",
    state={"quality_score": 0.85, "errors": []},
    history=["researcher", "writer"],
    metadata={"iteration": 1},
)

# Evaluate a single condition
if evaluator.evaluate(quality_above_threshold, context):
    next_node = "editor"

# Evaluate all conditions for a node
next_nodes = evaluator.evaluate_all(graph, "writer", context)
print(f"Next nodes: {next_nodes}")
```

---

### Agent Tools (Tools)

The `tools` module allows agents to use external tools via Native Function Calling.

**Key principle:** If an agent has tools specified, they are **ALWAYS** used automatically on every LLM call.

**Built-in tools:**
- `shell` — execute shell commands
- `code_interpreter` — execute Python code in a sandbox
- `file_search` — search files and their contents
- `web_search` — search the web (DuckDuckGo, Brave, Serper, Tavily, Exa, SearXNG, Bocha, Google) with auto-routing + Playwright/Selenium browser for dynamic pages
- `computer_use` - stateful desktop automation with sessions, observations, and actions
- `function_calling` — call custom functions

#### Quick start

```python
from builder import GraphBuilder
from execution import MACPRunner
from tools import tool, OpenAIToolsCaller
from openai import OpenAI

# 1. Register tools via the @tool decorator
@tool
def fibonacci(n: int) -> str:
    """Calculate the n-th Fibonacci number."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return str(a)

@tool
def is_prime(n: int) -> str:
    """Check if a number is prime."""
    if n < 2:
        return "False"
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return "False"
    return "True"

# 2. Create an agent with tools
builder = GraphBuilder()
builder.add_agent(
    agent_id="math",
    display_name="Math Agent",
    persona="a helpful math assistant",
    tools=["fibonacci", "is_prime"],  # <-- tools are specified here!
)
builder.add_task(query="Calculate fibonacci(20) and check if it's prime")
builder.connect_task_to_agents(agent_ids=["math"])

# 3. Create caller and runner
client = OpenAI(api_key="...")
caller = OpenAIToolsCaller(client, model="gpt-4")
runner = MACPRunner(llm_caller=caller)

# 4. Run — tools are used AUTOMATICALLY
result = runner.run_round(builder.build())
print(result.final_answer)
```

**Important:**
- Tools are set when creating an agent via the `tools` parameter
- Runner automatically passes tools to the LLM via the API
- No `enable_tools` flags are needed — it works automatically

#### Two ways to register tools

**Method 1: Global `@tool` decorator (recommended)**

```python
from tools import tool

@tool
def calculate(expression: str) -> str:
    """Evaluate a math expression."""
    return str(eval(expression))

@tool
def search_web(query: str) -> str:
    """Search the web for information."""
    return f"Results for: {query}"
```

**Method 2: Via ToolRegistry**

```python
from tools import ToolRegistry, get_registry

# Global registry
registry = get_registry()

@registry.function
def my_tool(arg: str) -> str:
    """Description for the LLM."""
    return arg.upper()

# Or create your own registry
my_registry = ToolRegistry()

@my_registry.function
def custom_tool(x: int) -> str:
    return str(x * 2)
```

#### Passing tools as objects

You can pass BaseTool objects directly into AgentProfile:

```python
from core.agent import AgentProfile
from tools import CodeInterpreterTool, ShellTool

# Create an agent with tool objects
agent = AgentProfile(
    agent_id="coder",
    display_name="Code Agent",
    persona="a Python programmer",
    tools=[CodeInterpreterTool(timeout=10), ShellTool()],  # <-- objects!
)

# Add to the graph
builder = GraphBuilder()
builder.add_agent_profile(agent)
```

#### Supported tools

| Tool | Description |
|------|-------------|
| `shell` | Execute shell commands |
| `function_calling` | Call registered Python functions (grouped) |
| `code_interpreter` | Execute Python code in a sandbox |
| `file_search` | Search files and file contents in directories |
| `computer_use` | Stateful desktop automation with `start/observe/act/close` lifecycle |

#### Base classes

```python
from tools import (
    BaseTool,              # Abstract base class for tools
    ToolCall,              # A tool-call request (parsed from LLM output)
    ToolResult,            # Tool execution result
    ToolRegistry,          # Tool registry
    ShellTool,             # Tool for shell commands
    FunctionTool,          # Tool for calling (grouped) functions
    CodeInterpreterTool,   # Tool for executing Python code
    FileSearchTool,        # Tool for searching files
)
```

#### ShellTool — executing shell commands

```python
from tools import ShellTool, ToolRegistry

# Create a ShellTool with safety settings
shell_tool = ShellTool(
    timeout=30,                               # Timeout in seconds
    max_output_size=8192,                     # Max output size
    working_dir="/path/to/dir",               # Working directory (optional)
    allowed_commands=["echo", "ls", "pwd"],   # Command allowlist (optional)
)

# Register in a registry
registry = ToolRegistry()
registry.register(shell_tool)

# Execute directly
result = shell_tool.execute(command="echo Hello World")
print(result.success)  # True
print(result.output)   # "Hello World"

# Or via the registry
from tools import ToolCall

call = ToolCall(name="shell", arguments={"command": "ls -la"})
result = registry.execute(call)
```

#### FunctionTool — calling custom functions

```python
from tools import FunctionTool, ToolRegistry

# Create a FunctionTool
func_tool = FunctionTool()

# Register functions via decorator
@func_tool.register
def calculate(expression: str) -> str:
    """Evaluate a math expression."""
    return str(eval(expression))

@func_tool.register
def uppercase(text: str) -> str:
    """Convert text to uppercase."""
    return text.upper()

@func_tool.register(name="word_count", description="Count words in text")
def count_words(text: str) -> int:
    """Count words."""
    return len(text.split())

# Register in the registry
registry = ToolRegistry()
registry.register(func_tool)

# Call a function
result = func_tool.execute(function="calculate", expression="2 ** 10")
print(result.output)  # "1024"

# List registered functions
print(func_tool.list_functions())  # ['calculate', 'uppercase', 'word_count']
```

#### Two ways to register functions

There are two ways to register functions as tools:

**Method 1: Via FunctionTool (grouped functions)**

Functions are grouped under a single tool named `function_calling`. The LLM must call them like this:
```json
{"name": "function_calling", "arguments": {"function": "calculate", "expression": "2+2"}}
```

```python
func_tool = FunctionTool()

@func_tool.register
def calculate(expression: str) -> str:
    return str(eval(expression))

registry.register(func_tool)
```

**Method 2: Via `@registry.function` (separate tools) — RECOMMENDED**

Each function becomes a separate tool. The LLM calls them directly:
```json
{"name": "calculate", "arguments": {"expression": "2+2"}}
```

```python
@registry.function
def calculate(expression: str) -> str:
    return str(eval(expression))

@registry.function
def fibonacci(n: int) -> str:
    """Calculate the n-th Fibonacci number."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return str(a)
```

**Recommendation:** Use `@registry.function` — it is simpler for the LLM and avoids confusion with nested arguments.

#### CodeInterpreterTool — executing Python code

Allows agents to execute arbitrary Python code in a safe sandbox environment.

```python
from tools import CodeInterpreterTool, ToolRegistry, ToolCall

# Create a CodeInterpreterTool
code_tool = CodeInterpreterTool(
    timeout=30,           # Execution timeout in seconds
    max_output_size=8192, # Maximum output size
    safe_mode=True,       # Restricted builtins for safety
)

# Register
registry = ToolRegistry()
registry.register(code_tool)

# Example 1: Simple computation
result = code_tool.execute(code="2 ** 10 + sum(range(5))")
print(result.output)  # "1034"

# Example 2: Multi-line code with functions
code = """
def fibonacci(n):
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

for i in range(10):
    print(f"fib({i}) = {fibonacci(i)}")
"""
result = code_tool.execute(code=code)
print(result.output)
# fib(0) = 0
# fib(1) = 1
# fib(2) = 1
# ...

# Example 3: Using preloaded modules
# Available in sandbox: math, statistics, json, re, datetime,
# collections, itertools, functools, random
result = code_tool.execute(code="""
# Modules are already loaded; no import needed
print(f"pi = {math.pi:.6f}")
print(f"e = {math.e:.6f}")
data = {"name": "Alice", "age": 30}
print(json.dumps(data, indent=2))
""")
print(result.output)

# Example 4: Error handling
result = code_tool.execute(code="1 / 0")
print(result.success)  # False
print(result.error)    # "ZeroDivisionError: division by zero"
```

**Safety:**
- With `safe_mode=True`, built-in functions are restricted
- Forbidden: `open`, `exec`, `eval`, `__import__`, `compile`
- Only safe modules are available
- Timeout prevents infinite loops

#### FileSearchTool — searching files and contents

Allows agents to search files by name, search text within files, and read file contents.

```python
from tools import FileSearchTool, ToolRegistry, ToolCall

# Create a FileSearchTool
file_tool = FileSearchTool(
    base_directory="./project",   # Base directory to search within
    max_results=50,               # Maximum number of results
    max_depth=10,                 # Maximum recursion depth
    max_file_size=100_000,        # Max file size for content search
    max_read_size=10_000,         # Max size for reading a file
    allowed_extensions=[".py", ".txt", ".md"],  # Allowed extensions (optional)
)

registry = ToolRegistry()
registry.register(file_tool)

# Example 1: Find all Python files
result = file_tool.execute(pattern="*.py")
print(result.output)
# Found 15 file(s) matching '*.py':
#   src/main.py (1,234 bytes)
#   src/utils.py (567 bytes)
#   ...

# Example 2: Search in a specific directory
result = file_tool.execute(pattern="test_*.py", directory="tests")
print(result.output)

# Example 3: Search within file contents
result = file_tool.execute(pattern="*.py", query="def main")
print(result.output)
# Search results for 'def main' in 15 file(s):
# Found 3 match(es).
# === src/main.py ===
#   42: def main():
# === src/cli.py ===
#   15: def main_entry():
#   ...

# Example 4: Regex search
result = file_tool.execute(pattern="*.py", query=r"def \w+_handler", regex=True)

# Example 5: Read a specific file
result = file_tool.execute(read_file="src/config.py")
print(result.output)
# === src/config.py ===
# """Configuration module."""
# import os
# ...

# Example 6: Via ToolCall (how the LLM calls it)
call = ToolCall(
    name="file_search",
    arguments={"pattern": "*.py", "query": "class Agent"}
)
result = registry.execute(call)
```

**Safety:**
- Cannot escape outside `base_directory`
- Hidden files and directories (starting with `.`) are skipped
- File size limits prevent reading huge files

#### WebSearchTool — searching, reading, and interacting with web pages

A tool for working with the internet: search via multiple providers (DuckDuckGo, Brave, Serper, Tavily, Exa, SearXNG, Bocha, Google Custom Search) with intelligent auto-routing, fetching pages, and full interaction via Playwright or Selenium (clicks, forms, JS, crawl).

> **Install browser backend** (optional — pick one):
> ```bash
> # Playwright (recommended — faster, auto-waiting, built-in browser management)
> pip install playwright && playwright install
>
> # Selenium (legacy)
> pip install selenium
> ```

##### Quick start

**Method 1 — dict config (recommended):**

```python
from builder import GraphBuilder
from execution import MACPRunner

builder = GraphBuilder()
builder.add_agent(
    "researcher",
    persona="research assistant",
    # Dict config — tool is created automatically with the desired parameters
    tools=[{"name": "web_search", "deep_search": "playwright", "fetch_content": True}],
)
builder.add_task(query="Find information about Python 3.13")
builder.connect_task_to_agents(agent_ids=["researcher"])
graph = builder.build()

runner = MACPRunner(llm_caller=my_caller)
result = runner.run_round(graph)
```

**Method 2 — registry registration:**

```python
from tools import WebSearchTool, get_registry

registry = get_registry()
registry.register(WebSearchTool(deep_search="playwright", fetch_content=True))

# Agent references it by name
builder.add_agent("researcher", tools=["web_search"])
```

**Method 3 — pass the object directly:**

```python
from tools import WebSearchTool

builder.add_agent(
    "researcher",
    tools=[WebSearchTool(deep_search="playwright")],
)
```

##### Dict config parameters

```python
tools=[{
    "name": "web_search",
    # All WebSearchTool constructor parameters:
    "deep_search": "playwright",  # "playwright" (recommended), "selenium", or omit for no browser
    "fetch_content": True,
    "max_results": 5,
    "timeout": 15,
    "max_content_length": 4000,
    "browser_config": {
        "headless": True,
        "browser": "chromium",  # Playwright: "chromium", "firefox", "webkit"
                                # Selenium: "chrome", "firefox", "edge", "auto"
        "extra_wait": 1.0,
        "disable_images": True,
        "page_load_timeout": 30,
    },
    # Provider by string:
    # "provider": "brave",  # see "Search providers" below
    # "api_key": "...",
}]
```

The browser is detected automatically. Playwright downloads browsers on install (`playwright install`). Selenium uses Selenium Manager (built into Selenium 4.6+) to auto-download drivers.

##### Search providers

The tool supports **8 search providers** out of the box.  Set the `provider` key in the dict config to use any of them.

| Provider | Name string | API key required | Free tier | Best for |
|----------|------------|-----------------|-----------|----------|
| **DuckDuckGo** | `"duckduckgo"` / `"ddg"` | No | Unlimited | Default fallback, privacy |
| **Brave Search** | `"brave"` | Yes (`BRAVE_API_KEY`) | 2 000 req/mo | Independent index, RAG, privacy |
| **Serper** | `"serper"` | Yes (`SERPER_API_KEY`) | 2 500 credits | Google SERP, news, shopping |
| **Tavily** | `"tavily"` | Yes (`TAVILY_API_KEY`) | 1 000 req/mo | AI-optimised, deep research |
| **Exa** | `"exa"` | Yes (`EXA_API_KEY`) | Free trial | Neural/semantic search, discovery |
| **SearXNG** | `"searxng"` | No (self-hosted) | Unlimited | Meta-search, full control |
| **Bocha** | `"bocha"` | Yes (`BOCHA_API_KEY`) | — | Chinese-language queries |
| **Google** | `"google"` | Yes (`GOOGLE_API_KEY` + `GOOGLE_CSE_ID`) | 100 req/day | Official Google results |

API keys can be passed explicitly via `"api_key"` in the config, or set as environment variables (shown in parentheses above).  If no key is found the tool falls back to DuckDuckGo automatically.

**Single-provider examples:**

```python
# Brave Search
tools=[{"name": "web_search", "provider": "brave", "api_key": "BSA..."}]

# Tavily with content fetching
tools=[{"name": "web_search", "provider": "tavily", "fetch_content": True}]

# SearXNG (self-hosted, no API key needed)
tools=[{"name": "web_search", "provider": "searxng", "instance_url": "http://localhost:8888"}]

# Exa (semantic search)
tools=[{"name": "web_search", "provider": "exa"}]
```

##### Auto-routing (intelligent provider selection)

When `auto_route=True`, the tool uses a `SearchRouter` that automatically selects the best provider based on query intent (news, technical, shopping, research, semantic, Chinese, general).

```python
# Auto-routing with explicit provider list
tools=[{
    "name": "web_search",
    "auto_route": True,
    "providers": [
        {"provider": "brave", "api_key": "..."},
        {"provider": "tavily"},
        {"provider": "exa"},
        {"provider": "duckduckgo"},
    ],
}]

# Auto-routing with env-var detection (simplest)
# Automatically discovers providers from BRAVE_API_KEY, TAVILY_API_KEY, etc.
tools=[{"name": "web_search", "auto_route": True}]
```

The router detects intent from query keywords and routes to the best provider:

| Intent | Example query | Preferred providers |
|--------|--------------|-------------------|
| News | "latest AI news today" | Brave → Serper → Tavily |
| Technical | "Python asyncio error" | Serper → Tavily → Brave → Exa |
| Shopping | "iPhone 16 price" | Serper → Brave → Google |
| Research | "transformer architecture paper" | Exa → Tavily → Brave |
| Semantic | "companies similar to Stripe" | Exa → Tavily → Brave |
| Chinese | "中文搜索引擎" | Bocha → Tavily → Brave |
| General | "Python tutorial" | Brave → Tavily → Serper |

You can also use the `SearchRouter` directly:

```python
from tools import SearchRouter, BraveProvider, TavilyProvider, DuckDuckGoProvider

router = SearchRouter(available_providers={
    "brave": BraveProvider(api_key="..."),
    "tavily": TavilyProvider(api_key="..."),
    "duckduckgo": DuckDuckGoProvider(),
})

intent = router.detect_intent("latest Python news")  # "news"
providers = router.route("latest Python news")        # ["brave", "tavily", "duckduckgo"]
```

##### Custom providers

You can register your own search provider:

```python
from tools import SearchProvider, register_provider

class MySearchProvider(SearchProvider):
    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        # Your implementation here
        return [{"title": "...", "url": "...", "snippet": "..."}]

register_provider("my_search", MySearchProvider)

# Now usable by name:
tools=[{"name": "web_search", "provider": "my_search"}]
```

##### Actions (the `action` parameter)

`action` is a command that defines what to do. All actions run within the same browser session.

| action | Description | Required parameters |
|--------|-------------|---------------------|
| `search` | Web search | `query` |
| `fetch` | Open and read a page | `url` |
| `click` | Click an element | `selector` |
| `fill` | Fill an input | `selector`, `value` |
| `extract_links` | Extract links from a page | — |
| `execute_js` | Execute JavaScript | `js_code` |
| `crawl` | Recursive site crawl | `url` |
| `get_content` | Text of the current page | — |

`search` and `fetch` work without a browser. The rest require `deep_search="playwright"` or `deep_search="selenium"`.

If `action` is not provided, it is inferred automatically: `query` → search, `url` → fetch, `selector` → click, `js_code` → execute_js.

##### Action examples

```python
from tools import WebSearchTool

# Playwright (recommended)
with WebSearchTool(deep_search="playwright") as tool:
    # Search
    result = tool.execute(action="search", query="Python tutorials")

    # Fetch a page (wait for an element)
    result = tool.execute(action="fetch", url="https://example.com", wait_for_selector="h1")

    # Click
    result = tool.execute(action="click", selector="a.nav-link")

    # Fill a form and submit
    result = tool.execute(action="fill", selector="input[name=q]", value="Python", submit=True)

    # Extract links
    result = tool.execute(action="extract_links", url="https://example.com")

    # Execute JS
    result = tool.execute(action="execute_js", js_code="return document.title")

    # Crawl
    result = tool.execute(action="crawl", url="https://docs.python.org", max_depth=2, max_pages=5)

    # Current page text
    result = tool.execute(action="get_content")
```

##### Deep search — Playwright vs Selenium

| Feature | Playwright | Selenium |
|---------|-----------|----------|
| **Speed** | Faster (no WebDriver protocol) | Slower |
| **Auto-waiting** | Built-in | Manual waits |
| **Browser install** | `playwright install` | Selenium Manager (auto) |
| **Shadow DOM** | Native support | Limited |
| **Browser types** | Chromium, Firefox, WebKit | Chrome, Firefox, Edge |
| **Install** | `pip install playwright` | `pip install selenium` |

```python
# Playwright (recommended)
tool = WebSearchTool(deep_search="playwright", browser_config={"browser": "chromium"})

# Selenium (legacy)
tool = WebSearchTool(deep_search="selenium", browser_config={"browser": "auto"})

# Pass a pre-built fetcher
from tools import PlaywrightFetcher
fetcher = PlaywrightFetcher(headless=True, browser="chromium")
tool = WebSearchTool(browser_fetcher=fetcher)
```

##### Search Caching

WebSearchTool includes built-in caching via `SearchCache` to reduce API calls and improve performance:

```python
from tools import WebSearchTool

tool = WebSearchTool(
    cache=True,              # Enable caching
    cache_ttl=300.0,         # TTL: 5 minutes
    cache_max_entries=256,   # Maximum cached items
    deduplicate=True,        # Remove duplicate results
)

# First call — hits the API
result1 = tool.execute(query="Python tutorials")

# Second call within 5 minutes — returns cached results
result2 = tool.execute(query="Python tutorials")

# Disable cache for a specific call
result3 = tool.execute(query="latest news", no_cache=True)
```

**Cache features:**
- Thread-safe LRU eviction
- Separate caches for search and fetch operations
- Cache statistics: `tool._cache.stats` → `{"hits", "misses", "size", "max_entries"}`
- Clear cache: `tool._cache.clear()`

##### Provider Registry

All providers are registered in `PROVIDER_REGISTRY` with aliases:

```python
from tools.web_search import PROVIDER_REGISTRY, get_provider_class, register_provider

# List all available providers
print(list(PROVIDER_REGISTRY.keys()))
# ['duckduckgo', 'ddg', 'serper', 'tavily', 'brave', 'searxng', 'exa', 'bocha', 'google']

# Get provider class by name
cls = get_provider_class("serper")

# Register a custom provider
register_provider("my_search", MySearchProvider)
```

##### Search providers (all 8)

| Provider | API key | Description |
|----------|---------|-------------|
| `DuckDuckGoProvider` | No | Default, free, privacy-focused |
| `SerperProvider` | `SERPER_API_KEY` | Google Search API |
| `TavilyProvider` | `TAVILY_API_KEY` | AI-optimized with summarization |
| `BraveProvider` | `BRAVE_API_KEY` | Independent search index |
| `ExaProvider` | `EXA_API_KEY` | Neural/semantic search |
| `SearXNGProvider` | No (self-hosted) | Meta-search engine |
| `BochaProvider` | `BOCHA_API_KEY` | Chinese-language search |
| `GoogleProvider` | `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` | Official Google Custom Search |

```python
# Via dict config
tools=[{"name": "web_search", "provider": "tavily", "api_key": "tvly-..."}]

# Or directly
from tools import WebSearchTool, TavilyProvider
tool = WebSearchTool(provider=TavilyProvider(api_key="tvly-..."))
```

Custom provider:

```python
from tools import WebSearchTool, SearchProvider

class MyProvider(SearchProvider):
    def search(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        return [{"title": "Result", "url": "https://example.com", "snippet": query}]

tool = WebSearchTool(provider=MyProvider())
```

##### Constructor parameters

| Parameter | Type | Default | Description |
|----------|------|---------|-------------|
| `provider` | `SearchProvider \| None` | `DuckDuckGoProvider` | Search provider |
| `max_results` | `int` | `5` | Max search results |
| `max_content_length` | `int` | `4000` | Max page content length |
| `fetch_content` | `bool` | `False` | Fetch page contents during search |
| `timeout` | `int` | `15` | Request timeout (sec) |
| `deep_search` | `str \| None` | `None` | Browser backend: `"playwright"` (recommended) or `"selenium"` |
| `browser_config` | `dict \| None` | `None` | Browser settings (headless, browser, extra_wait, etc.) |
| `browser_fetcher` | `BrowserFetcher \| None` | `None` | A pre-built BrowserFetcher instance |
| `callback_manager` | `CallbackManager \| None` | `None` | For events (if None — taken from context) |
| `cache` | `SearchCache \| bool \| None` | `None` | Cache instance or `True` to enable default |
| `cache_ttl` | `float` | `300.0` | Cache TTL in seconds (5 min) |
| `cache_max_entries` | `int` | `256` | Maximum cached entries |
| `deduplicate` | `bool` | `True` | Remove duplicate search results |
| `trust_env` | `bool` | `False` | Use HTTP_PROXY/HTTPS_PROXY from environment |

##### execute() parameters

| Parameter | Type | Description |
|----------|------|-------------|
| `action` | `str` | Action (see table above). Auto-inferred if omitted |
| `query` | `str` | Search query |
| `url` | `str` | Page URL |
| `selector` | `str` | CSS selector |
| `value` | `str` | Value for fill |
| `submit` | `bool` | Submit the form (default: False) |
| `js_code` | `str` | JavaScript code |
| `max_pages` | `int` | Max pages for crawl (default: 10) |
| `max_depth` | `int` | Max crawl depth (default: 2) |
| `url_filter` | `str` | Regex filter for crawl URLs |
| `fetch_content` | `bool` | Fetch contents (for search) |
| `max_results` | `int` | Max results (for search) |
| `wait_for_selector` | `str` | CSS selector to wait for page readiness |

##### Callback integration

WebSearchTool emits `on_tool_start`/`on_tool_end`/`on_tool_error` events via the callback system:

```python
from callbacks import CallbackManager, StdoutCallbackHandler
from tools import WebSearchTool

cb = CallbackManager(handlers=[StdoutCallbackHandler()])
tool = WebSearchTool(callback_manager=cb, deep_search="selenium")
tool.execute(action="fetch", url="https://example.com")
# 🛠️  Tool 'web_search.fetch' started
# ✅ Tool 'web_search.fetch' ended (1200ms)
```

##### Notes

- Two modes: `urllib` (no dependencies) and Selenium (full browser)
- Browsers: Chrome, Firefox, Edge (automatic fallback to system driver)
- Context manager: `with WebSearchTool(...) as tool:` — auto-closes the browser
- Built-in HTML parser without external dependencies
- `create_tool_from_config()` — build from dict config for agent integration

##### WebSearchPolicy — configurable retrieval and scoring

`WebSearchPolicy` controls content quality scoring, excerpt extraction, and deep search behavior:

```python
from tools.web_search._policy import WebSearchPolicy

policy = WebSearchPolicy(
    # Content fetching limits
    default_browser_fetch_pages=3,      # Pages to fetch with browser
    default_http_fetch_pages=5,         # Pages to fetch with HTTP
    bulk_fetch_timeout=5,               # Timeout for bulk fetch
    http_enrich_concurrency=5,          # Concurrent HTTP fetches

    # Quality thresholds
    content_quality_threshold=0.45,     # Min quality to accept HTTP content
    full_browser_rescue_threshold=0.30, # Below this, use browser for enrichment
    full_browser_rescue_pages=1,        # Pages to retry with browser

    # Output budgeting
    max_output_content_budget=4500,     # Max total content in output
    min_output_content_budget=1500,     # Min total content budget
    min_page_content_budget=600,        # Min content per page
    min_excerpt_remainder=120,          # Min chars to include partial section

    # Query processing
    query_term_limit=8,                 # Max query terms to extract

    # Custom patterns
    boilerplate_patterns=(              # Patterns that reduce quality score
        "enable javascript",
        "cookie policy",
        "privacy policy",
        "subscribe",
    ),
)

tool = WebSearchTool(policy=policy)
```

**Policy methods:**

| Method | Purpose |
|--------|---------|
| `extract_query_terms(query)` | Extract relevant terms from search query |
| `content_quality_score(terms, result, fetched)` | Score fetched content (0.0-1.0) |
| `snippet_quality_score(terms, result)` | Score search snippet without fetching |
| `results_need_content_fetch(query, results)` | Decide if HTTP fetch is needed |
| `should_browser_enrich_candidate(terms, idx, result, fetched)` | Decide if browser is needed |
| `extract_query_focused_excerpt(content, terms, max_chars)` | Extract relevant sections |
| `prepare_results_for_output(results, query, with_content, max_length)` | Format final output |

##### Advanced Playwright Session Actions

When using Playwright backend, additional session management actions are available:

```python
from tools import WebSearchTool

with WebSearchTool(deep_search="playwright") as tool:
    # === Tab management ===
    tool.execute(action="open_tab", url="https://example.com", background=False)
    tabs = tool.execute(action="list_tabs")
    tool.execute(action="switch_tab", tab_index=1)
    tool.execute(action="close_tab")

    # === Screenshots ===
    tool.execute(action="screenshot", path="page.png", full_page=True)
    tool.execute(action="screenshot", path="element.png", selector=".content")

    # === Cookie management ===
    cookies = tool.execute(action="get_cookies", urls=["https://example.com"])
    tool.execute(action="add_cookies", cookies=[
        {"name": "session", "value": "abc123", "domain": ".example.com"}
    ])
    tool.execute(action="storage_state", path="session.json")  # Export for later

    # === Network tracing ===
    tool.execute(action="start_tracing", trace_screenshots=True, trace_snapshots=True)
    # ... perform actions ...
    tool.execute(action="stop_tracing", path="trace.zip")
    events = tool.execute(action="network_events", limit=100, clear=True)

    # === Downloads ===
    tool.execute(action="download", selector="a.download-link", path="./downloads/")

    # === Frames ===
    frames = tool.execute(action="list_frames")
```

**Advanced action parameters:**

| Action | Parameters | Description |
|--------|------------|-------------|
| `open_tab` | `url`, `background`, `wait_for_selector` | Open new browser tab |
| `list_tabs` | — | List all open tabs |
| `switch_tab` | `tab_index` | Switch to tab by index |
| `close_tab` | `tab_index` (optional) | Close tab (current or by index) |
| `screenshot` | `path`, `selector`, `full_page` | Capture screenshot |
| `get_cookies` | `urls` (optional) | Get cookies for URLs |
| `add_cookies` | `cookies` | Add cookies to browser |
| `storage_state` | `path` | Export cookies + localStorage |
| `start_tracing` | `trace_screenshots`, `trace_snapshots`, `trace_sources` | Start Playwright trace |
| `stop_tracing` | `path` | Stop and save trace |
| `network_events` | `limit`, `clear` | Get captured network events |
| `download` | `selector`, `path`, `wait_timeout` | Click and save download |
| `list_frames` | — | List page frames |

#### ToolRegistry — tool registry

```python
from tools import ToolRegistry, ShellTool, FunctionTool

# Create a registry
registry = ToolRegistry()

# Register tools
registry.register(ShellTool(timeout=10))
registry.register(FunctionTool())

# Register functions via the registry decorator (convenient)
@registry.function
def greet(name: str) -> str:
    """Greeting."""
    return f"Hello, {name}!"

@registry.function(name="add", description="Add two numbers")
def add_numbers(a: int, b: int) -> int:
    return a + b

# Check tool presence
print(registry.has("shell"))  # True
print(registry.has("greet"))  # True

# List tools
print(registry.list_tools())  # ['shell', 'function_calling', 'greet', 'add']

# Get tools for an agent
tools = registry.get_tools_for_agent(["shell", "greet"])
print([t.name for t in tools])  # ['shell', 'greet']

# Format a prompt with tool descriptions
prompt = registry.format_tools_prompt(["shell", "greet"])
print(prompt)
# Available tools:
# - shell: Execute a shell command...
# - greet: Greeting.
# To use a tool, format your response as:
# <tool_call>{"name": "tool_name", "arguments": {...}}</tool_call>
```

#### Parsing tool_call from an LLM response

An agent can call a tool by including a special tag in its response:

```python
from tools import ToolCall

# LLM returns a response with tool calls
llm_response = """
I need to compute the result.

<tool_call>
{"name": "calculate", "arguments": {"expression": "2 + 2"}}
</tool_call>

And also check the directory:

<tool_call>
{"name": "shell", "arguments": {"command": "ls"}}
</tool_call>
"""

# Parse all calls
calls = ToolCall.parse_from_response(llm_response)
print(len(calls))  # 2
print(calls[0].name)       # "calculate"
print(calls[0].arguments)  # {"expression": "2 + 2"}

# Execute all calls
results = registry.execute_all(calls)
for result in results:
    print(f"{result.tool_name}: {result.output if result.success else result.error}")
```

#### Integration with MACPRunner

Tools are used **automatically** — it is enough to specify them when creating the agent.

```python
from execution import MACPRunner, RunnerConfig
from builder import GraphBuilder
from tools import (
    tool, get_registry, register_tool,
    ShellTool, CodeInterpreterTool, FileSearchTool,
    OpenAIToolsCaller,
)
from openai import OpenAI

# 1. Register built-in tools
register_tool(ShellTool(timeout=10))
register_tool(CodeInterpreterTool(timeout=10, safe_mode=True))
register_tool(FileSearchTool(base_directory="."))

# Register custom functions via @tool
@tool
def get_current_time() -> str:
    """Get current date and time."""
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@tool
def calculate(expression: str) -> str:
    """Evaluate math expression safely."""
    return str(eval(expression, {"__builtins__": {}}, {}))

# 2. Create a graph with agents
builder = GraphBuilder()

builder.add_agent(
    "assistant",
    display_name="AI Assistant",
    persona="Helpful assistant who uses tools to solve problems.",
    tools=["shell", "get_current_time"],  # <-- tools are used automatically!
)

builder.add_agent(
    "coder",
    display_name="Python Coder",
    persona="Python expert who writes and executes code.",
    tools=["code_interpreter"],
)

builder.add_agent(
    "calculator",
    display_name="Calculator Agent",
    persona="Math expert who calculates expressions.",
    tools=["calculate"],
)

builder.add_workflow_edge("assistant", "calculator")
builder.add_task(query="What is 25 * 17 and what time is it?")
builder.connect_task_to_agents()

graph = builder.build()

# 3. Create caller and runner
client = OpenAI(api_key="...")
caller = OpenAIToolsCaller(client, model="gpt-4")

runner = MACPRunner(llm_caller=caller)  # No extra configuration needed!

# 4. Execute — tools are used automatically
result = runner.run_round(graph)
print(result.final_answer)
```

**Note:** The `max_tool_iterations` parameter in `RunnerConfig` limits the number of tool-calling loops (default is 3).

#### Creating a custom tool

```python
from tools import BaseTool, ToolResult
from typing import Any

class WeatherTool(BaseTool):
    """A tool for getting weather."""

    @property
    def name(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        return "Get current weather for a city"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name"
                }
            },
            "required": ["city"]
        }

    def execute(self, city: str = "", **kwargs) -> ToolResult:
        if not city:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="City is required"
            )

        # A real API call would go here
        weather = f"Sunny, 22°C in {city}"

        return ToolResult(
            tool_name=self.name,
            success=True,
            output=weather
        )

# Usage: Two ways to use custom tools in agents

# Method 1: Register globally and use by name (recommended)
from tools import register_tool, get_registry
from builder import GraphBuilder
from core.agent import AgentProfile

# Register in the global registry
register_tool(WeatherTool())

# Create an agent that uses the tool by name
agent = AgentProfile(
    agent_id="weather_agent",
    display_name="Weather Agent",
    persona="a weather assistant",
    tools=["weather"],  # <-- use by name after registration
)

# Or via GraphBuilder
builder = GraphBuilder()
builder.add_agent(
    agent_id="weather_agent",
    display_name="Weather Agent",
    persona="a weather assistant",
    tools=["weather"],  # <-- tool name
)

# Method 2: Pass tool object directly
weather_tool = WeatherTool()
agent = AgentProfile(
    agent_id="weather_agent",
    display_name="Weather Agent",
    persona="a weather assistant",
    tools=[weather_tool],  # <-- pass object directly
)

# Test the tool directly
from tools import ToolCall
registry = get_registry()
result = registry.execute(ToolCall(name="weather", arguments={"city": "Moscow"}))
print(result.output)  # "Sunny, 22°C in Moscow"
```

#### Custom tool with configuration

For tools that need configuration (API keys, settings, etc.), pass parameters in `__init__`:

```python
from tools import BaseTool, ToolResult, register_tool
from typing import Any

class TelegramTool(BaseTool):
    """Tool for sending Telegram messages."""

    def __init__(self, bot_token: str, default_chat_id: int | None = None):
        self._bot_token = bot_token
        self._default_chat_id = default_chat_id

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def description(self) -> str:
        return "Send messages via Telegram Bot API"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message text"},
                "chat_id": {"type": "integer", "description": "Chat ID (optional)"},
            },
            "required": ["message"],
        }

    def execute(self, message: str = "", chat_id: int | None = None, **kwargs) -> ToolResult:
        if not message:
            return ToolResult(tool_name=self.name, success=False, error="Message is required")

        target_chat = chat_id or self._default_chat_id
        if not target_chat:
            return ToolResult(tool_name=self.name, success=False, error="Chat ID required")

        # Real API call would go here
        # requests.post(f"https://api.telegram.org/bot{self._bot_token}/sendMessage", ...)

        return ToolResult(
            tool_name=self.name,
            success=True,
            output=f"Message sent to chat {target_chat}: {message}"
        )

# Register with configuration
register_tool(TelegramTool(
    bot_token="123456:ABC-DEF...",
    default_chat_id=123456789
))

# Use in agent
agent = AgentProfile(
    agent_id="telegram_bot",
    display_name="Telegram Bot",
    persona="assistant that sends Telegram messages",
    tools=["telegram"],  # Use by name
)
```


#### Example: full workflow with tools

```python
"""Full example of using tools in a multi-agent system."""

import math
from execution import MACPRunner, RunnerConfig
from builder import GraphBuilder
from tools import (
    ToolRegistry,
    ShellTool,
    CodeInterpreterTool,
    FileSearchTool,
)

# Configure tools
registry = ToolRegistry()

# Shell with allowlist
registry.register(ShellTool(
    timeout=5,
    allowed_commands=["echo", "date", "pwd", "ls"]
))

# Code interpreter to execute Python code
registry.register(CodeInterpreterTool(timeout=10, safe_mode=True))

# File search to find files
registry.register(FileSearchTool(base_directory=".", max_results=20))

# Math functions — register directly via @registry.function
# This allows the LLM to call them by name: {"name": "sqrt", "arguments": {"x": 144}}
@registry.function
def sqrt(x: float) -> float:
    """Calculate square root."""
    return math.sqrt(x)

@registry.function
def power(base: float, exp: float) -> float:
    """Calculate base^exp."""
    return math.pow(base, exp)

@registry.function
def factorial(n: int) -> int:
    """Calculate factorial."""
    return math.factorial(n)

# Build the graph
builder = GraphBuilder()

builder.add_agent(
    "math_solver",
    persona="Expert mathematician",
    tools=["sqrt", "power", "factorial"],  # Direct access to functions
)

builder.add_agent(
    "coder",
    persona="Python developer",
    tools=["code_interpreter"],  # Execute Python code
)

builder.add_agent(
    "researcher",
    persona="Code researcher",
    tools=["file_search"],  # Search files
)

builder.add_agent(
    "coordinator",
    persona="Task coordinator that combines results",
    tools=[],  # No tools
)

builder.add_workflow_edge("math_solver", "coordinator")
builder.add_workflow_edge("coder", "coordinator")
builder.add_workflow_edge("researcher", "coordinator")
builder.add_task(query="Calculate sqrt(144), then write Python to verify")
builder.connect_task_to_agents()

graph = builder.build()

# Execute
def mock_llm(prompt: str) -> str:
    if "mathematician" in prompt:
        return '''I'll calculate the square root.
<tool_call>
{"name": "sqrt", "arguments": {"x": 144}}
</tool_call>
'''
    elif "developer" in prompt:
        return '''Let me verify with Python code.
<tool_call>
{"name": "code_interpreter", "arguments": {"code": "import math\\nprint(f'sqrt(144) = {math.sqrt(144)}')"}}
</tool_call>
'''
    elif "researcher" in prompt:
        return '''Let me find Python files.
<tool_call>
{"name": "file_search", "arguments": {"pattern": "*.py", "directory": "src"}}
</tool_call>
'''
    else:
        return "Based on the results: sqrt(144) = 12 and we're in the current directory."

config = RunnerConfig(enable_tools=True, max_tool_iterations=2)
runner = MACPRunner(llm_caller=mock_llm, tool_registry=registry, config=config)

result = runner.run_round(graph)
print("Final:", result.final_answer)
```

#### Running the example

```bash
# Run the tools example
uv run python examples/tools_example.py

# Run tests
uv run pytest tests/test_tools.py -v
```
### Computer Use Tool

`computer_use` is the framework's stateful UI automation tool. Unlike stateless
helpers, every call belongs to a session and carries forward observation
settings, safety policy, step budget, and action history.

The stack lives under `src/tools/computer_use/` and exposes:

- `ComputerUseTool` - framework `BaseTool` adapter registered as `computer_use`
- `ComputerUseClient` - standalone client for direct use outside the tool registry
- `ComputerUseController` - stateful `start/observe/act/close` orchestration
- `MockComputerRuntime` - deterministic backend for tests, CI, and examples
- `WindowsComputerRuntime` - native Win32 desktop runtime
- `LinuxComputerRuntime` - native X11 desktop runtime (Linux)
- `MacOSComputerRuntime` - native macOS desktop runtime
- `build_computer_use_tool_schema()` / `build_computer_use_full_schema()` - simplified and full JSON schemas
- `artifact_to_base64_url()` / `observation_to_openai_content()` - helpers for multimodal LLM payloads

#### Supported Runtimes

| Runtime | Platform | Dependencies |
|---------|----------|--------------|
| **windows_native** | Windows 10+ | pywin32, Pillow |
| **linux_native** | Linux (X11) | python-xlib, Pillow; system: xdotool, xclip/xsel, scrot |
| **macos_native** | macOS 10.15+ | Pillow; system: cliclick (optional), screencapture, osascript |
| **mock** | Any | None (for tests, CI) |

#### Installation

```bash
# Install computer-use extras
pip install -e ".[computer-use]"
```

**Platform-specific setup:**

##### Linux

```bash
pip install pillow python-xlib

# System packages (Debian/Ubuntu)
sudo apt install xdotool xclip xsel scrot

# Optional: OCR support
sudo apt install tesseract-ocr
```

##### macOS

```bash
pip install pillow

# Optional: cliclick for mouse/keyboard automation
brew install cliclick

# Built-in utilities used: screencapture, osascript
```

##### Windows

```bash
pip install pywin32 Pillow

# Optional: OCR support - install Tesseract from https://github.com/UB-Mannheim/tesseract/wiki
```

Notes:

- Platform runtimes require their respective dependencies
- OCR for `extract_text` with `strategy="ocr"` requires Tesseract
- If the native runtime is unavailable, the factory falls back to `mock`

#### Runtime selection and registration

The tool factory is registered globally under the name `computer_use`.
By default it selects the first available runtime based on platform:

- **Windows**: `windows_native` → `mock`
- **Linux**: `linux_native` → `mock`
- **macOS**: `macos_native` → `mock`

You can override the selection order via environment variable:

```bash
export GMAS_COMPUTER_USE_RUNTIME_ORDER="linux_native,macos_native,windows_native,mock"
```

You can use the default registration, create the tool from config, or register
an explicit runtime yourself:

```python
from builder import GraphBuilder
from tools import ComputerUseTool, create_tool_from_config, get_registry

registry = get_registry()

# Option 1: explicit instance
registry.register(ComputerUseTool(runtime_name="mock"))

# Option 2: config dict (same contract used by create_tool_from_config)
tool = create_tool_from_config({"name": "computer_use", "runtime_name": "mock"})
registry.register(tool)

builder = GraphBuilder()
builder.add_agent(
    "operator",
    persona="Desktop operator that inspects windows and performs safe UI actions",
    tools=["computer_use"],
)
```

#### Lifecycle and response model

The LLM-facing protocol has four operations:

| Operation | Required fields | What it returns |
|-----------|-----------------|-----------------|
| `start` | `config` | new `session`, initial `observation`, runtime `capabilities` |
| `observe` | `session_id` | fresh `observation` for the current session |
| `act` | `session_id`, `action` | `action_result`, updated `session`, fresh `observation` |
| `close` | `session_id` | closed session snapshot; repeated calls are idempotent |

Behavioral guarantees implemented by the controller:

- `session_id` is mandatory for `observe`, `act`, and `close`
- `act` and `observe` are rejected once a session is closed
- `close` is idempotent and returns the final snapshot on repeated calls
- `max_steps` is enforced at the controller level before dispatching actions
- concurrent `act` calls are serialized per session to avoid step-count races
- closed sessions are retained temporarily for inspection, then evicted to bound memory use

When used through `ComputerUseTool`, the returned `ToolResult.output` is a JSON
serialized `ComputerUseResponse`. Session history is intentionally stripped from
that JSON payload so repeated tool calls do not flood the LLM context window.

#### Session config and observations

`start` accepts `ComputerSessionConfig`, which includes:

- `runtime_name`
- `start_url`
- `max_steps`
- `safety_mode`
- `artifact_root`
- default `observation` request
- free-form `metadata`

By default, artifacts are stored under the caller's current working directory:

```python
Path.cwd() / ".gmas" / "artifacts" / "computer_use"
```

This keeps screenshots and trace artifacts inside the project that is actually
running the agent rather than inside the library source tree.

Observation capture is controlled by `ObservationRequest`.

Modes:

- `screenshot_only` - fastest path, screenshot only
- `standard` - screenshot plus text, windows, and elements
- `detailed` - maximum detail, including extra runtime diagnostics

Useful observation flags:

- `include_screenshot`
- `include_text`
- `include_dom`
- `include_elements`
- `include_windows`
- `include_clipboard`
- `active_window_only`
- `screenshot_max_dimension`
- `screenshot_format` (`png` or `jpeg`)
- `screenshot_quality` (JPEG quality)

An observation may contain:

- screenshot artifact
- page or runtime URL/title
- viewport metadata
- `text_excerpt` and `dom_excerpt`
- active window plus visible windows list
- semantic or bounds-based element references
- runtime metadata such as cursor state or clipboard text

#### Actions and validation

`act` accepts a single `ComputerAction`. Targets can be either pixel
coordinates or a semantic `UIElementRef`.

| Action type | Required fields | Notes |
|-------------|-----------------|-------|
| `click`, `double_click`, `right_click`, `hover` | `target` | target may be coordinates or element ref |
| `drag` | `target`, `end_target` | drag between two points/elements |
| `scroll` | none | use `delta_x` / `delta_y`; target is optional |
| `type` | `text` | types literal text |
| `hotkey`, `key_press` | `keys` | e.g. `["ctrl", "c"]` |
| `wait` | none | use `wait_ms` |
| `navigate` | `url` | opens URL or file path |
| `open_app` | `path` or `text` | `text` can be app name/path |
| `focus_window` | `text` or `metadata.title` | matches window title substring |
| `resize_window` | `text` or `metadata.title`, `width`, `height` | resize by title |
| `minimize_window`, `maximize_window` | `text` or `metadata.title` | window management |
| `screenshot` | none | captures a fresh screenshot |
| `extract_text` | none | choose strategy through `metadata.strategy` |

Validation of required fields happens in the controller, so error messages are
consistent across runtimes.

`extract_text` strategies supported by the Windows runtime:

- `clipboard` - read clipboard text
- `selection_copy` - send `Ctrl+C`, then read clipboard text
- `window_title` - return the foreground window title
- `ocr` - run OCR on the latest screenshot; optional `metadata.region` crops the image first

#### Safety modes

`ComputerSessionConfig.safety_mode` controls runtime-level restrictions:

- `prompt` - default mode; blocks dangerous hotkeys such as `Alt+F4`, `Ctrl+Alt+Delete`, `Win+L`, blocks launching shells like `cmd` / `powershell`, and blocks `file://` navigation into sensitive Windows system paths
- `allowlist` - only actions listed in `config.metadata["allowed_actions"]` are permitted
- `unrestricted` - no runtime safety restrictions

This safety layer is additive: controller-level argument validation still runs
regardless of the selected safety mode.

#### Runtime capabilities

The current runtimes intentionally expose the same typed contract but different
capability sets.

| Runtime | Best for | Key capabilities | Important limits |
|---------|----------|------------------|------------------|
| `mock` | tests, CI, prompt development | deterministic observations, semantic targets, screenshots, keyboard/mouse, window metadata | not a real desktop, no real OS side effects |
| `windows_native` | real Windows desktop automation | screenshots, keyboard/mouse, window discovery and management, clipboard access, text extraction, safety policy enforcement | Windows only; `supports_semantic_targeting=False`; browser support is not separate yet |

Windows runtime details worth knowing:

- supports full-screen and active-window screenshots
- falls back from native window capture to bounding-box capture when the native image is blank
- supports window focus, resize, minimize, and maximize operations
- supports post-action waits and per-action timeouts
- saves screenshots as PNG or JPEG depending on the observation request

#### Framework and standalone usage

Framework tool usage:

```python
from tools import ComputerUseTool

tool = ComputerUseTool(runtime_name="mock")

start = tool.execute(
    operation="start",
    config={
        "runtime_name": "mock",
        "max_steps": 5,
        "observation": {"mode": "detailed", "include_clipboard": True},
    },
)

# ToolResult.output contains serialized JSON
print(start.output)
```

Standalone client usage:

```python
from tools.computer_use import (
    ComputerUseClient,
    ComputerUseController,
    MockComputerRuntime,
)

client = ComputerUseClient(ComputerUseController(MockComputerRuntime()))

start = client.execute(
    operation="start",
    config={"runtime_name": "mock", "start_url": "https://example.com"},
)
session_id = start.session.session_id

client.execute(
    operation="act",
    session_id=session_id,
    action={"action_type": "type", "text": "hello"},
)

client.execute(operation="close", session_id=session_id)
```

Async usage is available through `ComputerUseTool.execute_async()`, which runs
the blocking desktop interaction in a worker thread so async agent loops do not
block the event loop.

#### Multimodal helpers

If your LLM accepts image input, convert observations into OpenAI-compatible
multimodal content:

```python
from tools.computer_use import observation_to_openai_content

content = observation_to_openai_content(start.observation)
messages = [{"role": "user", "content": content}]
```

If you only need the screenshot payload, use `artifact_to_base64_url()` on
`observation.screenshot`.

#### Package layout

- `src/tools/computer_use/models.py` - typed models for actions, sessions, observations, and responses
- `src/tools/computer_use/controller.py` - stateful command handling and validation
- `src/tools/computer_use/client.py` - standalone client, schema builders, and multimodal helpers
- `src/tools/computer_use/framework.py` - `BaseTool` adapter registered in the framework tool registry
- `src/tools/computer_use/windows.py` - Windows native runtime backed by Win32 and Pillow
- `src/tools/computer_use/mock.py` - deterministic test runtime
- `src/tools/computer_use/runtime.py` - runtime interface for future backends

### MCP Client Tool

> **Note:** This feature is available in branches `feat-23-remote-mcp-servers` and `new-feat-23-remote-mcp-servers`. Not yet merged to main.

gMAS provides an **MCP CLIENT** that connects to remote MCP (Model Context Protocol) servers. It exports MCP server tools as gMAS `BaseTool` instances for seamless integration with agents.

#### Installation

```bash
pip install gMAS[mcp]  # Requires mcp>=1.0
```

#### Overview

**Key classes:**

| Class | Description |
|-------|-------------|
| `MCPTool` | Wraps MCP server tools as gMAS `BaseTool` |
| `MCPClient` | Manages connections to remote MCP servers |

**What is exported:**

- **Tools** — MCP server tools become callable gMAS tools
- **NOT Resources** — MCP Resources are not currently exported

#### Usage

```python
from tools.mcp_client import MCPClient
from core.agent import AgentProfile

# Connect to remote MCP server
with MCPClient("https://mcp.deepwiki.com/mcp") as client:
    # Get available tools
    tools = client.tools()

    # Call a tool
    result = client.call_tool("ask_question", {
        "repoName": "modelcontextprotocol/python-sdk",
        "question": "How do I create a simple MCP server?",
    })

# Use with AgentProfile
client = MCPClient("https://mcp.example.com/mcp")
client.connect()

agent = AgentProfile(
    agent_id="researcher",
    display_name="Researcher",
    tools=client.tools(),  # Tools from MCP server
)

# Don't forget to close when done
client.close()
```

#### MCPClient API

```python
class MCPClient:
    def __init__(
        self,
        url: str,                    # URL of the MCP server
        headers: dict | None = None, # HTTP headers (for auth)
        timeout: float = 30.0,       # Connection timeout
        read_timeout_seconds: float | None = None,  # Read timeout
        transport: str = "streamable_http"  # Transport type
    )

    def connect(self) -> None                    # Connect to server
    def tools(self, *, refresh: bool = False) -> list[MCPTool]  # List tools
    def call_tool(self, name: str, arguments: dict, ...) -> CallToolResult | str  # Call tool
    def close(self) -> None                      # Close connection

    # Context manager support
    def __enter__(self) -> "MCPClient"
    def __exit__(self, *args) -> None
```

#### MCPTool

Each `MCPTool` wraps a tool from the remote MCP server:

```python
class MCPTool(BaseTool):
    @property
    def name(self) -> str           # Tool name
    @property
    def description(self) -> str    # Tool description
    @property
    def parameters_schema(self) -> dict  # JSON Schema for parameters

    def execute(self, **kwargs) -> ToolResult  # Execute the tool
```

#### Transport types

- `streamable_http` (default) — Streamable HTTP transport
- `sse` — Server-Sent Events transport

---

#### Remote MCP Servers

The `MCPClient` and `MCPTool` classes let agents use tools from any remote [MCP](https://modelcontextprotocol.io) server without writing any glue code. `MCPTool` implements `BaseTool`, so MCP tools are indistinguishable from built-in ones.

> **Install the optional dependency first:**
> ```bash
> pip install "gMAS[mcp]"
> # or
> uv add "mcp>=1.0"
> ```

##### Quick start

```python
from tools import MCPClient

# Connect, list tools, call a tool — all in three lines
with MCPClient("https://mcp.deepwiki.com/mcp") as client:
    tools = client.tools()
    print([t.name for t in tools])          # ['ask_question', 'read_wiki_structure', ...]

    answer = client.call_tool(
        "ask_question",
        {
            "repoName": "modelcontextprotocol/python-sdk",
            "question": "How do I create a simple MCP server with a tool?",
        },
    )
    print(answer[:500])
```

##### Calling tools via the BaseTool interface

Every tool returned by `client.tools()` is a `BaseTool` object. You can call it with `execute()` exactly the same way the runner does:

```python
with MCPClient("https://mcp.deepwiki.com/mcp") as client:
    tools = {t.name: t for t in client.tools()}

    result = tools["ask_question"].execute(
        repoName="modelcontextprotocol/python-sdk",
        question="What transports does the Python MCP SDK support?",
    )
    print(result.success)       # True
    print(result.output[:600])
```

`ToolResult.success` is `False` and `ToolResult.error` is populated if the server returns an error or the call times out.

##### Using MCP tools inside an AgentProfile

Pass the tool objects directly to `AgentProfile.tools` — the runner picks them up automatically:

```python
from core.agent import AgentProfile
from tools import MCPClient

client = MCPClient("https://mcp.deepwiki.com/mcp")
client.connect()

agent = AgentProfile(
    agent_id="researcher",
    display_name="Research Agent",
    persona="a helpful research assistant with access to GitHub documentation",
    tools=client.tools(),   # MCPTool objects work as native BaseTool
)

print(agent.get_tool_names())   # ['ask_question', 'read_wiki_structure', 'read_wiki_contents']

client.close()
```

> **Lifetime:** the `MCPClient` must stay open for as long as the agent is running. Use `client.close()` (or the context manager) after the runner finishes.

##### Authentication

Pass an `Authorization` header (or any other header) via the `headers` argument:

```python
import os
from tools import MCPClient

with MCPClient(
    "https://mcp.apify.com",
    headers={"Authorization": f"Bearer {os.environ['APIFY_TOKEN']}"},
) as client:
    result = client.call_tool(
        "apify/rag-web-browser",
        {"query": "latest news about AI agents"},
    )
    print(result)
```

##### MCPClient reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | — | MCP server URL |
| `headers` | `dict[str, str] \| None` | `None` | HTTP headers (e.g. for auth) |
| `timeout` | `float` | `30.0` | Connect and call timeout in seconds |

| Method | Returns | Description |
|--------|---------|-------------|
| `connect()` | `None` | Open the session (blocks until ready) |
| `tools()` | `list[MCPTool]` | List tools; result is cached |
| `call_tool(name, arguments)` | `str` | Call a tool and return its text output |
| `close()` | `None` | Close the session and background thread |

`MCPClient` is also a context manager (`with MCPClient(...) as client:`), which calls `connect()` on entry and `close()` on exit.

##### How it works

The MCP Python SDK is fully async, while gMAS is synchronous. `MCPClient` bridges the two by running a private `asyncio` event loop in a background thread. The main thread dispatches calls to that loop with `asyncio.run_coroutine_threadsafe` and waits for the result. Each `MCPClient` instance gets its own independent thread and loop, so multiple clients can run concurrently without interfering.

---

## Vector Search (RAG)

The vector search subsystem provides semantic retrieval for agent workflows:
- `VectorIndexTool` handles indexing and deletion
- `VectorSearchTool` handles retrieval and context formatting

Both tools can be created from settings and, by default, share the same store/provider/chunker instances.

#### Framework and tool usage

```python
from tools.vector_search import VectorSearchTool, VectorIndexTool

search = VectorSearchTool.from_settings()
index = VectorIndexTool(
    store=search._store,
    provider=search._provider,
    chunker=search._chunker,
)

index_result = index.execute(
    operation="index",
    texts=[
        "Cross-border payments compliance checklist.",
        "Marketing launch timeline and KPI review.",
    ],
    metadata=[
        {"source": "kb://legal", "title": "Compliance Notes", "doc_id": "legal-001"},
        {"source": "kb://marketing", "title": "Launch Plan", "doc_id": "mkt-001"},
    ],
)

print(index_result.output)  # JSON payload with ids

search_result = search.execute(
    query="compliance requirements for cross-border transfers",
    top_k=3,
    score_threshold=0.15,
    filters={"source": "kb://legal"},
)
print(search_result.output)
```

#### Indexing behavior and metadata enrichment

During indexing, each chunk is enriched with:
- `_chunk_index`
- `_document_index`
- `source`
- `title`
- `doc_id`

If `source/title/doc_id` are already provided (including empty strings), the provided values are preserved.

#### Score semantics

All backends return `score` as relevance where **higher is better**.

- Cosine/dot metrics are used directly.
- Distance-based metrics are converted using `1 / (1 + distance)`.

Each result also includes:
- `_raw_score` (backend-native value)
- `_score_semantics = "relevance_higher_is_better"`

This keeps ranking and `score_threshold` behavior consistent across backends.

#### Validation and safety checks

Before add/index/search operations, the subsystem validates:
- `documents/embeddings` length consistency
- `metadata/documents` length consistency
- embedding dimension consistency with store configuration
- query embedding dimension consistency for search

Validation errors are returned as `ToolResult(success=False, error=...)`.

#### Async usage

Async wrappers are available for both tools:
- `VectorSearchTool.execute_async()`
- `VectorIndexTool.execute_async()`

Both run blocking logic in a worker thread so async orchestration loops do not block the event loop.

#### Package layout

- `src/tools/vector_search.py` - embedding provider, chunker, vector stores, index/search tools, and registration
---

## API Reference

### Core classes

| Class | Description | Pydantic |
|-------|-------------|----------|
| `RoleGraph` | Role/agent graph with adjacency matrices | ❌ |
| `AgentProfile` | **Pydantic BaseModel** — Immutable agent profile | ✅ |
| `TaskNode` | **Pydantic BaseModel** — Virtual task node | ✅ |
| `NodeEncoder` | Text-to-embeddings encoder | ❌ |
| `MACPRunner` | MACP protocol executor | ❌ |
| `AdaptiveScheduler` | Adaptive scheduler | ❌ |
| `LLMCallerFactory` | Factory for creating LLM callers (multi-model) | ❌ |
| `LLMConfig` | **Pydantic BaseModel** — LLM configuration for schemas | ✅ |
| `AgentLLMConfig` | **Pydantic BaseModel** — LLM configuration for AgentProfile | ✅ |
| `AgentMemory` | Agent memory manager | ❌ |
| `SharedMemoryPool` | Shared memory pool | ❌ |
| `BudgetTracker` | Token/request budget tracker | ❌ |
| `MetricsTracker` | Performance metrics tracker | ❌ |
| `GraphVisualizer` | Graph visualization | ❌ |
| `BaseCallbackHandler` | Base callback handler | ❌ |
| `AsyncCallbackHandler` | Async callback handler | ❌ |
| `CallbackManager` | Callback handlers manager | ❌ |
| `AsyncCallbackManager` | Async callbacks manager | ❌ |
| `StdoutCallbackHandler` | Console event output | ❌ |
| `MetricsCallbackHandler` | Execution metrics aggregation | ❌ |
| `FileCallbackHandler` | Write events to JSON Lines file | ❌ |
| `EventBus` | Event bus for graph monitoring | ❌ |
| `EarlyStopCondition` | Early stopping condition | ❌ |
| `StepContext` | **Pydantic BaseModel** — Step context for hooks | ✅ |
| `TopologyAction` | **Pydantic BaseModel** — Topology modification action | ✅ |

### Schemas (Pydantic BaseModel)

| Schema class | Description | Usage |
|-------------|-------------|-------|
| `GraphSchema` | **Pydantic** — Full graph schema | Validation, serialization, migration |
| `BaseNodeSchema` | **Pydantic** — Base node schema | Parent class for nodes |
| `AgentNodeSchema` | **Pydantic** — Agent node schema | LLM config, tools, metrics, embeddings |
| `TaskNodeSchema` | **Pydantic** — Task node schema | Query, status, deadline |
| `BaseEdgeSchema` | **Pydantic** — Base edge schema | Weight, probability, cost |
| `WorkflowEdgeSchema` | **Pydantic** — Workflow edge | Conditions, priority, transforms |
| `CostMetrics` | **Pydantic** — Cost metrics | Tokens, latency, trust, reliability |
| `ValidationResult` | **Pydantic** — Validation result | Errors, warnings |

### Visualization (Pydantic BaseModel)

| Class | Description | Usage |
|-------|-------------|-------|
| `VisualizationStyle` | **Pydantic** — Global visualization style | Configure colors, shapes, what to show |
| `NodeStyle` | **Pydantic** — Node style | Shape, fill_color, stroke_color, icon |
| `EdgeStyle` | **Pydantic** — Edge style | Line style, arrow, colors |
| `NodeShape` | Enum — Node shapes | RECTANGLE, ROUND, STADIUM, CIRCLE, DIAMOND, etc. |
| `MermaidDirection` | Enum — Graph direction | TOP_BOTTOM, LEFT_RIGHT, etc. |

### GNN (Pydantic BaseModel)

| Class | Description | Usage |
|-------|-------------|-------|
| `FeatureConfig` | **Pydantic** — Feature configuration | Node/edge feature dimensions |
| `TrainingConfig` | **Pydantic** — Training configuration | Learning rate, epochs, optimizer |

### Graph construction functions

| Function | Description |
|---------|-------------|
| `build_property_graph()` | Main graph builder |
| `build_from_schema()` | Build from GraphSchema |
| `build_from_adjacency()` | Build from adjacency matrix |
| `GraphBuilder` | Fluent graph builder with multi-model support |
| `AutoGraphBuilder` | LLM-powered automatic graph assembly |
| `AutoBuilderConfig` | Configuration for AutoGraphBuilder |
| `EmbeddingGraphBuilder` | Embedding similarity-based graph assembly |
| `EmbeddingBuilderConfig` | Configuration for EmbeddingGraphBuilder |
| `LinkStrategy` | Enum: `knn`, `threshold`, `mst` |

### Multi-model functions

| Function | Description |
|---------|-------------|
| `create_openai_caller()` | Create a legacy flat-string `(str) -> str` LLM caller |
| `create_openai_structured_caller()` | Create a sync structured caller `(list[dict]) -> str` — **recommended** |
| `create_openai_async_structured_caller()` | Create an async structured caller — required for `astream()` with `enable_parallel=True` |
| `LLMCallerFactory.create_openai_factory()` | Create a factory for automatic caller generation |
| `LLMConfig.merge_with()` | Merge LLM configurations (fallback) |
| `AgentProfile.with_llm_config()` | Set LLM configuration for an agent |
| `AgentProfile.has_custom_llm()` | Check whether an agent has a custom LLM config |

### Scheduling functions

| Function | Description |
|---------|-------------|
| `build_execution_order()` | Topological execution order |
| `get_parallel_groups()` | Parallel execution groups |
| `extract_agent_adjacency()` | Extract the agent adjacency matrix |
| `get_incoming_agents()` | Agent predecessors |
| `get_outgoing_agents()` | Agent successors |

### Configuration classes

| Class | Description |
|------|-------------|
| `RunnerConfig` | MACPRunner configuration |
| `LLMConfig` | LLM configuration for an agent (multi-model) |
| `AgentLLMConfig` | Immutable LLM configuration for AgentProfile |
| `RoutingPolicy` | Routing policies |
| `PruningConfig` | Agent pruning configuration |
| `MemoryConfig` | Memory system configuration |
| `TrainingConfig` | GNN training configuration |
| `ErrorPolicy` | Error-handling policies |
| `FrameworkSettings` | Global framework settings |

---

## FAQ

### Why Pydantic? What benefits does it provide?

gMAS Framework is built entirely on **Pydantic 2.0+** to ensure type safety, automatic validation, and convenient serialization. Key benefits:

1. **Automatic type validation** — errors are caught when objects are created, not later at runtime
2. **Declarative typing** — IDE autocompletion, static checking (mypy, pyright)
3. **Automatic serialization** — `.model_dump()`, `.model_dump_json()` work out of the box
4. **Default values** — no need to write boilerplate
5. **Nested models** — automatic validation of nested structures
6. **Migrations** — safe schema upgrades between versions
7. **Immutability** — `frozen=True` prevents accidental mutation

```python
from core import AgentProfile
from pydantic import ValidationError

# ✅ Correct usage — Pydantic validates
agent = AgentProfile(
    agent_id="test",
    display_name="Test Agent",
    tools=["tool1", "tool2"],
)

# ❌ Incorrect — Pydantic will raise ValidationError
try:
    bad_agent = AgentProfile(
        agent_id=123,  # Must be str, not int
        display_name="Test",
    )
except ValidationError as e:
    print(e.errors())  # Detailed error info

# Automatic serialization (Pydantic v2 API)
data = agent.model_dump()  # → dict
json_str = agent.model_dump_json(indent=2)  # → JSON string

# Automatic deserialization
loaded = AgentProfile.model_validate(data)
from_json = AgentProfile.model_validate_json(json_str)
```

### Which Pydantic version is required? Is it compatible with Pydantic 1.x?

**gMAS Framework requires Pydantic 2.0+ and is not compatible with Pydantic 1.x.**

Key API differences:
- Pydantic 1.x: `.dict()`, `.parse_obj()`, `.json()`
- Pydantic 2.x: `.model_dump()`, `.model_validate()`, `.model_dump_json()`

If you have Pydantic 1.x installed:
```bash
pip install --upgrade "pydantic>=2.0"
```

Version check:
```python
import pydantic
print(pydantic.VERSION)  # Must be >= 2.0.0
```

### How do I use different models for different agents?

```python
from builder import GraphBuilder
from execution import MACPRunner, LLMCallerFactory

# Method 1: Via GraphBuilder (recommended)
builder = GraphBuilder()

builder.add_agent(
    "analyst",
    llm_backbone="gpt-4",                 # Strong model
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.0,
    max_tokens=4000,
)

builder.add_agent(
    "formatter",
    llm_backbone="gpt-4o-mini",           # Cheaper model
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
    temperature=0.3,
    max_tokens=1000,
)

builder.add_workflow_edge("analyst", "formatter")
graph = builder.build()

# Factory auto-creates callers
factory = LLMCallerFactory.create_openai_factory()
runner = MACPRunner(llm_factory=factory)

result = runner.run_round(graph)
```

### How do I integrate with OpenAI?

```python
import openai

# Method 1: Simple integration (one LLM for all)
def openai_caller(prompt: str) -> str:
    response = openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content

runner = MACPRunner(llm_caller=openai_caller)

# Method 2: Multi-model integration (recommended)
from execution import create_openai_caller

# Uses the openai SDK automatically
runner = MACPRunner(
    llm_factory=LLMCallerFactory.create_openai_factory(
        default_api_key="sk-...",
        default_base_url="https://api.openai.com/v1",
    )
)
```

### How do I use local models (Ollama)?

```python
import requests

def ollama_caller(prompt: str) -> str:
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={"model": "llama2", "prompt": prompt, "stream": False},
    )
    return response.json()["response"]

runner = MACPRunner(llm_caller=ollama_caller)
```

### How do I add custom tools?

Tools are just strings that are included in the agent prompt:

```python
agent = AgentProfile(
    agent_id="code_executor",
    display_name="Code Executor",
    tools=["python_execute", "file_read", "file_write"],
)
```

Tool logic is implemented inside your LLM call.

### How do I visualize the graph? Which formats are supported?

gMAS Framework provides a powerful visualization system with **Pydantic styles** and support for multiple formats:

**Supported formats:**
1. **Mermaid** — for GitHub/docs
2. **ASCII art** — for terminals
3. **Graphviz DOT** — for professional visualization
4. **Rich Console** — colored terminal output
5. **PNG/SVG/PDF** — image rendering (requires system Graphviz)

```python
from core.visualization import (
    GraphVisualizer,
    VisualizationStyle,
    NodeStyle,
    NodeShape,
    MermaidDirection,
    # Convenience functions
    to_mermaid,
    to_ascii,
    print_graph,
    render_to_image,
)

# Quick visualization (convenience functions)
print(to_mermaid(graph, direction=MermaidDirection.LEFT_RIGHT))
print(to_ascii(graph, show_edges=True))
print_graph(graph, format="auto")  # Auto-selects colored/ascii

# Advanced custom styles (Pydantic models)
style = VisualizationStyle(
    direction=MermaidDirection.LEFT_RIGHT,
    agent_style=NodeStyle(
        shape=NodeShape.ROUND,
        fill_color="#e3f2fd",
        stroke_color="#1976d2",
        icon="🤖",
    ),
    show_weights=True,
    show_tools=True,
)

viz = GraphVisualizer(graph, style)
viz.save_mermaid("graph.md", title="My Workflow")
viz.save_dot("graph.dot")

# Image rendering (requires: pip install graphviz + system graphviz)
try:
    render_to_image(graph, "output.png", format="png", dpi=150, style=style)
    render_to_image(graph, "output.svg", format="svg", style=style)
    print("✅ Images created")
except Exception as e:
    print(f"⚠️  Install system Graphviz: {e}")
    # Ubuntu: sudo apt install graphviz
    # macOS: brew install graphviz
```

**Installing Graphviz for image rendering:**
```bash
# Python library
pip install graphviz

# System Graphviz
# Ubuntu/Debian:
sudo apt install graphviz

# macOS:
brew install graphviz

# Windows:
winget install graphviz
```

### How do I save and load a graph?

```python
import json

# Save
data = graph.to_dict()
with open("graph.json", "w") as f:
    json.dump(data, f)

# Load
with open("graph.json", "r") as f:
    data = json.load(f)
graph = RoleGraph.from_dict(data)
```

**Saving via Pydantic schemas (recommended):**
```python
from core.schema import GraphSchema

# Build a schema from the graph
schema = GraphSchema(
    name="MyGraph",
    nodes={agent.agent_id: AgentNodeSchema.from_profile(agent) for agent in graph.agents},
    edges=[BaseEdgeSchema.from_edge(e) for e in graph.edges],
)

# Save (Pydantic auto-serialization)
schema_json = schema.model_dump_json(indent=2)
with open("graph_schema.json", "w") as f:
    f.write(schema_json)

# Load (Pydantic auto-validation)
with open("graph_schema.json", "r") as f:
    loaded_schema = GraphSchema.model_validate_json(f.read())

# Build a graph from the schema
from builder import build_from_schema
graph = build_from_schema(loaded_schema)
```

### How do I handle agent errors?

```python
from execution import RunnerConfig, ErrorPolicy

config = RunnerConfig(
    error_policy=ErrorPolicy(
        on_error="fallback",  # skip, retry, fallback, fail
        max_retries=3,
    ),
    pruning_config=PruningConfig(
        enable_fallback=True,
        max_fallback_attempts=2,
    ),
)

result = runner.run_round(graph)

if result.errors:
    for error in result.errors:
        print(f"Error in {error.agent_id}: {error.message}")
```

### How do I track agent performance?

```python
from core.metrics import MetricsTracker

tracker = MetricsTracker()

# Runner integration
runner = MACPRunner(llm_caller=my_llm, metrics_tracker=tracker)
result = runner.run_round(graph)

# Retrieve metrics
for agent_id in graph.node_ids:
    metrics = tracker.get_node_metrics(agent_id)
    print(f"{agent_id}:")
    print(f"  Reliability: {metrics.reliability:.2%}")
    print(f"  Avg latency: {metrics.avg_latency_ms:.0f}ms")
    print(f"  Quality: {metrics.avg_quality:.2f}")

# Save metrics
tracker.save("metrics.json")
```

### How do I use dynamic topology?

```python
# Modify the graph at runtime
graph.add_node(new_agent, connections_to=["existing_agent"])
graph.add_edge("agent1", "new_agent", weight=0.8)

# Remove inefficient agents
if metrics.get_node_metrics("slow_agent").avg_latency_ms > 5000:
    graph.remove_node("slow_agent", policy=StateMigrationPolicy.DISCARD)

# Update weights based on performance
new_weights = compute_weights_from_metrics(tracker)
graph.update_communication(new_weights)
```

### How do I integrate with LangChain?

```python
from langchain.chat_models import ChatOpenAI
from langchain.schema import HumanMessage

llm = ChatOpenAI(model="gpt-4")

def langchain_caller(prompt: str) -> str:
    messages = [HumanMessage(content=prompt)]
    response = llm(messages)
    return response.content

runner = MACPRunner(llm_caller=langchain_caller)
result = runner.run_round(graph)
```

### How do I implement human-in-the-loop?

```python
from execution import StreamEventType

def human_approval(agent_id: str, response: str) -> bool:
    print(f"\n{agent_id} replied: {response}")
    approval = input("Approve? (y/n): ")
    return approval.lower() == 'y'

def stream_with_approval(graph):
    for event in runner.stream(graph):
        if event.event_type == StreamEventType.AGENT_OUTPUT:
            if not human_approval(event.agent_id, event.content):
                # Restart the agent with feedback
                feedback = input("Your feedback: ")
                # ... restart logic ...
        yield event
```

### How do I use a graph with multiple tasks?

```python
# Option 1: sequential
queries = ["Task 1", "Task 2", "Task 3"]

for query in queries:
    graph.query = query
    result = runner.run_round(graph)
    print(f"{query}: {result.final_answer}")

# Option 2: parallel (async)
async def process_queries(queries):
    tasks = []
    for query in queries:
        graph_copy = copy.deepcopy(graph)
        graph_copy.query = query
        tasks.append(runner.arun_round(graph_copy))

    results = await asyncio.gather(*tasks)
    return results
```

### How do I combine cloud and local models?

```python
from builder import GraphBuilder

builder = GraphBuilder()

# Cloud model for public data
builder.add_agent(
    "public_analyzer",
    llm_backbone="gpt-4",
    base_url="https://api.openai.com/v1",
    api_key="$OPENAI_API_KEY",
)

# Local model (Ollama) for confidential data
builder.add_agent(
    "private_analyzer",
    llm_backbone="llama3:70b",
    base_url="http://localhost:11434/v1",
    api_key="not-needed",  # Ollama does not require an API key
)

builder.add_workflow_edge("public_analyzer", "private_analyzer")
graph = builder.build()

factory = LLMCallerFactory.create_openai_factory()
runner = MACPRunner(llm_factory=factory)
```

### How do I optimize LLM cost with multi-model routing?

```python
# Strategy: cheap models for routine tasks, expensive for complex tasks

builder = GraphBuilder()

# Steps 1-3: simple operations → cheap model
for i in range(3):
    builder.add_agent(
        f"processor_{i}",
        llm_backbone="gpt-4o-mini",  # $0.15/$0.60 per 1M tokens
        max_tokens=500,
    )

# Step 4: complex analysis → expensive model
builder.add_agent(
    "analyst",
    llm_backbone="gpt-4",            # $30/$60 per 1M tokens
    max_tokens=2000,
)

# Step 5: final formatting → cheap model
builder.add_agent(
    "formatter",
    llm_backbone="gpt-4o-mini",
    max_tokens=500,
)

# Savings: ~70–80% vs using gpt-4 for all steps
```

### How do I use API keys safely?

```python
# ❌ DO NOT do this (hardcode keys)
builder.add_agent("agent", api_key="sk-1234567890...")

# ✅ Correct: use environment variables
import os

# Method 1: load from a .env file
from dotenv import load_dotenv
load_dotenv()

builder.add_agent("agent", api_key="$OPENAI_API_KEY")

# Method 2: set the env var explicitly
os.environ["OPENAI_API_KEY"] = open("keys/openai.key").read().strip()
builder.add_agent("agent", api_key="$OPENAI_API_KEY")

# Method 3: use a factory with a default key
factory = LLMCallerFactory.create_openai_factory(
    default_api_key=os.getenv("OPENAI_API_KEY"),
)
```

### How do I configure logging?

```python
from config import setup_logging

# Configure global logging
setup_logging(
    level="DEBUG",
    log_file="framework.log",
    rotation="500 MB",
    retention="10 days",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    backtrace=True,
    diagnose=True,
)

# Use in code
from config import logger

logger.info("Starting execution")
logger.debug(f"Graph has {graph.num_nodes} nodes")
logger.error("Failed to execute agent", exc_info=True)
```

### How do I export a graph for analysis?

```python
# 1. JSON serialization
import json

graph_data = graph.to_dict()
with open("graph.json", "w") as f:
    json.dump(graph_data, f, indent=2)

# 2. PyTorch Geometric format
pyg_data = graph.to_pyg_data()
torch.save(pyg_data, "graph.pt")

# 3. NetworkX format (if needed)
import networkx as nx

G = nx.DiGraph()
for node_id in graph.node_ids:
    G.add_node(node_id, **graph.get_agent_by_id(node_id).to_dict())

for i, j in zip(*graph.edge_index):
    src = graph.node_ids[i]
    tgt = graph.node_ids[j]
    G.add_edge(src, tgt, weight=graph.A_com[i, j])

nx.write_gexf(G, "graph.gexf")

# 4. CSV export
import pandas as pd

# Nodes
nodes_df = pd.DataFrame([
    {"id": agent.agent_id, "name": agent.display_name, "tools": ",".join(agent.tools)}
    for agent in graph.agents
])
nodes_df.to_csv("nodes.csv", index=False)

# Edges
edges = []
for i in range(graph.num_nodes):
    for j in range(graph.num_nodes):
        if graph.A_com[i, j] > 0:
            edges.append({
                "source": graph.node_ids[i],
                "target": graph.node_ids[j],
                "weight": graph.A_com[i, j],
            })
edges_df = pd.DataFrame(edges)
edges_df.to_csv("edges.csv", index=False)
```

### How do I test agents?

```python
import pytest
from unittest.mock import Mock

def test_agent_execution():
    # Mock the LLM
    mock_llm = Mock(return_value="Mocked response")

    # Build a graph
    agents = [AgentProfile(agent_id="test", display_name="Test Agent")]
    graph = build_property_graph(agents, [], query="Test query")

    # Run
    runner = MACPRunner(llm_caller=mock_llm)
    result = runner.run_round(graph)

    # Assertions
    assert result.final_answer == "Mocked response"
    assert len(result.execution_order) == 1
    assert result.total_tokens >= 0
    mock_llm.assert_called_once()

def test_error_handling():
    # Mock the LLM with an error
    mock_llm = Mock(side_effect=Exception("LLM error"))

    graph = build_property_graph([agent], [], query="Test")

    config = RunnerConfig(
        max_retries=2,
        error_policy=ErrorPolicy(on_error=ErrorAction.SKIP),
    )
    runner = MACPRunner(llm_caller=mock_llm, config=config)

    result = runner.run_round(graph)

    assert len(result.errors) > 0
    assert result.final_answer is None

def test_parallel_execution():
    agents = [
        AgentProfile(agent_id=f"agent_{i}", display_name=f"Agent {i}")
        for i in range(3)
    ]
    edges = [("agent_0", "agent_1"), ("agent_0", "agent_2")]
    graph = build_property_graph(agents, edges, query="Test")

    config = RunnerConfig(enable_parallel=True, max_parallel_size=2)
    runner = MACPRunner(llm_caller=mock_llm, config=config)

    result = runner.run_round(graph)

    assert len(result.execution_order) == 3
```

### How do I scale to large graphs?

```python
# 1. Use pruning to cut inefficient paths
config = RunnerConfig(
    pruning_config=PruningConfig(
        min_weight_threshold=0.2,
        min_probability_threshold=0.1,
        token_budget=5000,
    ),
)

# 2. Use parallel execution
config.enable_parallel = True
config.max_parallel_size = 10

# 3. Use beam search to cap paths
config.routing_policy = RoutingPolicy.BEAM_SEARCH
scheduler = AdaptiveScheduler(policy=RoutingPolicy.BEAM_SEARCH, beam_width=5)

# 4. Use subgraph filtering
from core.algorithms import GraphAlgorithms, SubgraphFilter

algo = GraphAlgorithms(graph)
subgraph = algo.filter_subgraph(SubgraphFilter(
    max_hop_distance=3,
    from_node="start",
    min_edge_weight=0.3,
))

# 5. Use async for parallel requests
async def process_large_graph(graph):
    results = await runner.arun_round(graph)
    return results
```

---

## License

<a href="https://github.com/frontier-ai-next/gmas-framework">gmas</a> © 2026 by <a href="https://github.com/frontier-ai-next">frontier-ai</a> is licensed under <a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a><img src="https://mirrors.creativecommons.org/presskit/icons/cc.svg" alt="" style="max-width: 1em;max-height:1em;margin-left: .2em;"><img src="https://mirrors.creativecommons.org/presskit/icons/by.svg" alt="" style="max-width: 1em;max-height:1em;margin-left: .2em;"><img src="https://mirrors.creativecommons.org/presskit/icons/sa.svg" alt="" style="max-width: 1em;max-height:1em;margin-left: .2em;">

---


<p align="center">
  Made with ❤️ for the multi-agent systems developer community
</p>
