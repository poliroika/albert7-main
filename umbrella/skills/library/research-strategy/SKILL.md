---
name: research-strategy
status: active
domains: ["research", "architecture"]
phases: ["research"]
when_to_use: "At the start of the research phase when exploring unfamiliar territory."
---

## Research order

1. **Check palace first** — `palace_search` for lessons, ideas, codeptrs from prior runs.
2. **Scan workspace** — `read_file` / `list_files` on charter, TASK_MAIN, existing code.
3. **GMAS (if LLM/agents)** — `load_skill(gmas-overview)` then `search_gmas_knowledge` / `get_gmas_context`; note `key_symbols` for later execute.
4. **GitHub** — `load_skill(github-discovery)` → search → **extract snippets** on 1–2 repos → record reuse intent in palace.
5. **Web** — `web_search` / `deep_search` for repos or patterns not found on GitHub.
6. **MCP** — `load_skill(mcp-discovery)` → `mcp_discover`; register candidates in plan, not research.
7. **Persist** — accepted `palace_add` findings before `submit_research_summary`.

## External reuse decision (required mental step)

For each external source (GitHub repo, web project, MCP server), state explicitly:

| Intent | When | What to store |
|--------|------|----------------|
| idea_only | licence blocked or architecture hint | `research_finding` summary, no code |
| pattern_adapt | permissive snippet | finding + read `knowledge_md` in execute |
| codeptr | snippet maps to specific files | `codeptr` + plan `codeptr_refs` |
| dependency_import | real PyPI/npm library | note in plan + pyproject change subtask |

## Architecture draft must include

- Problem statement (1 paragraph)
- Key design decisions with trade-offs
- Components to build vs adapt from prior art (name sources)
- Dependencies (packages, MCPs, APIs)
- Risks and unknowns

## Output criteria

Call `submit_research_summary` only when required palace findings are accepted and external sources are either harvested or honestly marked `source_scarce`.
