"""Web bridge behaviour specific to harness (multi-candidate) runs."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from umbrella.harness import HarnessEvent
from umbrella.web_bridge.app import WebBridgeApp


@pytest.fixture
def minimal_workspace(tmp_path: Path) -> tuple[Path, str]:
    ws = "ws_harness_ui"
    root = tmp_path
    wdir = root / "workspaces" / ws
    wdir.mkdir(parents=True)
    (wdir / "TASK_MAIN.md").write_text("task", encoding="utf-8")
    return root, ws


def test_start_workspace_run_harness_targets_harness_worker(minimal_workspace) -> None:
    root, ws = minimal_workspace
    app = WebBridgeApp(root)
    with patch.object(app, "_active_run_for_workspace", return_value=None):
        with patch("umbrella.web_bridge.app.threading.Thread") as thread_cls:
            mock_thread = MagicMock()
            thread_cls.return_value = mock_thread
            run = app.start_workspace_run(
                {
                    "workspace_id": ws,
                    "harness_mode": True,
                    "harness_candidates": 4,
                    "model": "test-model",
                    "max_rounds": 2,
                    "max_verify_retries": 1,
                }
            )
    assert run.get("mode") == "harness"
    meta = run.get("harness_meta") or {}
    assert meta.get("candidates") == 4
    assert len(meta.get("candidate_run_ids") or []) == 4
    thread_cls.assert_called_once()
    call_kw = thread_cls.call_args[1]
    assert call_kw["target"] == app._run_harness_worker
    args = call_kw["args"]
    assert args[0] == run["id"]
    assert args[1] == ws
    assert args[6] == "test-model"
    assert args[7] == 4


def test_cancel_run_includes_harness_child_task_ids(minimal_workspace) -> None:
    root, ws = minimal_workspace
    app = WebBridgeApp(root)
    parent = "harness_web_deadbeef"
    children = [f"{parent}__c1", f"{parent}__c2", f"{parent}__c3"]
    app._upsert_web_run(
        parent,
        {
            "id": parent,
            "workspace_id": ws,
            "status": "running",
            "attempt_task_ids": [parent, *children],
            "harness_meta": {
                "candidates": 3,
                "candidate_run_ids": children,
            },
        },
    )
    state_dir = root / "workspaces" / ws / ".memory" / "drive" / "state"
    state_dir.mkdir(parents=True)

    app.cancel_run(parent)

    stop_path = state_dir / "stop_requested.json"
    assert stop_path.exists()
    payload = json.loads(stop_path.read_text(encoding="utf-8"))
    ids = set(payload.get("attempt_task_ids") or [])
    assert parent in ids
    for child in children:
        assert child in ids
    assert payload.get("candidate_run_ids") == children


def test_attempt_task_ids_include_staged_harness_candidates(minimal_workspace) -> None:
    root, ws = minimal_workspace
    app = WebBridgeApp(root)
    parent = "harness_web_staged"
    children = [f"{parent}__s1__c1", f"{parent}__s1__c2", f"{parent}__s2__c1"]
    run = {
        "id": parent,
        "workspace_id": ws,
        "status": "running",
        "harness_meta": {
            "candidate_run_ids": children[:1],
            "stages": [
                {
                    "stage_id": "s1",
                    "candidates": [
                        {"run_id": children[0]},
                        {"run_id": children[1]},
                    ],
                },
                {
                    "stage_id": "s2",
                    "candidates": [
                        {"run_id": children[2]},
                    ],
                },
            ],
        },
    }

    ids = app._attempt_task_ids_for_run(parent, run)

    assert parent in ids
    for child in children:
        assert child in ids


def test_task_id_matching_includes_remediation_children(minimal_workspace) -> None:
    root, _ws = minimal_workspace
    app = WebBridgeApp(root)
    run_id = "sync_improve_web_abc123"

    assert app._task_id_matches_run(f"{run_id}__remediation_1", run_id, {run_id})
    assert app._task_id_matches_run(
        f"{run_id}__s1__c1__remediation_1", run_id, {f"{run_id}__s1__c1"}
    )
    assert not app._task_id_matches_run("other_run__remediation_1", run_id, {run_id})


def test_list_runs_hides_harness_child_candidates(minimal_workspace) -> None:
    root, ws = minimal_workspace
    app = WebBridgeApp(root)
    parent = "harness_web_parent"
    child = f"{parent}__s1__c1"
    app._upsert_web_run(
        parent,
        {
            "id": parent,
            "workspace_id": ws,
            "status": "completed",
            "mode": "harness",
            "harness_meta": {"candidate_run_ids": [child]},
            "created_at": "2026-05-08T00:00:00Z",
            "updated_at": "2026-05-08T00:00:00Z",
        },
    )
    results = root / "workspaces" / ws / ".memory" / "drive" / "task_results"
    results.mkdir(parents=True)
    (results / f"{child}.json").write_text(
        json.dumps(
            {"task_id": child, "status": "complete", "ts": "2026-05-08T00:00:01Z"}
        ),
        encoding="utf-8",
    )

    rows = app.list_runs(ws)["runs"]

    row_ids = [row["id"] for row in rows]
    assert parent in row_ids
    assert child not in row_ids


def test_harness_finished_event_does_not_append_ghost_stage(minimal_workspace) -> None:
    root, ws = minimal_workspace
    app = WebBridgeApp(root)
    run_id = "harness_web_no_ghost"
    app._upsert_web_run(
        run_id,
        {
            "id": run_id,
            "workspace_id": ws,
            "status": "running",
            "mode": "harness",
            "harness_meta": {"mode": "staged", "candidates": 1},
        },
    )

    class FakeHarnessResult:
        status = "completed"
        stages = []
        candidates = []
        winner_index = None
        winner_applied = False
        final_message = "done"

        def to_dict(self):
            return {
                "status": self.status,
                "stages": self.stages,
                "candidates": self.candidates,
                "final_message": self.final_message,
            }

    class FakeHarnessOrchestrator:
        def __init__(self, **kwargs):
            self.on_event = kwargs["on_event"]

        def run(self):
            self.on_event(
                HarnessEvent(
                    type="harness_started",
                    message="starting staged harness: 1 stages, 1 candidates per stage",
                    data={
                        "num_stages": 1,
                        "num_candidates": 1,
                        "candidate_run_ids": ["harness_web_no_ghost__s1__c1"],
                        "stages": [
                            {
                                "index": 0,
                                "stage_id": "s1",
                                "title": "Research",
                                "kind": "planning",
                                "description": "",
                                "success_check": "",
                                "source": "test",
                            }
                        ],
                    },
                    ts="2026-05-08T00:00:00Z",
                )
            )
            self.on_event(
                HarnessEvent(
                    type="stage_started",
                    stage_index=0,
                    stage_id="s1",
                    stage_title="Research",
                    stage_kind="planning",
                    message="stage 1: split into 1 candidates",
                    data={
                        "stage": {
                            "index": 0,
                            "stage_id": "s1",
                            "title": "Research",
                            "kind": "planning",
                        },
                        "candidate_run_ids": ["harness_web_no_ghost__s1__c1"],
                        "candidates": [
                            {
                                "index": 0,
                                "candidate_id": "s1-c1",
                                "run_id": "harness_web_no_ghost__s1__c1",
                                "status": "running",
                                "strategy_id": "evidence_first",
                                "strategy_title": "Evidence-first",
                                "strategy_summary": "Collects evidence before patching.",
                            }
                        ],
                    },
                    ts="2026-05-08T00:00:01Z",
                )
            )
            self.on_event(
                HarnessEvent(
                    type="stage_recovered_candidate_selected",
                    stage_index=0,
                    stage_id="s1",
                    stage_title="Research",
                    stage_kind="planning",
                    message="recovered with s1-c1",
                    data={
                        "winner": {
                            "index": 0,
                            "candidate_id": "s1-c1",
                            "run_id": "harness_web_no_ghost__s1__c1",
                            "status": "recovered",
                            "strategy_id": "evidence_first",
                        },
                        "reason": "partial candidate had promotable work",
                    },
                    ts="2026-05-08T00:00:01Z",
                )
            )
            self.on_event(
                HarnessEvent(
                    type="harness_finished",
                    message="done",
                    data={"status": "completed"},
                    ts="2026-05-08T00:00:02Z",
                )
            )
            return FakeHarnessResult()

    with patch("umbrella.harness.HarnessOrchestrator", FakeHarnessOrchestrator):
        app._run_harness_worker(run_id, ws, "task", 0, 0, 0, "test-model", 1)

    run = app._get_web_run(run_id) or {}
    stages = run.get("harness_stages") or []
    assert [stage.get("stage_id") for stage in stages] == ["s1"]
    assert stages[0]["candidates"][0]["strategy_id"] == "evidence_first"
    assert stages[0]["recovered"] is True


def test_delete_run_removes_meta_harness_candidates_glob(minimal_workspace) -> None:
    root, ws = minimal_workspace
    cand_root = root / ".umbrella" / "meta_harness" / "candidates"
    cand_root.mkdir(parents=True)
    run_id = "harness_web_abc12345"
    leftover = cand_root / f"{run_id}_sandbox"
    leftover.mkdir()
    (leftover / "x.txt").write_text("k", encoding="utf-8")

    app = WebBridgeApp(root)
    app._upsert_web_run(
        run_id,
        {
            "id": run_id,
            "workspace_id": ws,
            "status": "completed",
            "attempt_task_ids": [run_id],
        },
    )
    out = app.delete_run(run_id, ws)

    assert out.get("ok") is True
    assert not leftover.exists()
