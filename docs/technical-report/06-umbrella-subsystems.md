# Part 6: Umbrella Subsystems

Detailed description of each Umbrella subsystem after the refactoring.

## Phases (`umbrella/phases/`)

### Phase Manifest Schema

Each manifest is validated against `umbrella/phases/schema/manifest.schema.json`. Required fields:

- `id` (kebab-case, unique)
- `version` (int, increment on breaking changes)
- `prompt_files.system` (non-empty array of repo-relative paths)
- `allowed_tools` and `forbidden_tools` (mutually exclusive sets)
- `allowed_skills` (list of skill slugs)
- `memory` block: `always_on`, `hot`, `warm_search`, `graph`, `write_rules`
- `permissions.rules` (evaluated top-to-bottom, default deny)
- `exit_criteria`: `required_calls`, `required_palace_writes`, `min_palace_writes`
- `budgets`: `max_tokens`, `max_seconds`, `max_tool_calls` (all optional)

### Loading Pipeline

1. `registry.py::discover_manifests()` scans `manifests/*.yaml`.
2. Each YAML is loaded by `loader.py` into a `PhaseManifest` dataclass.
3. Validated against `manifest.schema.json`.
4. Invalid manifests raise `PhaseManifestError(path, errors[])`.
5. CI test `test_phase_manifests_valid.py` validates all manifests.

### Data Classes

```python
@dataclass(frozen=True)
class PhaseManifest:
    id: str
    version: int
    prompt_files: PromptFiles
    allowed_tools: frozenset[str]
    forbidden_tools: frozenset[str]
    allowed_skills: frozenset[str]
    memory: MemoryPolicy
    permissions: PermissionPolicy
    exit_criteria: ExitCriteria
    mini_review_after: str | None
    budgets: Budgets

@dataclass
class PhaseNode:
    id: str
    manifest_id: str
    status: Literal["pending", "running", "done", "skipped", "failed"]
    subtasks: list[SubtaskCard] | None
    overlay: dict[str, Any] | None
    started_at: float | None
    ended_at: float | None

@dataclass
class SubtaskCard:
    id: str
    title: str
    goal: str
    allowed_tools: frozenset[str]
    allowed_skills: frozenset[str]
    success_test: SuccessTest
    status: Literal["pending", "running", "done", "failed"]
```

## Orchestrator (`umbrella/orchestrator/`)

### PhaseRunner (`runner.py`)

The main loop:
1. Load `PhasePlan` from `drive/state/phase_plan.json` (or create default).
2. For each pending `PhaseNode`:
   - Build `RecallBundle` via `palace.recall(phase_id)`.
   - Spawn Worker via `worker.py::spawn_worker_for_phase()`.
   - Start Watcher monitoring via `watcher.py`.
   - Wait for phase completion or Watcher signal.
   - Process signals, mutate plan if needed.
   - On phase done: `palace.expire_scope(phase_scoped)`.
3. After all phases: build `FinalReport` via `final_report.py`.
4. Return report to CLI/Web bridge.

### Worker Spawn (`worker.py`)

```python
def spawn_worker_for_phase(
    *, phase_node, manifest, workspace_id, run_id, palace, launcher
) -> WorkerHandle:
    task = {
        "id": f"{run_id}:{phase_node.id}",
        "type": "phase_run",
        "input": render_phase_user_prompt(manifest, palace, phase_node),
        "context_overlays": {
            "phase_manifest": manifest.to_payload(),
            "phase_node": asdict(phase_node),
            "recall_bundle": palace.recall(phase_node.manifest_id).to_payload(),
            "permissions": manifest.permissions.compile().to_payload(),
        },
        "tool_filter": {
            "allow": list(manifest.allowed_tools),
            "deny": list(manifest.forbidden_tools),
        },
        "budgets": asdict(manifest.budgets),
        "role": "worker",
    }
    return launcher.submit_task(task)
```

### Watcher (`watcher.py`)

Poll loop with trigger evaluation:
1. Poll `palace.transient` for new events.
2. Evaluate triggers from `watcher_triggers.py`.
3. On trigger: single LLM call with Watcher prompt + trigger context.
4. Write `WatcherSignal` to `drive/state/watcher_signal.json`.

### FinalReport (`final_report.py`)

Evidence collection (deterministic, no LLM):
- `changed_files` from git diff.
- `commands_run` from `palace.transient`.
- `verification_reports` from verify phase.
- `watcher_incidents` from `palace.run`.
- `memory_promotions` from promotion events.

LLM call only for `human_summary_md` with mandatory `[ev:event_id]` / `[art:artifact_id]` citations.

Validator checks: every claim has corresponding evidence. On failure: retry once, then `status=partial`.

## MemPalace (`umbrella/memory/palace/`)

### Store Layout

9 Chroma collections + 2 SQLite databases:

| Store | Backend | Embedding | Use |
|-------|---------|-----------|-----|
| charter | Chroma | Yes | Project goal, architecture, envelope |
| lesson | Chroma | Yes | Verified durable lessons |
| idea | Chroma | Yes | Hypotheses, findings (verified=false suppressed) |
| codeptr | Chroma | Yes | External code references |
| skill_index | Chroma | Yes | Skills library mirror |
| run | Chroma | Yes | Current run state |
| phase | Chroma | Yes | Phase scratchpad |
| subtask | Chroma | Yes | Subtask scratchpad |
| transient | SQLite (FTS5) | No | Events, tool I/O, terminal (TTL) |
| graph | SQLite | No | Edge table linking nodes |

### Recall Policy per Phase

Each manifest defines `memory.always_on`, `memory.hot`, `memory.warm_search`, `memory.graph`:
- `always_on`: always loaded (charter, active architecture).
- `hot`: loaded from `palace.run` by tag.
- `warm_search`: vector search with tier/filter constraints.
- `graph`: 1-hop walk from hot/warm nodes by edge types.

### Graph Edges

SQLite table `edges(src_id, dst_id, edge_type, weight, phase, created_at)`.

Edge types: `derived_from`, `cites`, `tests`, `implements`, `supersedes`, `references_file`, `triggered_by_error`, `flagged_by`, `blocks`, `from_phase`.

## Permissions (`umbrella/permissions/`)

### Rule Evaluation

Phase rules (top-to-bottom, first match wins) -> Global denials (can override allows) -> Default deny.

Rule types: `allow_tool`, `deny_tool`, `allow_tools`, `deny_tools`, `deny_path`.

Rule args: regex (`cmd_re`) or glob (`working_directory`, paths).

Variable substitution: `${workspace_id}`, `${repo_root}`.

### Watcher Envelope

Hardcoded in Python (`watcher_envelope.py`): read-only tools + palace.run incident writes only. Cannot be overridden by YAML.

## Retrieval (`umbrella/retrieval/`)

Stack: BM25 (lexical) + symbol index (classes, functions) + docs index (mkdocs.yml navigation) + workspace usage patterns + retrieval cards.

`RetrievalService` orchestrates all methods. `build_gmas_context()` produces ready-to-inject LLM context.

## Integration (`umbrella/integration/`)

- `services.py`: `UmbrellaServices` initializes telemetry -> memory -> retrieval -> registry in order.
- `ouroboros_bridge.py`: Syncs workspace context, lessons, state to Ouroboros drive.
- `ouroboros_launcher.py`: Manages Ouroboros process lifecycle.

---

Next: [Part 7 — Workspaces and Policy](07-workspaces-and-policy.md)
