"""Unit tests for ``ouroboros.task_planner``.

These are pure-function / file-I/O tests with no LLM in the loop; the
orchestration tests live in ``test_loop_planner_orchestration.py``.
"""

import json
from pathlib import Path

import pytest

from ouroboros import task_planner as tp
from ouroboros.task_planner import (
    PLANNER_MODE_ALWAYS,
    PLANNER_MODE_AUTO,
    PLANNER_MODE_OFF,
    SUBTASK_STATUS_DONE,
    SUBTASK_STATUS_IN_PROGRESS,
    SUBTASK_STATUS_PENDING,
    SUBTASK_STATUS_SKIPPED,
    PlanExecutionContext,
    TaskPlan,
    TaskPlanStore,
    active_plan_id,
    focus_block,
    plan_progress_block,
    planner_system_prompt,
    review_block,
    should_run_planner,
)


def _steps_fixture() -> list[dict]:
    return [
        {
            "title": "Discover",
            "description": "Read TASK_MAIN",
            "success_check": "summary saved",
        },
        {
            "title": "Build",
            "description": "Implement v1",
            "success_check": "tests pass",
        },
        {
            "title": "Verify",
            "description": "Run smoke",
            "success_check": "no regressions",
        },
    ]


def test_active_plan_id_uses_plan_execution_context() -> None:
    class Ctx:
        task_id = "base-task"
        plan_execution_context = PlanExecutionContext(
            active_plan_id="remediation-plan",
            plan_store_root="/tmp/drive",
            task_id="base-task",
            phase="remediation",
            subtask_id="st_1",
        )

    assert active_plan_id(Ctx()) == "remediation-plan"


def test_create_save_load_round_trip(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="objective body",
        steps=_steps_fixture(),
    )

    assert isinstance(plan, TaskPlan)
    assert plan.task_id == "t1"
    assert plan.cursor == 0
    assert len(plan.subtasks) == 3
    assert all(s.status == SUBTASK_STATUS_PENDING for s in plan.subtasks)

    reloaded = store.load("t1")
    assert reloaded is not None
    assert reloaded.task_id == "t1"
    assert len(reloaded.subtasks) == 3
    assert reloaded.subtasks[0].title == "Discover"
    assert reloaded.subtasks[1].success_check == "tests pass"

    on_disk = json.loads(
        (tmp_path / "task_plans" / "t1.json").read_text(encoding="utf-8")
    )
    assert on_disk["workspace_id"] == "ws"
    assert on_disk["objective_digest"] == "objective body"


def test_create_from_steps_preserves_control_tags(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t_tags",
        workspace_id="ws",
        objective_digest="objective",
        steps=[
            {
                "title": "Research unfamiliar API",
                "description": "Use current docs before implementing",
                "success_check": "source cited",
                "tags": ["domain_unknown"],
            }
        ],
    )

    assert plan.subtasks[0].tags == ["domain_unknown"]
    assert store.load("t_tags").subtasks[0].tags == ["domain_unknown"]


def test_archive_moves_plan_aside_so_next_load_replans(tmp_path: Path) -> None:
    """Without ``archive`` the verification remediation loop reuses the
    cached completed plan, ``plan.is_complete()`` short-circuits the
    subtask phase, and the loop drops straight into final aggregation
    with no tools — the model gets the failure context but cannot fix
    anything. ``archive`` MUST move the live plan aside so the next
    ``load(task_id)`` returns ``None`` (forcing a fresh planner pass)
    while keeping the previous plan on disk for audit history.
    """
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="task42",
        workspace_id="ws",
        objective_digest="objective body",
        steps=_steps_fixture(),
    )
    assert store.load("task42") is not None

    archived_path = store.archive("task42", reason="before_remediation_3")

    assert archived_path is not None
    assert archived_path.exists(), "archived copy must remain on disk"
    assert archived_path.name.startswith("task42.before_remediation_3")
    assert store.load("task42") is None, (
        "after archive(), load() must return None so the next run "
        "re-plans from scratch instead of seeing a 'completed' plan"
    )
    body = json.loads(archived_path.read_text(encoding="utf-8"))
    assert body["task_id"] == "task42"
    assert body["objective_digest"] == "objective body"
    assert plan.task_id == "task42"


