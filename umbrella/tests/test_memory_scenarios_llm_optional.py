"""Opt-in real LLM behavioral checks for memory scenarios."""

import json
import os
from pathlib import Path

import pytest

from umbrella.evals.memory_scenarios.fixtures import REPO_ROOT
from umbrella.evals.memory_scenarios.llm_judge import judge_memory_behavior
from umbrella.evals.memory_scenarios.runner import MemoryScenarioRunner
from umbrella.evals.memory_scenarios.scenario_loader import load_scenario

pytestmark = pytest.mark.memory_llm_real

LLM_SCENARIOS_DIR = (
    REPO_ROOT / "umbrella" / "evals" / "memory_scenarios" / "scenarios" / "llm"
)


def _enabled() -> bool:
    return os.environ.get("UMBRELLA_MEMORY_LLM_REAL_TESTS", "").strip() == "1"


@pytest.mark.skipif(not _enabled(), reason="Set UMBRELLA_MEMORY_LLM_REAL_TESTS=1")
@pytest.mark.parametrize(
    "yaml_name",
    [
        "01_research_provenance.yaml",
        "02_execute_antipattern.yaml",
        "03_verify_no_research_bkb.yaml",
    ],
)
def test_memory_scenario_llm_behavior(tmp_path, yaml_name: str) -> None:
    path = LLM_SCENARIOS_DIR / yaml_name
    scenario = load_scenario(path)
    runner = MemoryScenarioRunner(report_root=tmp_path / "llm_reports")
    result = runner.run(scenario)
    assert result.ok, result.summary_text

    llm_spec = scenario.llm
    step_id = str(llm_spec.get("step_id") or "")
    step = next((s for s in result.step_results if s.step_id == step_id), None)
    assert step and step.prompt, f"missing prompt for step {step_id}"

    verdict = judge_memory_behavior(
        phase_prompt=step.prompt,
        task_question=str(llm_spec.get("task_question") or ""),
        expect_provenance=bool(llm_spec.get("expect_provenance")),
        expect_antipattern=bool(llm_spec.get("expect_antipattern")),
        forbid_candidate_phrase=str(
            llm_spec.get("forbid_candidate_phrase") or "inject me please"
        ),
        forbid_research_rule=bool(llm_spec.get("forbid_research_rule")),
    )
    out_dir = result.report_dir / "llm"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "response.txt").write_text(verdict.raw_response, encoding="utf-8")
    (out_dir / "judge.json").write_text(
        json.dumps(
            {
                "followed_directive": verdict.followed_directive,
                "violated_candidate": verdict.violated_candidate,
                "cited_bkb_ids": verdict.cited_bkb_ids,
                "reasoning": verdict.reasoning,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert verdict.followed_directive, verdict.reasoning
    assert not verdict.violated_candidate, "model followed candidate BKB text"
