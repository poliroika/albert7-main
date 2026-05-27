from types import SimpleNamespace

from umbrella.phases.identity import (
    KNOWN_PHASE_IDS,
    phase_control_row_matches,
    phase_head,
    phase_id_from_task_id,
    resolve_phase_id,
)


def test_known_phase_ids_match_manifest_set() -> None:
    assert "subtask_review" in KNOWN_PHASE_IDS
    assert "plan_review" in KNOWN_PHASE_IDS


def test_phase_id_from_task_id_subtask_review_with_subtask_suffix() -> None:
    assert (
        phase_id_from_task_id("phase_web_d6901209:subtask_review:calculator-core-logic")
        == "subtask_review"
    )


def test_phase_id_from_task_id_simple_phase_suffix() -> None:
    assert phase_id_from_task_id("phase_web_d6901209:plan") == "plan"


def test_phase_id_from_task_id_with_attempt_suffix() -> None:
    assert phase_id_from_task_id("phase_web_d6901209:execute:1779693000123") == "execute"
    assert (
        phase_id_from_task_id(
            "phase_web_d6901209:subtask_review:calculator-core:1779693000123"
        )
        == "subtask_review"
    )


def test_phase_head_splits_subtask_review_node_id() -> None:
    assert phase_head("subtask_review:project-setup") == "subtask_review"


def test_resolve_phase_id_uses_task_id_when_loop_phase_is_linear() -> None:
    ctx = SimpleNamespace(
        task_id="phase_web_d6901209:subtask_review:calculator-core-logic",
        loop_state_view={"phase_label": "linear"},
    )

    assert resolve_phase_id(ctx) == "subtask_review"


def test_resolve_phase_id_falls_back_to_phase_node_overlay() -> None:
    ctx = SimpleNamespace(
        task_id="",
        loop_state_view={"phase_label": "linear"},
        context_overlays={"phase_node": {"id": "subtask_review:project-setup"}},
    )

    assert resolve_phase_id(ctx) == "subtask_review"


def test_phase_control_row_matches_across_attempt_suffix() -> None:
    row = {
        "task_id": "phase_web_abc:plan_review:111",
        "phase": "plan_review",
        "kind": "submit_micro_review",
    }
    assert phase_control_row_matches(
        row, task_id="phase_web_abc:plan_review:222"
    )


def test_phase_control_row_matches_earlier_attempt_same_phase() -> None:
    row = {
        "task_id": "phase_web_abc:research:111",
        "phase": "research",
        "kind": "submit_capability_declaration",
    }
    assert phase_control_row_matches(
        row, task_id="phase_web_abc:research:222"
    )


def test_phase_control_row_matches_rejects_wrong_phase() -> None:
    row = {
        "task_id": "phase_web_abc:plan:111",
        "phase": "plan",
        "kind": "submit_phase_plan",
    }
    assert not phase_control_row_matches(
        row, task_id="phase_web_abc:plan_review:222"
    )
