"""Web Bridge API helpers for Memory Scenario Harness / Memory Lab."""

import json
import threading
from pathlib import Path
from typing import Any

from umbrella.evals.memory_scenarios.fixtures import REPO_ROOT
from umbrella.evals.memory_scenarios.reports import build_dashboard
from umbrella.evals.memory_scenarios.runner import run_all_scenarios, run_scenario_by_id
from umbrella.evals.memory_scenarios.scenario_loader import list_scenario_paths, load_scenario

_run_lock = threading.Lock()
_last_run: dict[str, Any] = {}


def _report_root(repo_root: Path) -> Path:
    return repo_root / ".mrt" / "memory_scenarios"


def list_scenarios(repo_root: Path | None = None) -> dict[str, Any]:
    root = (repo_root or REPO_ROOT).resolve()
    items = []
    for path in list_scenario_paths():
        sc = load_scenario(path)
        items.append(
            {
                "id": sc.id,
                "file": path.name,
                "description": sc.description.strip(),
                "mode": sc.mode,
            }
        )
    latest = _report_root(root) / "latest" / "dashboard.json"
    dashboard = {}
    if latest.is_file():
        dashboard = json.loads(latest.read_text(encoding="utf-8"))
    return {"scenarios": items, "last_dashboard": dashboard}


def run_scenarios(
    repo_root: Path | None = None,
    *,
    scenario_id: str | None = None,
    run_all: bool = False,
) -> dict[str, Any]:
    root = (repo_root or REPO_ROOT).resolve()
    report_dir = _report_root(root)
    with _run_lock:
        if run_all:
            result = run_all_scenarios(report_root=report_dir)
            payload = {
                "ok": result.ok,
                "dashboard": result.dashboard,
                "reports_dir": str(report_dir),
            }
        elif scenario_id:
            result = run_scenario_by_id(scenario_id, report_root=report_dir / scenario_id)
            payload = {
                "ok": result.ok,
                "scenario_id": result.scenario_id,
                "failures": result.invariant_failures,
                "report_dir": str(result.report_dir),
            }
        else:
            return {"ok": False, "error": "scenario_id or all=true required"}
        global _last_run
        _last_run = payload
        return payload


def latest_dashboard(repo_root: Path | None = None) -> dict[str, Any]:
    root = (repo_root or REPO_ROOT).resolve()
    path = _report_root(root) / "latest" / "dashboard.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return build_dashboard([], _report_root(root))


def scenario_report(repo_root: Path | None, scenario_id: str) -> dict[str, Any]:
    root = (repo_root or REPO_ROOT).resolve()
    report_dir = _report_root(root) / scenario_id
    result_path = report_dir / "result.json"
    report_path = report_dir / "report.md"
    out: dict[str, Any] = {"scenario_id": scenario_id, "report_dir": str(report_dir)}
    if result_path.is_file():
        out["result"] = json.loads(result_path.read_text(encoding="utf-8"))
    if report_path.is_file():
        out["report_md"] = report_path.read_text(encoding="utf-8")
    prompts = sorted(report_dir.glob("prompt_*.txt"))
    out["prompts"] = [
        {"step_id": p.stem.replace("prompt_", ""), "path": str(p)} for p in prompts
    ]
    return out
