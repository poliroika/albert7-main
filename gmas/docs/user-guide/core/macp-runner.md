# MACPRunner

The execution engine for running agents on a graph.

## Basic Usage

```python
from gmas.execution import MACPRunner

# Define LLM caller
def llm_caller(prompt: str) -> str:
    # Your LLM call here
    return response

# Create runner
runner = MACPRunner(llm_caller=llm_caller)

# Execute
result = runner.run_round(graph)

print(result.final_answer)
print(result.execution_order)
print(result.total_time)
```

## Configuration

```python
from gmas.execution import MACPRunner, RunnerConfig

config = RunnerConfig(
    enable_parallel=True,
    max_parallel_size=3,
    enable_memory=True,
    memory_context_limit=5,
)

runner = MACPRunner(
    llm_caller=llm_caller,
    config=config,
)
```

## Streaming

```python
from gmas.execution import StreamEventType

for event in runner.stream(graph):
    if event.event_type == StreamEventType.RUN_START:
        print("Execution started")
    elif event.event_type == StreamEventType.AGENT_START:
        print(f"{event.agent_name} started")
    elif event.event_type == StreamEventType.AGENT_OUTPUT:
        print(f"{event.agent_name}: {event.content}")
    elif event.event_type == StreamEventType.RUN_END:
        print(f"Done in {event.total_time:.2f}s")
```

## Async Execution

```python
import asyncio

async def async_llm(prompt: str) -> str:
    # Your async LLM call
    return await async_response

runner = MACPRunner(async_llm_caller=async_llm)
result = await runner.arun_round(graph)

# Or async streaming
async for event in runner.astream(graph):
    print(format_event(event))
```

## Multi-Model Support

```python
from gmas.execution import MACPRunner, LLMCallerFactory

# Create factory
factory = LLMCallerFactory()

# Register different callers
factory.register("gpt-4", my_gpt4_caller)
factory.register("claude", my_claude_caller)
factory.register("local", my_local_llm_caller)

# Use with runner
runner = MACPRunner(llm_caller_factory=factory)
```

## Budget Control

```python
from gmas.execution import MACPRunner, BudgetConfig

budget = BudgetConfig(
    total_token_limit=10000,
    max_prompt_length=4000,
    time_limit_seconds=300,
)

runner = MACPRunner(
    llm_caller=llm_caller,
    budget_config=budget,
)
```

## Memory

```python
from gmas.execution import MACPRunner, MemoryConfig

memory_config = MemoryConfig(
    working_max_entries=10,
    long_term_max_entries=50,
)

runner = MACPRunner(
    llm_caller=llm_caller,
    enable_memory=True,
    memory_config=memory_config,
    memory_context_limit=3,  # Include last 3 in prompt
)

# Access memory after execution
agent_memory = runner.get_agent_memory("agent_id")
```

## Error Handling

```python
from gmas.execution import MACPRunner, ErrorPolicy

runner = MACPRunner(
    llm_caller=llm_caller,
    error_policy=ErrorPolicy.CONTINUE,  # or RAISE, RETRY
    max_retries=3,
)
```

## Structured Prompts

For modern chat LLMs:

```python
from gmas.execution import MACPRunner, create_openai_structured_caller

runner = MACPRunner(
    llm_caller=create_openai_structured_caller(
        api_key="sk-...",
        model="gpt-4o",
    ),
)
```
