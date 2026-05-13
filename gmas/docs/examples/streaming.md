# Streaming Example

Execute agents with real-time output.

## Basic Streaming

```python
from gmas.core import AgentProfile
from gmas.builder import build_property_graph
from gmas.execution import MACPRunner, StreamEventType

agents = [AgentProfile(agent_id="agent", display_name="Agent")]
graph = build_property_graph(agents, query="Hello")

runner = MACPRunner(llm_caller=llm_caller)

for event in runner.stream(graph):
    if event.event_type == StreamEventType.AGENT_START:
        print(f"🤖 {event.agent_name} started")
    elif event.event_type == StreamEventType.AGENT_OUTPUT:
        print(f"✅ {event.content}")
```

## Stream Buffer

Collect events:

```python
from gmas.execution import StreamBuffer

buffer = StreamBuffer()

for event in runner.stream(graph):
    buffer.add(event)
    # Process event...

print(f"Final answer: {buffer.final_answer}")
```

## Helper Functions

```python
from gmas.execution import print_stream, stream_to_string

# Print with formatting
print_stream(runner.stream(graph))

# Or get final answer as string
answer = stream_to_string(runner.stream(graph))
```

## Async Streaming

```python
import asyncio

async def run():
    async for event in runner.astream(graph):
        print(format_event(event))

asyncio.run(run())
```
