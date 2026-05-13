from pathlib import Path

from umbrella.artifacts.manifests import build_run_manifest
from umbrella.artifacts.models import RunManifest, RunStatus, WorkspaceRunIndex


def _run_dirs(workspace_root: Path) -> list[Path]:
    runs_dir = workspace_root / "runs"
    if not runs_dir.exists():
        return []
    return sorted(
        [p for p in runs_dir.iterdir() if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )


def index_workspace_runs(
    workspace_root: Path, workspace_id: str | None = None
) -> WorkspaceRunIndex:
    resolved_id = workspace_id or workspace_root.name
    runs: list[RunManifest] = []
    for run_dir in _run_dirs(workspace_root):
        runs.append(build_run_manifest(run_dir, resolved_id))
    return WorkspaceRunIndex(workspace_id=resolved_id, runs=runs)


def get_run_by_id(
    workspace_root: Path, run_id: str, workspace_id: str | None = None
) -> RunManifest | None:
    resolved_id = workspace_id or workspace_root.name
    run_dir = workspace_root / "runs" / run_id
    if not run_dir.exists():
        return None
    return build_run_manifest(run_dir, resolved_id)


def get_latest_run(
    workspace_root: Path,
    workspace_id: str | None = None,
    *,
    successful_only: bool = False,
) -> RunManifest | None:
    idx = index_workspace_runs(workspace_root, workspace_id)
    if not successful_only:
        return idx.latest_run
    for run in idx.runs:
        if run.status == RunStatus.COMPLETED:
            return run
    return None
