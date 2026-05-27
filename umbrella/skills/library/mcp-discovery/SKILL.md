---
name: mcp-discovery
status: active
domains: ["research", "mcp", "tools"]
phases: ["research", "plan"]
when_to_use: "When research should check whether external tools or MCP servers can help."
---

## MCP Discovery

Use MCP when an external tool server can reduce implementation risk (browser, DB, APIs).

### Workflow

1. `mcp_discover(query=...)` — review `results` and `candidate_source_ids`.
2. For promising servers, record `palace_add(kind=research_finding, source_id=mcp_discover:<query>, ...)` with transport needs and risks.
3. **Research**: discover only; do not install.
4. **Plan**: call `mcp_install` to register a candidate in `.umbrella/mcp/registry.json` (starts **disabled**; user enables in UI when appropriate).
5. Treat `install_hint_npx` as a heuristic — verify README via `web_fetch` or `github_extract_snippets` before trusting commands.

### Reuse intent

Same decision tree as GitHub (`adoption_playbook` on tool results):

- **idea_only** — MCP exists but not needed this run.
- **mcp_register** — register in plan for a later subtask that will call MCP tools after enablement.

Empty discovery is acceptable evidence when logged in findings or observations.
