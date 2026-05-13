# gMAS — graph Multi-Agent System

A modern framework for building multi-agent systems based on rustworkx — a flexible and high-performance alternative to LangGraph with dynamic topology, decentralized memory, and full access to graph structures.

## Why gMAS is better than LangGraph

### 1. Dynamic Topology

Unlike LangGraph, where the topology is fixed, our framework allows you to dynamically change the structure of the agent graph at runtime via:

- `RoleGraph.update_communication()` - dynamic edge updates
- Direct access to `rx.PyDiGraph` for adding/removing nodes and edges

### 2. Decentralized Memory

Unlike the centralized architecture of LangGraph, we implement a decentralized approach:

- `AgentProfile.state` - local state of each agent
- Ability to save/restore states of individual nodes
- Separation of agent state from overall graph state

### 3. Graph as a First-Class Citizen

LangGraph hides graph structures from the developer. We provide full control:

- Full access to the adjacency matrix (`A_com`, `edge_index`)
- Ability to add data to nodes/edges (`edge_attr`, node data)
- Conversion to PyTorch Geometric for graph neural networks

### 4. Alternative Information Transfer Methods

Support for more than just text messages:

- Embeddings are stored inside each agent (`AgentProfile.embedding`), encoded via `NodeEncoder`
- Hidden agent states (`AgentProfile.hidden_state`) for passing between agents
- Edge attributes for weights/connection types
- Readiness for tokens and hidden representations

## File Structure

```text
src/
├── core/
│   ├── graph.py                 # RoleGraph - main agent graph
│   ├── agent.py                 # AgentProfile, TaskNode, AgentLLMConfig
│   ├── schema.py                # GraphSchema, AgentNodeSchema, LLMConfig, etc.
│   ├── encoder.py               # NodeEncoder - description encoding
│   ├── algorithms.py            # Graph algorithms
│   ├── gnn.py                   # GNN routing (PyTorch Geometric)
│   ├── metrics.py               # Execution metrics
│   ├── visualization.py         # Graph visualization
│   └── events.py                # Event system
├── execution/
│   ├── runner/                  # MACPRunner - modular design
│   │   ├── __init__.py          # Public facade, re-exports
│   │   ├── core.py              # Lifecycle, memory, caller selection
│   │   ├── execution.py         # Simple/adaptive execution paths
│   │   ├── batch.py             # run_round(), arun_round() entrypoints
│   │   ├── stream.py            # stream(), astream()
│   │   ├── topology.py          # Dynamic topology changes
│   │   ├── state.py             # RunnerConfig, ExecutionContext, TopologyAction
│   │   ├── llm.py               # LLM caller protocols, factories
│   │   ├── prompting.py         # StructuredPrompt helpers
│   │   └── shared.py            # Common imports
│   ├── scheduler.py             # Scheduler (topological sort, SCC, adaptive)
│   ├── streaming.py             # Streaming API (execution events)
│   ├── budget.py                # Token/request budget
│   └── errors.py                # Typed errors and handling policies
├── builder/
│   └── graph_builder.py         # GraphBuilder, build_property_graph
├── callbacks/
│   ├── manager.py               # CallbackManager, AsyncCallbackManager
│   ├── base.py                  # BaseCallbackHandler, AsyncCallbackHandler
│   ├── context.py               # Context manager for callbacks
│   ├── events.py                # Callback event types
│   └── handlers/
│       ├── stdout.py            # StdoutCallbackHandler
│       ├── metrics.py           # MetricsCallbackHandler
│       └── file.py              # FileCallbackHandler
├── tools/
│   ├── base.py                  # BaseTool, FunctionTool, ToolRegistry
│   ├── shell.py                 # ShellTool
│   ├── web_search/              # Web search tool (modular)
│   │   ├── _tool.py             # Main WebSearchTool class
│   │   ├── _policy.py           # WebSearchPolicy - scoring/excerpt config
│   │   ├── _providers.py        # DuckDuckGo, Serper, Tavily providers
│   │   ├── _fetchers.py         # URLFetcher, SeleniumFetcher, PlaywrightFetcher
│   │   └── ...
│   ├── code_interpreter.py      # Code interpreter tool
│   └── ...
├── config/
│   ├── settings.py              # FrameworkSettings (pydantic-settings)
│   └── logging.py               # Logging via loguru
└── utils/
    ├── async_utils.py           # Async/sync utilities
    ├── memory.py                # AgentMemory, SharedMemoryPool
    └── state_storage.py         # StateStorage implementations
```

## Key Concepts

### RoleGraph

The main data structure — a directed graph based on rustworkx with:

- A list of agents (embeddings stored inside each `AgentProfile`)
- An adjacency matrix for fast access
- Edge attributes (weight, type, additional data)
- Conversion methods to different formats
- `embeddings` accessor to collect embeddings of all agents into a tensor

### AgentProfile

Agent profile with:

- Identifier and description
- List of available tools
- Embedding (`embedding`) and hidden state (`hidden_state`) — stored inside the agent
- Local state (`state`) — decentralized memory

### Execution Scheduler

Execution scheduler:

- Topological sort for DAGs
- SCC processing for graphs with cycles
- Support for parallel execution of independent agents

