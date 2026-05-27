# Part 11: Configuration

Configuration is spread across environment variables, CLI flags, YAML files, and TOML workspace configs.

## Environment Variables (`.env`)

### Core LLM

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_API_KEY` | (required) | API key for LLM provider. Also accepts `OPENAI_API_KEY`. |
| `LLM_MODEL` | (none) | Default model for some code paths |
| `LLM_BASE_URL` | (none) | Proxy or non-standard endpoint URL |
| `OUROBOROS_MODEL` | (none) | Override model for Ouroboros (Worker and Watcher) |

### Phase Runner

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_MAX_ROUNDS` | `0` (unlimited) | Max LLM rounds per phase. <=0 means no hard cap. |
| `OUROBOROS_PHASES_DIR` | `umbrella/phases/manifests` | Where to look for phase manifests |
| `OUROBOROS_PHASE_PLAN_PATH` | `<drive>/state/phase_plan.json` | Path to PhasePlan file |
| `OUROBOROS_TEMP_TOOLS_DIR` | `<drive>/tmp_tools` | Where agent-authored temporary tools live |

### Watcher

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_WATCHER_MODEL` | inherit from `OUROBOROS_MODEL` / `LLM_MODEL` | Separate model for Watcher |
| `OUROBOROS_WATCHER_STALL_SEC` | `180` | Stall trigger threshold (seconds) |
| `OUROBOROS_WATCHER_REPEAT_M` | `30` | Legacy alias for semantic abort threshold when `SEMANTIC_ABORT_M` unset |
| `OUROBOROS_WATCHER_SEMANTIC_INJECT_M` | `3` | Consecutive semantic failures before `inject_lesson` |
| `OUROBOROS_WATCHER_SEMANTIC_RESTART_M` | `15` | Consecutive semantic failures before `restart_phase` |
| `OUROBOROS_WATCHER_SEMANTIC_ABORT_M` | `30` | Consecutive semantic failures before `abort_phase` |
| `OUROBOROS_WATCHER_USE_LLM_ON_SEMANTIC` | unset | When `1`, optional LLM refinement of semantic inject lessons |
| `OUROBOROS_WATCHER_POLL_SEC` | `5` | Watcher poll interval (seconds) |

### Truncation / previews

Logs and control-plane checks read `tools.jsonl` previews; the LLM chat uses separate caps. When history grows, the agent can call `compact_context` (summarize) before hitting model limits.

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_TOOL_LOG_PREVIEW_CHARS` | `16000` | `tools.jsonl` `result_preview` (was 2000) |
| `OUROBOROS_TOOL_RESULT_TO_MODEL_CHARS` | `48000` | Tool message body in active chat (was 15000) |
| `OUROBOROS_TOOL_TRACE_SNIPPET_CHARS` | `2400` | `llm_trace` / completion nudge snippets |
| `OUROBOROS_TOOL_ARGS_LOG_CHARS` | `12000` | Large fields in logged tool args |
| `OUROBOROS_TRUNCATE_FOR_LOG_DEFAULT` | `16000` | Default for `truncate_for_log()` |
| `OUROBOROS_DISCOVERY_CONTENT_CHARS` | `12000` | web/deep_search/github excerpt payloads |
| `OUROBOROS_MEMORY_HIT_PREVIEW_CHARS` | `8000` | Palace search hits in tool responses |
| `OUROBOROS_RECALL_DRAWER_PREVIEW_CHARS` | `1200` | Inline recall drawer snippets |
| `OUROBOROS_RECALL_LESSON_PREVIEW_CHARS` | `2000` | Inline recall lesson snippets |
| `OUROBOROS_MODEL_CONTEXT_TOKENS` | `200000` | Soft model window (see `umbrella/llm_budget.py`) |
| `OUROBOROS_GMAS_CONTEXT_TOKENS` | `60000` | Aggregated GMAS retrieval budget |

