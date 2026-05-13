# Callbacks

Track and respond to execution events.

## Base Callback Handler

```python
from gmas.callbacks import BaseCallbackHandler

class MyHandler(BaseCallbackHandler):
    def on_run_start(self, graph, config):
        print("Execution started")

    def on_agent_end(self, agent_id, output):
        print(f"Agent {agent_id} finished")

    def on_tool_start(self, tool_name, inputs):
        print(f"Tool {tool_name} called")
```

## Using Callbacks

```python
from gmas.execution import MACPRunner

runner = MACPRunner(
    llm_caller=llm_caller,
    callbacks=[MyHandler()],
)
result = runner.run_round(graph)
```

## Built-in Handlers

- **StdoutCallbackHandler** - Print to console
- **MetricsCallbackHandler** - Track metrics
- **FileCallbackHandler** - Write to file
