# Quick Start

This guide will get you running with gMAS in 5 minutes.

## Step 1: Create Agents

```python
from gmas.core import AgentProfile

agents = [
    AgentProfile(
        agent_id="researcher",
        display_name="Researcher",
        description="Searches for and collects information",
    ),
    AgentProfile(
        agent_id="analyst",
        display_name="Analyst",
        description="Analyzes data and forms insights",
    ),
    AgentProfile(
        agent_id="writer",
        display_name="Writer",
        description="Writes the final response",
    ),
]
```

## Step 2: Build Graph

```python
from gmas.builder import build_property_graph

graph = build_property_graph(
    agents,
    workflow_edges=[
        ("researcher", "analyst"),
        ("analyst", "writer"),
    ],
    query="What will AI be like in 2026?",
)
```

## Step 3: Configure LLM

```python
import openai

def llm_caller(prompt: str) -> str:
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
```

## Step 4: Execute

```python
from gmas.execution import MACPRunner

runner = MACPRunner(llm_caller=llm_caller)
result = runner.run_round(graph)

print(f"Execution order: {result.execution_order}")
print(f"Time: {result.total_time:.2f}s")
print(f"Answer: {result.final_answer}")
```

## Step 5: Stream (Optional)

```python
from gmas.execution import StreamEventType

for event in runner.stream(graph):
    if event.event_type == StreamEventType.AGENT_START:
        print(f"\n{event.agent_name} started...")
    elif event.event_type == StreamEventType.AGENT_OUTPUT:
        print(f"{event.agent_name}: {event.content[:100]}...")
```

## Next Steps

- [Key Concepts](../user-guide/key-concepts.md)
- [API Reference](../api/core.md)
