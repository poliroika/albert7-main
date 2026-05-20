# Creating Workspaces

Workspaces can be created in three ways: manually as a new seed, programmatically as a task-instance, or via the CLI runner.

## Method 1: New Seed Workspace (Manual)

Creating a new seed is a five-step process.

### Step 1. Copy the structure

Use an existing seed as a template:

```powershell
Copy-Item -Recurse workspaces\multi_agent_debate_graph workspaces\my_workspace
```

Remove runtime-generated directories from the copy: `runs/`, `snapshots/`, `reports/`, `memory/`, `logs/`, `instances/`, and any `instance_metadata.json`.

### Step 2. Fill in workspace.toml

Open `workspaces/my_workspace/workspace.toml` and update the metadata:

```toml
workspace_id = "my_workspace"
name = "My Workspace"
description = "Description of the workspace purpose"
task_main_file = "TASK_MAIN.md"

mutable_paths = [
    "graph", "agents", "prompts", "tools", "models",
    "evals", "experiments", "runs", "snapshots", "reports",
]

[metadata]
owner = "manual"
engine = "gmas"
engine_mutable = false
notes = "Standalone workspace."
```

Required fields: `workspace_id`, `name`, `description`, `task_main_file`.

### Step 3. Write TASK_MAIN.md

```markdown
# My Workspace

## Objective
What the workspace should do.

## Final Deliverable
What artifact the workspace produces.

## Success Criteria
- Criterion 1
- Criterion 2

## Constraints
- Constraint 1
```

### Step 4. Register the seed

Add the workspace to `workspaces/registry.toml`:

```toml
seeds = ["multi_agent_debate_graph", "my_workspace"]
```

Optionally create `seed_profile.toml` for automatic selection:

```toml
name = "My Workspace"
maturity = "experimental"
primary_task_classes = ["my_task_class"]

[[capabilities]]
name = "my_capability"
description = "What the workspace does"
weight = 1.0

[selection_hints]
keywords = ["keyword1", "keyword2"]
```

### Step 5. Configure the agent graph

Define the topology in `graph/topology.toml`:

```toml
name = "my_graph"
start_node = "first_agent"
end_node = "last_agent"
agents = ["first_agent", "processor", "last_agent"]

[[edges]]
source = "first_agent"
target = "processor"
weight = 1.0

[[edges]]
source = "processor"
target = "last_agent"
weight = 1.0
```

For each agent, create a `.toml` config in `agents/` and a corresponding prompt in `prompts/`.

### Verification

```powershell
uv run python -c "
from umbrella.workspace_registry.registry import WorkspaceRegistry
from pathlib import Path
reg = WorkspaceRegistry(Path('.'))
found = reg.discover()
print([w.workspace_id for w in found])
"
```

## Method 2: Task-Instance via Code (Programmatic)

Task-instances are created automatically when the PhaseRunner picks a workspace for a task.

### Via create_task_instance directly

```python
from pathlib import Path
from umbrella.workspace_registry.discovery import load_seed_profile
from umbrella.workspace_registry.models import TaskBrief
from umbrella.workspace_runtime.instances import create_task_instance

seed = load_seed_profile(Path("workspaces/multi_agent_debate_graph"))
brief = TaskBrief(
    description="Solve a specific problem using multi-agent debate",
    task_id="task_debate_001",
    task_class="debate",
)
instance = create_task_instance(seed, brief, copy_seed_files=True)
print(f"Instance path: {instance.path}")
```

Result: a new directory `workspaces/<seed_id>/instances/<id>_<timestamp>/` with a copy of the seed, its own `TASK_MAIN.md`, and empty `runs/`, `snapshots/`, `reports/`, `memory/`, `logs/`.

### Via UmbrellaServices

```python
from pathlib import Path
from umbrella.integration.services import UmbrellaServices

services = UmbrellaServices(
    repo_root=Path("."),
    use_live_llm=True,
    llm_model="anthropic/claude-sonnet-4-20250514",
    llm_api_key="sk-...",
)
```

The PhaseRunner handles workspace selection, instance creation, and the full phase lifecycle.

## Method 3: CLI Runner

### umbrella/app_ouroboros.py

The current single-run entrypoint:

```powershell
uv run python umbrella/app_ouroboros.py workspaces/<workspace_id> --live --verbose --max-verify-retries 3
```

The script reads the workspace's `TASK_MAIN.md`, builds a mission prompt, and runs the PhaseRunner through the full phase lifecycle.

Key flags: `--task`, `--task-file`, `--timeout-hours`, `--max-budget`, `--max-rounds`, `--mock`, `--no-verify`, `--verification-timeout-seconds`, `--allow-seed-writes`.

### Web Bridge

Start the operator UI and run workspaces through the web interface:

```powershell
cd web && yarn install && yarn build && cd ..
uv run bridge
```

Open `http://127.0.0.1:8765`, navigate to the Chat page, select a workspace, and start a run. The UI shows the PhasePlan, timeline, agent requests, and final report.

## Environment Check

Before creating workspaces, verify that tests pass:

```powershell
uv run pytest -q umbrella/tests
uv run pytest -q umbrella/tests/test_workspace_registry.py
uv run pytest -q umbrella/tests/test_workspace_runtime.py
```
