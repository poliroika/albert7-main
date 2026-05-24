"""Pytest entrypoint for Memory Scenario Harness."""

import pytest

from umbrella.evals.memory_scenarios.runner import run_all_scenarios, run_scenario_by_id

pytestmark = pytest.mark.memory_scenario


def test_memory_scenarios_all(tmp_path) -> None:
    result = run_all_scenarios(report_root=tmp_path / "reports")
    assert result.ok, result.summary_text


@pytest.mark.memory_contract
def test_memory_scenario_bkb_filtering(tmp_path) -> None:
    result = run_scenario_by_id("01_bkb_filtering", report_root=tmp_path / "bkb")
    assert result.ok, result.summary_text
