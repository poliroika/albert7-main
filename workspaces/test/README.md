# Test workspace (memory harness seed)

Minimal committed workspace for Umbrella memory scenario harness and `workspace_live` pytest.

- **TASK_MAIN.md** — task stub (no live LLM runs).
- **workspace.toml** — workspace config.

Runtime memory (Core/BKB/MemPalace/drive) is **not** committed under `.memory/` (gitignored). Harness and tests overlay declarative fixtures from:

`umbrella/tests/fixtures/memory_scenarios/`

Use `uv run memory-scenarios run --all` for the full memory audit lab.
