# AgentProfile

Represents an individual agent in the multi-agent system.

## Creating an Agent

```python
from gmas.core import AgentProfile

agent = AgentProfile(
    agent_id="researcher",
    display_name="Senior Researcher",
    description="Conducts thorough research on given topics",
    persona="You are an experienced researcher with attention to detail",
    tools=["search", "browse", "calculator"],
)
```

## Agent Properties

```python
agent.agent_id          # Unique identifier
agent.display_name      # Human-readable name
agent.description       # Functional description
agent.persona           # Agent personality/role
agent.tools             # Available tools
agent.state             # Local state dict
agent.embedding         # Encoded representation
```

## Agent State

Each agent maintains its own state (decentralized memory):

```python
# Read state
current_state = agent.state

# Update state
agent.state["last_query"] = "example query"
agent.state["results"] = [1, 2, 3]

# Check if key exists
if "context" in agent.state:
    context = agent.state["context"]
```

## Agent with Hidden State

For GNN routing and advanced features:

```python
import torch

hidden = torch.randn(128)  # Hidden state vector
agent = agent.with_hidden_state(hidden)
```

## LLM Configuration

```python
from gmas.core import AgentProfile, AgentLLMConfig

config = AgentLLMConfig(
    model="gpt-4",
    temperature=0.7,
    max_tokens=1000,
)

agent = AgentProfile(
    agent_id="agent",
    display_name="Agent",
    llm_config=config,
)
```

## Tools

Assign tools to agents:

```python
agent = AgentProfile(
    agent_id="analyst",
    display_name="Data Analyst",
    tools=[
        "python",           # Code execution
        "calculator",       # Math operations
        "web_search",      # Search the web
    ],
)

# Check available tools
if "web_search" in agent.tools:
    # Agent can search the web
    pass
```

## Cloning and Updating

```python
# Update specific fields
updated = agent.model_copy(update={
    "display_name": "New Name",
    "description": "New description",
})

# Or use with methods
updated = agent.with_state({"key": "value"})
```
