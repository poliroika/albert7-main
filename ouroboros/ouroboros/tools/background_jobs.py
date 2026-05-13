"""Detached background-process helpers for Ouroboros workspace tools.

Servers (uvicorn, fastapi, vllm, mlflow, ...) are blocking by design: the
existing ``run_workspace_command`` waits for the process to exit and on
Windows the ``OneShotBackend`` cannot back jobs with a real shell, so any
``uvicorn.run(...)`` call burns the per-tool wall-clock budget and leaves
a zombie listener bound to the port. ``news_cards_ai`` hit exactly that.

This module owns a tiny on-disk job registry under
``<drive_root>/state/bg_jobs/`` and exposes simple primitives that the tool
layer wires up (`bg_start`, `bg_status`, `bg_tail`, `bg_kill`).

Each job has:
  * stable ``job_id`` (used by tail/kill)
  * detached OS process (CREATE_NEW_PROCESS_GROUP on Windows /
    ``start_new_session`` on POSIX) so termination kills the whole tree
  * dedicated log file ``<drive_root>/logs/bg/<job_id>.log`` capturing
    stdout+stderr (combined, line-buffered)
  * JSON manifest beside the log so the agent can resume across rounds
    even if the in-memory registry is lost (e.g. process restart).

We deliberately do NOT use the persistent terminal session for this: shell
``&`` works on POSIX but is unreliable on Windows OneShotBackend, and we
also need the model to address the job by id from any later round.
"""

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from collections.abc import Sequence

log = logging.getLogger(__name__)


@dataclass
class BackgroundJob:
    job_id: str
    pid: int
    argv: list[str]
    cwd: str
    log_path: str
    started_at: float
    label: str = ""
    env_overrides: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _registry_root(drive_root: Path) -> Path:
    root = Path(drive_root) / "state" / "bg_jobs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _logs_root(drive_root: Path) -> Path:
    root = Path(drive_root) / "logs" / "bg"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _manifest_path(drive_root: Path, job_id: str) -> Path:
    return _registry_root(drive_root) / f"{job_id}.json"


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or ""))[:32]
    return cleaned.strip("_") or "job"


def start_background(
    drive_root: Path,
    *,
    argv: Sequence[str],
    cwd: Path,
    label: str = "",
    env_overrides: dict[str, str] | None = None,
) -> BackgroundJob:
    """Spawn ``argv`` detached from the parent and return the job record."""
    if not argv:
        raise ValueError("argv must be non-empty")

    job_id = f"{int(time.time())}-{_safe_label(label)}-{secrets.token_hex(3)}"
    log_path = _logs_root(drive_root) / f"{job_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("ab", buffering=0)

    env = os.environ.copy()
    if env_overrides:
        for key, value in env_overrides.items():
            env[str(key)] = str(value)

    popen_kwargs: dict[str, Any] = {
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(cwd),
        "env": env,
        "close_fds": True,
    }
    if os.name == "nt":
        DETACHED = 0x00000008
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | DETACHED
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(list(argv), **popen_kwargs)
    finally:
        # Once Popen has dup'd the handle, our reference is no longer needed.
        # Closing prevents accidental retention by the parent process.
        try:
            log_fh.close()
        except Exception:
            log.debug("background log fh close failed", exc_info=True)

    record = BackgroundJob(
        job_id=job_id,
        pid=proc.pid,
        argv=list(argv),
        cwd=str(cwd),
        log_path=str(log_path),
        started_at=time.time(),
        label=str(label or ""),
        env_overrides=dict(env_overrides or {}),
    )
    _manifest_path(drive_root, job_id).write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("background job started job_id=%s pid=%s argv=%s", job_id, proc.pid, argv)
    return record