### Memory

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_PALACE_TRANSIENT_TTL_SEC` | `86400` | TTL for transient store nodes (default 24h) |
| `OUROBOROS_REFLEXION_PROMOTE_REQUIRES_VERIFY_PASS` | `1` | Safety toggle for reflexion promotion gate |

Durable backend selection (canonical / dual / Hindsight), mirror flags, and release smoke env are documented in **[Durable memory backends](../memory-durable-backends.md)** — not duplicated here.

### Permissions

| Variable | Default | Purpose |
|----------|---------|---------|
| `UMBRELLA_PERMISSIONS_GLOBAL` | `umbrella/permissions/global.yaml` | Path to global deny rules |
| `UMBRELLA_SELF_IMPROVEMENT_ENVELOPE` | `umbrella/permissions/self_improvement.yaml` | Relaxed envelope for self-improvement mode |

### Output

| Variable | Default | Purpose |
|----------|---------|---------|
| `UMBRELLA_OUTPUT_FORMAT` | `json` | Default CLI output format (`json` or `pretty`) |

### Web Bridge

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_WEB_MAX_VERIFY_RETRIES` | `20` | Max verify retries in web-initiated runs |

### Experimental

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_ENABLE_EXPERIMENTAL_REVIEW_TOOLS` | (not set) | Enable experimental review tool module |

## CLI Flags

### `umbrella/app_ouroboros.py`

| Flag | Default | Purpose |
|------|---------|---------|
| `--live` | false | Use live LLM (not mock) |
| `--verbose` | false | Verbose output |
| `--task` | (none) | Task description override |
| `--task-file` | (none) | Task description from file |
| `--max-verify-retries` | 3 | Max verification retries |
| `--no-verify` | false | Skip verification |
| `--max-rounds` | (unlimited) | Max LLM rounds |
| `--max-budget` | (none) | Budget limit in USD |
| `--timeout-hours` | (none) | Timeout in hours |
| `--verification-timeout-seconds` | (none) | Per-step verification timeout |
| `--allow-seed-writes` | false | Allow writing to seed workspace |
| `--mock` | false | Use mock LLM responses |

### `umbrella/web_bridge/server.py`

| Flag | Default | Purpose |
|------|---------|---------|
| `--host` | `127.0.0.1` | Server bind address |
| `--port` | `8765` | Server port |
| `--repo-root` | (cwd) | Repository root |
| `--log-level` | `INFO` | Logging level |

## YAML Configuration

### Phase Manifests (`umbrella/phases/manifests/*.yaml`)

Each manifest configures one phase. Key sections:

- `prompt_files`: system, user_overlay, charter_blocks
- `allowed_tools` / `forbidden_tools`: tool access lists
- `allowed_skills`: skill pack slugs
- `memory`: always_on, hot, warm_search, graph walk, write_rules
- `permissions.rules`: per-phase allow/deny with path/command patterns
- `exit_criteria`: required_calls, required_palace_writes, min_palace_writes
- `budgets`: max_tokens, max_seconds, max_tool_calls

### Global Permissions (`umbrella/permissions/global.yaml`)

Hard denials that override any phase rule:

```yaml
rules:
  - deny_path: ["**/.env*", "**/secrets/**", ".git/**"]
  - deny_tool: shell
    args: {cmd_re: ".*(rm\\s+-rf|sudo|curl.*\\|\\s*sh).*"}
```

### Self-Improvement Envelope (`umbrella/permissions/self_improvement.yaml`)

Relaxed rules for the `self_improvement_run` mode: allows writes to `umbrella/`, `ouroboros/` (but still denies `gmas/`, secrets, `.git`).

## Workspace Configuration

### `workspace.toml`

Per-workspace settings:

```toml
workspace_id = "my_workspace"
name = "My Workspace"
task_main_file = "TASK_MAIN.md"
mutable_paths = ["graph", "agents", "prompts", "tools"]

[metadata]
engine = "gmas"
engine_mutable = false

[verification]
steps = [
    {type = "pytest", command = "pytest test_smoke.py"},
    {type = "http_health", command = "python web_server.py", port = 8080, path = "/health"},
]
```

### `registry.toml`

Workspace registry:

```toml
version = "0.1.0"
seeds = ["multi_agent_debate_graph"]
instances = []
```

---

Next: [Part 12 — Harness vs meta-harness](12-meta-harness.md)
