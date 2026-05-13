"""Entry point executed *inside* the Terminal-Bench task container.

Workflow:

1. Read the per-task instruction (passed either via ``--instruction`` or
   from a file via ``--instruction-file``).
2. Render it into the ``workspaces/terminal_bench/TASK_MAIN.md`` file
   inside the container's umbrella checkout.
3. Invoke :mod:`umbrella.app_ouroboros` against that workspace, with the
   dashboard disabled but runtime verification enabled so the agent must
   pass its own checks before declaring completion.
4. Exit with the same return code.

Logs go to ``/agent-logs/umbrella-tb.log`` if that directory exists
(Terminal-Bench mounts it from the host) and otherwise to stderr.
"""



import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO_ROOT = Path("/opt/umbrella")
DEFAULT_WORKSPACE_ID = "terminal_bench"
DEFAULT_AGENT_LOGS_DIR = Path("/agent-logs")
PROMPT_HEADER = "# Terminal-Bench task instruction\n\n"


def _setup_logging(logs_dir: Path) -> logging.Logger:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logs_dir / "umbrella-tb.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("terminal_bench_integration.run_inside")


def _read_instruction(args: argparse.Namespace) -> str:
    if args.instruction_file:
        text = Path(args.instruction_file).read_text(encoding="utf-8")
    elif args.instruction is not None:
        text = args.instruction
    else:
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        raise SystemExit("No instruction provided (empty after stripping).")
    return text


def _write_task_main(repo_root: Path, workspace_id: str, instruction: str) -> Path:
    task_main = repo_root / "workspaces" / workspace_id / "TASK_MAIN.md"
    task_main.parent.mkdir(parents=True, exist_ok=True)
    body = (
        PROMPT_HEADER
        + instruction
        + "\n\n"
        + "## CRITICAL: where to do the work\n\n"
        + "Your `run_workspace_command` tool's default cwd is the agent\n"
        + "workspace at `/opt/umbrella/workspaces/" + workspace_id + "/`. **That is\n"
        + "NOT where the task wants its files.** The Terminal-Bench grader\n"
        + "runs from **`/app`** and only looks at files under `/app`. Anything\n"
        + "you create in the workspace dir is invisible to the grader.\n\n"
        + "Therefore: every shell command you issue MUST begin with\n"
        + "`cd /app && ` (or pass an absolute `/app/...` path). The very\n"
        + "first command of the task should be `cd /app && ls -lAh` to\n"
        + "confirm what is already there.\n\n"
        + "Examples:\n\n"
        + "    cd /app && echo 'Hello, world!' > hello.txt\n"
        + "    cd /app && python script.py\n"
        + "    cd /app && pytest tests/\n\n"
        + "## Working environment\n\n"
        + "- You are inside a Terminal-Bench task container.\n"
        + "- Your `run_workspace_command` tool executes commands inside\n"
        + "  this container's shell.\n"
        + "- You have root + apt-get; install whatever you need.\n"
        + "- When you believe the task is solved, stop. Terminal-Bench\n"
        + "  will then run the hidden pytest suite (from `/app`) to score\n"
        + "  the result on the filesystem state of `/app`.\n"
    )
    task_main.write_text(body, encoding="utf-8")
    return task_main


def _build_command(
    repo_root: Path,
    workspace_id: str,
    timeout_hours: float,
    max_rounds: int,
    extra: list[str],
) -> list[str]:
    python = repo_root / ".venv" / "bin" / "python"
    cmd: list[str] = [
        str(python),
        "-m",
        "umbrella.app_ouroboros",
        f"workspaces/{workspace_id}",
        "--no-dashboard",
        "--live",
        f"--timeout-hours={timeout_hours}",
        f"--max-rounds={max_rounds}",
    ]
    cmd.extend(extra)
    return cmd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inside-container runner for the umbrella Terminal-Bench adapter.",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=DEFAULT_REPO_ROOT,
        help="Path to the umbrella checkout inside the container.",
    )
    parser.add_argument(
        "--workspace-id", default=DEFAULT_WORKSPACE_ID,
        help="Workspace inside `workspaces/` to use as the adapter target.",
    )
    parser.add_argument(
        "--instruction", default=None,
        help="Inline task instruction (mutually exclusive with --instruction-file).",
    )
    parser.add_argument(
        "--instruction-file", type=Path, default=None,
        help="Read the task instruction from a file.",
    )
    parser.add_argument(
        "--timeout-hours", type=float, default=0.5,
        help="Hard timeout for a single Ouroboros attempt. Default 30 min.",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=200,
        help="Hard cap on LLM rounds per attempt (0 = unlimited).",
    )
    parser.add_argument(
        "--logs-dir", type=Path, default=DEFAULT_AGENT_LOGS_DIR,
        help="Directory for agent logs; mounted from host by Terminal-Bench.",
    )
    parser.add_argument(
        "--",
        dest="passthrough_marker",
        nargs=argparse.REMAINDER,
        help="Everything after `--` is forwarded verbatim to app_ouroboros.",
    )
    args = parser.parse_args(argv)

    extra: list[str] = []
    if args.passthrough_marker:
        extra = [a for a in args.passthrough_marker if a != "--"]

    log = _setup_logging(args.logs_dir)
    log.info("=" * 70)
    log.info("UMBRELLA / OUROBOROS Terminal-Bench adapter")
    log.info("repo_root=%s workspace=%s", args.repo_root, args.workspace_id)
    log.info("=" * 70)

    instruction = _read_instruction(args)
    log.info("Instruction length: %d chars", len(instruction))
    task_main = _write_task_main(args.repo_root, args.workspace_id, instruction)
    log.info("Wrote TASK_MAIN to %s", task_main)

    cmd = _build_command(
        repo_root=args.repo_root,
        workspace_id=args.workspace_id,
        timeout_hours=args.timeout_hours,
        max_rounds=args.max_rounds,
        extra=extra,
    )
    log.info("Launching: %s", " ".join(cmd))

    env = os.environ.copy()
    # `umbrella.app_ouroboros` and ouroboros expect to import from the repo
    # root and from `ouroboros/` next to it.
    pythonpath_parts = [
        str(args.repo_root),
        str(args.repo_root / "ouroboros"),
    ]
    # `gmas` itself officially targets Python >=3.12. On older interpreters we
    # still ship the source tree for retrieval/file-reading purposes, but we do
    # not put it on PYTHONPATH because a direct import can fail on syntax or
    # dependency floors before Ouroboros even starts.
    if sys.version_info >= (3, 12):
        pythonpath_parts.append(str(args.repo_root / "gmas" / "src"))
    existing = env.get("PYTHONPATH", "")
    if existing:
        pythonpath_parts.append(existing)
    env["PYTHONPATH"] = ":".join(pythonpath_parts)
    env.setdefault("PYTHONUNBUFFERED", "1")

    proc = subprocess.run(
        cmd,
        cwd=str(args.repo_root),
        env=env,
        check=False,
    )
    log.info("app_ouroboros exited with code %d", proc.returncode)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
