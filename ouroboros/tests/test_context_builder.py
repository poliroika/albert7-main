import json
from pathlib import Path

from ouroboros.agent import Env
from ouroboros.context import build_llm_messages
from ouroboros.memory import Memory
from ouroboros.utils import sanitize_task_for_event


def _system_text(messages):
    content = messages[0]["content"]
    if isinstance(content, list):
        return "\n\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content)


def test_context_uses_workspace_prompt_overlay_and_skips_identity(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo" / "ouroboros"
    host = tmp_path / "repo"
    drive = tmp_path / "repo" / "workspaces" / "demo" / ".memory" / "drive"
    (repo / "prompts").mkdir(parents=True)
    (repo / "prompts" / "SYSTEM.md").write_text("seed system", encoding="utf-8")
    (repo / "prompts" / "CONSCIOUSNESS.md").write_text(
        "seed consciousness", encoding="utf-8"
    )
    (repo / "BIBLE.md").write_text("seed bible", encoding="utf-8")
    (repo / "README.md").write_text("readme", encoding="utf-8")
    overlay = host / "workspaces" / "demo" / ".memory" / "prompts"
    overlay.mkdir(parents=True)
    (overlay / "SYSTEM.md").write_text("overlay system", encoding="utf-8")
    (overlay / "BIBLE.md").write_text("overlay bible", encoding="utf-8")
    (overlay / "CONSCIOUSNESS.md").write_text("overlay consciousness", encoding="utf-8")
    (drive / "state").mkdir(parents=True)
    (drive / "state" / "state.json").write_text(
        json.dumps({"current_task": {"workspace_id": "demo"}}),
        encoding="utf-8",
    )
    (drive / "memory").mkdir(parents=True)
    (drive / "memory" / "identity.md").write_text("legacy identity", encoding="utf-8")
    (drive / "memory" / "dialogue_summary.md").write_text(
        "legacy dialogue", encoding="utf-8"
    )

    messages, _info = build_llm_messages(
        Env(repo_dir=repo, drive_root=drive, host_repo_root=host),
        Memory(drive_root=drive, repo_dir=repo),
        {"id": "task1", "type": "user", "input": "do work"},
    )

    system = _system_text(messages)
    assert "overlay system" in system
    assert "overlay bible" in system
    assert "overlay consciousness" in system
    assert "legacy identity" not in system
    assert "legacy dialogue" not in system


def test_system_prompt_does_not_mention_legacy_identity_or_channels() -> None:
    prompt = (Path(__file__).parents[1] / "prompts" / "SYSTEM.md").read_text(
        encoding="utf-8"
    )
    forbidden = [
        "identity.md",
        "update_identity",
        "Telegram",
        "Google Colab",
        "MyDrive/Ouroboros",
        "repo_commit_push",
        "run_shell",
        "ouroboros-stable",
    ]
    for needle in forbidden:
        assert needle not in prompt


def test_task_context_overlay_serializes_dict_as_json() -> None:
    from ouroboros.context import _task_context_overlay

    task = {"context_overlays": {"phase_manifest": {"id": "execute", "version": 1}}}
    rendered = _task_context_overlay(task, "phase_manifest")
    assert rendered.startswith("{")
    assert '"id": "execute"' in rendered


def test_task_event_sanitizer_converts_frozensets(tmp_path: Path) -> None:
    task = {
        "id": "task1",
        "context_overlays": {
            "phase_node": {
                "subtasks": [
                    {
                        "id": "st1",
                        "allowed_tools": frozenset({"shell"}),
                        "allowed_skills": frozenset({"task-decomposition"}),
                    }
                ]
            }
        },
    }

    sanitized = sanitize_task_for_event(task, tmp_path)

    json.dumps(sanitized, ensure_ascii=False)
    subtask = sanitized["context_overlays"]["phase_node"]["subtasks"][0]
    assert subtask["allowed_tools"] == ["shell"]
    assert subtask["allowed_skills"] == ["task-decomposition"]
