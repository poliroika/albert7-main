# 🚀 Quick Start — gMAS

This brief guide will help you get started with the framework in 5 minutes.

## Installation

```bash
pip install rustworkx>=0.13 pydantic>=2.0 pydantic-settings>=2.0 torch>=2.0 loguru>=0.7
pip install sentence-transformers>=2.0  # optional, for embeddings
```

## Step 1: Create Agents

```python
from core import AgentProfile

# Each agent has a unique agent_id and a description of its role
# Embeddings and state are stored inside AgentProfile (decentralized)
agents = [
    AgentProfile(
        agent_id="researcher",
        display_name="Researcher",
        description="Searches for information and collects facts",
        tools=["search", "browse"],
    ),
    AgentProfile(
        agent_id="analyst",
        display_name="Analyst",
        description="Analyzes data and draws conclusions",
        tools=["calculate", "compare"],
    ),
    AgentProfile(
        agent_id="writer",
        display_name="Writer",
        description="Formulates the final answer",
    ),
]
```

## Step 2: Build the Graph

```python
from builder import build_property_graph

# Define connections: researcher -> analyst -> writer
workflow_edges = [
    ("researcher", "analyst"),
    ("analyst", "writer"),
]

# Build the graph with a task
graph = build_property_graph(
    agents,
    workflow_edges=workflow_edges,
    query="What technologies will be important in 2025?",
)

print(f"Graph: {graph.num_nodes} nodes, {graph.num_edges} edges")
```

## Step 3: Configure LLM

```python
# Example with OpenAI
import openai

def my_llm_caller(prompt: str) -> str:
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content

# Example with local Ollama
import requests

def ollama_caller(prompt: str) -> str:
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={"model": "llama3", "prompt": prompt, "stream": False},
    )
    return response.json()["response"]
```

## Step 4: Run Execution

```python
from execution import MACPRunner

runner = MACPRunner(llm_caller=my_llm_caller)
result = runner.run_round(graph)

print("=" * 50)
print(f"Execution order: {result.execution_order}")
print(f"Tokens used: {result.total_tokens}")
print(f"Time: {result.total_time:.2f} sec")
print("=" * 50)
print(f"\n📝 Final answer:\n{result.final_answer}")
```

## Step 5: Streaming (optional)

```python
from execution import StreamEventType

# Get results in real time
for event in runner.stream(graph):
    if event.event_type == StreamEventType.AGENT_START:
        print(f"\n🤖 {event.agent_name} started...")
    elif event.event_type == StreamEventType.AGENT_OUTPUT:
        print(f"✅ {event.agent_name}: {event.content[:100]}...")
    elif event.event_type == StreamEventType.RUN_END:
        print(f"\n🏁 Completed in {event.total_time:.2f} sec")
```

---

## Useful Patterns

### Parallel Processing

```python
# Multiple agents work in parallel
edges = [
    ("planner", "researcher_1"),
    ("planner", "researcher_2"),
    ("researcher_1", "synthesizer"),
    ("researcher_2", "synthesizer"),
]

from execution import RunnerConfig

config = RunnerConfig(enable_parallel=True, max_parallel_size=3)
runner = MACPRunner(llm_caller=my_llm, config=config)
```

### Dynamic Graph Modification

```python
# Add a new agent on the fly
new_agent = AgentProfile(agent_id="fact_checker", display_name="Fact Checker")
graph.add_node(new_agent, connections_to=["writer"])
graph.add_edge("analyst", "fact_checker", weight=0.8)
```

### Asynchronous Execution

```python
async def async_llm(prompt: str) -> str:
    # Your async LLM call
    return await call_llm_async(prompt)

runner = MACPRunner(async_llm_caller=async_llm)
result = await runner.arun_round(graph)
```

---

## Next Steps

📚 Read the [full documentation](DOCUMENTATION.md) for:
- Memory and agent embedding configuration (stored inside `AgentProfile`)
- GNN routing
- Adaptive execution with pruning and fallback
- PyTorch Geometric integration
- Configuration via environment variables

💡 Explore the examples in the `examples/` folder:
- `basic_usage.py` — basic operations
- `gnn_routing.py` — GNN routing
- `streaming_example.py` — streaming execution
