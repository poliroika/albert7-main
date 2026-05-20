"""Umbrella CLI entrypoint: launches Ouroboros via PhaseRunner.

PhaseRunner is the only execution engine. Every run goes through the phase
pipeline: preflight → research → review → plan → review → execute → review →
final → verify → reflexion. Each phase loads its own prompts, tools, and skills.

Operator UI: ``uv run bridge`` (web bridge with PhaseRunner integrated).
"""

import argparse
import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from umbrella.config import (
    DEFAULT_DASHBOARD_WORKSPACE_ID,
    load_runtime_config,
)
from umbrella.env import get_llm_env_config, load_env
from umbrella.orchestration.task_input import resolve_task_text
from umbrella.orchestration.status import write_status
from umbrella.orchestrator.runner import PhaseRunner


def _windows_utf8_stream(stream):
    if sys.platform != "win32":
        return stream
    if type(stream).__module__.startswith("_pytest."):
        return stream
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        return stream
    encoding = str(getattr(stream, "encoding", "") or "").replace("-", "").lower()
    if encoding == "utf8":
        return stream
    return io.TextIOWrapper(buffer, encoding="utf-8", errors="replace")


sys.stdout = _windows_utf8_stream(sys.stdout)
sys.stderr = _windows_utf8_stream(sys.stderr)


def _ensure_log_dir() -> None:
    Path(".umbrella").mkdir(parents=True, exist_ok=True)


_ensure_log_dir()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(".umbrella/app_ouroboros.log", encoding="utf-8"),
    ],
)

log = logging.getLogger(__name__)


def _resolve_live_mode(
    repo_root: Path, *, prefer_live: bool, force_mock: bool
) -> tuple[bool, str]:
    load_env(repo_root=repo_root)
    _llm_model, llm_api_key, _llm_base_url = get_llm_env_config()
    if force_mock:
        return False, "forced by --mock"
    if llm_api_key:
        return (True, "enabled by --live") if prefer_live else (True, "auto-enabled from .env")
    if prefer_live:
        log.warning("Live mode requested but no LLM credentials found; degraded mode")
    return False, "no live credentials found"


def _clear_stop_requests(repo_root: Path) -> None:
    for path in (
        repo_root / ".umbrella" / "launcher" / "stop_requested.json",
        repo_root / ".umbrella" / "ouroboros_drive" / "state" / "stop_requested.json",
    ):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            log.debug("Failed to clear stale stop request %s", path, exc_info=True)


def _resolve_workspace(repo_root: Path, workspace_arg: str) -> Path:
    path = Path(workspace_arg)
    if not path.is_absolute():
        path = (repo_root / path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Workspace path not found: {path}")
    return path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch Ouroboros through Umbrella PhaseRunner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "workspace", nargs="?", default=f"workspaces/{DEFAULT_DASHBOARD_WORKSPACE_ID}"
    )
    parser.add_argument("--task", help="Task text. Defaults to TASK_MAIN.md from the workspace.")
    parser.add_argument("--task-file", type=Path, help="Read task text from this file.")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--harness-candidates",
        type=int,
        default=1,
        help="Run N candidates per phase in parallel (default 1 = no harness).",
    )
    parser.add_argument("--live", action="store_true", help="Prefer live LLM mode")
    parser.add_argument("--mock", action="store_true", help="Force degraded/mock mode")
    parser.add_argument(
        "--output-format", choices=["json", "pretty"], default="pretty",
        help="Output format (default: pretty)",
    )
    parser.add_argument("--stream", action="store_true", help="Stream NDJSON envelopes to stdout")
    parser.add_argument("--phase", default=None, help="Run a single named phase only")
    parser.add_argument("--dry-run", action="store_true", help="Print phase plan without LLM calls")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def _resolve_task_text(args: argparse.Namespace, workspace_path: Path) -> str:
    parts: list[str] = []
    if args.task_file:
        parts.append(Path(args.task_file).read_text(encoding="utf-8"))
    if args.task:
        parts.append(args.task)
    explicit = "\n\n".join(p.strip() for p in parts if p.strip())
    if explicit:
        return explicit
    return resolve_task_text(workspace_path).task_text


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    repo_root = (args.repo_root or Path.cwd()).resolve()
    _clear_stop_requests(repo_root)

    workspace_path = _resolve_workspace(repo_root, args.workspace)
    workspace_id = workspace_path.name
    resolved_live, live_reason = _resolve_live_mode(
        repo_root, prefer_live=args.live, force_mock=args.mock
    )
    task_text = _resolve_task_text(args, workspace_path)

    log.info("=" * 70)
    log.info("UMBRELLA PHASE RUNNER")
    log.info("Workspace: %s", workspace_id)
    log.info("Mode: %s (%s)", "LIVE LLM" if resolved_live else "degraded", live_reason)
    log.info("Harness candidates per phase: %d", args.harness_candidates)
    log.info("=" * 70)

    write_status(
        repo_root,
        active=True,
        status="running",
        workspace_id=workspace_id,
        workspace_path=str(workspace_path),
        live_llm=resolved_live,
        live_reason=live_reason,
        task_preview=task_text[:1200],
    )

    final_status = "complete"
    last_error: str | None = None

    try:
        runner = PhaseRunner(
            repo_root=repo_root,
            workspace_id=workspace_id,
            candidates_per_phase=max(1, args.harness_candidates),
        )
        phases = [args.phase] if args.phase else None
        for envelope in runner.run(task_text, phases=phases, dry_run=args.dry_run):
            payload = envelope.to_dict()
            if args.output_format == "json" or args.stream:
                print(json.dumps(payload, ensure_ascii=False), flush=True)
            else:
                tag = "OK" if envelope.ok else "FAIL"
                data_str = json.dumps(payload.get("data") or {}, ensure_ascii=False)[:200]
                print(f"[{tag}] phase={payload.get('phase') or '-'} {data_str}", flush=True)
            if not envelope.ok and envelope.error:
                last_error = envelope.error
                final_status = "failed"
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        write_status(
            repo_root, active=False, status="interrupted", workspace_id=workspace_id
        )
        return 130
    except Exception as exc:
        log.error("PhaseRunner failed: %s", exc, exc_info=True)
        write_status(
            repo_root,
            active=False,
            status="error",
            workspace_id=workspace_id,
            error=str(exc),
        )
        return 1

    write_status(
        repo_root,
        active=False,
        status=final_status,
        workspace_id=workspace_id,
        error=last_error,
    )
    return 0 if final_status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
