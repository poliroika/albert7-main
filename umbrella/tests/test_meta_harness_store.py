"""Tests for Meta-Harness filesystem store."""

import pytest

from umbrella.meta_harness.models import (
    CandidateEval,
    CandidateManifest,
    ExperimentStatus,
    MetaPromotionDecision,
    MetaPromotionEligibility,
    SearchSet,
    SearchTask,
)
from umbrella.meta_harness.store import MetaHarnessStore


@pytest.fixture
def store(tmp_path):
    return MetaHarnessStore(tmp_path / "meta_harness")


@pytest.fixture
def search_set():
    return SearchSet(
        name="test_set",
        tasks=[SearchTask(task_id="t1", workspace_id="ws1", task_text="test task")],
    )


class TestExperimentCRUD:
    def test_create_and_get(self, store, search_set):
        exp = store.create_experiment(
            name="test_exp",
            workspace_id="ws1",
            search_set=search_set,
        )
        assert exp.id.startswith("exp_")
        assert exp.workspace_id == "ws1"

        loaded = store.get_experiment(exp.id)
        assert loaded is not None
        assert loaded.id == exp.id
        assert loaded.name == "test_exp"

    def test_list_experiments(self, store):
        store.create_experiment(name="exp1")
        store.create_experiment(name="exp2")
        experiments = store.list_experiments()
        assert len(experiments) == 2

    def test_get_latest(self, store):
        store.create_experiment(name="first")
        import time

        time.sleep(0.01)
        second = store.create_experiment(name="second")
        latest = store.get_latest_experiment()
        assert latest is not None
        assert latest.id == second.id

    def test_get_or_create_returns_active(self, store):
        first = store.get_or_create_experiment(name="first")
        second = store.get_or_create_experiment(name="second")
        assert first.id == second.id

    def test_update_experiment(self, store):
        exp = store.create_experiment(name="test")
        exp.iterations_completed = 5
        exp.status = ExperimentStatus.COMPLETED
        store.update_experiment(exp)

        loaded = store.get_experiment(exp.id)
        assert loaded.iterations_completed == 5
        assert loaded.status == ExperimentStatus.COMPLETED

    def test_nonexistent_experiment(self, store):
        assert store.get_experiment("nonexistent") is None


class TestCandidateCRUD:
    def test_save_and_get(self, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(
            experiment_id=exp.id,
            task_id="task_1",
            workspace_id="ws1",
            write_calls=3,
        )
        store.save_candidate(manifest)

        loaded = store.get_candidate(exp.id, manifest.candidate_id)
        assert loaded is not None
        assert loaded.task_id == "task_1"
        assert loaded.write_calls == 3

    def test_find_candidate_across_experiments(self, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(experiment_id=exp.id, task_id="task_1")
        store.save_candidate(manifest)

        found = store.find_candidate(manifest.candidate_id)
        assert found is not None
        assert found.candidate_id == manifest.candidate_id

    def test_list_candidates(self, store):
        exp = store.create_experiment(name="test")
        for i in range(3):
            store.save_candidate(
                CandidateManifest(
                    experiment_id=exp.id,
                    task_id=f"task_{i}",
                )
            )
        candidates = store.list_candidates(exp.id)
        assert len(candidates) == 3

    def test_candidate_added_to_experiment(self, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(experiment_id=exp.id)
        store.save_candidate(manifest)

        updated_exp = store.get_experiment(exp.id)
        assert manifest.candidate_id in updated_exp.candidate_ids


class TestEvalCRUD:
    def test_save_and_get_eval(self, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(experiment_id=exp.id)
        store.save_candidate(manifest)

        ev = CandidateEval(
            candidate_id=manifest.candidate_id,
            avg_score=0.75,
            tasks_total=5,
            tasks_complete=4,
        )
        store.save_eval(ev)

        loaded = store.get_eval(manifest.candidate_id)
        assert loaded is not None
        assert loaded.avg_score == 0.75
        assert loaded.tasks_complete == 4


class TestPromotionDecisionCRUD:
    def test_save_and_get(self, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(experiment_id=exp.id)
        store.save_candidate(manifest)

        decision = MetaPromotionDecision(
            candidate_id=manifest.candidate_id,
            decision=MetaPromotionEligibility.PROMOTE,
            reasoning="Good improvement",
        )
        store.save_promotion_decision(decision)

        loaded = store.get_promotion_decision(manifest.candidate_id)
        assert loaded is not None
        assert loaded.decision == MetaPromotionEligibility.PROMOTE


class TestExecutionEvents:
    def test_save_and_read(self, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(experiment_id=exp.id)
        store.save_candidate(manifest)

        events = [
            {"type": "tool_call", "tool": "read_file"},
            {"type": "error", "msg": "oops"},
        ]
        store.save_execution_events(exp.id, manifest.candidate_id, events)

        loaded = store.get_execution_events(manifest.candidate_id)
        assert len(loaded) == 2
        assert loaded[1]["type"] == "error"


class TestTopCandidates:
    def test_top_by_score(self, store):
        exp = store.create_experiment(name="test")

        for i, score in enumerate([0.3, 0.9, 0.6]):
            m = CandidateManifest(experiment_id=exp.id, task_id=f"t{i}")
            store.save_candidate(m)
            store.save_eval(CandidateEval(candidate_id=m.candidate_id, avg_score=score))

        top = store.top_candidates(exp.id, n=2, sort_by="score")
        assert len(top) == 2
        assert top[0][1].avg_score == 0.9


class TestGetFailures:
    def test_finds_errors(self, store):
        exp = store.create_experiment(name="test")

        ok = CandidateManifest(experiment_id=exp.id, run_status="complete")
        store.save_candidate(ok)

        err = CandidateManifest(experiment_id=exp.id, run_status="error")
        store.save_candidate(err)

        failures = store.get_failures(exp.id)
        assert len(failures) == 1
        assert failures[0][0].run_status == "error"
