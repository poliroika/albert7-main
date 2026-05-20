# Part 1: Goals, Audience, Terms

## Purpose

This technical report describes the post-refactoring Umbrella system for engineers who will edit code, debug runs, and extend functionality. It complements the higher-level [architecture](../architecture.md) with code-level detail.

## Audience

- Engineers extending Umbrella, Ouroboros, or workspaces.
- Operators running and debugging workspace execution.
- Contributors writing new phase manifests, tools, or skills.

## Glossary

| Term | Definition |
|------|-----------|
| **PhaseManifest** | YAML file declaring a phase's tools, skills, prompts, memory access, permissions, exit criteria, and budgets. Validated against `manifest.schema.json`. |
| **PhasePlan** | Mutable ordered list of `PhaseNode` objects defining the run's progression. Can be modified in-run via `mutate_phase_plan`. |
| **PhaseNode** | A single phase in the PhasePlan: `id`, `manifest_id`, `status` (pending/running/done/failed/skipped), optional `subtasks`, `overlay`. |
| **SubtaskCard** | A recipe within the execute phase: goal, allowed tools/skills, success test, codeptr refs, MCP refs. |
| **PhaseRunner** | `umbrella/orchestrator/runner.py` — walks the PhasePlan, spawns Worker/Watcher per phase, processes signals. |
| **Worker** | An Ouroboros agent executing a single phase with the manifest's tool filter and prompt overlays. |
| **Watcher** | An Ouroboros agent running in parallel, idle by default, waking on trigger conditions (stall, repeat error, etc.). |
| **PermissionEnvelope** | Per-phase access control: `allow/deny(tool, path?, command?)`. Evaluated as pre-hook in `ToolRegistry.execute`. |
| **MemPalace** | Unified memory facade (`umbrella/memory/palace/facade.py`) with 9 stores (Chroma + SQLite), tier/scope semantics, graph edges. |
| **MemNode** | A single memory record: `id, store, tier, scope, tags, content, embedding?, workspace_id, run_id, phase, verified, created_at`. |
| **Tier** | Recall priority: `always_on`, `hot`, `warm`, `cold`, `transient`. Orthogonal to scope. |
| **Scope** | Lifetime: `cross_run_durable`, `run_scoped`, `phase_scoped`, `subtask_scoped`, `transient`. |
| **RecallBundle** | Pre-loaded memory context for a phase: always_on nodes, hot nodes, warm vector search results, graph neighbours. |
| **FinalReport** | Evidence-based report built after `verify(pass)`: claims must cite event/artifact IDs. |
| **WatcherSignal** | Control signal from Watcher to Runner: `abort_phase`, `restart_phase`, `mutate_phase_plan`, `force_verify`, `inject_lesson`. Written to `drive/state/watcher_signal.json`. |
| **Reflexion** | Verbal self-feedback mini-phase after failed verify. Promotes to `palace.lesson` only after verified evidence of success. |
| **Drive** | Ouroboros filesystem workspace at `.umbrella/ouroboros_drive/`: logs, memory, state, task results. |
| **ResultEnvelope** | Unified JSON response for all CLI and HTTP endpoints: `{ok, data, errors[], meta}`. |
| **Skill** | A reusable knowledge pack in `umbrella/skills/library/<slug>/SKILL.md` with phase-tagged frontmatter. |
| **MCP** | Model Context Protocol — external tool servers discovered and loaded per-phase. |
| **Harness** | Runtime tool `harness_run` that runs N parallel Worker candidates on a subtask and picks the winner. |
| **Meta-Harness** | Optional experiment/candidate surface under `.umbrella/meta_harness/` (cleanup + bridge summaries). Not the same as the `harness_run` tool; no dedicated `umbrella/meta_harness/` package in this checkout. |

## Out of Scope

- Internal GMAS implementation details (see `gmas/docs/`).
- Deployment infrastructure beyond local development.
- LLM provider specifics beyond the API contract.

## Conventions

- File paths are relative to the repository root.
- Code references use `module.py::symbol` notation.
- Diagrams use Mermaid syntax.
- Configuration values show defaults in parentheses.

---

Next: [Part 2 — System Context](02-system-context.md)
