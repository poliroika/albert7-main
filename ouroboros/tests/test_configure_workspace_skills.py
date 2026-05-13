"""Tests for the ``configure_workspace_skills`` agent tool.

The agent must be able to opt the workspace in or out of named skills
(currently just ``multi_agent_gmas``) by editing ``[skills]`` in
``workspace.toml`` itself, instead of relying on humans to set the
policy. Editing ``workspace.toml`` also has to invalidate the
per-workspace skill cache so the next attempt picks up the new policy.
"""

import json
import tomllib
from pathlib import Path

import pytest

from ouroboros.tools.umbrella_tools import (
    _upsert_workspace_toml_skill,
    configure_workspace_skills,
)


class _FakeCtx:
    def __init__(self, repo_root: Path, drive_root: Path) -> None:
        self.repo_dir = repo_root
        self.host_repo_root = repo_root
        self.drive_root = drive_root


def _make_workspace(tmp_path: Path, workspace_id: str) -> Path:
    (tmp_path / "umbrella").mkdir(exist_ok=True)
    workspace = tmp_path / "workspaces" / workspace_id
    workspace.mkdir(parents=True)
    (workspace / "TASK_MAIN.md").write_text("# task", encoding="utf-8")
    drive_root = workspace / ".memory" / "drive"
    (drive_root / "state").mkdir(parents=True, exist_ok=True)
    return workspace


@pytest.fixture()
def ctx_and_workspace(tmp_path: Path) -> tuple[_FakeCtx, Path, str]:
    workspace_id = "demo"
    workspace = _make_workspace(tmp_path, workspace_id)
    drive_root = workspace / ".memory" / "drive"
    return _FakeCtx(tmp_path, drive_root), workspace, workspace_id


# -- _upsert_workspace_toml_skill ----------------------------------------


def test_upsert_creates_skills_section_in_empty_toml() -> None:
    out = _upsert_workspace_toml_skill("", "multi_agent_gmas", True)
    parsed = tomllib.loads(out)
    assert parsed == {"skills": {"multi_agent_gmas": True}}


def test_upsert_appends_to_existing_other_section() -> None:
    original = "[verification]\nskip_behavioral = true\n"
    out = _upsert_workspace_toml_skill(original, "multi_agent_gmas", False)
    parsed = tomllib.loads(out)
    assert parsed["verification"] == {"skip_behavioral": True}
    assert parsed["skills"] == {"multi_agent_gmas": False}


def test_upsert_updates_existing_skill_value() -> None:
    original = "[skills]\nmulti_agent_gmas = false\nother = true\n"
    out = _upsert_workspace_toml_skill(original, "multi_agent_gmas", True)
    parsed = tomllib.loads(out)
    assert parsed["skills"]["multi_agent_gmas"] is True
    assert parsed["skills"]["other"] is True


def test_upsert_inserts_new_skill_into_existing_skills_section() -> None:
    original = "[skills]\nfoo = true\n\n[verification]\nskip_behavioral = true\n"
    out = _upsert_workspace_toml_skill(original, "multi_agent_gmas", True)
    parsed = tomllib.loads(out)
    assert parsed["skills"]["foo"] is True
    assert parsed["skills"]["multi_agent_gmas"] is True
    assert parsed["verification"]["skip_behavioral"] is True


# -- configure_workspace_skills tool -------------------------------------


def test_configure_writes_workspace_toml_and_invalidates_cache(
    ctx_and_workspace: tuple[_FakeCtx, Path, str],
) -> None:
    ctx, workspace, ws_id = ctx_and_workspace
    cache = workspace / ".memory" / "drive" / "state" / "active_skills.json"
    cache.write_text(
        json.dumps({"entry": {"workspace_id": ws_id, "domains": []}}),
        encoding="utf-8",
    )

    result = json.loads(
        configure_workspace_skills(
            ctx,
            workspace_id=ws_id,
            skill_id="multi_agent_gmas",
            enabled=True,
            reason="task is genuinely multi-agent",
        )
    )

    assert result["status"] == "ok"
    assert result["enabled"] is True
    assert result["previous_value"] is None

    parsed = tomllib.loads((workspace / "workspace.toml").read_text(encoding="utf-8"))
    assert parsed["skills"]["multi_agent_gmas"] is True
    assert not cache.exists(), "skill cache should be invalidated after policy change"


def test_configure_can_disable_skill(
    ctx_and_workspace: tuple[_FakeCtx, Path, str],
) -> None:
    ctx, workspace, ws_id = ctx_and_workspace
    (workspace / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n", encoding="utf-8"
    )

    result = json.loads(
        configure_workspace_skills(
            ctx,
            workspace_id=ws_id,
            skill_id="multi_agent_gmas",
            enabled=False,
            reason="task is plain CRUD",
        )
    )

    assert result["status"] == "ok"
    assert result["previous_value"] is True
    parsed = tomllib.loads((workspace / "workspace.toml").read_text(encoding="utf-8"))
    assert parsed["skills"]["multi_agent_gmas"] is False


def test_configure_is_noop_when_value_unchanged(
    ctx_and_workspace: tuple[_FakeCtx, Path, str],
) -> None:
    ctx, workspace, ws_id = ctx_and_workspace
    (workspace / "workspace.toml").write_text(
        "[skills]\nmulti_agent_gmas = true\n", encoding="utf-8"
    )

    result = json.loads(
        configure_workspace_skills(
            ctx,
            workspace_id=ws_id,
            skill_id="multi_agent_gmas",
            enabled=True,
        )
    )

    assert result["status"] == "noop"


def test_configure_rejects_unknown_skill(
    ctx_and_workspace: tuple[_FakeCtx, Path, str],
) -> None:
    ctx, _workspace, ws_id = ctx_and_workspace
    result = json.loads(
        configure_workspace_skills(
            ctx,
            workspace_id=ws_id,
            skill_id="quantum_supremacy",
            enabled=True,
        )
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "unknown_skill"


def test_configure_rejects_missing_workspace(
    ctx_and_workspace: tuple[_FakeCtx, Path, str],
) -> None:
    ctx, _workspace, _ws_id = ctx_and_workspace
    result = json.loads(
        configure_workspace_skills(
            ctx,
            workspace_id="does_not_exist",
            skill_id="multi_agent_gmas",
            enabled=True,
        )
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "workspace_not_found"


def test_configure_blocks_on_unparseable_toml(
    ctx_and_workspace: tuple[_FakeCtx, Path, str],
) -> None:
    ctx, workspace, ws_id = ctx_and_workspace
    (workspace / "workspace.toml").write_text("[[[ broken\n", encoding="utf-8")

    result = json.loads(
        configure_workspace_skills(
            ctx,
            workspace_id=ws_id,
            skill_id="multi_agent_gmas",
            enabled=True,
        )
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "workspace_toml_unparseable"
