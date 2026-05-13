# Memory System

Each agent maintains its own decentralized state.

## Agent State

```python
# Access agent state
agent = graph.get_agent("agent_id")
current_state = agent.state

# Update state
agent.state["context"] = "information"
agent.state["history"] = [1, 2, 3]
```

## Memory in Execution

Enable memory in the runner:

```python
from gmas.execution import MACPRunner, MemoryConfig

config = MemoryConfig(
    working_max_entries=10,
    long_term_max_entries=50,
)

runner = MACPRunner(
    llm_caller=llm_caller,
    enable_memory=True,
    memory_config=config,
)
```

## Shared Memory Pool

Share state between agents:

```python
from gmas.execution import SharedMemoryPool

pool = SharedMemoryPool()
pool.write("key", "value")
value = pool.read("key")
```
