# GMAS

GMAS is the multi-agent framework on which all workspaces in Umbrella are built. It provides the agent graph engine, execution runtime, tools, memory, and callback system.

## Role in Umbrella

GMAS is a vendor-like, read-only layer. It serves as the standard assembly language for multi-agent systems: any workspace describes its logic using GMAS primitives.

```
+------------------------------------------+
|        Umbrella (control plane)          |
|  phases, orchestrator, memory,           |
|  permissions, retrieval                  |
+------------------------------------------+
|        Workspaces (application)          |
|  agent graphs, prompts, evals            |
+------------------------------------------+
|         GMAS (framework)                 |
|  runners, scheduler, tools, memory       |
+------------------------------------------+
```

## Read-Only Policy

The `gmas/` directory is protected by policy (`umbrella/policies/default_policy.yaml`):

```yaml
framework_boundary:
  gmas_readonly: true
  requires_human_approval: true
  framework_paths:
    - "gmas/"
```

Calling `can_edit_path(Path("gmas/foo.py"))` returns `allowed=False` with escalation.

If GMAS capabilities are insufficient, the solution is found through:

1. Configuration and composition outside `gmas/`.
2. Local helper scripts in the workspace.
3. Domain-specific tool adapters.
4. Thin wrappers over the GMAS API.

Direct editing of `gmas/` requires explicit human approval.

## What GMAS Provides

| Component | Description |
|-----------|-------------|
| Agent graphs | `gmas.core.graph` — topology definitions and routes |
| Execution runtime | `gmas.execution.runner` — graph execution with state |
| Scheduler | `gmas.execution.scheduler` — agent traversal order |
| Tools | `gmas.tools` — web search, file search, MCP client, shell, computer use |
| Memory | `gmas.utils.memory` — shared memory between agents |
| Budget | `gmas.execution.budget` — token and cost control |
| Callback system | `gmas.callbacks` — events, metrics, handlers |
| Streaming | `gmas.execution.streaming` — streaming result delivery |
| Auto-builder | `gmas.builder.auto_builder` — automatic graph assembly |

## Usage Contract in Workspaces

Every workspace should describe its multi-agent logic using GMAS primitives when possible.

Allowed on top of GMAS:

- Local helper launch scripts.
- Domain-specific tool adapters.
- External eval harnesses.
- Artifact converters.
- Workspace-specific policies.

Prohibited by default:

- Cloning GMAS logic inside a workspace.
- Replacing the GMAS runner with a custom system without strong justification.
- Editing `gmas/` for a single task.

## GMAS Retrieval

Umbrella indexes GMAS code and documentation so that the Worker can make informed improvements to workspaces. Retrieval is implemented in `umbrella/retrieval/`.

### Index Sources

```yaml
gmas_retrieval:
  mode: bm25_first
  code_aware: true
  preferred_sources:
    - gmas/README.md
    - gmas/QUICKSTART.md
    - gmas/DOCUMENTATION.md
    - gmas/docs/
    - gmas/src/
    - gmas/examples/
```

### Search Stack

`RetrievalService` (`umbrella/retrieval/service.py`) combines multiple methods:

1. **Lexical (BM25)** — exact matches on symbols, configs, and APIs.
2. **Symbol index** — classes, functions, modules, and tools in GMAS.
3. **Docs index** — navigation of `gmas/docs/` via `mkdocs.yml`.
4. **Workspace usage index** — patterns of GMAS usage in existing workspaces.
5. **Retrieval cards** — compact structured briefs with recommended patterns, key symbols, and files.

### Context for Ouroboros

`umbrella/retrieval/gmas_context.py` provides `build_gmas_context()`, which returns retrieval hits with code and documentation ready for direct inclusion in the LLM context.

## GMAS Documentation

Internal framework documentation:

- `gmas/README.md` — overview
- `gmas/DOCUMENTATION.md` — full documentation
- `gmas/docs/` — structured guides (getting started, user guide, API reference, examples, contributing)
