# Ouroboros

Ouroboros is the **deep LLM agent** that executes individual phases within an Umbrella run. It is spawned by the `PhaseRunner` as either a **Worker** (executing a phase) or a **Watcher** (monitoring for problems). It does not orchestrate phases itself — that is Umbrella's responsibility.

## Role in the System

Ouroboros consumes a **phase manifest** from Umbrella, which defines what tools it can use, what prompts to load, and what memory it can access. The manifest is passed through `task["context_overlays"]` and enforced by the `PermissionEnvelope` pre-hook.

Key principle: Ouroboros is a **per-phase executor**, not a run orchestrator. It receives instructions, executes them, and reports back. The PhaseRunner decides what comes next.

## Core Modules

| Module | Purpose |
|--------|---------|
| `ouroboros/ouroboros/loop.py` | Main LLM tool loop (~5800 lines). Manages rounds, tool calls, context building, phase enforcement. |
| `ouroboros/ouroboros/agent.py` | Thin orchestrator. Delegates to loop, tools, LLM, memory, context, review. |
| `ouroboros/ouroboros/context.py` | Context builder: `build_llm_messages`, message compaction, prompt overlays. |
| `ouroboros/ouroboros/llm.py` | LLM client: chat calls, pricing, model resolution. |
| `ouroboros/ouroboros/memory.py` | Scratchpad / identity memory. |
| `ouroboros/ouroboros/memory_hooks.py` | Memory hook system for events. |
| `ouroboros/ouroboros/utils.py` | Shared utilities: `utc_now_iso`, `append_jsonl`, `get_git_info`, etc. |
| `ouroboros/ouroboros/task_planner.py` | Adaptive task planning and decomposition. |
| `ouroboros/ouroboros/discipline.py` | `VerifyGate`, `WRITE_TOOL_NAMES` constants. |
| `ouroboros/ouroboros/deadline.py` | Runtime deadline enforcement. |
| `ouroboros/ouroboros/preflight_recovery.py` | Preflight error tracking and repair suggestions. |
| `ouroboros/ouroboros/review.py` | Code collection and metrics. |

## Tool Registry

Ouroboros has 22 tool modules in `ouroboros/ouroboros/tools/`:

### Core Tools

| Tool | Module | Purpose |
|------|--------|---------|
| `registry.py` | `ToolRegistry` | Plugin architecture: auto-discovers tool modules, manages tool entries, PermissionEnvelope pre-hook |
| `core.py` | Core tools | Basic file operations, workspace inspection |
| `shell.py` | Shell execution | Run shell commands with working directory control |
| `git.py` | Git operations | Status, diff, commit, branch management |
| `github.py` | GitHub tools | Repository and issue operations |
| `github_discovery.py` | GitHub discovery | Find similar projects and patterns |
| `browser.py` | Browser automation | Web page interaction |
| `search.py` | Web search | Search the web |
| `vision.py` | Vision/image | Image analysis |
| `knowledge.py` | Knowledge retrieval | Read knowledge bases |
| `deep_search.py` | Deep search | Comprehensive search with findings stored in MemPalace |
| `health.py` | Health check | System health diagnostics |
| `terminal_session.py` | Terminal session | Interactive terminal management |
| `background_jobs.py` | Background jobs | Async job management |
| `compact_context.py` | Context compaction | Reduce context size |

### Umbrella Integration Tools

| Tool | Module | Purpose |
|------|--------|---------|
| `umbrella_tools.py` | Umbrella integration | GMAS retrieval, workspace read/run, memory operations, promotion, sandbox self-edit |
| `palace_tools.py` | MemPalace tools | `get_umbrella_memory`, `list_memory_tree`, `save_umbrella_memory`, `record_idea`, `save_umbrella_lesson` (handlers still delegate to `umbrella_tools.py`) |
| `skills_tools.py` | Skills | Load and discover skill packs |
| `mcp_servers.py` | MCP servers | Start/stop MCP servers |
| `mcp_discovery.py` | MCP discovery | Find available MCP tools |
| `tool_discovery.py` | Meta-tools | `list_available_tools`, `enable_tools` |
| `evolution_stats.py` | Evolution statistics | Track self-improvement metrics |
| `completion_gates.py` | Completion gates | Phase/subtask completion checks |

### Phase Control Tools (new)

| Tool | Module | Purpose |
|------|--------|---------|
| `phase_control.py` | In-run self-modification | `mutate_phase_plan`, `add_phase`, `loop_back_to`, `submit_research_summary`, `submit_micro_review`, `submit_phase_plan`, `submit_final_review`, `submit_verification`, `submit_reflection`, `submit_preflight_report`, `edit_subtask_card`, `mark_subtask_complete`, `request_watcher_review`, `harness_run` |

### Review Tools

| Tool | Module | Purpose |
|------|--------|---------|
| `review.py` | Review tools | Experimental code review tools (gated by `OUROBOROS_ENABLE_EXPERIMENTAL_REVIEW_TOOLS`) |
| `control.py` | Supervisor control | Supervisor-level control operations |

## Phase Manifest Consumption

The phase manifest flows from the PhaseRunner to Ouroboros as follows:

