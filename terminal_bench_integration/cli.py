"""Host-side launcher around the terminal-bench `tb run` command.

This is a thin convenience wrapper that:

1. Loads `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` from the repo's
   `.env` (the same file `umbrella.env.load_env` reads), so the user does
   not have to export them by hand.
2. Validates that Docker is running (Terminal-Bench needs it).
3. Calls `tb run` with the `UmbrellaAgent` import path, sensible defaults
   for a smoke run, and `--output-path runs/`.

Usage:

    uv run python -m terminal_bench_integration.cli --n-tasks 3
    uv run python -m terminal_bench_integration.cli --task-id hello-world
    uv run python -m terminal_bench_integration.cli --n-tasks 0   # full run
"""



import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "runs"

log = logging.getLogger("terminal_bench_integration.cli")


def _ensure_sitecustomize(repo_root: Path) -> None:
    """Make sure the venv has our `sitecustomize.py` patch in place.

    The patch fixes a Windows-specific terminal-bench bug; on non-Windows
    hosts the file is a no-op but we still install it so the venv is
    portable.
    """
    src = repo_root / "terminal_bench_integration" / "_sitecustomize.py"
    venv_site = repo_root / ".venv" / "Lib" / "site-packages"
    if not venv_site.is_dir():
        venv_site = repo_root / ".venv" / "lib" / "site-packages"
    if not venv_site.is_dir():
        # Try POSIX layout (`lib/python3.x/site-packages`).
        for cand in sorted((repo_root / ".venv" / "lib").glob("python*")):
            if (cand / "site-packages").is_dir():
                venv_site = cand / "site-packages"
                break
    if not venv_site.is_dir():
        log.warning("Could not locate venv site-packages; sitecustomize patch skipped")
        return
    dst = venv_site / "sitecustomize.py"
    try:
        if not src.is_file():
            return
        if dst.is_file() and dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8"):
            return
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        log.info("Installed sitecustomize.py patch into %s", dst)
    except OSError as exc:
        log.warning("Failed to install sitecustomize patch (%s)", exc)


