import json
from dataclasses import dataclass
from pathlib import Path

from ouroboros.tools.umbrella_tools import (
    update_prompt,
    record_idea,
    save_umbrella_lesson,
    save_umbrella_memory,
)
from ouroboros.tools import umbrella_tools


@dataclass
class _Ctx:
    repo_dir: Path
    drive_root: Path
    host_repo_root: Path
    task_id: str = "task1"


def test_update_prompt_writes_workspace_overlay(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "workspaces" / "demo").mkdir(parents=True)
    (repo / "umbrella").mkdir()
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (drive / "state" / "state.json").write_text(
        json.dumps({"current_task": {"workspace_id": "demo"}}),
        encoding="utf-8",
    )
    ctx = _Ctx(repo_dir=repo / "ouroboros", drive_root=drive, host_repo_root=repo)

    result = update_prompt(ctx, name="SYSTEM", new_content="new system", reason="test")

    assert json.loads(result)["updated"] is True
    assert (
        repo / "workspaces" / "demo" / ".memory" / "prompts" / "SYSTEM.md"
    ).read_text(encoding="utf-8") == "new system"
    assert (drive / "logs" / "prompt_changes.jsonl").exists()


def test_record_idea_is_workspace_scoped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "workspaces" / "demo").mkdir(parents=True)
    (repo / "umbrella").mkdir()
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (drive / "state" / "state.json").write_text(
        json.dumps({"current_task": {"workspace_id": "demo"}}),
        encoding="utf-8",
    )
    ctx = _Ctx(repo_dir=repo / "ouroboros", drive_root=drive, host_repo_root=repo)

    result = record_idea(ctx, content="try behavioral checks", tags="verify")

    assert json.loads(result)["saved"] is True
    assert "try behavioral checks" in (
        repo / "workspaces" / "demo" / ".memory" / "ideas.jsonl"
    ).read_text(encoding="utf-8")


