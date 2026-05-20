# Workspaces

A workspace is the primary unit of production in Umbrella: an application system built to solve a specific class of tasks. Each workspace contains agent graphs, prompts, models, evals, and artifacts, all running on GMAS.

## Seed and Task-Instance

The system separates workspaces into two levels to keep stable templates clean from experimental patches of specific tasks.

### Seed Workspace

A stable template created by a human. Lives in `workspaces/<workspace_id>/`.

Characteristics:

- Version-controlled in git.
- Not auto-patched — changes go through promotion (minimum eval score 0.7).
- Defines the base graph, prompts, roles, tools, and policies for the task class.
- Contains `TASK_MAIN.md` with the seed's general mission.

Registered seeds are listed in `workspaces/registry.toml`:

```toml
version = "0.1.0"
seeds = ["multi_agent_debate_graph"]
instances = []
```

### Task-Instance

A mutable copy of a seed for a specific task. Created automatically via `create_task_instance()` in `umbrella/workspace_runtime/instances.py`.

Instance path: `workspaces/<seed_id>/instances/<instance_id>_<timestamp>/`.

On creation:

1. Copy seed files (excluding `runs/`, `snapshots/`, `reports/`, `memory/`, `logs/`, `instances/`, `__pycache__/`, `.git/`).
2. Create runtime directories: `runs/`, `snapshots/`, `reports/`, `memory/`, `logs/`.
3. Overwrite workspace identity (new `workspace_id`, linked to `task_id`).
4. Initialize `TASK_MAIN.md` from the task brief.
5. Record lineage metadata.

The task-instance is the primary surface for iterative improvement. The Worker-Ouroboros freely modifies graphs, prompts, evals, and experiments within the instance, evaluates the result, and repeats the cycle.

Upon task completion, useful patches may be **promoted** back to the seed through review.

## Required Workspace Files

### workspace.toml

The workspace contract: structure description, file references, mutable paths list.

Example:

```toml
workspace_id = "my_workspace"
name = "My Workspace"
description = "Description of what this workspace does"
task_main_file = "TASK_MAIN.md"

mutable_paths = [
    "graph", "agents", "prompts", "tools", "models",
    "evals", "experiments", "runs", "snapshots", "reports",
]

[metadata]
owner = "manual"
engine = "gmas"
engine_mutable = false
```

Key fields:

| Field | Purpose |
|-------|---------|
| `workspace_id` | Unique identifier |
| `task_main_file` | Path to `TASK_MAIN.md` |
| `mutable_paths` | Directories allowed for automatic changes |
| `metadata.engine` | Framework (`gmas`) |
| `metadata.engine_mutable` | Can the engine be edited? (always `false`) |

### TASK_MAIN.md

The primary task contract. Stores the goal, deliverables, success criteria, and constraints.

For seeds: the general mission of the template. For instances: the specific task.

Example:

```markdown
## Objective
What the workspace should do.

## Success Criteria
- Criterion 1
- Criterion 2

## Constraints
- Constraint 1
```

The PhaseRunner, Worker, and Watcher all reference `TASK_MAIN.md` as the source of truth for what the workspace is trying to accomplish.

### seed_profile.toml (optional)

Metadata for automatic workspace selection: capabilities, task classes, selection hints.

```toml
name = "My Workspace"
maturity = "experimental"
primary_task_classes = ["my_task_class"]

[[capabilities]]
name = "my_capability"
description = "What this workspace can do"
weight = 1.0

[selection_hints]
keywords = ["keyword1", "keyword2"]
```

## Workspace Discovery

`umbrella/workspace_registry/discovery.py` scans the filesystem for `workspace.toml`:

1. Recursive traversal of `workspaces/**/workspace.toml`.
2. Ignores runtime directories: `runs`, `snapshots`, `reports`, `memory`, `logs`, `__pycache__`, `.git`, `archived`.
3. Loads configuration into `WorkspaceRef`.
4. For seeds: loads additional `seed_profile.toml` into `SeedWorkspaceProfile`.

Everything is orchestrated through `WorkspaceRegistry` (`umbrella/workspace_registry/registry.py`):

- `discover()` — find all workspaces
- `register_workspace()` — register a workspace
- `get_workspace()` — get by ID
- `get_seed_profile()` — get seed metadata
- `match()` — find workspaces matching a task
- `select_best()` — pick the best workspace for a task

## Lineage

Each instance stores its origin record:

- Which seed it was created from.
- Which task (`task_id`).
- What iterations and patches were applied.
- Which patches were useful and promoted to seed.

The lineage model is defined in `umbrella/workspace_registry/models.py` (`WorkspaceLineageRecord`).

## Workspace and Phase Execution

In the phase-driven model, a workspace run proceeds as follows:

1. The PhaseRunner loads the workspace's `TASK_MAIN.md` and creates a `PhasePlan`.
2. The **preflight** phase checks environment readiness.
3. The **research** phase studies the task and drafts architecture.
4. The **plan** phase builds subtask cards.
5. The **execute** phase runs subtasks, writing code within the workspace instance.
6. The **verify** phase runs workspace verification (tests, e2e).
7. The **FinalReport** captures all changes and evidence.

During execution, the Worker writes only within the workspace instance directory (enforced by `PermissionEnvelope` with path policies). The root repository, `umbrella/`, `ouroboros/`, and `gmas/` are all denied.

## Directory Structure

```
workspaces/<workspace_id>/
    workspace.toml          # workspace contract
    TASK_MAIN.md            # task contract
    seed_profile.toml       # seed profile (optional)
    graph/
        topology.toml       # GMAS agent graph topology
    agents/                 # agent configs (.toml)
    prompts/                # agent prompts (.md)
    tools/
        allowlist.toml      # allowed tools
    models/
        models.toml         # LLM model configuration
    policies.toml           # workspace-local policies
    evals/                  # eval harness
    experiments/            # experiments and launch scripts
    runs/                   # run results (runtime)
    snapshots/              # state snapshots (runtime)
    reports/                # generated reports (runtime)
    instances/              # task-instances (runtime)
```

`runs/`, `snapshots/`, `reports/`, `memory/`, `logs/`, and `instances/` are populated at runtime and are not part of the seed template.
