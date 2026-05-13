"""Background Ouroboros launcher for the web chat (subprocess + run registry)."""

import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from collections.abc import Callable

from umbrella.web_bridge.util import iso_utc, load_store, now_ts, save_store, store_path


_LAUNCHER_STORE = "launcher_runs.json"


def _runs_path() -> Path:
    return store_path(_LAUNCHER_STORE)


def load_launcher_runs() -> list[dict[str, Any]]:
    return list(load_store(_LAUNCHER_STORE, []))


def save_launcher_runs(runs: list[dict[str, Any]]) -> None:
    save_store(_LAUNCHER_STORE, runs)


def _update_run(run_id: str, patch: dict[str, Any]) -> None:
    runs = load_launcher_runs()
    changed = False
    for i, r in enumerate(runs):
        if r.get("id") == run_id:
            runs[i] = {**r, **patch, "updated_at": iso_utc(now_ts())}
            changed = True
            break
    if changed:
        save_launcher_runs(runs)


def _append_run(rec: dict[str, Any]) -> None:
    runs = load_launcher_runs()
    runs.insert(0, rec)
    save_launcher_runs(runs)


def start_ouroboros_subprocess(
    *,
    repo_root: Path,
    workspace_rel: str,
    task_text: str,
    model: str | None,
    on_exit: Callable[[str, int | None], None] | None = None,
) -> tuple[str, dict[str, Any], subprocess.Popen[Any]]:
    """
    Spawn ``python -m umbrella.app_ouroboros`` in the background.

    Returns (run_id, initial_run_record, Popen).
    """
    run_id = f"ui_run_{uuid.uuid4().hex[:12]}"
    log_dir = store_path("launcher_logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run_id}.log"

    env = os.environ.copy()
    if model:
        env["LLM_MODEL"] = str(model)
        env["OUROBOROS_MODEL"] = str(model)

    cmd = [
        sys.executable,
        "-m",
        "umbrella.app_ouroboros",
        workspace_rel,
        "--task",
        task_text,
        "--live",
        "--no-dashboard",
        "--max-verify-retries",
        os.environ.get("UMBRELLA_WEB_CHAT_MAX_VERIFY_RETRIES", "2"),
    ]
    if os.environ.get("UMBRELLA_WEB_CHAT_VERBOSE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        cmd.append("--verbose")

    ts = iso_utc(now_ts())
    log_fh = log_path.open("w", encoding="utf-8")
    log_fh.write(f"# umbrella.app_ouroboros log for {run_id}\n# cmd: {cmd!r}\n\n")
    log_fh.flush()

    popen = subprocess.Popen(
        cmd,
        cwd=str(repo_root),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    rec: dict[str, Any] = {
        "id": run_id,
        "workspace_id": Path(workspace_rel).name,
        "status": "running",
        "model": model,
        "pid": popen.pid,
        "exit_code": None,
        "log_path": str(log_path.relative_to(repo_root)),
        "task_preview": task_text[:400],
        "created_at": ts,
        "updated_at": ts,
    }
    _append_run(rec)

    def _watch() -> None:
        try:
            exit_code = popen.wait()
        except Exception:
            exit_code = -1
        finally:
            try:
                log_fh.close()
            except Exception:
                pass
        status = "completed" if exit_code == 0 else "failed"
        _update_run(
            run_id,
            {"status": status, "exit_code": exit_code, "updated_at": iso_utc(now_ts())},
        )
        if on_exit:
            try:
                on_exit(run_id, exit_code)
            except Exception:
                pass

    threading.Thread(
        target=_watch, name=f"ouroboros-watch-{run_id}", daemon=True
    ).start()
    return run_id, rec, popen


def dry_run_stub_response(
    *,
    run_id: str,
    workspace_id: str,
    model: str | None,
    task_preview: str,
) -> dict[str, Any]:
    """Test-only fake run row (no subprocess)."""
    ts = iso_utc(now_ts())
    rec = {
        "id": run_id,
        "workspace_id": workspace_id,
        "status": "completed",
        "model": model,
        "pid": None,
        "exit_code": 0,
        "log_path": "",
        "task_preview": task_preview[:400],
        "created_at": ts,
        "updated_at": ts,
    }
    _append_run(rec)
    return rec
