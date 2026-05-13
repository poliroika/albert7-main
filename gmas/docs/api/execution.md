# Execution API

## Execution Module

```python
from gmas.execution import (
    # Runner
    MACPRunner,
    MACPResult,
    RunnerConfig,

    # Scheduling
    build_execution_order,
    get_parallel_groups,
    AdaptiveScheduler,

    # Streaming
    StreamEventType,
    StreamEvent,
    StreamBuffer,
    stream_to_string,
    print_stream,

    # Budget
    BudgetConfig,
    BudgetTracker,

    # Errors
    ErrorPolicy,
    ExecutionError,
    BudgetExceededError,

    # Callbacks
    CallbackManager,
    AsyncCallbackManager,
)
```

## MACPRunner

Main execution engine.

### Parameters

- `llm_caller: Callable[[str], str]` - Synchronous LLM caller
- `async_llm_caller: Callable[[str], Awaitable[str]]` - Async LLM caller
- `config: RunnerConfig` - Configuration options
- `budget_config: BudgetConfig` - Budget limits
- `callbacks: list[BaseCallbackHandler]` - Event handlers

### Methods

```python
runner.run_round(graph) -> MACPResult
runner.arun_round(graph) -> Awaitable[MACPResult]
runner.stream(graph) -> Iterator[StreamEvent]
runner.astream(graph) -> AsyncIterator[StreamEvent]
```

## RunnerConfig

Configuration options.

```python
config = RunnerConfig(
    enable_parallel=True,
    max_parallel_size=3,
    enable_memory=True,
    memory_context_limit=5,
    enable_tools=True,
    enable_logging=True,
)
```

## Streaming

### StreamEventType

- `RUN_START` - Execution started
- `AGENT_START` - Agent started
- `AGENT_OUTPUT` - Agent produced output
- `RUN_END` - Execution finished
- `ERROR` - Error occurred

### StreamBuffer

```python
buffer = StreamBuffer()
buffer.add(event)
print(buffer.final_answer)
```

## Budget

```python
budget = BudgetConfig(
    total_token_limit=10000,
    max_prompt_length=4000,
    time_limit_seconds=300,
)
```
