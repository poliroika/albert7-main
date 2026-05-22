# Umbrella (Control Plane)

Umbrella is the phase-driven control plane that binds together GMAS, workspaces, and Ouroboros. After the refactoring, it owns the phase machine (`umbrella/phases/`), the orchestrator (`umbrella/orchestrator/`), the unified memory facade (`umbrella/memory/palace/`), and the permission system (`umbrella/permissions/`).

## Subsystems

### Phases (`umbrella/phases/`)

The phase machine defines how a run progresses. Each phase is a YAML manifest validated against a JSON schema.

| Module | Purpose |
|--------|---------|
| `base.py` | Data classes: `PhaseManifest`, `PhasePlan`, `PhaseNode`, `SubtaskCard` |
| `loader.py` | YAML -> `PhaseManifest` dataclass |
| `registry.py` | Discover manifests + validate against schema |
| `schema/manifest.schema.json` | JSON Schema for manifest validation |
| `manifests/*.yaml` | 11 phase manifests (preflight, research, plan, execute, verify, reflexion, etc.) |

**11 manifests:**

| Manifest | Phase |
|-----------|-------|
| `preflight.yaml` | Environment health check |
| `research.yaml` | Task understanding, architecture draft |
| `research_review.yaml` | Review research output |
| `plan.yaml` | Build PhasePlan + subtask cards |
| `plan_review.yaml` | Review plan completeness |
| `execute.yaml` | Container for subtask execution |
| `subtask_template.yaml` | Template for individual subtasks |
| `subtask_review.yaml` | Review after each subtask |
| `final_review.yaml` | Goal alignment check |
| `verify.yaml` | Final verification + memory promotion |
| `reflexion.yaml` | Verbal self-feedback on failure |

### Orchestrator (`umbrella/orchestrator/`)

The orchestrator drives a run from start to finish: loading the PhasePlan, spawning Worker/Watcher per phase, processing control signals, and building the FinalReport.

| Module | Purpose |
|--------|---------|
| `runner.py` | `PhaseRunner`: walks PhasePlan, spawns Worker + Watcher, main loop |
| `worker.py` | Spawns Worker-Ouroboros per phase via `OuroborosLauncher` |
| `watcher.py` | Watcher pump-loop: poll triggers, send control signals |
| `watcher_triggers.py` | Deterministic trigger heuristics (stall, repeat error, budget overrun) |
| `phase_plan.py` | `PhasePlan` model: mutable ordered list of phases with audit trail |
| `final_report.py` | Evidence-based FinalReport builder and validator |
| `verify_loop.py` | Verification retry loop |
| `promotion.py` | Memory node promotion (run-scoped -> cross-run durable) |
| `self_improvement_runner.py` | Separate runner for system self-improvement mode (relaxed envelope) |

### MemPalace (`umbrella/memory/palace/`)

Unified memory facade with multiple Chroma stores, SQLite transient + graph, and tier/scope semantics.

| Module | Purpose |
|--------|---------|
| `facade.py` | `MemPalace`: add, search, recall, link, walk, promote, expire_scope |
| `stores.py` | Multiple Chroma collections (charter, lesson, idea, codeptr, skill_index, run, phase, subtask) |
| `tiers.py` | Enum: always_on, hot, warm, cold, transient |
| `graph.py` | SQLite edge table (src_id, dst_id, edge_type, weight, phase, created_at) |
| `recall.py` | Tier-aware recall: always_on -> hot -> vector search -> 1-hop graph walk |
| `transient.py` | SQLite transient store: events, tool I/O, terminal scrollback (TTL 24h) |

**API surface:**

```python
palace.add(store, content, tier, scope, tags, phase, links)
palace.search(query, stores, tiers, scopes, hop, n)
palace.recall(phase_id, n, include_graph)  # auto-applies phase recall policy
palace.link(src_id, dst_id, edge_type, weight)
palace.walk(node_id, edge_types, hops, direction)
palace.promote(node_id, target_store, verified)
palace.expire_scope(scope_kind, key)
palace.health()
```

### Permissions (`umbrella/permissions/`)

Per-phase access control for all tool calls.

| Module | Purpose |
|--------|---------|
| `envelope.py` | `PermissionEnvelope`: allow/deny based on phase manifest rules |
| `global.yaml` | Global hard denials (overrides any phase rule) |
| `watcher_envelope.py` | Hardcoded read-only envelope for Watcher (Python, not YAML) |
| `self_improvement.yaml` | Relaxed envelope for self-improvement run mode |
| `loader.py` | Loads and compiles permission rules from YAML |

Permission evaluation: phase rules (top-to-bottom, first match wins) then global denials (can override allows). The Watcher envelope is in Python to prevent accidental YAML edits from granting write access.

### Workspace Registry (`umbrella/workspace_registry/`)

Discovery and catalog of workspaces.

| Module | Purpose |
|--------|---------|
| `registry.py` | `WorkspaceRegistry`: discover, register, select |
| `discovery.py` | File-based discovery of `workspace.toml` |
| `models.py` | `WorkspaceRef`, `SeedWorkspaceProfile`, `TaskInstanceProfile`, `WorkspaceLineageRecord` |
| `task_main.py` | Load and parse `TASK_MAIN.md` |

### Workspace Runtime (`umbrella/workspace_runtime/`)

Instance creation, execution, and inspection.

| Module | Purpose |
|--------|---------|
| `runner.py` | Unified runner: `prepare_workspace()`, `run_workspace()`, `inspect_workspace()` |
| `instances.py` | `create_task_instance()`, `snapshot_instance()`, `archive_instance()` |
| `adapters/` | Per-seed adapters for workspace-specific logic |
| `checkpoints.py` | Run checkpoint management |

