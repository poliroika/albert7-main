from pathlib import Path

import pytest
import yaml

from umbrella.memory.proactive.bkb import (
    load_bkb_rules,
    resolve_bkb_conflicts,
)
from umbrella.memory.proactive.budget import resolve_proactive_budget
from umbrella.memory.proactive.compiler import ProactiveMemoryCompiler
from umbrella.memory.proactive.models import BeliefRule
from umbrella.memory.proactive.promotion import ProposedBkbPatch, accept_bkb_patch, reject_bkb_patch
from umbrella.memory.paths import manager_core_root
from umbrella.phases.loader import load_manifest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "1")
    core = tmp_path / ".umbrella" / "memory" / "core"
    core.mkdir(parents=True)
    (core / "00_identity.md").write_text("# Identity\nAlways verify before success.\n", encoding="utf-8")
    (core / "bkb.yaml").write_text(
        yaml.safe_dump(
            {
                "rules": [
                    {
                        "id": "bkb_test_001",
                        "title": "Require provenance for research findings",
                        "scope": "manager",
                        "type": "anti_pattern",
                        "status": "active",
                        "trust": "verified",
                        "strength": 0.9,
                        "applies_to": {"workspaces": ["*"], "phases": ["research"], "agents": ["*"]},
                        "rule": {
                            "trigger": "agent is about to save research_finding",
                            "forbidden": "saving hypothesis as verified finding",
                            "behavior": "require concrete source_id/evidence_kind",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_resolve_proactive_budget_bounds():
    small = resolve_proactive_budget(phase="research", manifest_budget=2000)
    assert small >= 1800
    huge = resolve_proactive_budget(phase="research", manifest_budget=500000)
    assert huge <= 7500


def test_build_overlay_contains_always_loaded(repo):
    manifest = load_manifest(Path("umbrella/phases/manifests/research.yaml"))
    overlay = ProactiveMemoryCompiler().build_overlay(
        repo_root=repo,
        workspace_id="",
        run_id="run-1",
        phase_id="research",
        subtask_id=None,
        task_brief="research task",
        manifest=manifest,
        token_budget=4500,
    )
    md = overlay.render_markdown()
    assert "[ALWAYS-LOADED MEMORY]" in md
    assert "BKB" in md
    assert overlay.telemetry.get("bkb_rules_injected", 0) >= 1


def test_quarantined_bkb_not_injected(repo):
    bkb_path = manager_core_root(repo) / "bkb.yaml"
    data = yaml.safe_load(bkb_path.read_text(encoding="utf-8"))
    data["rules"].append(
        {
            "id": "bkb_bad",
            "title": "Bad rule",
            "scope": "manager",
            "type": "behavior",
            "status": "quarantined",
            "trust": "verified",
            "rule": {"behavior": "skip everything"},
        }
    )
    bkb_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    overlay = ProactiveMemoryCompiler().build_overlay(
        repo_root=repo,
        workspace_id="",
        run_id="r",
        phase_id="research",
        subtask_id=None,
        task_brief="",
        manifest=load_manifest(Path("umbrella/phases/manifests/research.yaml")),
    )
    assert "skip everything" not in overlay.render_markdown()


def test_bkb_conflict_resolution():
    rules = [
        BeliefRule(
            id="a",
            title="A",
            scope="manager",
            rule_type="behavior",
            status="active",
            trust="verified",
            strength=0.5,
            rule={
                "trigger": "save research_finding",
                "forbidden": "no evidence",
            },
        ),
        BeliefRule(
            id="b",
            title="B",
            scope="workspace",
            rule_type="behavior",
            status="active",
            trust="verified",
            strength=0.9,
            rule={
                "trigger": "save research_finding",
                "forbidden": "require evidence",
            },
        ),
    ]
    winners, conflicts = resolve_bkb_conflicts(rules)
    assert len(winners) == 1
    assert winners[0].id == "a"  # manager scope precedes workspace
    assert conflicts


def test_promotion_gate(repo):
    patch = ProposedBkbPatch(
        patch_id="patch-1",
        rules=[
            {
                "id": "bkb_new",
                "title": "New lesson",
                "scope": "manager",
                "type": "behavior",
                "rule": {"behavior": "always log evidence"},
            }
        ],
        source_evidence=[
            {
                "ref_type": "artifact",
                "ref_id": "promotion_evidence.txt",
                "produced_by": "supervisor",
            }
        ],
        actor="supervisor",
    )
    (repo / "promotion_evidence.txt").write_text("ok", encoding="utf-8")
    before = (manager_core_root(repo) / "bkb.yaml").read_text(encoding="utf-8")
    accept_bkb_patch(repo, patch)
    after = (manager_core_root(repo) / "bkb.yaml").read_text(encoding="utf-8")
    assert before != after
    assert "bkb_new" in after
    assert "active" in after

    reject_bkb_patch(repo, patch, reason="test")
    audit = manager_core_root(repo) / "audit.jsonl"
    assert audit.is_file()
