from pathlib import Path

import pytest

from umbrella.memory.reflection import run_reflection_phase


class _FakePalace:
    def add(self, **kwargs):
        return kwargs


def test_reflection_skips_when_not_complex(tmp_path: Path) -> None:
    repo_root = tmp_path
    (repo_root / "workspaces" / "agent_research").mkdir(parents=True, exist_ok=True)
    result = run_reflection_phase(
        repo_root=repo_root,
        workspace_id="agent_research",
        task_id="task_1",
        verification_report={"passed": True},
        tool_call_count=2,
        final_message="done",
        changes_made=[],
    )
    assert result.status == "skipped"


def test_reflection_writes_lesson_and_candidate_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path
    (repo_root / "workspaces" / "agent_research").mkdir(parents=True, exist_ok=True)
    (repo_root / "umbrella" / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "umbrella.memory.reflection.get_palace_backend", lambda _path: _FakePalace()
    )
    monkeypatch.setattr(
        "umbrella.memory.reflection._reflect_with_llm",
        lambda **kwargs: {
            "lesson": {
                "change_summary": "Added role graph",
                "expected_effect": "Better routing",
                "observed_effect": "Verified pass",
                "conclusion": "Role graph should be reused",
                "repeat_tags": ["role_graph"],
                "avoid_tags": [],
            },
            "candidate_skill": {
                "name": "gmas role bootstrap",
                "domains": ["multi_agent_gmas"],
                "when_to_use": "Need agent graph",
                "params": [{"name": "agents_count", "description": "number of agents"}],
                "steps": ["Define roles", "Build RoleGraph", "Run MACPRunner"],
            },
            "gap_signal": None,
        },
    )

    result = run_reflection_phase(
        repo_root=repo_root,
        workspace_id="agent_research",
        task_id="task_2",
        verification_report={"passed": True, "summary": "all green"},
        tool_call_count=7,
        final_message="Implemented workspace patch",
        changes_made=["workspaces/agent_research/app.py"],
    )
    assert result.status == "recorded"
    assert result.lesson_id.startswith("lesson_")
    assert result.skill_slug == "gmas-role-bootstrap"
    skill_path = (
        repo_root
        / "umbrella"
        / "skills"
        / "library"
        / "gmas-role-bootstrap"
        / "SKILL.md"
    )
    assert skill_path.exists()
