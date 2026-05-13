# Basic Usage Example

A simple multi-agent pipeline.

## Setup

```python
from gmas.core import AgentProfile
from gmas.builder import build_property_graph
from gmas.execution import MACPRunner
import openai
```

## Create Agents

```python
agents = [
    AgentProfile(
        agent_id="researcher",
        display_name="Researcher",
        description="Gathers information",
    ),
    AgentProfile(
        agent_id="writer",
        display_name="Writer",
        description="Writes final answer",
    ),
]
```

## Build Graph

```python
graph = build_property_graph(
    agents,
    workflow_edges=[("researcher", "writer")],
    query="What is quantum computing?",
)
```

## Execute

```python
def llm_caller(prompt: str) -> str:
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content

runner = MACPRunner(llm_caller=llm_caller)
result = runner.run_round(graph)

print(result.final_answer)
```

## Output

```
Execution order: ['researcher', 'writer']
Time: 3.45s
Answer: [The final answer from the writer agent]
```
