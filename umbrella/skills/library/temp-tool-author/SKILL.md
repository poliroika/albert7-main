---
name: temp-tool-author
status: active
domains: ["tooling", "automation"]
phases: ["research", "plan", "execute"]
when_to_use: "When no existing tool covers a needed capability and mcp_discover / enable_tools have been checked first."
---

## When to write a temp tool

Only write a temp tool if:
1. `list_available_tools` shows nothing that fits
2. `mcp_discover` found no relevant MCP server
3. The need is specific to this run, not a general tool

## How to write one

Call `register_temp_tool(name, python_src, json_schema, scope="phase"|"subtask")`.

- `name`: snake_case, lowercase, descriptive
- `python_src`: a standalone function `def run(ctx, **kwargs) -> str`. Must not import from umbrella or ouroboros internals.
- `json_schema`: OpenAI function schema object
- `scope`: "subtask" expires at subtask end; "phase" expires at phase end

## Rules

- No network access in temp tools unless explicitly needed
- No file writes outside `drive/tmp_tools/<phase>/` 
- No shell execution unless the phase allows shell tools
- Keep it under 50 lines
- Test with a simple input before using in production subtask

## Example

```python
def run(ctx, *, url: str) -> str:
    import urllib.request
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read(4096).decode("utf-8", errors="replace")
```