def load_job(drive_root: Path, job_id: str) -> BackgroundJob | None:
    path = _manifest_path(drive_root, job_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BackgroundJob(**data)
    except Exception:
        log.debug("failed to load bg manifest %s", path, exc_info=True)
        return None


def list_jobs(drive_root: Path) -> list[BackgroundJob]:
    jobs: list[BackgroundJob] = []
    for path in sorted(_registry_root(drive_root).glob("*.json")):
        try:
            jobs.append(BackgroundJob(**json.loads(path.read_text(encoding="utf-8"))))
        except Exception:
            log.debug("skip unparseable bg manifest %s", path, exc_info=True)
    return jobs


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            cp = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return str(pid) in (cp.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(drive_root: Path, job_id: str) -> dict[str, Any]:
    job = load_job(drive_root, job_id)
    if job is None:
        return {"job_id": job_id, "status": "unknown", "reason": "no manifest"}
    alive = is_alive(job.pid)
    log_size = 0
    try:
        log_size = Path(job.log_path).stat().st_size
    except Exception:
        pass
    return {
        "job_id": job.job_id,
        "pid": job.pid,
        "label": job.label,
        "status": "running" if alive else "exited",
        "started_at": job.started_at,
        "uptime_seconds": round(max(0.0, time.time() - job.started_at), 3),
        "log_path": job.log_path,
        "log_bytes": log_size,
        "argv": job.argv,
        "cwd": job.cwd,
    }


def tail(drive_root: Path, job_id: str, *, lines: int = 200) -> dict[str, Any]:
    job = load_job(drive_root, job_id)
    if job is None:
        return {"job_id": job_id, "error": "no manifest"}
    log_path = Path(job.log_path)
    if not log_path.exists():
        return {
            "job_id": job_id,
            "error": "log not yet written",
            "log_path": str(log_path),
        }
    n = max(1, min(int(lines), 5000))
    try:
        with log_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            buf = bytearray()
            block = 4096
            while size > 0 and buf.count(b"\n") <= n:
                read_size = min(block, size)
                size -= read_size
                f.seek(size)
                buf[:0] = f.read(read_size)
            text = buf.decode("utf-8", errors="replace")
    except Exception as e:
        return {"job_id": job_id, "error": f"read failed: {e}"}
    out_lines = text.splitlines()[-n:]
    return {
        "job_id": job_id,
        "pid": job.pid,
        "alive": is_alive(job.pid),
        "log_path": str(log_path),
        "lines_returned": len(out_lines),
        "tail": "\n".join(out_lines),
    }


def kill(
    drive_root: Path, job_id: str, *, remove_manifest: bool = True
) -> dict[str, Any]:
    job = load_job(drive_root, job_id)
    if job is None:
        return {"job_id": job_id, "status": "unknown", "reason": "no manifest"}
    pid = job.pid
    killed = False
    if is_alive(pid):
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
                killed = True
            except Exception:
                log.warning("taskkill failed for pid=%s", pid, exc_info=True)
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
                time.sleep(0.5)
                if is_alive(pid):
                    os.killpg(pid, signal.SIGKILL)
                killed = True
            except Exception:
                log.warning("killpg failed for pid=%s", pid, exc_info=True)
    else:
        killed = True
    if remove_manifest:
        try:
            _manifest_path(drive_root, job_id).unlink(missing_ok=True)
        except Exception:
            log.debug("manifest unlink failed", exc_info=True)
    return {
        "job_id": job_id,
        "pid": pid,
        "killed": killed,
        "alive_after": is_alive(pid),
    }


def shutdown_all(drive_root: Path) -> list[dict[str, Any]]:
    """Best-effort kill of every registered job. Used during Ouroboros teardown."""
    out: list[dict[str, Any]] = []
    for job in list_jobs(drive_root):
        out.append(kill(drive_root, job.job_id, remove_manifest=True))
    return out


__all__ = [
    "BackgroundJob",
    "start_background",
    "list_jobs",
    "load_job",
    "is_alive",
    "status",
    "tail",
    "kill",
    "shutdown_all",
]
