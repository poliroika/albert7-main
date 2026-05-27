"""Platform BKB rules merge into manager core on overlay compile."""

from pathlib import Path

from umbrella.memory.proactive.bkb import (
    ensure_platform_bkb_rules,
    load_bkb_rules,
    merge_bkb_rules,
)
from umbrella.memory.proactive.compiler import ProactiveMemoryCompiler


def test_ensure_platform_bkb_rules_merges_by_id(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir()
    (core / "bkb.yaml").write_text(
        "rules:\n  - id: custom_rule\n    title: Custom\n    scope: manager\n"
        "    type: behavior\n    status: active\n    trust: verified\n"
        "    applies_to:\n      phases: ['*']\n      workspaces: ['*']\n"
        "      agents: ['*']\n    rule:\n      behavior: keep me\n",
        encoding="utf-8",
    )

    ensure_platform_bkb_rules(core)
    rules = load_bkb_rules(core / "bkb.yaml")
    ids = {rule.id for rule in rules}
    assert "custom_rule" in ids
    assert "platform_diff_hash_exit_context" in ids
    assert "platform_loop_back_superseded" in ids
    assert "platform_revise_review_superseded_on_passing_completion" in ids


def test_merge_bkb_rules_updates_existing_rule_by_id(tmp_path: Path) -> None:
    bkb_path = tmp_path / "core" / "bkb.yaml"
    bkb_path.parent.mkdir()
    bkb_path.write_text(
        "rules:\n"
        "  - id: custom_rule\n"
        "    title: Old\n"
        "    scope: manager\n"
        "    type: behavior\n"
        "    status: active\n"
        "    trust: verified\n"
        "    rule:\n"
        "      behavior: old behavior\n",
        encoding="utf-8",
    )

    merge_bkb_rules(
        bkb_path,
        [
            {
                "id": "custom_rule",
                "title": "New",
                "scope": "manager",
                "type": "behavior",
                "status": "active",
                "trust": "verified",
                "rule": {"behavior": "new behavior"},
            },
            {
                "id": "second_rule",
                "title": "Second",
                "scope": "workspace",
                "type": "behavior",
                "status": "active",
                "trust": "verified",
                "rule": {"behavior": "second behavior"},
            },
        ],
    )

    rules = {rule.id: rule for rule in load_bkb_rules(bkb_path)}
    assert rules["custom_rule"].title == "New"
    assert rules["custom_rule"].rule["behavior"] == "new behavior"
    assert rules["second_rule"].rule["behavior"] == "second behavior"


def test_proactive_compiler_calls_platform_bkb_merge(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".umbrella" / "memory" / "core").mkdir(parents=True)
    compiler = ProactiveMemoryCompiler()
    overlay = compiler.build_overlay(
        repo_root=repo,
        workspace_id="demo",
        run_id="run-1",
        phase_id="execute",
        subtask_id=None,
        task_brief="brief",
        manifest=type("M", (), {"budgets": type("B", (), {"max_tokens": 8000})()})(),
    )
    assert overlay is not None
    rules = load_bkb_rules(repo / ".umbrella" / "memory" / "core" / "bkb.yaml")
    assert any(rule.id == "platform_diff_hash_exit_context" for rule in rules)