def test_record_idea_accepts_kind_title_body_and_hierarchy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "workspaces" / "demo").mkdir(parents=True)
    (repo / "umbrella").mkdir()
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (drive / "state" / "state.json").write_text(
        json.dumps({"current_task": {"workspace_id": "demo"}}),
        encoding="utf-8",
    )
    ctx = _Ctx(repo_dir=repo / "ouroboros", drive_root=drive, host_repo_root=repo)

    result = record_idea(
        ctx,
        kind="verification_fix",
        title="Tighten tests",
        body="Print-only pytest tests must fail the quality guard.",
        tags="verify",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["palace_path"] == "workspaces/demo/ideas/verification_fix"
    stored = (repo / "workspaces" / "demo" / ".memory" / "ideas.jsonl").read_text(
        encoding="utf-8"
    )
    assert "Tighten tests" in stored
    assert "verification_fix" in stored


def test_save_umbrella_memory_preserves_nested_workspace_room(tmp_path: Path) -> None:
    ctx = _workspace_ctx(tmp_path)

    result = save_umbrella_memory(
        ctx,
        palace_path="workspaces/demo/plan/subtasks",
        title="Subtask: build-core",
        content="Plan subtask card should stay under the nested plan/subtasks room.",
        kind="subtask_card",
        workspace_id="demo",
        tags="subtask_card",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["room"] == "plan/subtasks"


def test_save_umbrella_memory_uses_workspace_path_before_backend(
    tmp_path: Path,
) -> None:
    ctx = _workspace_ctx(tmp_path)
    repo = ctx.host_repo_root

    result = save_umbrella_memory(
        ctx,
        palace_path="workspaces/demo/research",
        title="Research finding",
        content="Workspace-scoped memory should not land in manager palace.",
        kind="finding",
        tags="research",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["wing"] == "wing_demo"
    assert (repo / "workspaces" / "demo" / ".memory" / "palace").exists()
    assert not (repo / ".umbrella" / "palace").exists()


def _workspace_ctx(tmp_path: Path) -> "_Ctx":
    repo = tmp_path / "repo"
    (repo / "workspaces" / "demo").mkdir(parents=True)
    (repo / "umbrella").mkdir()
    drive = repo / "workspaces" / "demo" / ".memory" / "drive"
    (drive / "state").mkdir(parents=True)
    (drive / "state" / "state.json").write_text(
        json.dumps({"current_task": {"workspace_id": "demo"}}),
        encoding="utf-8",
    )
    return _Ctx(repo_dir=repo / "ouroboros", drive_root=drive, host_repo_root=repo)


def test_record_idea_rejects_kind_lesson(tmp_path: Path) -> None:
    """Tier 2.1: lessons must go through save_umbrella_lesson, not record_idea."""
    ctx = _workspace_ctx(tmp_path)

    result = record_idea(
        ctx,
        kind="lesson",
        content="anything",
    )

    assert result.startswith("ERROR")
    assert "save_umbrella_lesson" in result


def test_stale_phase_palace_memory_is_demoted_from_trusted_results() -> None:
    old_hit = {
        "room": "phase_plan",
        "metadata": {"task_id": "phase_web_old:execute"},
        "content": "old run claimed completion",
    }
    current_hit = {
        "room": "phase_plan",
        "metadata": {"task_id": "phase_web_new:execute"},
        "content": "current run evidence",
    }

    trusted, unverified = umbrella_tools._split_verified_first(
        [old_hit, current_hit], current_run_id="phase_web_new"
    )

    assert trusted == [current_hit]
    assert unverified == [old_hit]


def test_record_idea_defaults_to_hypothesis_and_skips_palace_mirror(
    tmp_path: Path,
) -> None:
    """Tier 2.1: an idea with no evidence_kind is treated as a hypothesis and
    must NOT be mirrored to semantic palace (otherwise it would pollute
    recall search just like the news_cards_ai run did).
    """
    ctx = _workspace_ctx(tmp_path)

    result = record_idea(ctx, content="maybe path resolution is the issue")

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["evidence_kind"] == "hypothesis"
    assert payload["mirrored_to_semantic"] is False
    stored = json.loads(
        (ctx.host_repo_root / "workspaces" / "demo" / ".memory" / "ideas.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()[-1]
    )
    assert "evidence:hypothesis" in stored["tags"]
    assert "candidate" in stored["tags"]
    assert "unverified" in stored["tags"]


def test_record_idea_unknown_evidence_kind_falls_back_to_hypothesis(
    tmp_path: Path,
) -> None:
    ctx = _workspace_ctx(tmp_path)

    result = record_idea(ctx, content="hello", evidence_kind="totally_legit_evidence")

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["evidence_kind"] == "hypothesis"
    assert "warning" in payload
    assert "totally_legit_evidence" in payload["warning"]


def test_record_idea_observation_from_log_stays_out_of_semantic_memory(
    tmp_path: Path, monkeypatch
) -> None:
    from ouroboros.tools import umbrella_tools

    mirror_calls: list[dict] = []

    class _FakePalace:
        def add(self, **kw):
            mirror_calls.append(kw)
            return {"drawer_id": "drawer-1"}

    fake_backend = lambda repo_root, ws: _FakePalace()
    monkeypatch.setattr(umbrella_tools, "_palace_backend", fake_backend)
    monkeypatch.setitem(record_idea.__globals__, "_palace_backend", fake_backend)

    ctx = _workspace_ctx(tmp_path)

    result = record_idea(
        ctx,
        content="Observed pytest failure from tool output",
        evidence_kind="observation_from_log",
        kind="verification_fix",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["evidence_kind"] == "observation_from_log"
    assert payload["mirrored_to_semantic"] is False
    assert mirror_calls == []


def test_save_umbrella_lesson_demoted_without_verify_run_id(tmp_path: Path) -> None:
    """Tier 2.2: verification_passed alone is not enough — a lesson without
    verify_run_id is recorded as unverified/avoid so it doesn't shadow
    real lessons in recall.
    """
    ctx = _workspace_ctx(tmp_path)

    result = save_umbrella_lesson(
        ctx,
        workspace_id="demo",
        change_summary="Tightened verification spec",
        expected_effect="file_exists understands list paths",
        verification_passed=True,
        critic_verdict="pass",
    )
    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["verified"] is False
    assert payload["downgrade_reason"] == "verify_run_id missing"


def test_save_umbrella_lesson_does_not_semantically_verify_demoted_lesson(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[dict] = []

    class _FakeMemPalace:
        def __init__(self, repo_root, workspace_id):
            self.repo_root = repo_root
            self.workspace_id = workspace_id

        def add(self, **kw):
            calls.append(kw)
            return "lesson-node"

    monkeypatch.setattr("umbrella.memory.palace.facade.MemPalace", _FakeMemPalace)
    ctx = _workspace_ctx(tmp_path)

    result = save_umbrella_lesson(
        ctx,
        workspace_id="demo",
        change_summary="Unverified lesson",
        expected_effect="Should not be trusted",
        verification_passed=False,
        critic_verdict="",
        tags="calibration",
    )

    payload = json.loads(result)
    assert payload["verified"] is False
    assert calls
    assert calls[-1]["store"] == "palace.lesson"
    assert calls[-1]["verified"] is False
    assert "unverified_lesson" in calls[-1]["tags"]
    assert "verified" not in calls[-1]["tags"]


def test_save_umbrella_lesson_demoted_when_failed_step_count_positive(
    tmp_path: Path,
) -> None:
    ctx = _workspace_ctx(tmp_path)

    result = save_umbrella_lesson(
        ctx,
        workspace_id="demo",
        change_summary="claimed fix",
        expected_effect="all pass",
        verification_passed=True,
        critic_verdict="pass",
        verify_run_id="round-12",
        failed_step_count=2,
    )
    payload = json.loads(result)
    assert payload["verified"] is False
    assert "failed_step_count" in (payload["downgrade_reason"] or "")


def test_save_umbrella_lesson_verified_when_all_invariants_present(
    tmp_path: Path,
) -> None:
    ctx = _workspace_ctx(tmp_path)

    result = save_umbrella_lesson(
        ctx,
        workspace_id="demo",
        change_summary="Real fix",
        expected_effect="ok",
        verification_passed=True,
        critic_verdict="pass",
        verify_run_id="round-42",
        failed_step_count=0,
    )
    payload = json.loads(result)
    assert payload["verified"] is True
    assert payload["verify_run_id"] == "round-42"
    assert payload["downgrade_reason"] is None


def test_record_idea_verified_outcome_mirrors_to_semantic(
    tmp_path: Path, monkeypatch
) -> None:
    """When the agent explicitly marks an idea as verified outcome, it does
    enter palace — that's the path for high-confidence knowledge."""
    from ouroboros.tools import umbrella_tools

    mirror_calls: list[dict] = []

    class _FakePalace:
        def add(self, **kw):
            mirror_calls.append(kw)
            return {"drawer_id": "drawer-1"}

    fake_backend = lambda repo_root, ws: _FakePalace()
    monkeypatch.setattr(umbrella_tools, "_palace_backend", fake_backend)
    monkeypatch.setitem(record_idea.__globals__, "_palace_backend", fake_backend)

    ctx = _workspace_ctx(tmp_path)

    result = record_idea(
        ctx,
        content="path field must be a single string in workspace.toml",
        evidence_kind="verified_outcome",
        kind="verification_fix",
    )

    payload = json.loads(result)
    assert payload["saved"] is True
    assert payload["evidence_kind"] == "verified_outcome"
    assert payload["mirrored_to_semantic"] is True
    assert mirror_calls and mirror_calls[0]["workspace_id"] == "demo"
