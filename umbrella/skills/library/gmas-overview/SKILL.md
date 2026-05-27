---
name: gmas-overview
status: active
domains: ["multi_agent_gmas", "multi_agent", "gmas", "llm"]
phases: ["research", "plan", "execute", "subtask_template"]
when_to_use: "When a workspace task needs LLM-backed agents, multi-agent coordination, or GMAS integration."
---

## GMAS Usage

Prefer the in-repo GMAS primitives for LLM-backed agents (`gmas/` package, dependency `frontier-ai-gmas`).

### How to load context (do not guess APIs)

1. **Research / plan** — broad queries are allowed. Call `get_gmas_context` or `search_gmas_knowledge`, then read `key_symbols`, `key_files`, `results`, and `implementation_guide`.
2. **Execute (GMAS subtask)** — before the first write, call again with **concrete symbols** from step 1 plus the subtask goal, e.g. `AgentProfile MACPRunner LLMCallerFactory <your subtask>`.
3. Implement only APIs that appear in retrieval hits. Do **not** invent `gmas.LLMClient` or similar unless listed in `key_symbols`.

### Design guidance

- Model agents as explicit roles with inputs, outputs, and state.
- Keep game or domain state in deterministic code.
- Use LLM agents for decisions, negotiation, planning, and narrative reasoning.
- Parse LLM outputs through structured contracts before mutating state.
- Resolve runtime LLM env through public aliases `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`.
- Handle missing credentials with explicit errors — no silent mock decisions pretending to be LLM output.

### Typical patterns (verify in context, do not assume)

- `AgentProfile` + system prompt for bot identity.
- `MACPRunner` / graph execution for multi-agent turns.
- `LLMCallerFactory` / caller config for real LLM calls.

Verification should prove at least one real agent decision path when credentials are present.
