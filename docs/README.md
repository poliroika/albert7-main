# Umbrella Documentation

This directory contains the full documentation for the Umbrella project. It is also published as a static site via **MkDocs Material** (see `mkdocs.yml` and `.gitlab-ci.yml`).

The repository clone directory name is not fixed (it may still be `albert7`); all paths below are relative to the repository root where `pyproject.toml` and `umbrella/` live.

## Sections

| Section | File | Description |
|---------|------|-------------|
| Architecture | [architecture.md](architecture.md) | Three-layer model, phase-driven control plane, dual Worker/Watcher, MemPalace, PermissionEnvelope |
| Workspaces | [workspaces.md](workspaces.md) | Seed and task-instance model, workspace.toml, TASK_MAIN.md, registry |
| Creating Workspaces | [creating-workspaces.md](creating-workspaces.md) | Manual seed creation, programmatic instances, CLI entrypoints |
| GMAS | [gmas.md](gmas.md) | Framework role, read-only policy, retrieval over GMAS |
| Umbrella Layer | [umbrella-layer.md](umbrella-layer.md) | Post-refactor subsystems: phases, orchestrator, MemPalace, permissions, retrieval |
| Durable memory backends | [memory-durable-backends.md](memory-durable-backends.md) | Canonical vs dual vs Hindsight env, product defaults, live smoke tests |
| Ouroboros | [ouroboros.md](ouroboros.md) | Deep agent per phase, manifest consumption, tool registry, supervisor |
| Technical Report | [technical-report/README.md](technical-report/README.md) | Multi-page deep dive: 15 chapters covering every aspect of the system |
| JSON examples | [examples/README.md](examples/README.md) | Static drive-state samples (e.g. `capability_declaration.json`) for operators and manifest authors |

## Key Entrypoints

| Module | Purpose |
|--------|---------|
| `umbrella/app_ouroboros.py` | CLI single-run entrypoint |
| `umbrella/web_bridge/server.py` | Operator UI: React from `web/build` + JSON API at `/api/*` |
| `umbrella/orchestrator/runner.py` | PhaseRunner: walks PhasePlan, spawns Worker + Watcher per phase |
| `umbrella/phases/registry.py` | Discovers and validates YAML phase manifests |
| `umbrella/memory/palace/facade.py` | MemPalace unified memory facade |
| `umbrella/permissions/envelope.py` | PermissionEnvelope per-phase access control |

## Starting the Web Bridge

One process serves the **React static build** and the **JSON API** (`/api/*`) on a single port (default `8765`). Build order matters: frontend first, then Python, then bridge.

```powershell
# 1. Build frontend
cd web
yarn install
yarn build
cd ..

# 2. Sync Python deps
uv sync --extra dev

# 3. Start bridge
uv run bridge
```

Open `http://127.0.0.1:8765` in your browser. Without `yarn build`, the page will not render correctly.

## Quick Start

```powershell
uv sync --extra dev
uv run pytest -q umbrella/tests
```

For the operator UI, see [Starting the Web Bridge](#starting-the-web-bridge) above.

## Philosophy

Umbrella differs from generic coding assistants in several key ways:

- **Workspace-first**: application competence crystallizes in `workspaces/`, not in the manager.
- **Phase-driven**: every run follows a structured PhasePlan with per-phase manifests, tool filters, and exit criteria.
- **Evidence-based**: FinalReport requires every claim to cite an event/artifact ID.
- **Dual-agent safety**: Watcher monitors Worker and can abort, restart, or mutate the plan.
- **Verified memory**: Reflexions only promote to durable lessons after verified evidence of success.
- **Framework discipline**: GMAS is a stable read-only substrate, not a surface for ad-hoc patching.
