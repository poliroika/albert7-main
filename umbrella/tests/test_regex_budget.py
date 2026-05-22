from __future__ import annotations

from pathlib import Path


def test_contract_and_analysis_layers_do_not_add_semantic_regex():
    root = Path(__file__).resolve().parents[1]
    checked = [root / "contracts", root / "analysis"]
    offenders: list[str] = []
    for directory in checked:
        for path in directory.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "re.compile(" in text:
                offenders.append(path.relative_to(root).as_posix())

    assert offenders == []


def test_legacy_semantic_regex_modules_are_removed():
    root = Path(__file__).resolve().parents[1]
    removed = [
        "deep_agent_tools/evidence_graph.py",
        "deep_agent_tools/phase_contract_success.py",
        "deep_agent_tools/phase_contract_paths.py",
        "deep_agent_tools/phase_contract_revisions.py",
        "deep_agent_tools/phase_control_completion.py",
        "deep_agent_tools/phase_control_review.py",
    ]
    assert [path for path in removed if (root / path).exists()] == []


def test_active_phase_modules_do_not_expose_success_test_regex_gates():
    root = Path(__file__).resolve().parents[1]
    checked = [
        root / "orchestrator" / "runner.py",
        root / "deep_agent_tools" / "phase_contract_tools.py",
        root / "deep_agent_tools" / "phase_control_tools.py",
        root / "deep_agent_tools" / "phase_control_common.py",
    ]
    forbidden = (
        "_SUCCESS_TEST",
        "_PLAN_REVIEW",
        "_RESEARCH_SUMMARY_PLACEHOLDER",
        "_phase_plan_policy_issues",
    )
    offenders = [
        path.relative_to(root).as_posix()
        for path in checked
        if any(token in path.read_text(encoding="utf-8", errors="replace") for token in forbidden)
    ]
    assert offenders == []
