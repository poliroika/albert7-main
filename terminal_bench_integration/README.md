# `terminal_bench_integration/`

Adapter that lets the `umbrella` / `ouroboros` stack be evaluated by
[Terminal-Bench](https://www.tbench.ai/).

## How it works

```
┌─ host (Windows / macOS / Linux) ─────────────────────────────────┐
│                                                                  │
│   $ uv run python -m terminal_bench_integration.cli --n-tasks 3  │
│                                                                  │
│   loads .env  →  validates docker  →  invokes `tb run`           │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─ Terminal-Bench harness (`tb run`) ──────────────────────────────┐
│   for task in dataset:                                           │
│       1. build task Docker image                                 │
│       2. start task container + tmux session                     │
│       3. call UmbrellaAgent.perform_task(instruction, session)    │
│       4. wait until pane idle OR --agent-timeout-sec             │
│       5. run hidden pytest grader inside the container           │
│       6. record pass/fail in results.json                        │
│       7. tear down container                                     │
└──────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌─ UmbrellaAgent.perform_task ──────────────────────────────────────┐
│   1. tar host repo (excludes .git/.venv/.umbrella/__pycache__/...) │
│   2. copy_to_container → /installed-agent/umbrella.tar.gz         │
│   3. setup.sh: apt install python3, untar, venv, pip install     │
│   4. cat instruction → /tmp/tb/instruction.txt                   │
│   5. python -m terminal_bench_integration.run_inside ...         │
│        ↳ writes instruction → workspaces/terminal_bench/         │
│          TASK_MAIN.md (over the placeholder)                     │
│        ↳ runs `python -m umbrella.app_ouroboros workspaces/        │
│          terminal_bench --no-dashboard --no-verify --live`       │
│        ↳ ouroboros loops (LLM ↔ shell tool) until done           │
│   6. agent process exits, tmux pane returns to prompt            │
└──────────────────────────────────────────────────────────────────┘
```

### What lives where

| File                                                | Role |
|-----------------------------------------------------|------|
| `agent.py`                                          | `UmbrellaAgent` (subclass of `AbstractInstalledAgent`). Packs repo, ships it in, declares env, declares run command. |
| `setup.sh`                                          | Sourced inside the container by the abstract base. apt-installs python, untars, sets up venv, pip-installs runtime deps. |
| `run_inside.py`                                     | Runs *inside* the container. Writes the per-task instruction into `workspaces/terminal_bench/TASK_MAIN.md` and invokes `umbrella.app_ouroboros`. |
| `_repo_packer.py`                                   | Builds the `.tar.gz` of the repo, excluding host-OS cruft. |
| `cli.py`                                            | Host-side launcher around `tb run`. Loads `.env`, validates Docker, points `tb` at this agent. |
| `../workspaces/terminal_bench/TASK_MAIN.md`         | **Stable** generic playbook for any TB task. The header block gets overwritten with the per-task instruction at runtime, the rest stays. |
| `../workspaces/terminal_bench/tools/inventory.sh`   | First command the agent should run inside any task: dumps box state, `/app` contents, toolchains, env vars. |

## One-time host setup

1. Docker Desktop running.
2. `.env` with `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` (already present
   in this repo).
3. Install the harness:

   ```powershell
   uv pip install terminal-bench
   ```

   The first time `cli.py` runs it auto-installs a tiny
   `sitecustomize.py` patch into `.venv/Lib/site-packages/` that fixes a
   Windows-only `pathlib.WindowsPath("/tmp")` bug in terminal-bench
   0.2.x. The patch is a no-op on POSIX.

4. **Windows only**: `tb datasets download` is currently broken on
   Windows because the implementation shells out to `rm -rf .git`,
   which does not exist on Windows. Clone the dataset manually with
   the right line-ending settings:

   ```powershell
   $dest = "$env:USERPROFILE\.cache\terminal-bench\datasets\terminal-bench-core\0.1.1"
   git -c core.autocrlf=input -c core.eol=lf clone --depth 1 `
       --branch dataset/terminal-bench-core/v0.1.x `
       https://github.com/laude-institute/terminal-bench $dest
   git -C $dest -c core.autocrlf=input checkout `
       91e10457b5410f16c44364da1a34cb6de8c488a5
   ```

   `--core.autocrlf=input` is **mandatory**: without it git rewrites the
   tasks' `*.sh` files with `CRLF`, which then fail in the Linux task
   container with `$'\r': command not found`.

   `cli.py` autodetects this path and passes it to `tb` as
   `--dataset-path`.

## Running

```powershell
# smoke run: 3 tasks from terminal-bench-core, sequential, 30 min each
uv run python -m terminal_bench_integration.cli --n-tasks 3

# specific task
uv run python -m terminal_bench_integration.cli --task-id hello-world

# full run (~80 tasks; takes hours, costs LLM tokens)
uv run python -m terminal_bench_integration.cli --n-tasks 0

# anything you want forwarded to `tb run` directly:
uv run python -m terminal_bench_integration.cli -- --livestream --task-id foo
```

Results land in `runs/<UTC-timestamp>__umbrella__terminal-bench-core/`:

- `results.json` — per-task pass/fail, accuracy, token usage
- `<task_id>/agent.log` — what the agent did (its own stderr)
- `<task_id>/tmux.log` — full pane history
- `<task_id>/tests.log` — output of the hidden pytest grader

## Honest reporting

When you cite a number from a run, always cite alongside it:

- model + endpoint (e.g. `GLM-4.7 @ garfield3.frontierai.ru:7080/v1`)
- dataset + version
- `n_tasks` (or list of task IDs)
- `pass@1` vs `pass@k` and `k`
- agent flags (verification on/off, max_rounds, timeout)
- link to the `runs/` directory containing `results.json`

A pass rate without these is meaningless and will be (rightly) discounted
by anyone who reads it.

## Known gotchas

- **garfield3 endpoint reachability.** The default `LLM_BASE_URL` is on a
  corporate network. If the task container can't reach it, every task
  will fail with an OpenAI client error in `agent.log`. Use a public
  endpoint (or run from a network that can reach garfield3) for honest
  numbers.
- **First run is slow.** The first time you run a given task, Docker has
  to pull / build the task base image, and `setup.sh` has to apt-install
  python and pip-install our deps. Subsequent runs reuse the image
  cache.
- **Disk usage.** Each run leaves task images in your local Docker. Run
  `docker image prune` periodically.
- **Windows host.** Everything works because the agent itself runs
  inside a Linux container; only `tb` and the `tar` packer run on the
  host. `tar` is built into Python (`tarfile`), so no msys/cygwin needed.
