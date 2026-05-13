"""
Prompt versioning helpers.

Prompt rewrites need lightweight auditability and rollback references, so this
module snapshots prompt surfaces into a control-plane-owned history.
"""

import hashlib
import json
from pathlib import Path

from umbrella.control_plane.models import (
    PromptSurface,
    PromptVersionRecord,
    generate_prompt_version_id,
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_surface_text(surface: PromptSurface, repo_root: Path | None) -> str:
    if repo_root is None:
        path = surface.path
    else:
        path = (repo_root / surface.path).resolve()
    return path.read_text(encoding="utf-8")


def record_prompt_version(
    surface: PromptSurface,
    version_dir: Path,
    repo_root: Path | None = None,
    *,
    task_id: str | None = None,
    label: str = "snapshot",
    content: str | None = None,
) -> PromptVersionRecord:
    """Record a prompt-surface snapshot and return the audit record."""
    version_id = generate_prompt_version_id()
    snapshot_root = version_dir / surface.id
    snapshot_root.mkdir(parents=True, exist_ok=True)

    text = content if content is not None else _read_surface_text(surface, repo_root)
    suffix = surface.path.suffix or ".txt"
    snapshot_path = snapshot_root / f"{version_id}{suffix}"
    snapshot_path.write_text(text, encoding="utf-8")

    record = PromptVersionRecord(
        id=version_id,
        task_id=task_id,
        surface_id=surface.id,
        surface_path=surface.path,
        content_hash=_sha256_text(text),
        snapshot_path=snapshot_path.resolve(),
        label=label,
    )

    metadata_path = snapshot_root / f"{version_id}.json"
    metadata_path.write_text(
        json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return record


def load_prompt_version(
    version_id: str, version_dir: Path, surface_id: str | None = None
) -> PromptVersionRecord:
    """Load a recorded prompt version from disk."""
    candidate_paths: list[Path] = []
    if surface_id is not None:
        candidate_paths.append(version_dir / surface_id / f"{version_id}.json")
    else:
        candidate_paths.extend(version_dir.glob(f"*/{version_id}.json"))

    for path in candidate_paths:
        if path.exists():
            return PromptVersionRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )

    raise FileNotFoundError(f"Prompt version {version_id} not found in {version_dir}")


class PromptVersionStore:
    """Small convenience wrapper around version recording and lookup."""

    def __init__(self, version_dir: Path, repo_root: Path | None = None):
        self.version_dir = version_dir
        self.repo_root = repo_root

    def record(
        self,
        surface: PromptSurface,
        *,
        task_id: str | None = None,
        label: str = "snapshot",
        content: str | None = None,
    ) -> PromptVersionRecord:
        return record_prompt_version(
            surface,
            self.version_dir,
            repo_root=self.repo_root,
            task_id=task_id,
            label=label,
            content=content,
        )

    def load(
        self, version_id: str, surface_id: str | None = None
    ) -> PromptVersionRecord:
        return load_prompt_version(version_id, self.version_dir, surface_id=surface_id)
