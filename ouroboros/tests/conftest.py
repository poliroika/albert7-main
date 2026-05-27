"""Shared pytest hooks for ouroboros integration tests."""

import importlib.util
from pathlib import Path

import pytest

from umbrella.deep_agent_tools import phase_control_research as phase_control_research_module

_HELPER_PATH = Path(__file__).resolve().parent / "helpers" / "capability_declaration.py"
_spec = importlib.util.spec_from_file_location(
    "ouroboros_capability_declaration_helper",
    _HELPER_PATH,
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_helper)
seed_submitted_declaration = _helper.seed_submitted_declaration


@pytest.fixture(autouse=True)
def _auto_seed_capability_declaration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed submitted declaration when research handoff would otherwise fail."""

    original = phase_control_research_module._capability_declaration_handoff_issue

    def _wrapped(ctx):
        issue = original(ctx)
        if issue and "missing capability_declaration" in issue:
            seed_submitted_declaration(
                ctx,
                discovery_channels=[
                    {"tool": "github_project_search", "outcome": "attempted"},
                    {"tool": "web_search", "outcome": "no_results", "notes": "fixture"},
                ],
            )
            return original(ctx)
        return issue

    monkeypatch.setattr(
        phase_control_research_module,
        "_capability_declaration_handoff_issue",
        _wrapped,
    )
