"""Unit tests for the harness orchestrator (no real Ouroboros run involved)."""

import threading
import time
from pathlib import Path


from umbrella.harness import (
    HarnessEvent,
    HarnessOrchestrator,
    HarnessStagePlan,
)
from umbrella.harness.orchestrator import score_candidate


def _fake_run_factory(
    return_values: list[dict],
    *,
    delay: float = 0.0,
    cancel_event: threading.Event | None = None,
):
    """Build a fake ``run_ouroboros_improvement_sync`` that returns scripted results in order."""
    lock = threading.Lock()
    counter = {"i": 0}

    def fake(**kwargs):
        if cancel_event and cancel_event.is_set():
            return {"status": "cancelled", "task_id": kwargs.get("task_id")}
        if delay:
            time.sleep(delay)
        with lock:
            idx = counter["i"]
            counter["i"] += 1
        if idx >= len(return_values):
            return {"status": "failed", "error": "no scripted result"}
        return {**return_values[idx], "task_id": kwargs.get("task_id")}

    return fake


def test_score_candidate_prefers_completed_with_changes(tmp_path: Path) -> None:
    good = {
        "status": "completed",
        "verification_report": {
            "results": [{"status": "passed", "name": "tests"}],
        },
        "changes_made": ["a.py", "b.py"],
        "total_rounds": 8,
    }
    bad = {
        "status": "failed",
        "verification_report": {
            "results": [{"status": "failed", "name": "tests"}],
        },
        "changes_made": [],
        "total_rounds": 60,
    }
    s_good, _ = score_candidate(good)
    s_bad, _ = score_candidate(bad)
    assert s_good > s_bad


def test_score_candidate_accepts_launcher_complete_status() -> None:
    score, breakdown = score_candidate(
        {
            "status": "complete",
            "changes_made": ["notes.md"],
            "total_rounds": 4,
        },
        stage_kind="planning",
    )

    assert score > 0
    assert breakdown["status_completed"] == 5.0


