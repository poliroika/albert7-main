# Tools API

## Tools Module

```python
from gmas.tools import (
    # Base
    BaseTool,
    FunctionTool,
    ToolRegistry,

    # Specific tools
    ShellTool,
    WebSearchTool,
    CodeInterpreter,
    MCPClient,

    # Web search
    WebSearchProvider,
    DuckDuckGoProvider,
    SerperProvider,
)
```

## BaseTool

Base class for creating tools.

```python
from gmas.tools import BaseTool

class MyTool(BaseTool):
    name = "my_tool"
    description = "Does something"

    def run(self, **kwargs) -> str:
        return "result"
```

## FunctionTool

Create tool from function.

```python
from gmas.tools import FunctionTool

tool = FunctionTool()

@tool.register
def my_function(param: str) -> str:
    """Function description."""
    return f"Processed: {param}"
```

## ToolRegistry

Register and manage tools.

```python
from gmas.tools import ToolRegistry, ShellTool

registry = ToolRegistry()
registry.register(ShellTool(timeout=10))
registry.register(my_tool)

# Execute tool
result = registry.call("tool_name", **kwargs)
```

## WebSearchTool

```python
from gmas.tools import WebSearchTool, DuckDuckGoProvider

search = WebSearchTool(
    provider=DuckDuckGoProvider(),
    max_results=5,
)

results = search.run("search query")
```