1. **PhaseRunner** reads the YAML manifest, compiles the PermissionEnvelope, and builds a `recall_bundle` from MemPalace.
2. **Worker spawn** (`umbrella/orchestrator/worker.py`) creates a task with `context_overlays` containing:
   - `phase_manifest`: the full manifest payload
   - `phase_node`: current phase state
   - `recall_bundle`: pre-loaded memory context
   - `permissions`: compiled permission rules
   - `tool_filter`: `{allow: [...], deny: [...]}`
   - `budgets`: token/second/tool-call limits
3. **Agent** (`agent.py`) reads `task["context_overlays"]["phase_manifest"]` — if present, phase-driven mode; otherwise legacy fallback.
4. **Loop** (`loop.py`) uses `tool_filter.allow/deny` instead of hardcoded tool name lists. Every tool call checks: `if allowed_tool_names is None or fn_name in allowed_tool_names`.
5. **Context builder** (`context.py`) includes `recall_bundle` as a new section and `permissions` for self-awareness (agent sees its own envelope in the prompt).

**Forbidden tool handling:**
- A `_format_forbidden_tool_error` message is returned to the LLM
- Strike counting tracks repeated violations (`forbidden_strike_counts`)
- After enough strikes, the call may be auto-delegated or the phase may be flagged

## Supervisor

The supervisor (`ouroboros/supervisor/`) manages Ouroboros processes, event dispatch, and external communication.

| Module | Purpose |
|--------|---------|
| `telegram.py` | Telegram client: message sending, markdown-to-HTML conversion, budget tracking |
| `events.py` | Event dispatcher: 15 event types from `EVENT_Q` to handler functions |
| `queue.py` | Task queue: priority, timeouts, persistence, scheduling |
| `workers.py` | Worker lifecycle: multiprocessing, health checks, direct chat handling |
| `state.py` | Persistent state on Drive: load, save, atomic writes, file locks, budget management |
| `git_ops.py` | Git operations: clone, checkout, reset, rescue snapshots, dependency sync |

Event types: `llm_usage`, `task_heartbeat`, `typing_start`, `send_message`, `task_done`, `task_metrics`, `review_request`, `restart_request`, `promote_to_stable`, `schedule_task`, `cancel_task`, `send_photo`, `toggle_evolution`, `toggle_consciousness`, `owner_message_injected`.

## Removed Modules

The following modules were removed during the refactoring:

- `ouroboros/ouroboros/consciousness.py` — replaced by the Watcher agent in `umbrella/orchestrator/watcher.py`
- `ouroboros/ouroboros/workspaces/polymarket_sim_empty/` — sample workspace removed
- `_PERIODIC_RECALL_DEFAULT_PHASES` in `loop.py` — replaced by manifest-driven memory policy
- Hardcoded `_PLANNER_DISCOVERY_TOOL_NAMES`, `_SUBTASK_TOOL_NAMES`, `_REVIEW_TOOL_NAMES` — replaced by `tool_filter` from manifests

## What Changed from the Pre-Refactor Ouroboros

| Aspect | Before | After |
|--------|--------|-------|
| Tool access | Hardcoded tool name lists per phase | Manifest-driven `tool_filter` (allow/deny) |
| Memory recall | `_PERIODIC_RECALL_DEFAULT_PHASES` | MemPalace `recall_bundle` per phase manifest |
| Self-modification | Mixed into loop | Dedicated `phase_control.py` tools |
| Monitoring | `BackgroundConsciousness` (in-process) | External Watcher agent (parallel process) |
| Workspace storage | `ouroboros/ouroboros/workspaces/` | Removed (workspaces live in `workspaces/`) |
| Context building | Hardcoded prompt sections | Phase manifest `prompt_files` + overlays |
| Permission enforcement | None | PermissionEnvelope pre-hook in `ToolRegistry.execute` |

## Integration with Umbrella

Ouroboros connects to Umbrella through:

- **Ouroboros drive** (`.umbrella/ouroboros_drive/`): filesystem bridge for task context, memory, and state. Managed by `umbrella/integration/ouroboros_bridge.py`.
- **PhaseRunner** (`umbrella/orchestrator/runner.py`): spawns Worker/Watcher, passes manifests, processes control signals.
- **Phase control channel** (`drive/state/`): JSON files for `phase_plan.json`, `watcher_signal.json`, subtask state. `phase_control.py` tools read/write here.
- **MemPalace** (`umbrella/memory/palace/`): Worker and Watcher both access the unified memory facade.

## CLI Entrypoints

| Script | Purpose |
|--------|---------|
| `umbrella/app_ouroboros.py` | Single-run CLI entrypoint |
| `umbrella/web_bridge/server.py` | Operator UI + JSON API |

There is no longer a standalone `run_ouroboros_self_improve.py` or `run_meta_harness.py` — these entrypoints were consolidated into the phase-driven runner.

## Key Design Principle

Ouroboros is a **head** that executes instructions, not an **orchestrator** that decides what to do next. The PhaseRunner decides the sequence of phases. Ouroboros' job is to execute each phase well, using the tools and memory it has been granted, and to report results honestly.
