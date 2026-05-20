---
name: mcp-bridge-author
status: active
domains: ["mcp", "tools", "integration"]
phases: ["execute", "subtask_template"]
when_to_use: "When implementing or consuming MCP-style tools in a workspace task."
---

## MCP Bridge Rules

Prefer existing registered tools before inventing new bridges.

When a bridge is needed:
- Define a narrow contract and schema.
- Keep secrets in env, never in source.
- Log request shape and result status without sensitive values.
- Add a health or smoke command.
- Make failure modes explicit and recoverable.

Install or register new tools only when the task cannot be solved with available tools.
