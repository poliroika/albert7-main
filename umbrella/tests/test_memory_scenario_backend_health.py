"""Backend unavailable visibility — must run without pytest volatile stub autouse."""

import pytest

from umbrella.evals.memory_scenarios.runner import run_scenario_by_id

pytestmark = pytest.mark.memory_scenario


def test_backend_unavailable_without_volatile_stub(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", raising=False)
    monkeypatch.setenv("UMBRELLA_ALLOW_VOLATILE_MEMORY_STUB", "0")
    result = run_scenario_by_id(
        "12_backend_unavailable_visibility",
        report_root=tmp_path / "backend",
    )
    assert result.ok, result.summary_text
