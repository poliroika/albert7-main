# Part 8: Ouroboros Runtime

Ouroboros is the deep LLM agent that executes individual phases. This chapter covers its internal architecture: the tool loop, context building, manifest consumption, and tool registry.

## Main Loop (`loop.py`)

The main loop (~5800 lines) manages the LLM conversation with tool calls:

1. **Round start**: Build messages via `context.py::build_llm_messages()`.
2. **LLM call**: Send messages, receive response (possibly with tool calls).
3. **Tool execution**: For each tool call:
   - Check `allowed_tool_names` from `tool_filter` (derived from phase manifest).
   - If forbidden: return `TOOL_DENIED_BY_ENVELOPE` with strike counting.
   - If allowed: execute via `ToolRegistry.execute()` (which runs PermissionEnvelope pre-hook).
   - Append tool result to messages.
4. **Watcher signal check**: At round boundary, read `drive/state/watcher_signal.json`.
5. **Phase completion**: Check exit criteria (required tool calls, palace writes).
6. **Repeat** until budget exhausted or completion tool called.

### Phase Label Tracking

The loop tracks `phase_label` (e.g., `"subtask_*"`, `"remediation_*"`) used in:
- Error messages and logging.
- Forbidden tool error formatting.
- Phase-specific behavior branching.

### Forbidden Tool Handling

```python
allowed_tool_names = _allowed_tool_names_from_schemas(tool_schemas)
# Returns frozenset or None (all tools)

for tool_call in response.tool_calls:
    if allowed_tool_names is not None and fn_name not in allowed_tool_names:
        forbidden_strike_counts[fn_name] += 1
        return _format_forbidden_tool_error(fn_name, phase_label)
    # else: execute normally
```

After repeated strikes, the loop may auto-delegate or flag the phase.

## Context Builder (`context.py`)

`build_llm_messages()` assembles the LLM prompt from multiple sources:

1. **System prompt**: from phase manifest `prompt_files.system`.
2. **Charter blocks**: from `prompt_files.charter_blocks` (palace.charter nodes).
3. **Recall bundle**: from `context_overlays.recall_bundle` (always_on, hot, warm, graph neighbours).
4. **Permission display**: from `context_overlays.permissions` (agent sees its own envelope).
5. **User overlay**: from phase manifest `prompt_files.user_overlay` (task-specific content).
6. **Previous messages**: conversation history with compaction.

New overlay sections (added during refactoring):
- `phase_manifest` overlay: manifest data for self-aware agent.
- `recall_bundle` overlay: pre-loaded memory context.
- `permissions` overlay: compiled permission rules.

## Agent (`agent.py`)

Thin orchestrator that delegates to:
- `loop.py` for the main execution cycle.
- `llm.py` for LLM API calls.
- `context.py` for message assembly.
- `tools/registry.py` for tool discovery and execution.
- `memory.py` for scratchpad.
- `review.py` for code collection.

Bifurcation: if `task["context_overlays"]["phase_manifest"]` exists -> phase-driven mode; otherwise -> legacy fallback.

## Tool Registry (`tools/registry.py`)

Plugin architecture that auto-discovers tool modules via `pkgutil.iter_modules`:

1. Scan `ouroboros/ouroboros/tools/` for modules.
2. Each module registers tools via `@registry.register` decorator.
3. `ToolRegistry.execute(name, args)` dispatches to the handler.

**PermissionEnvelope pre-hook**: inserted after module loading, before execution. Calls `envelope.check(phase_id, tool_name, paths, commands)` and returns `TOOL_DENIED_BY_ENVELOPE` on deny.

## Phase Control Tools (`tools/phase_control.py`)

In-run self-modification tools that communicate with the PhaseRunner via `drive/state/` JSON files:

| Tool | Action |
|------|--------|
| `mutate_phase_plan(patch)` | Patch the active PhasePlan |
| `add_phase(after, manifest)` | Insert an extra phase |
| `loop_back_to(phase)` | Return to a previous phase |
| `submit_research_summary(architecture_id, findings_ids)` | Signal research completion |
| `submit_micro_review(verdict, revisions)` | Submit ok/revise/abort verdict |
| `submit_phase_plan(plan_id)` | Signal planning completion |
| `submit_final_review(verdict)` | Submit ok/loop_back verdict |
| `submit_verification(pass, details)` | Submit verify result |
| `submit_reflection(text, evidence_refs)` | Submit evidence-backed reflection |
| `submit_preflight_report(status, blockers)` | Report ready/blocked |
| `edit_subtask_card(subtask_id, patch)` | Modify subtask recipe |
| `mark_subtask_complete(subtask_id)` | Mark subtask done |
| `request_watcher_review(reason)` | Ask Watcher to review |
| `harness_run(subtask_id, n, strategy)` | Run N parallel candidates |

All tools read/write `drive/state/phase_plan.json` and `drive/state/watcher_signal.json`.

## LLM Client (`llm.py`)

- Chat completion calls with pricing calculation.
- Model resolution from env vars (`LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`).
- Budget tracking and reporting.

## Supervisor (`ouroboros/supervisor/`)

| Module | Lines | Purpose |
|--------|-------|---------|
| `telegram.py` | ~550 | Telegram notifications: messages, photos, budget alerts |
| `events.py` | ~520 | Event dispatcher: 15 event types -> handlers |
| `queue.py` | ~490 | Task queue: priority, persistence, scheduling |
| `workers.py` | ~655 | Worker process lifecycle, health checks |
| `state.py` | ~760 | Drive state: atomic load/save, file locks, budget |
| `git_ops.py` | ~510 | Git operations: clone, checkout, stash, rescue |

---

Next: [Part 9 — Verification](09-verification.md)