def test_archive_returns_none_when_no_plan_exists(tmp_path: Path) -> None:
    """Archiving a non-existent plan is a no-op (no exception). The
    remediation loop calls ``archive`` unconditionally, so this must
    not raise even on the first iteration where no plan was saved yet.
    """
    store = TaskPlanStore(tmp_path)
    assert store.archive("never_existed") is None


def test_archive_then_save_creates_clean_new_plan(tmp_path: Path) -> None:
    """End-to-end: archive an existing plan, then create+save a new
    plan with the same task_id. The new plan must be loadable and the
    archived copy must still exist.
    """
    store = TaskPlanStore(tmp_path)
    store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="v1",
        steps=_steps_fixture(),
    )
    archived = store.archive("t1", reason="before_remediation_1")
    assert archived is not None and archived.exists()

    new_plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="v2-remediation",
        steps=[
            {
                "title": "fix import",
                "description": "expose RequirementsParser",
                "success_check": "pytest tests passes",
            }
        ],
    )
    assert new_plan is not None
    reloaded = store.load("t1")
    assert reloaded is not None
    assert reloaded.objective_digest == "v2-remediation"
    assert len(reloaded.subtasks) == 1
    assert reloaded.subtasks[0].title == "fix import"
    assert archived.exists(), "archive must survive subsequent saves"


def test_create_with_no_steps_rejected(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    with pytest.raises(ValueError):
        store.create_from_steps(
            task_id="t1",
            workspace_id="",
            objective_digest="",
            steps=[],
        )


def test_complete_advance_and_failure(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="",
        steps=_steps_fixture(),
    )

    started = store.start_current(plan)
    assert started is not None
    assert store.load("t1").subtasks[0].status == SUBTASK_STATUS_IN_PROGRESS

    completed = store.complete_current(
        plan,
        status=SUBTASK_STATUS_DONE,
        summary="ok",
        evidence=["a.py", "b.txt"],
    )
    assert completed is not None
    again = store.load("t1")
    assert again.cursor == 1
    assert again.subtasks[0].status == SUBTASK_STATUS_DONE
    assert again.subtasks[0].summary == "ok"
    assert again.subtasks[0].evidence == ["a.py", "b.txt"]

    skipped = store.fail_current(again, reason="phase cap reached")
    assert skipped is not None
    after_skip = store.load("t1")
    assert after_skip.cursor == 2
    assert after_skip.subtasks[1].status == SUBTASK_STATUS_SKIPPED
    assert "phase cap reached" in after_skip.subtasks[1].summary


def test_apply_revision_replaces_tail_only(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="orig",
        steps=_steps_fixture(),
    )
    store.start_current(plan)
    store.complete_current(plan, status=SUBTASK_STATUS_DONE, summary="done step 1")
    plan = store.load("t1")
    assert plan.cursor == 1

    plan = store.apply_revision(
        plan,
        replacement_steps_for_remaining=[
            {
                "title": "Repair",
                "description": "fix discovered issue",
                "success_check": "issue gone",
            },
        ],
        reason="Step 2 obsolete after evidence",
    )

    assert plan.revisions == 1
    assert len(plan.subtasks) == 2
    assert plan.subtasks[0].status == SUBTASK_STATUS_DONE
    assert plan.subtasks[0].summary == "done step 1"
    assert plan.subtasks[1].title == "Repair"
    assert plan.cursor == 1
    assert "Step 2 obsolete" in plan.objective_digest


def test_revision_rejects_non_list(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="",
        objective_digest="",
        steps=_steps_fixture(),
    )
    with pytest.raises(ValueError):
        store.apply_revision(plan, replacement_steps_for_remaining="oops", reason="x")


def test_focus_and_review_blocks_are_human_readable(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="",
        steps=_steps_fixture(),
    )
    block = focus_block(plan)
    assert "[SUBTASK 1/3]" in block
    assert "Discover" in block
    assert "Read TASK_MAIN" in block
    assert "mark_subtask_complete" in block
    assert "update_scratchpad" not in block

    store.start_current(plan)
    completed_subtask = store.complete_current(
        plan,
        status=SUBTASK_STATUS_DONE,
        summary="discovery summary",
    )
    plan = store.load("t1")
    review = review_block(plan, completed_subtask)
    assert "[REVIEW PHASE]" in review
    assert "discovery summary" in review
    assert "revise_remaining_plan" in review
    assert "Allowed tool in this phase" in review


def test_phase_prompts_surface_external_discovery_as_quality_expectation(
    tmp_path: Path,
) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t_discovery",
        workspace_id="ws",
        objective_digest="",
        steps=[
            {
                "title": "Implement API client",
                "description": "Build against an unfamiliar public API",
                "success_check": "tests pass",
            },
            {
                "title": "Verify",
                "description": "Run pytest",
                "success_check": "pytest",
            },
        ],
    )
    focus = focus_block(plan)
    store.start_current(plan)
    completed = store.complete_current(plan, status=SUBTASK_STATUS_DONE, summary="done")
    plan = store.load("t_discovery")
    review = review_block(plan, completed)
    remediation = tp.remediation_block(plan)

    for text in (focus, review, remediation):
        assert "deep_search" in text
        assert "github_project_search" in text or "github_extract_snippets" in text
        assert "mcp_discover" in text


