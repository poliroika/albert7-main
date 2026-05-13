# Installation

## Requirements

- Python 3.12 or higher
- [uv](https://docs.astral.sh/uv/) (recommended)
- PyTorch 2.0 or higher
- rustworkx 0.13 or higher

## Basic Installation

### Using uv (Recommended)

```bash
uv pip install frontier-ai-gmas
```

### Using pip

```bash
pip install frontier-ai-gmas
```

Core dependencies include:

- rustworkx (graph operations)
- pydantic (data validation)
- torch (tensor operations)
- loguru (logging)
- openai (LLM client)

## Optional Extras

### Web Search

```bash
uv pip install frontier-ai-gmas[web-search]   # DuckDuckGo
uv pip install frontier-ai-gmas[web-fast]     # Fast HTML parsing
uv pip install frontier-ai-gmas[selenium]     # Browser automation
```

### Machine Learning

```bash
uv pip install frontier-ai-gmas[embeddings]   # sentence-transformers
uv pip install frontier-ai-gmas[pyg]          # PyTorch Geometric
```

### Visualization

```bash
uv pip install frontier-ai-gmas[viz]          # graphviz, matplotlib
```

### All Extras

```bash
uv pip install frontier-ai-gmas[all]
```

## Development Installation

Clone and install with uv:

```bash
git clone https://github.com/frontier-ai-next/gmas.git
cd gmas
uv sync
```

Install pre-commit hooks:

```bash
uv run prek install
```

## Verify Installation

```python
from gmas.core import AgentProfile
from gmas.builder import build_property_graph

agents = [AgentProfile(agent_id="test", display_name="Test")]
graph = build_property_graph(agents, query="Test")
print(f"Success! Graph has {graph.num_nodes} nodes")
```
