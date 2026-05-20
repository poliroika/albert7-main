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
| `OUROBOROS_WATCHER_REPEAT_M` | `3` | Repeat error trigger: same error in M rounds |
| `OUROBOROS_WATCHER_POLL_SEC` | `5` | Watcher poll interval (seconds) |

### Memory

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_PALACE_TRANSIENT_TTL_SEC` | `86400` | TTL for transient store nodes (default 24h) |
| `OUROBOROS_REFLEXION_PROMOTE_REQUIRES_VERIFY_PASS` | `1` | Safety toggle for reflexion promotion gate |

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
