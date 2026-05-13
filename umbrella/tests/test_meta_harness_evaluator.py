"""Tests for Meta-Harness evaluator."""

import pytest

from umbrella.meta_harness.evaluator import (
    compute_weighted_score,
    evaluate_candidate_on_search_set,
    evaluate_candidate_task,
)
from umbrella.meta_harness.models import (
    CandidateManifest,
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
        name="test",
        tasks=[
            SearchTask(task_id="t1", workspace_id="ws1", task_text="task one"),
            SearchTask(task_id="t2", workspace_id="ws2", task_text="task two"),
        ],
    )


class TestWeightedScore:
    def test_perfect_score(self):
        score = compute_weighted_score(
            task_success=1.0,
            artifact_quality=1.0,
            validation_pass=1.0,
            stability=1.0,
            cost_efficiency=1.0,
            observability=1.0,
            runtime_verification=1.0,
        )
        assert abs(score - 1.0) < 0.001

    def test_zero_score(self):
        score = compute_weighted_score(
            task_success=0.0,
            artifact_quality=0.0,
            validation_pass=0.0,
            stability=0.0,
            cost_efficiency=0.0,
            observability=0.0,
            runtime_verification=0.0,
        )
        assert score == 0.0

    def test_task_success_dominates(self):
        high_task = compute_weighted_score(
            1.0, 0.0, 0.0, 0.0, 0.0, 0.0, runtime_verification=0.0
        )
        high_artifact = compute_weighted_score(
            0.0, 1.0, 0.0, 0.0, 0.0, 0.0, runtime_verification=0.0
        )
        assert high_task > high_artifact

    def test_runtime_verification_materially_affects_score(self):
        with_verify = compute_weighted_score(
            task_success=1.0,
            artifact_quality=1.0,
            validation_pass=1.0,
            stability=1.0,
            cost_efficiency=1.0,
            observability=1.0,
            runtime_verification=1.0,
        )
        without_verify = compute_weighted_score(
            task_success=1.0,
            artifact_quality=1.0,
            validation_pass=1.0,
            stability=1.0,
            cost_efficiency=1.0,
            observability=1.0,
            runtime_verification=0.0,
        )
        assert with_verify - without_verify >= 0.10


class TestEvaluateCandidateTask:
    def test_complete_candidate(self, tmp_path):
        candidate = CandidateManifest(
            workspace_id="ws1",
            run_status="verified",
            write_calls=5,
            changed_files=["a.py", "b.py", "c.py"],
            final_message="Done",
            events_count=10,
            cost_usd=0.5,
        )
        task = SearchTask(task_id="t1", workspace_id="ws1", task_text="test")
        result = evaluate_candidate_task(tmp_path, candidate, task)

        assert result.task_id == "t1"
        assert result.score > 0.5
        assert result.task_success == 1.0

    def test_error_candidate(self, tmp_path):
        candidate = CandidateManifest(
            workspace_id="ws1",
            run_status="error",
            error="Something broke",
        )
        task = SearchTask(task_id="t1", workspace_id="ws1", task_text="test")
        result = evaluate_candidate_task(tmp_path, candidate, task)

        assert result.score < 0.3
        assert result.task_success == 0.0

    def test_uses_cached_verification_report(self, tmp_path, monkeypatch):
        """Evaluator must reuse ``metadata['verification_report']`` instead
        of re-running the workspace spec. This keeps Meta-Harness
        consistent with the Ouroboros post-gate (which verifies the
        instance_path) and avoids double-spending compute.
        """
        import umbrella.verification as verif_mod

        called = {"count": 0}

        def _never_call(*args, **kwargs):
            called["count"] += 1
            return []

        monkeypatch.setattr(
            verif_mod, "load_verification_spec", _never_call, raising=False
        )
        monkeypatch.setattr(verif_mod, "run_verification", _never_call, raising=False)

        candidate = CandidateManifest(
            workspace_id="ws1",
            run_status="verified",
            write_calls=3,
            changed_files=["a.py"],
            final_message="Done",
            events_count=3,
            metadata={
                "verification_report": {
                    "passed": True,
                    "pass_rate": 1.0,
                    "skipped": False,
                    "summary": "- [required] pytest -> passed (0.12s)",
                }
            },
        )
        task = SearchTask(task_id="t1", workspace_id="ws1", task_text="test")
        result = evaluate_candidate_task(tmp_path, candidate, task)

        assert called["count"] == 0, "cached report must short-circuit re-verification"
        assert result.runtime_verification_passed is True
        assert result.runtime_verification == 1.0
        assert "pytest" in result.verification_summary

    def test_prefers_instance_path_over_seed(self, tmp_path, monkeypatch):
        """When the candidate has an ``instance_path`` on disk, the evaluator
        must verify the INSTANCE, not the seed workspace. Before the fix,
        the hardcoded ``workspaces/<id>`` path gave a stale score.
        """
        import umbrella.verification as verif_mod

        seed = tmp_path / "workspaces" / "ws1"
        seed.mkdir(parents=True)
        instance = tmp_path / "instances" / "ws1_inst"
        instance.mkdir(parents=True)

        seen_roots: list[str] = []

        def _fake_load(path):
            seen_roots.append(str(path))
            return []

        monkeypatch.setattr(
            verif_mod, "load_verification_spec", _fake_load, raising=False
        )

        candidate = CandidateManifest(
            workspace_id="ws1",
            run_status="verified",
            instance_path=str(instance),
        )
        task = SearchTask(task_id="t1", workspace_id="ws1", task_text="test")
        evaluate_candidate_task(tmp_path, candidate, task)

        assert seen_roots, (
            "load_verification_spec must be invoked when no cached report"
        )
        assert str(instance.resolve()) in seen_roots[0]

    def test_skips_mismatched_workspace(self, tmp_path):
        candidate = CandidateManifest(
            workspace_id="ws_other",
            run_status="complete",
            write_calls=5,
        )
        task = SearchTask(task_id="t1", workspace_id="ws1", task_text="test")
        result = evaluate_candidate_task(tmp_path, candidate, task)

        assert result.status == "skipped"
        assert "different workspace" in result.notes


class TestEvaluateCandidateOnSearchSet:
    def test_candidate_not_found(self, tmp_path, search_set, store):
        ev = evaluate_candidate_on_search_set(
            tmp_path,
            "nonexistent",
            search_set,
            store=store,
        )
        assert "not found" in ev.notes

    def test_evaluation_with_candidate(self, tmp_path, search_set, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(
            experiment_id=exp.id,
            run_status="complete",
            write_calls=3,
            changed_files=["a.py"],
            final_message="Done",
            events_count=5,
        )
        store.save_candidate(manifest)

        ev = evaluate_candidate_on_search_set(
            tmp_path,
            manifest.candidate_id,
            search_set,
            store=store,
        )
        assert ev.tasks_total == 2
        assert ev.avg_score > 0
        assert ev.candidate_id == manifest.candidate_id

    def test_eval_saved_to_store(self, tmp_path, search_set, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(experiment_id=exp.id, run_status="complete")
        store.save_candidate(manifest)

        evaluate_candidate_on_search_set(
            tmp_path,
            manifest.candidate_id,
            search_set,
            store=store,
        )
        loaded = store.get_eval(manifest.candidate_id)
        assert loaded is not None
