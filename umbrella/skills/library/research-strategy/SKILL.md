---
name: research-strategy
status: active
domains: ["research", "architecture"]
phases: ["research", "research_review"]
when_to_use: "At the start of the research phase when exploring unfamiliar territory."
---

## Research order

1. **Check palace first** — search `palace.lesson`, `palace.idea`, `palace.codeptr` for relevant prior knowledge
2. **Scan existing workspace** — read key files to understand what's already built
3. **Search externally** — `github_project_search`, `deep_search`, `web_search` for comparable implementations
4. **Find MCP tools** — `mcp_discover` for any services that solve this domain
5. **Persist findings** — use accepted `palace_add` calls for the phase-required findings before exiting

## Architecture draft must include

- Problem statement (1 paragraph)
- Key design decisions with trade-offs
- List of components / modules to build or reuse
- Dependencies (packages, MCPs, external APIs)
- Risks and unknowns

## Output criteria

Call `submit_research_summary` only when:
- The phase prompt's required `palace_add` count has been accepted
- At least one architecture finding is persisted with concrete trade-offs
- External code references or search-provider limitations are recorded as evidence
