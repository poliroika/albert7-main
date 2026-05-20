# Part 3: Repository Topology

## Directory Structure

```
albert7/                              # repository root (name is not fixed)
  pyproject.toml                      # project: umbrella, hatchling build
  mkdocs.yml                          # docs site config
  .gitlab-ci.yml                      # CI: pages job for MkDocs
  .env                                # LLM keys, model, endpoint (gitignored)
  README.md                           # project overview

  umbrella/                           # Control plane
    __init__.py
    app_ouroboros.py                  # CLI single-run entrypoint
    config.py, env.py, llm_budget.py  # Configuration
    phases/                           # Phase machine
      base.py, loader.py, registry.py
      schema/manifest.schema.json
      manifests/                      # 11 YAML manifests
        preflight.yaml, research.yaml, plan.yaml, execute.yaml, ...
    orchestrator/                     # Run orchestration
      runner.py, worker.py, watcher.py, watcher_triggers.py
      phase_plan.py, final_report.py, verify_loop.py
      promotion.py, self_improvement_runner.py
    permissions/                      # Access control
      envelope.py, loader.py, global.yaml, watcher_envelope.py, self_improvement.yaml
    memory/                           # Memory layer
      palace/                         # Unified facade
        facade.py, stores.py, tiers.py, graph.py, recall.py, transient.py, migrators.py
      store.py, lessons.py, competency.py, context_builder.py, ...
    prompts/phases/                   # Phase prompt files
      research.system.md, plan.system.md, execute.system.md, ...
      watcher.system.md
    skills/library/                   # Skill packs with SKILL.md
    web_bridge/                       # HTTP server + API
      server.py, handler.py, app.py, chat_launcher.py, util.py, cleanup.py
      api/report_api.py
    retrieval/                        # GMAS search
    verification/                     # Workspace verification
    workspace_registry/               # Workspace discovery
    workspace_runtime/                # Instance management
    integration/                      # UmbrellaServices, launcher, bridge
    control_plane/                    # Critic, remediation, sandbox self-edit
    policies/                         # Boundary rules
    artifacts/                        # Run indexing
    telemetry/                        # Metrics
    mcp/                              # MCP tool bridge
    tests/                            # 55+ test files

  ouroboros/                          # Deep LLM agent
    pyproject.toml
    ouroboros/
      agent.py                        # Thin orchestrator
      loop.py                         # Main LLM tool loop (~5800 lines)
      context.py                      # Context builder
      llm.py                          # LLM client
      memory.py, memory_hooks.py
      task_planner.py, discipline.py, deadline.py
      utils.py, review.py
      apply_patch.py, workspace_patch.py
      preflight_recovery.py, tool_args_repair.py, owner_inject.py
      tools/                          # 22 tool modules
        registry.py, core.py, shell.py, git.py, github.py
        phase_control.py, palace_tools.py, umbrella_tools.py
        skills_tools.py, mcp_servers.py, mcp_discovery.py
        tool_discovery.py, evolution_stats.py, completion_gates.py
        deep_search.py, knowledge.py, search.py
        browser.py, vision.py, health.py
        terminal_session.py, background_jobs.py, compact_context.py
        control.py, review.py
    supervisor/                       # Process management
      telegram.py, events.py, queue.py
      workers.py, state.py, git_ops.py

  gmas/                               # Multi-agent framework (read-only)
    pyproject.toml
    src/gmas/core/, execution/, tools/, callbacks/, builder/
    docs/

  workspaces/                         # Application workspaces
    registry.toml
    multi_agent_debate_graph/         # Seed workspace
    world_prediction/                 # Workspace with README
    example_workspace/                # Workspace with TASK_MAIN

  web/                                # React operator UI
    package.json, craco.config.js, tailwind.config.js
    src/
      App.js, index.js
      pages/                          # 9 pages: Chat, Runs, Logs, etc.
      components/chat/               # Composer, MessageCard, TimelinePanel, etc.
      components/layout/             # AppShell, Sidebar, Topbar
      components/ui/                 # ~30 shadcn/ui primitives
      lib/api.js                     # Axios API client
      context/                       # ThemeContext, WorkspaceContext
```

## Python Dependencies

From `pyproject.toml`:

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | >=0.136.1 | Web bridge API routes |
| `flask` | >=3.1.3 | Web bridge server |
| `mcp` | >=1.26.0 | Model Context Protocol |
| `mempalace` | >=3.2.0 | MemPalace backend |
| `openai` | >=2.30.0 | LLM API client |
| `pydantic` | >=2.12.5 | Data models |
| `pyyaml` | >=6.0 | YAML manifest loading |
| `uvicorn` | >=0.46.0 | ASGI server |
| `pytest` | >=9.0.3 | Testing |

GMAS is an editable path dependency: `frontier-ai-gmas = { path = "gmas" }`.

## Entry Points

| Entry | Module | Invocation |
|-------|--------|------------|
| CLI run | `umbrella/app_ouroboros.py` | `uv run python umbrella/app_ouroboros.py <workspace>` |
| Web bridge | `umbrella/web_bridge/server.py` | `uv run bridge` (= `uv run python -m umbrella.web_bridge`) |
| Tests | pytest | `uv run pytest -q` |

---

Next: [Part 4 — Runtime Artifacts](04-runtime-artifacts.md)