def test_focus_block_warns_about_interactive_launch_success_check(
    tmp_path: Path,
) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="",
        steps=[
            {
                "title": "Document launch",
                "description": "Update README launch instructions",
                "success_check": "acceptance_command: python main.py",
            }
        ],
    )
    block = focus_block(plan)
    assert "[NON_INTERACTIVE_VALIDATION_RULE]" in block
    assert "Do NOT execute local app/game launches" in block


def test_focus_block_adds_preferred_write_tools_for_implementation_subtask(
    tmp_path: Path,
) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="",
        steps=[
            {
                "title": "Implement player movement",
                "description": "Create and write core player logic and controls",
                "success_check": "file_exists: game/player.py",
            }
        ],
    )
    block = focus_block(plan)
    assert "[PREFERRED_WRITE_TOOLS]" in block
    assert "Prioritize workspace write tools" in block


def test_focus_block_renders_workspace_inventory_when_path_provided(
    tmp_path: Path,
) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="",
        steps=[
            {
                "title": "Inspect",
                "description": "review files",
                "success_check": "summary written",
            }
        ],
    )
    workspace = tmp_path / "ws_inv"
    workspace.mkdir()
    (workspace / "main.py").write_text("# entrypoint", encoding="utf-8")
    (workspace / "README.md").write_text("# readme", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("# app", encoding="utf-8")
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_app.py").write_text("# tests", encoding="utf-8")

    block = focus_block(plan, workspace_root=workspace)

    assert "[WORKSPACE_INVENTORY]" in block
    assert "workspace root:" in block
    assert "main.py" in block
    assert "src/:" in block
    assert "app.py" in block
    assert "tests/:" in block
    assert "test_app.py" in block


def test_focus_block_omits_inventory_when_path_missing(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="",
        steps=[{"title": "x", "description": "y", "success_check": "z"}],
    )
    block = focus_block(plan)
    assert "[WORKSPACE_INVENTORY]" not in block


def test_focus_block_renders_noise_detected_when_paths_passed(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="",
        steps=[{"title": "x", "description": "y", "success_check": "z"}],
    )
    block = focus_block(
        plan,
        noise_paths=["extract_docx.py", "real_test_output.pptx"],
    )
    assert "[NOISE_DETECTED]" in block
    assert "extract_docx.py" in block
    assert "real_test_output.pptx" in block
    assert "src/scripts/" in block


def test_focus_block_omits_noise_section_when_empty(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="",
        steps=[{"title": "x", "description": "y", "success_check": "z"}],
    )
    block = focus_block(plan, noise_paths=[])
    assert "[NOISE_DETECTED]" not in block


def test_plan_progress_block_marks_cursor(tmp_path: Path) -> None:
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="ws",
        objective_digest="",
        steps=_steps_fixture(),
    )
    store.start_current(plan)
    store.complete_current(plan, status=SUBTASK_STATUS_DONE, summary="step done")
    plan = store.load("t1")

    block = plan_progress_block(plan)
    assert "[PLAN_PROGRESS]" in block
    assert "Resume from the marked step" in block
    # Cursor marker '>' should sit on the active (second) line, not first.
    lines = block.splitlines()
    cursor_lines = [ln for ln in lines if ln.startswith(" >")]
    assert len(cursor_lines) == 1
    assert "2." in cursor_lines[0]