### MACPRunner

Multi-Agent Communication Protocol executor:

- Executes agents in the order specified by the graph
- Passes messages between connected agents
- Asynchronous and synchronous versions
- **Structured prompts**: `structured_llm_caller` for modern chat LLMs
- **Multi-model support**: per-agent callers via `llm_callers` or `LLMCallerFactory`
- **Streaming**: `stream()`, `astream()` for real-time output
- **Dynamic topology**: runtime graph modifications via topology hooks

## Dependencies

```text
rustworkx>=0.17.1
pydantic>=2.12.5
pydantic-settings>=2.13.1
torch>=2.11.0
loguru>=0.7.3
openai>=2.30.0
httpx>=0.28.1
semver>=3.0.4
sentence-transformers>=5.3.0  # optional for embeddings
```

### Compatibility and Versions

- Minimum supported Python: **3.12** (tested on 3.12 and 3.13).
- Library compatibility test matrix:

| rustworkx | torch | Status |
| --- | --- | --- |
| 0.17.x | 2.11.x | ✅ verified |

When upgrading rustworkx or torch, confirm compatibility of routing
and `RoleGraph` serialization.

## Development

### Quick Start

```bash
# Install development dependencies
uv sync --all-extras --group test --group lint --group typecheck

# Run all tests (Python 3.12 and 3.13, linting, type checking)
tox

# Run tests for a specific Python version
tox -e py312

# Quick tests without coverage (faster feedback)
tox -e quick
```

### Testing

```bash
# Run all tests with coverage
pytest tests/ -v

# Single test file
pytest tests/path/to/test_file.py -v

# Single test function
pytest tests/path/to/test_file.py::test_function_name -v

# With coverage
coverage run -m pytest tests/ -v
coverage report --show-missing
```

### Linting and Formatting

```bash
# Check linting
ruff check src/ tests/ examples/ benchmarks/

# Format code
ruff format src/ tests/ examples/ benchmarks/

# Run via tox
tox -e lint
```

### Type Checking

```bash
# Type check with ty
ty check --ignore unresolved-import src/ tests/

# Run via tox
tox -e typecheck
```

### Tox Environments

The project uses tox with tox-uv for multi-version testing:

- `py312`, `py313` - Run tests with coverage on Python 3.12/3.13
- `lint` - Run ruff linter and formatter check
- `typecheck` - Run ty type checking
- `coverage` - Combine coverage from all Python versions and generate report
- `quick` - Run tests without coverage for faster feedback
- `staging` - Test against uv.lock for CI/CD reproducibility
- `benchmarks` - Run benchmark suite

```bash
# Run specific environments
tox -e py312,py313,coverage

# Test against lock file (for CI/CD)
tox -e staging
```

## Secure Configuration

Use `FrameworkSettings` to load settings, which supports strict validation
and secure keys via file:

```bash
export GMAS_API_KEY="sk-..."
export GMAS_BASE_URL="https://api.provider.example"
```

or

```bash
echo "sk-from-vault" > /secure/rwxf.key
export GMAS_API_KEY_FILE=/secure/rwxf.key
```

```python
from gmas.config import FrameworkSettings

settings = FrameworkSettings()
llm_key = settings.resolved_api_key  # explicit error if key is missing
```

Invalid or empty keys block startup without silent fallback. Timeout,
retry, and logging parameters are supported via `GMAS_*` environment variables.

## Usage Example

```python
from gmas.core import RoleGraph, AgentProfile
from gmas.execution import MACPRunner, build_execution_order
from gmas.builder import build_property_graph

# Create agents
agents = [
    AgentProfile(agent_id="solver", display_name="Math Solver", ...),
    AgentProfile(agent_id="checker", display_name="Checker", ...),
]

# Build the graph
graph = build_property_graph(agents, edges=[("solver", "checker")])

# Dynamic topology modification
graph.graph.add_edge(node_a, node_b, {"weight": 1.0})

# Get execution order
order = build_execution_order(graph.A_com, agent_ids)

runner = MACPRunner(llm_caller=my_llm_caller)
result = runner.run_round(graph)

# Access graph data
adjacency = graph.A_com  # torch.Tensor adjacency matrix
edge_index = graph.edge_index  # PyG format (torch.Tensor)
pyg_data = graph.to_pyg_data()  # PyTorch Geometric Data object
```

### Advanced Routing and Memory

```python
graph.update_communication(A_com, s_tilde=scores, p_matrix=probabilities)

# Stratified memory with active hidden_state
agent = AgentProfile(...)
agent = agent.with_hidden_state(hidden_state_tensor)
graph.add_node(agent, connections_to=["checker"])

# Online rescheduling: execution will automatically recalculate order based on new weights
runner.run_round(graph)
```

For GNN routing, use the `examples/gnn_routing.py` example: it prepares
`edge_index/edge_attr`, runs a PyTorch Geometric model, and updates `RoleGraph`
based on its output.

## Extending for New Transfer Methods

To add alternative information transfer methods (tokens, hidden representations):

1. Extend `AgentProfile` with new fields to store representations
2. Modify `MACPRunner._format_user_prompt()` to pass more than just text
3. Add attributes on edges to store intermediate representations
