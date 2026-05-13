# GMAS Agent Authoring Checklist

Use this checklist when building agents or graphs:

1. Query `get_gmas_context` for the exact pattern: graph construction, runner invocation, tools, streaming, memory, or routing. If retrieval is empty or irrelevant, say so and record the gap in memory — do not invent `gmas` APIs from stale recall.
2. Prefer examples from `gmas/examples/` and docs from `gmas/docs/` before reading raw framework internals.
3. Copy the import style and runner wiring from the retrieved example.
4. Add a small smoke test or runnable script in the workspace.
5. Run it with `run_workspace_command`.
6. Record the result in Umbrella memory.