def _load_dotenv_into_os(repo_root: Path) -> None:
    env_file = repo_root / ".env"
    if not env_file.is_file():
        log.warning("No .env file at %s; assuming env vars are already exported", env_file)
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _find_tb_executable(repo_root: Path) -> str:
    """Locate the `tb` CLI without relying on PATH.

    The terminal-bench wheel installs `tb` into the active venv's scripts
    directory; on Windows that is `.venv/Scripts/tb.exe`, on POSIX
    `.venv/bin/tb`. We prefer the in-repo venv to avoid surprising the
    user with some other installation, and fall back to PATH lookup.
    """
    candidates = [
        repo_root / ".venv" / "Scripts" / "tb.exe",
        repo_root / ".venv" / "bin" / "tb",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    on_path = shutil.which("tb")
    if on_path:
        return on_path
    raise SystemExit(
        "ERROR: could not find the `tb` executable.\n"
        "Install with:\n"
        "    uv pip install terminal-bench\n"
    )


def _check_prereqs() -> None:
    if shutil.which("docker") is None:
        raise SystemExit(
            "ERROR: `docker` not found on PATH. Install Docker Desktop and try again."
        )
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(
            "ERROR: `docker info` failed. Start Docker Desktop and try again.\n"
            f"Underlying error: {exc}"
        ) from exc
    if not os.environ.get("LLM_API_KEY"):
        raise SystemExit(
            "ERROR: LLM_API_KEY is not set and was not found in .env."
        )


def _build_tb_command(
    tb_exe: str, args: argparse.Namespace, output_dir: Path
) -> list[str]:
    cmd: list[str] = [
        tb_exe,
        "run",
        "--agent-import-path",
        "terminal_bench_integration.agent:UmbrellaAgent",
        "--output-path",
        str(output_dir),
        "--n-concurrent",
        str(args.n_concurrent),
        "--n-attempts",
        str(args.n_attempts),
        "--global-agent-timeout-sec",
        str(args.agent_timeout_sec),
        "--log-level",
        args.log_level,
    ]
    # Prefer a local dataset path if given (or auto-discoverable on disk)
    # because `tb datasets download` is currently broken on Windows: it
    # shells out to `rm -rf .git` after cloning, and that command does
    # not exist on stock Windows. By cloning the dataset ourselves and
    # pointing tb at the directory we sidestep the broken codepath.
    if args.dataset_path:
        cmd.extend(["--dataset-path", str(args.dataset_path.resolve())])
    else:
        cmd.extend(["--dataset", args.dataset])
    if args.task_id:
        for tid in args.task_id:
            cmd.extend(["--task-id", tid])
    elif args.n_tasks > 0:
        cmd.extend(["--n-tasks", str(args.n_tasks)])
    if args.livestream:
        cmd.append("--livestream")
    if args.cleanup:
        cmd.append("--cleanup")
    else:
        cmd.append("--no-cleanup")
    if args.extra:
        cmd.extend(args.extra)
    return cmd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run Terminal-Bench against the umbrella / ouroboros adapter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dataset", default="terminal-bench-core",
                        help="Terminal-Bench dataset name (default: terminal-bench-core). "
                             "Ignored when --dataset-path is given.")
    # Prefer the newest locally-cloned dataset version. We auto-discover
    # under the canonical cache root so that bumping to a fresh dataset
    # branch is just a `git clone` away. Order of preference: newest
    # version directory containing a non-empty `tasks/` folder.
    _dataset_cache_root = (
        Path.home() / ".cache" / "terminal-bench" / "datasets" / "terminal-bench-core"
    )
    default_local_dataset: Path | None = None
    if _dataset_cache_root.is_dir():
        candidates = []
        for child in _dataset_cache_root.iterdir():
            tasks_dir = child / "tasks"
            if tasks_dir.is_dir() and any(tasks_dir.iterdir()):
                candidates.append((child.name, tasks_dir))
        if candidates:
            # naive lexicographic sort works for "0.1.1" < "0.2.0" < "0.2.1"...
            candidates.sort(key=lambda p: [int(x) if x.isdigit() else x for x in p[0].split(".")])
            default_local_dataset = candidates[-1][1]
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=default_local_dataset,
        help="Path to a locally cloned dataset's `tasks/` directory. "
             "Defaults to the newest cached terminal-bench-core version. "
             "Use this on Windows -- `tb datasets download` is currently broken there.",
    )
    parser.add_argument("--n-tasks", type=int, default=3,
                        help="How many tasks to run (smoke default: 3). Use 0 to run all.")
    parser.add_argument("--task-id", action="append", default=None,
                        help="Restrict to specific task IDs (repeatable). Overrides --n-tasks.")
    parser.add_argument("--n-concurrent", type=int, default=1,
                        help="Parallel trials. Keep at 1 unless you have spare RAM and LLM slots.")
    parser.add_argument("--n-attempts", type=int, default=1,
                        help="Trials per task (pass@k). Keep at 1 unless you know why.")
    parser.add_argument("--agent-timeout-sec", type=int, default=1800,
                        help="Per-task wall-clock cap for the agent (default 1800 = 30 min).")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error", "critical"],
                        help="tb-side log level.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help=f"Where tb writes results.json + per-task logs (default {DEFAULT_OUTPUT_DIR}).")
    parser.add_argument("--livestream", action="store_true",
                        help="Stream the tmux pane to stdout while running.")
    parser.add_argument("--cleanup", action="store_true", default=True,
                        help="Pass --cleanup to tb run (remove containers afterwards).")
    parser.add_argument("--no-cleanup", dest="cleanup", action="store_false",
                        help="Keep task containers around for post-mortem.")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("extra", nargs=argparse.REMAINDER,
                        help="After `--`, everything is forwarded to `tb run` verbatim.")

    args = parser.parse_args(argv)
    args.extra = [a for a in (args.extra or []) if a != "--"]

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    _load_dotenv_into_os(REPO_ROOT)
    _ensure_sitecustomize(REPO_ROOT)
    _check_prereqs()
    tb_exe = _find_tb_executable(REPO_ROOT)
    log.info("Using tb at: %s", tb_exe)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir / f"{timestamp}__umbrella__{args.dataset}"
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = _build_tb_command(tb_exe, args, output_dir)
    log.info("Output dir : %s", output_dir)
    log.info("Command    : %s", " ".join(cmd))

    # `tb run` must see PYTHONPATH=<repo_root> so that
    # `terminal_bench_integration.agent:UmbrellaAgent` is importable when
    # the harness loads the agent class.
    env = os.environ.copy()
    pp = env.get("PYTHONPATH", "")
    pp_parts = [str(REPO_ROOT)]
    if pp:
        pp_parts.append(pp)
    env["PYTHONPATH"] = os.pathsep.join(pp_parts)
    env["UMBRELLA_REPO_ROOT"] = str(REPO_ROOT)

    proc = subprocess.run(cmd, env=env, cwd=str(REPO_ROOT))
    log.info("`tb run` exited with code %d", proc.returncode)
    log.info("Inspect results at: %s", output_dir / "results.json")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