def test_orchestrator_accepts_launcher_complete_and_passes_timeout(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {
            "status": "complete",
            "changes_made": ["workspaces/ws_test/.memory/knowledge/notes.md"],
            "candidate_id": "meta_candidate",
            "task_id": kwargs.get("task_id"),
        }

    applied: list[str] = []
    orch = HarnessOrchestrator(
        repo_root=tmp_path,
        workspace_id="ws_test",
        task_description="research the task",
        num_candidates=1,
        stages=[
            HarnessStagePlan(
                index=0,
                stage_id="s1",
                title="Research",
                description="Capture evidence",
                kind="planning",
            )
        ],
        run_fn=fake,
        apply_fn=lambda _root, candidate_id: applied.append(candidate_id) or True,
        max_parallel=1,
        timeout_seconds=123.0,
    )

    result = orch.run()

    assert result.status == "completed"
    assert result.candidates[0].status == "completed"
    assert calls[0]["timeout_seconds"] == 123.0
    assert calls[0]["verify"] is False
    assert applied == ["meta_candidate"]


def test_orchestrator_runs_candidates_in_parallel_and_picks_best(
    tmp_path: Path,
) -> None:
    events: list[HarnessEvent] = []

    fake = _fake_run_factory(
        [
            {
                "status": "completed",
                "verification_report": {
                    "results": [{"status": "passed", "name": "tests"}]
                },
                "changes_made": ["a.py"],
                "candidate_id": "cand_1",
            },
            {
                "status": "failed",
                "verification_report": {
                    "results": [{"status": "failed", "name": "tests"}]
                },
                "changes_made": [],
                "candidate_id": "cand_2",
            },
            {
                "status": "completed",
                "verification_report": {
                    "results": [{"status": "passed", "name": "tests"}]
                },
                "changes_made": ["a.py", "b.py", "c.py"],
                "candidate_id": "cand_3",
            },
        ]
    )

    applied: list[str] = []

    def fake_apply(repo_root: Path, candidate_id: str) -> bool:
        applied.append(candidate_id)
        return True

    orch = HarnessOrchestrator(
        repo_root=tmp_path,
        workspace_id="ws_test",
        task_description="hello",
        num_candidates=3,
        stages=[
            HarnessStagePlan(
                index=0,
                stage_id="s1",
                title="Single stage",
                description="Do the focused stage",
                kind="subtask",
            )
        ],
        on_event=events.append,
        run_fn=fake,
        apply_fn=fake_apply,
        max_parallel=3,
        timeout_seconds=10,
    )
    result = orch.run()

    assert result.status == "completed"
    assert len(result.stages) == 1
    assert result.winner_index is not None
    winner = result.candidates[result.winner_index]
    assert winner.status == "completed"
    assert winner.score > 0
    # Both completed candidates should have a score; at least one should win.
    completed = [c for c in result.candidates if c.status == "completed"]
    assert len(completed) == 2
    # Apply called for the winner's candidate id.
    assert applied == [winner.full_result.get("candidate_id")]
    # Events fired in expected order.
    types = [e.type for e in events]
    assert "harness_started" in types
    assert "stage_started" in types
    started_event = next(e for e in events if e.type == "stage_started")
    started_candidates = started_event.data.get("candidates") or []
    assert started_candidates[0]["strategy_id"] == "evidence_first"
    assert started_candidates[1]["strategy_id"] == "minimal_risk"
    assert "stage_candidate_started" in types
    assert "stage_candidate_completed" in types
    assert "stage_candidates_scored" in types
    assert "stage_winner_selected" in types
    assert "stage_losers_pruned" in types
    assert "stage_patch_applied" in types or "stage_patch_skipped" in types
    assert types[-1] == "harness_finished"


def test_orchestrator_repeats_tournament_per_stage(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake(**kwargs):
        task_id = str(kwargs.get("task_id") or "")
        calls.append(task_id)
        if task_id.endswith("__c2"):
            return {
                "status": "completed",
                "changes_made": ["winner.py"],
                "candidate_id": f"meta_{task_id}",
                "task_id": task_id,
            }
        return {
            "status": "failed",
            "error": "not enough",
            "candidate_id": f"meta_{task_id}",
            "task_id": task_id,
        }

    applied: list[str] = []
    events: list[HarnessEvent] = []
    stages = [
        HarnessStagePlan(
            index=0,
            stage_id="s1",
            title="Research",
            description="Search",
            kind="research",
        ),
        HarnessStagePlan(
            index=1, stage_id="s2", title="Bug fix", description="Fix", kind="bugfix"
        ),
    ]
    orch = HarnessOrchestrator(
        repo_root=tmp_path,
        workspace_id="ws_test",
        task_description="hello",
        num_candidates=2,
        stages=stages,
        on_event=events.append,
        run_fn=fake,
        apply_fn=lambda _root, candidate_id: applied.append(candidate_id) or True,
        max_parallel=2,
        timeout_seconds=10,
    )

    result = orch.run()

    assert result.status == "completed"
    assert len(result.stages) == 2
    assert len(result.candidates) == 4
    assert {c.run_id for c in result.candidates} == {
        f"{result.harness_id}__s1__c1",
        f"{result.harness_id}__s1__c2",
        f"{result.harness_id}__s2__c1",
        f"{result.harness_id}__s2__c2",
    }
    assert all(stage.winner_id.endswith("c2") for stage in result.stages)
    assert len(applied) == 2
    types = [event.type for event in events]
    assert types.count("stage_started") == 2
    assert types.count("stage_winner_selected") == 2
    assert types.count("stage_losers_pruned") == 2
    assert set(calls) == set(orch.candidate_run_ids)


def test_orchestrator_handles_all_failed_candidates(tmp_path: Path) -> None:
    fake = _fake_run_factory(
        [
            {"status": "failed", "error": "verification failed"},
            {"status": "failed", "error": "verification failed"},
        ]
    )

    orch = HarnessOrchestrator(
        repo_root=tmp_path,
        workspace_id="ws_test",
        task_description="hello",
        num_candidates=2,
        stages=[
            HarnessStagePlan(
                index=0,
                stage_id="s1",
                title="Single stage",
                description="Do the focused stage",
                kind="subtask",
            )
        ],
        run_fn=fake,
        apply_fn=lambda *a, **k: True,
        max_parallel=2,
        timeout_seconds=5,
    )
    result = orch.run()
    assert result.status == "failed"
    assert result.winner_index is None
    assert all(c.status == "failed" for c in result.candidates)


def test_orchestrator_recovers_useful_partial_candidate(tmp_path: Path) -> None:
    events: list[HarnessEvent] = []
    fake = _fake_run_factory(
        [
            {
                "status": "incomplete",
                "final_message": "partial patch produced",
                "changes_made": ["partial.py"],
                "candidate_id": "meta_partial",
                "total_rounds": 2,
            },
            {
                "status": "failed",
                "error": "hard failure",
                "changes_made": [],
                "total_rounds": 2,
            },
        ]
    )
    applied: list[str] = []
    orch = HarnessOrchestrator(
        repo_root=tmp_path,
        workspace_id="ws_test",
        task_description="fix the bug",
        num_candidates=2,
        stages=[
            HarnessStagePlan(
                index=0,
                stage_id="s1",
                title="Bug fix",
                description="Fix a concrete bug",
                kind="bugfix",
            )
        ],
        on_event=events.append,
        run_fn=fake,
        apply_fn=lambda _root, candidate_id: applied.append(candidate_id) or True,
        max_parallel=2,
        timeout_seconds=5,
    )

    result = orch.run()

    assert result.status == "completed"
    assert result.stages[0].status == "completed"
    assert result.stages[0].winner_id == "s1-c1"
    assert result.candidates[0].status == "recovered"
    assert applied == ["meta_partial"]
    assert "stage_recovered_candidate_selected" in [event.type for event in events]


def test_orchestrator_cancel_marks_pending_cancelled(tmp_path: Path) -> None:
    started_event = threading.Event()
    release_event = threading.Event()

    def fake(**kwargs):
        started_event.set()
        release_event.wait(timeout=5)
        return {"status": "completed", "candidate_id": "cand_x"}

    orch = HarnessOrchestrator(
        repo_root=tmp_path,
        workspace_id="ws_test",
        task_description="hello",
        num_candidates=2,
        stages=[
            HarnessStagePlan(
                index=0,
                stage_id="s1",
                title="Single stage",
                description="Do the focused stage",
                kind="subtask",
            )
        ],
        run_fn=fake,
        apply_fn=lambda *a, **k: True,
        max_parallel=1,  # serial so we can interrupt second
        timeout_seconds=5,
    )

    def runner():
        orch.run()

    thread = threading.Thread(target=runner)
    thread.start()
    assert started_event.wait(timeout=2)
    orch.cancel()
    release_event.set()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_score_candidate_handles_invalid_input() -> None:
    score, breakdown = score_candidate("not a dict")  # type: ignore[arg-type]
    assert score == 0.0
    assert "invalid_result" in breakdown
