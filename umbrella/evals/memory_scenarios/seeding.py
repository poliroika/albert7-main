"""Apply scenario seed blocks onto a prepared repo."""

from pathlib import Path
from typing import Any

import yaml

from umbrella.evals.memory_scenarios.fixtures import fixture_core_dir, overlay_core_files
from umbrella.evals.memory_scenarios.models import ScenarioSeed
from umbrella.memory.paths import manager_core_root, workspace_core_root
from umbrella.memory.palace.facade import MemPalace


def _write_text_files(base: Path, files: dict[str, Any]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        if name == "bkb":
            continue
        (base / name).write_text(str(content), encoding="utf-8")


def _apply_bkb_block(core_root: Path, bkb_data: Any) -> None:
    from umbrella.memory.proactive.bkb import merge_bkb_rules
    core_root.mkdir(parents=True, exist_ok=True)
    bkb_path = core_root / "bkb.yaml"
    if isinstance(bkb_data, dict):
        rules = list(bkb_data.get("rules") or [])
        merge_bkb_rules(bkb_path, rules)
    elif isinstance(bkb_data, str):
        parsed = yaml.safe_load(bkb_data) or {}
        merge_bkb_rules(bkb_path, list(parsed.get("rules") or []))


def apply_seed(
    repo_root: Path,
    workspace_id: str,
    seed: ScenarioSeed,
    *,
    raw_seed: dict[str, Any] | None = None,
) -> None:
    mgr = manager_core_root(repo_root)
    if seed.manager_fixture:
        overlay_core_files(fixture_core_dir(seed.manager_fixture), mgr)
    _write_text_files(mgr, seed.manager_core)
    raw_mgr = (raw_seed or {}).get("manager_core") or {}
    if "bkb" in raw_mgr:
        _apply_bkb_block(mgr, raw_mgr["bkb"])

    ws_core = workspace_core_root(repo_root, workspace_id)
    if seed.workspace_fixture:
        overlay_core_files(fixture_core_dir(seed.workspace_fixture), ws_core)
    _write_text_files(ws_core, seed.workspace_core)
    raw_ws = (raw_seed or {}).get("workspace_core") or {}
    if "bkb" in raw_ws:
        _apply_bkb_block(ws_core, raw_ws["bkb"])

    for ws_name, fix_name in seed.extra_workspaces.items():
        overlay_core_files(
            fixture_core_dir(fix_name),
            workspace_core_root(repo_root, ws_name),
        )

    nodes = (seed.palace or {}).get("nodes") or []
    if nodes:
        palace = MemPalace(repo_root, workspace_id)
        try:
            for node in nodes:
                store = str(node.get("store") or "palace.lesson")
                title = str(node.get("title") or "seed")
                body = str(node.get("content") or "")
                scope = str(node.get("scope") or "cross_run_durable")
                palace.add(
                    store=store,
                    content=f"{title}\n\n{body}".strip(),
                    scope=scope,
                    tags=list(node.get("tags") or []),
                    verified=bool(node.get("verified")),
                    phase=str(node.get("phase") or "") or None,
                    run_id=str(node.get("run_id") or "") or None,
                    extra={"title": title},
                )
        finally:
            palace.close()


def seed_from_dict(repo_root: Path, workspace_id: str, raw: dict[str, Any]) -> None:
    seed = ScenarioSeed(
        manager_core={
            k: v for k, v in (raw.get("manager_core") or {}).items() if k != "bkb"
        },
        workspace_core={
            k: v for k, v in (raw.get("workspace_core") or {}).items() if k != "bkb"
        },
        palace=dict(raw.get("palace") or {}),
        workspace_fixture=str(raw.get("workspace_fixture") or "default"),
        manager_fixture=str(raw.get("manager_fixture") or "manager_default"),
        extra_workspaces={str(k): str(v) for k, v in (raw.get("extra_workspaces") or {}).items()},
    )
    apply_seed(repo_root, workspace_id, seed, raw_seed=raw)
