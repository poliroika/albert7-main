# Tools

Agents can use tools to extend their capabilities.

## Available Tools

- **ShellTool** - Execute shell commands
- **WebSearchTool** - Search the web
- **CodeInterpreter** - Execute Python code
- **MCPClient** - Model Context Protocol

## Using Tools

```python
from gmas.tools import ToolRegistry, ShellTool

registry = ToolRegistry()
registry.register(ShellTool(timeout=10))

agent = AgentProfile(
    agent_id="agent",
    display_name="Agent",
    tools=["shell"],
)
```

## Custom Tools

```python
from gmas.tools import FunctionTool

func_tool = FunctionTool()

@func_tool.register
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression."""
    return str(eval(expression))

registry.register(func_tool)
```
