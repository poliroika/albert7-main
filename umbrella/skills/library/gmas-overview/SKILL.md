---
name: gmas-overview
status: active
domains: ["multi_agent", "gmas", "llm"]
phases: ["research", "plan", "execute"]
when_to_use: "When a workspace task needs LLM-backed agents, multi-agent coordination, or GMAS integration."
---

## GMAS Usage

Prefer the in-repo GMAS primitives for LLM-backed agents.

Design guidance:
- Model agents as explicit roles with inputs, outputs, and state.
- Keep game or domain state in deterministic code.
- Use LLM agents for decisions, negotiation, planning, and narrative reasoning.
- Parse LLM outputs through structured contracts before mutating state.
- Resolve runtime LLM env through the standalone project aliases `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`. When the workspace is launched by Umbrella, also accept inherited compatibility aliases `OUROBOROS_LLM_API_KEY`, `OUROBOROS_LLM_BASE_URL`, and `OUROBOROS_MODEL`. Do not require `OPENAI_API_KEY` unless the selected provider is OpenAI, and do not hardcode OpenAI base URLs or `gpt-*` fallback models in generated workspace code.
- Handle missing credentials with explicit startup/runtime errors, retry, or paused bot turns. Do not fall back to static, random, cached, or hardcoded decisions while pretending LLM decisions happened.

Verification should prove at least one real agent decision path using the inherited runtime env when credentials are present.
