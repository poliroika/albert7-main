# Umbrella Policy: LLM/Agent Runtime

This policy applies only when the workspace task or current subtask implements
LLM, GMAS, agent, multi-agent, bot, judge, model-driven, or AI-opponent
behavior. Ignore it for ordinary non-LLM, non-agent workspaces.

Umbrella is the control plane: it launches Ouroboros/deep-agent workers,
provides tools, memory, review, and verification. Generated workspace code must
inherit the runtime that Umbrella provides; it must not invent a provider or
silently replace required LLM behavior.

## Runtime Contract

- Resolve credentials and model through public project aliases:
  `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`.
  Umbrella maps host control-plane launch env into those public aliases before
  workspace commands run, so generated projects should not read, document, or
  test control-plane aliases.
- Do not turn unsupported or obsolete control-plane alias names into generated
  project docs, tests, or user-facing requirements.
- Do not require `OPENAI_API_KEY` as the universal workspace LLM credential.
  It can be one provider credential when the generated project intentionally
  chooses OpenAI, but not the project runtime contract.
- Do not hardcode provider/model defaults such as `https://api.openai.com/v1`
  or `gpt-*` as silent runtime fallbacks.

## Failure Semantics

Required LLM/agent/bot decisions must be produced by the inherited real LLM
runtime. If that runtime is unavailable or a call fails, acceptable behavior is:
bounded retry, pause the affected turn/task, skip only an explicitly live-LLM
test with a clear reason, or surface a startup/runtime error.

Forbidden replacement behavior includes mock/fake/dry-run LLM paths,
deterministic/static/heuristic/random/default decisions, rule-based AI
replacement, cached decision reuse, "safe minimal actions", "human-only" agent
mode, or "graceful degradation" that substitutes non-LLM decisions for required
LLM behavior.

## Memory And Review

Research memory and phase handoffs must not store replacement behavior as an
architecture recommendation. If an unsafe current-run memory entry was saved,
research must create and cite a corrected memory entry; later phases must treat
the latest accepted artifact plus cited current-run memory as the source of
truth. Review and watcher phases should block unsafe runtime replacement
behavior, but keep implementation-owned details in execute/subtask review.
