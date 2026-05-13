"""Shared task text resolution for CLI and web launches."""

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TaskInputResolution:
    task_text: str
    task_source: str
    task_file: Path | None = None
    task_hash: str = ""
    task_missing: bool = False
    missing_status: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.task_missing and bool(self.task_text.strip())

    def metadata(self) -> dict[str, object]:
        return {
            "task_source": self.task_source,
            "task_hash": self.task_hash,
            "task_missing": self.task_missing,
            "task_file": str(self.task_file) if self.task_file else "",
            "missing_status": self.missing_status,
        }


def _hash_text(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_task_text(
    workspace_path: Path,
    explicit_task_text: str | None = None,
    *,
    task_file_name: str = "TASK_MAIN.md",
) -> TaskInputResolution:
    """Resolve the task body without ever silently returning an empty task.

    ``explicit_task_text`` wins when non-empty. Otherwise the workspace's
    canonical task file is required. Missing or blank ``TASK_MAIN.md`` is a
    typed pre-LLM stop condition so web and CLI callers do not send an empty
    ``## Task`` section into Ouroboros.
    """

    workspace_path = Path(workspace_path).resolve()
    explicit = (explicit_task_text or "").strip()
    if explicit:
        return TaskInputResolution(
            task_text=explicit,
            task_source="explicit",
            task_hash=_hash_text(explicit),
        )

    task_file = workspace_path / task_file_name
    if not task_file.exists() or not task_file.is_file():
        return TaskInputResolution(
            task_text="",
            task_source=task_file_name,
            task_file=task_file,
            task_missing=True,
            missing_status="missing_task_main",
            error=f"{task_file_name} is missing in {workspace_path}",
        )

    try:
        text = task_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return TaskInputResolution(
            task_text="",
            task_source=task_file_name,
            task_file=task_file,
            task_missing=True,
            missing_status="missing_task_main",
            error=f"Could not read {task_file_name}: {exc}",
        )

    if not text.strip():
        return TaskInputResolution(
            task_text="",
            task_source=task_file_name,
            task_file=task_file,
            task_missing=True,
            missing_status="missing_task_main",
            error=f"{task_file_name} is empty in {workspace_path}",
        )

    return TaskInputResolution(
        task_text=text.strip(),
        task_source=task_file_name,
        task_file=task_file,
        task_hash=_hash_text(text.strip()),
    )