def test_planner_system_prompt_embeds_digest_and_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_PLANNER_MAX_STEPS", "5")
    prompt = planner_system_prompt("Build a CSV exporter for the workspace.")
    assert "1 and 5 subtasks" in prompt
    assert "[TASK_MAIN_DIGEST]" in prompt
    assert "Build a CSV exporter" in prompt
    assert "propose_task_plan" in prompt


def test_planner_phase_round_cap_defaults_to_finite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OUROBOROS_PLANNER_PHASE_ROUNDS", raising=False)
    assert tp.planner_phase_round_cap() == 12


def test_planner_phase_round_cap_allows_explicit_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OUROBOROS_PLANNER_PHASE_ROUNDS", "0")
    assert tp.planner_phase_round_cap() == 0


def test_should_run_planner_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    long_text = "x" * 500
    short_text = "Hello"

    assert (
        should_run_planner(
            mode=PLANNER_MODE_OFF, task_main_text=long_text, has_existing_plan=False
        )
        is False
    )
    assert (
        should_run_planner(
            mode=PLANNER_MODE_ALWAYS, task_main_text=short_text, has_existing_plan=False
        )
        is True
    )
    assert (
        should_run_planner(
            mode=PLANNER_MODE_AUTO, task_main_text=long_text, has_existing_plan=False
        )
        is True
    )
    assert (
        should_run_planner(
            mode=PLANNER_MODE_AUTO, task_main_text=short_text, has_existing_plan=False
        )
        is False
    )
    assert (
        should_run_planner(
            mode=PLANNER_MODE_AUTO, task_main_text=long_text, has_existing_plan=True
        )
        is False
    )

    monkeypatch.setenv("OUROBOROS_PLANNER_AUTO_MIN_CHARS", "3")
    assert (
        should_run_planner(
            mode=PLANNER_MODE_AUTO, task_main_text="abcd", has_existing_plan=False
        )
        is True
    )


def test_save_uses_per_write_temp_files(tmp_path: Path) -> None:
    """Each save must use a unique temp file (avoids cross-process races on Windows)."""
    store = TaskPlanStore(tmp_path)
    plan = store.create_from_steps(
        task_id="t1",
        workspace_id="",
        objective_digest="",
        steps=_steps_fixture(),
    )
    plan_dir = tmp_path / "task_plans"

    for i in range(10):
        plan.subtasks[0].summary = f"iter {i}"
        store.save(plan)
        # No leftover temp files at steady state.
        leftover = [p for p in plan_dir.iterdir() if p.name.endswith(".tmp")]
        assert leftover == []

    reloaded = store.load("t1")
    assert reloaded is not None
    assert reloaded.subtasks[0].summary == "iter 9"
