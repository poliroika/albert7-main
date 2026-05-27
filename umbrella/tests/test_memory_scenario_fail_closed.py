"""Runner must fail when a step errors even with empty assert block."""

import pytest

from umbrella.evals.memory_scenarios.models import MemoryScenario, ScenarioStep
from umbrella.evals.memory_scenarios.runner import MemoryScenarioRunner

pytestmark = pytest.mark.memory_contract


def test_step_action_error_fails_without_assert_block(tmp_path) -> None:
    scenario = MemoryScenario(
        id="fail_closed_probe",
        description="probe",
        workspace="test",
        mode="tool_flow",
        env={},
        seed=__import__(
            "umbrella.evals.memory_scenarios.models", fromlist=["ScenarioSeed"]
        ).ScenarioSeed(),
        steps=[
            ScenarioStep(
                id="broken",
                action="unsupported_action_xyz",
                phase="verify",
            )
        ],
        assertions={},
    )
    result = MemoryScenarioRunner(report_root=tmp_path / "fc").run(scenario)
    assert not result.ok
    assert any("unsupported" in msg for msg in result.invariant_failures)
