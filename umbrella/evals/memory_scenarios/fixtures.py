"""Repo/workspace preparation for memory scenarios."""

import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKSPACES_TEST_SRC = REPO_ROOT / "workspaces" / "test"
FIXTURES_ROOT = REPO_ROOT / "umbrella" / "tests" / "fixtures" / "memory_scenarios"
SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
EXPECTED_ROOT = FIXTURES_ROOT / "expected"


def manifest_path(manifest_name: str) -> Path:
    return REPO_ROOT / "umbrella" / "phases" / "manifests" / f"{manifest_name}.yaml"


def fixture_core_dir(name: str) -> Path:
    return FIXTURES_ROOT / "core" / name


def overlay_core_files(src: Path, dst: Path) -> None:
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, dst / item.name)


def apply_default_workspace_memory(repo_root: Path, workspace_id: str = "test") -> None:
    """Overlay committed default workspace core fixture (for conftest / live tests)."""
    ws_core = repo_root / "workspaces" / workspace_id / ".memory" / "core"
    overlay_core_files(fixture_core_dir("default"), ws_core)


def ensure_manager_core(repo_root: Path, fixture_name: str = "manager_default") -> None:
    core = repo_root / ".umbrella" / "memory" / "core"
    overlay_core_files(fixture_core_dir(fixture_name), core)
    if not (core / "10_operating_principles.md").is_file():
        (core / "10_operating_principles.md").write_text(
            "# Principles\nUse typed evidence refs.\n",
            encoding="utf-8",
        )


def prepare_scenario_repo(
    tmp_root: Path,
    *,
    workspace_id: str = "test",
    workspace_fixture: str = "default",
    manager_fixture: str = "manager_default",
    extra_workspaces: dict[str, str] | None = None,
) -> Path:
    """Copy workspaces/test seed and overlay memory fixtures into tmp_root."""
    assert WORKSPACES_TEST_SRC.is_dir(), f"missing seed workspace: {WORKSPACES_TEST_SRC}"
    (tmp_root / "umbrella").mkdir(parents=True, exist_ok=True)
    dst_ws = tmp_root / "workspaces" / workspace_id
    if dst_ws.exists():
        shutil.rmtree(dst_ws)
    shutil.copytree(WORKSPACES_TEST_SRC, dst_ws)
    overlay_core_files(fixture_core_dir(workspace_fixture), dst_ws / ".memory" / "core")
    ensure_manager_core(tmp_root, manager_fixture)
    for ws_name, fix_name in (extra_workspaces or {}).items():
        ws_path = tmp_root / "workspaces" / ws_name
        if not ws_path.is_dir():
            shutil.copytree(WORKSPACES_TEST_SRC, ws_path)
        overlay_core_files(fixture_core_dir(fix_name), ws_path / ".memory" / "core")
    return tmp_root


def drive_root(repo: Path, workspace_id: str) -> Path:
    drive = repo / "workspaces" / workspace_id / ".memory" / "drive"
    drive.mkdir(parents=True, exist_ok=True)
    (drive / "logs").mkdir(parents=True, exist_ok=True)
    (drive / "state").mkdir(parents=True, exist_ok=True)
    return drive