### Retrieval (`umbrella/retrieval/`)

Search over GMAS code, docs, and symbols.

| Module | Purpose |
|--------|---------|
| `service.py` | `RetrievalService`: orchestrates all search methods |
| `gmas_context.py` | `build_gmas_context()`: context for Ouroboros |
| `lexical.py` | BM25 index |
| `symbols.py` | Symbol index (classes, functions, modules) |
| `docs_index.py` | Documentation index from `mkdocs.yml` |
| `code_index.py` | Code-aware symbol index |
| `workspace_usage.py` | GMAS usage patterns in workspaces |
| `cards.py` | Retrieval card generation |
| `gmas_chunk_cache.py` | Cached GMAS code chunks |
| `gmas_summarizer.py` | GMAS documentation summarization |
| `sources.py` | Source file discovery |

### Artifacts and Observability (`umbrella/artifacts/`)

Run indexing and log access.

| Module | Purpose |
|--------|---------|
| `run_index.py` | `index_workspace_runs()`: index all runs |
| `log_summary.py` | Log summary generation |
| `models.py` | `RunManifest`, `WorkspaceRunIndex`, `RunStatus` |

### Verification (`umbrella/verification/`)

Workspace verification runner that executes test commands after Ouroboros completes.

| Module | Purpose |
|--------|---------|
| `final_sweep.py` | Final sweep verification |
| `test_quality.py` | Test quality checks |
| `workspace_path_policy.py` | Path policy for verification |

### Policies (`umbrella/policies/`)

Boundary rules for the repository.

| File | Purpose |
|------|---------|
| `engine.py` | `PolicyEngine` with decision API: `classify_path`, `can_edit_path`, `should_prefer_workspace_patch` |
| `README.md` | Policy documentation |

### Integration (`umbrella/integration/`)

Bridge between Umbrella and Ouroboros.

| Module | Purpose |
|--------|---------|
| `services.py` | `UmbrellaServices`: central service locator |
| `ouroboros_bridge.py` | Sync Umbrella context to Ouroboros drive |
| `ouroboros_launcher.py` | Launch and manage Ouroboros process |

### Control Plane (`umbrella/control_plane/`)

Manager-level decision modules (post-refactor, the `ControlPlaneEngine` monolith was removed).

| Module | Purpose |
|--------|---------|
| `critic.py` | Critic review tool for review phases |
| `decision_policy.py` | Decision policy skill for plan phase |
| `remediation_planner.py` | Remediation planning for failed verification |
| `sandbox_self_edit.py` | Temporary code edits with git rollback |
| `task_bridge.py` | Task bridging utilities |
| `terminal_check.py` | Terminal health checks |
| `workspace_code_update.py` | Workspace code update operations |
| `workspace_patching.py` | Workspace patching utilities |
| `escalation.py` | Human escalation management |
| `human_checkpoints.py` | Human checkpoint flow |
| `prompt_diff.py` | Prompt diff utilities |
| `prompt_versioning.py` | Prompt version management |
| `prompt_policy.py` | Prompt policy rules |
| `code_analyzer.py` | Code analysis utilities |
| `code_improver.py` | Code improvement suggestions |
| `tracing.py` | Execution tracing |

### Web Bridge (`umbrella/web_bridge/`)

HTTP server: serves React static build + JSON API at `/api/*`.

| Module | Purpose |
|--------|---------|
| `server.py` | Server entrypoint (`uv run bridge`) |
| `handler.py` | Route handler registration |
| `app.py` | Main application (Flask/FastAPI routes) |
| `chat_launcher.py` | Chat session launcher |
| `util.py` | Utility functions |
| `cleanup.py` | Cleanup routines |
| `api/report_api.py` | Report-specific API routes |

Start: `uv run bridge` (port 8765). Before starting, build the frontend: `cd web && yarn install && yarn build`.

### Skills (`umbrella/skills/`)

Skill packs with phase-tagged frontmatter in `SKILL.md`.

| Directory | Purpose |
|-----------|---------|
| `library/` | Skill pack directories, each with `SKILL.md` |
| `registry.py` | `SkillPack` discovery and phase filtering |

Skills are filtered per-phase via `phases:` field in `SKILL.md` frontmatter.

### MCP (`umbrella/mcp/`)

MCP (Model Context Protocol) integration.

| Module | Purpose |
|--------|---------|
| `tools_bridge.py` | Bridge between MCP tools and Ouroboros tool registry |

MCP tool entries are filtered per-phase by the manifest's `allowed_tools`.

### Other Modules

| Module | Purpose |
|--------|---------|
| `umbrella/telemetry/` | Telemetry and metrics storage |
| `umbrella/evals/` | Evaluation utilities |
| `umbrella/utils/` | Shared utilities |
| `umbrella/config.py` | Configuration loading |
| `umbrella/env.py` | Environment variable loading (`.env`) |
| `umbrella/llm_budget.py` | LLM budget tracking |
| `umbrella/run_observer.py` | Run observation utilities |
| `umbrella/umbrella_api.py` | Umbrella-level API utilities |

## Configuration

Runtime configuration is spread across:

- `umbrella/policies/default_policy.yaml` — boundary rules, runtime defaults
- `.env` at repository root — API keys, model names, endpoint URLs
- Phase manifests (`umbrella/phases/manifests/*.yaml`) — per-phase settings
- `umbrella/permissions/global.yaml` — global permission denials

See [technical-report/11-configuration.md](technical-report/11-configuration.md) for the full list of environment variables.
