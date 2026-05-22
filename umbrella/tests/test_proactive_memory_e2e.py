"""E2E behavioral tests: core memory shapes first prompt content."""

from pathlib import Path

import pytest
import yaml

from umbrella.memory.proactive.compiler import ProactiveMemoryCompiler
from umbrella.memory.paths import manager_core_root, workspace_core_root
from umbrella.orchestrator.worker import render_phase_user_prompt
from umbrella.phases.loader import load_manifest
from umbrella.memory.palace.recall import RecallBundle


@pytest.fixture
def research_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    core = tmp_path / ".umbrella" / "memory" / "core"
    core.mkdir(parents=True)
    (core / "00_identity.md").write_text("# Identity\n", encoding="utf-8")
    (core / "bkb.yaml").write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {
                        "id": "bkb_research_provenance",
                        "title": "Do not mark candidate research as verified",
                        "scope": "manager",
                        "type": "anti_pattern",
                        "status": "active",
                        "trust": "verified",
                        "strength": 0.95,
                        "applies_to": {"phases": ["research"], "workspaces": ["*"], "agents": ["*"]},
                        "rule": {
                            "trigger": "research phase start",
                            "forbidden": "mark candidate research as verified",
                            "behavior": "require provenance source_id before any palace_search",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ws = "demo_ws"
    ws_core = workspace_core_root(tmp_path, ws)
    ws_core.mkdir(parents=True)
    (ws_core / "00_workspace_charter.md").write_text("# Charter\n", encoding="utf-8")
    (ws_core / "30_workspace_antipatterns.md").write_text(
        "- Never repeat the forbidden deploy-without-tests pattern.\n",
        encoding="utf-8",
    )
    (ws_core / "bkb.yaml").write_text("rules: []\n", encoding="utf-8")
    return tmp_path, ws


def test_e2e1_research_provenance_before_archive(research_repo):
    repo, ws = research_repo
    manifest = load_manifest(Path("umbrella/phases/manifests/research.yaml"))
    overlay = ProactiveMemoryCompiler().build_overlay(
        repo_root=repo,
        workspace_id=ws,
        run_id="run-e2e",
        phase_id="research",
        subtask_id=None,
        task_brief="investigate API",
        manifest=manifest,
    )
    prompt = render_phase_user_prompt(
        manifest,
        RecallBundle(),
        proactive_overlay=overlay,
    )
    memory_pos = prompt.find("[ALWAYS-LOADED MEMORY]")
    search_pos = prompt.find("palace_search")
    assert memory_pos >= 0
    assert "provenance" in prompt.lower() or "verified" in prompt.lower()
    if search_pos >= 0:
        assert memory_pos < search_pos


def test_e2e2_execute_forbidden_repeat_before_supplemental(research_repo):
    repo, ws = research_repo
    manifest = load_manifest(Path("umbrella/phases/manifests/execute.yaml"))
    overlay = ProactiveMemoryCompiler().build_overlay(
        repo_root=repo,
        workspace_id=ws,
        run_id="run-e2e",
        phase_id="execute",
        subtask_id=None,
        task_brief="implement feature",
        manifest=manifest,
    )
    bundle = RecallBundle()
    bundle.warm = [{"id": "w1", "content": "old warm recall hit"}]
    prompt = render_phase_user_prompt(
        manifest,
        bundle,
        proactive_overlay=overlay,
    )
    assert "forbidden" in prompt.lower() or "deploy-without-tests" in prompt.lower()
    supplemental_pos = prompt.find("Supplemental palace recall")
    always_pos = prompt.find("[ALWAYS-LOADED MEMORY]")
    if supplemental_pos >= 0 and always_pos >= 0:
        assert always_pos < supplemental_pos


def test_e2e3_candidate_not_in_always_loaded(research_repo):
    repo, _ws = research_repo
    bkb_path = manager_core_root(repo) / "bkb.yaml"
    data = yaml.safe_load(bkb_path.read_text(encoding="utf-8"))
    data["rules"].append(
        {
            "id": "bkb_candidate_only",
            "title": "Unverified reflexion candidate",
            "scope": "manager",
            "type": "behavior",
            "status": "candidate",
            "trust": "candidate",
            "rule": {"behavior": "inject me please"},
        }
    )
    bkb_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    overlay = ProactiveMemoryCompiler().build_overlay(
        repo_root=repo,
        workspace_id="",
        run_id="r",
        phase_id="reflexion",
        subtask_id=None,
        task_brief="",
        manifest=load_manifest(Path("umbrella/phases/manifests/reflexion.yaml")),
    )
    md = overlay.render_markdown()
    assert "inject me please" not in md
