"""Tests for Meta-Harness data models."""

import json
import time

from umbrella.meta_harness.models import (
    CandidateEval,
    CandidateManifest,
    CandidateStatus,
    ContrastiveMemoryBundle,
    ExperimentRecord,
    ExperimentStatus,
    MetaPromotionDecision,
    MetaPromotionEligibility,
    SearchSet,
    SearchTask,
    TaskEvalResult,
    generate_candidate_id,
    generate_experiment_id,
    generate_search_set_id,
)


class TestIDGenerators:
    def test_candidate_id_format(self):
        cid = generate_candidate_id()
        assert cid.startswith("cand_")
        assert len(cid) > 15

    def test_experiment_id_format(self):
        eid = generate_experiment_id()
        assert eid.startswith("exp_")

    def test_search_set_id_format(self):
        sid = generate_search_set_id()
        assert sid.startswith("ss_")

    def test_ids_are_unique(self):
        ids = {generate_candidate_id() for _ in range(20)}
        assert len(ids) == 20


class TestCandidateManifest:
    def test_defaults(self):
        m = CandidateManifest()
        assert m.candidate_id.startswith("cand_")
        assert m.status == CandidateStatus.CAPTURED
        assert m.changed_files == []
        assert m.cost_usd == 0.0

    def test_serialization_roundtrip(self):
        m = CandidateManifest(
            task_id="task_1",
            workspace_id="ws_1",
            changed_files=["a.py", "b.py"],
            cost_usd=1.23,
        )
        data = json.loads(json.dumps(m.model_dump(mode="json")))
        restored = CandidateManifest(**data)
        assert restored.task_id == "task_1"
        assert restored.changed_files == ["a.py", "b.py"]
        assert restored.cost_usd == 1.23


class TestSearchSet:
    def test_empty_set(self):
        ss = SearchSet(name="empty")
        assert ss.size == 0

    def test_with_tasks(self):
        tasks = [
            SearchTask(task_id="t1", workspace_id="ws1", task_text="do stuff"),
            SearchTask(
                task_id="t2", workspace_id="ws2", task_text="more stuff", difficulty=5
            ),
        ]
        ss = SearchSet(name="test", tasks=tasks)
        assert ss.size == 2
        assert ss.tasks[1].difficulty == 5


class TestCandidateEval:
    def test_defaults(self):
        ev = CandidateEval(candidate_id="cand_123")
        assert ev.avg_score == 0.0
        assert ev.tasks_total == 0

    def test_with_results(self):
        results = [
            TaskEvalResult(
                task_id="t1", workspace_id="ws1", score=0.8, status="complete"
            ),
            TaskEvalResult(
                task_id="t2", workspace_id="ws2", score=0.4, status="partial"
            ),
        ]
        ev = CandidateEval(
            candidate_id="cand_123",
            task_results=results,
            tasks_total=2,
            avg_score=0.6,
        )
        assert ev.tasks_total == 2
        assert ev.avg_score == 0.6


class TestPromotionDecision:
    def test_defaults(self):
        d = MetaPromotionDecision(candidate_id="cand_123")
        assert d.decision == MetaPromotionEligibility.INSUFFICIENT_DATA
        assert d.reviewed_by == "auto"

    def test_promote(self):
        d = MetaPromotionDecision(
            candidate_id="cand_123",
            decision=MetaPromotionEligibility.PROMOTE,
            reasoning="Score improved",
            score_delta=0.1,
        )
        assert d.decision == MetaPromotionEligibility.PROMOTE
        assert d.score_delta == 0.1


class TestExperimentRecord:
    def test_defaults(self):
        exp = ExperimentRecord()
        assert exp.status == ExperimentStatus.ACTIVE
        assert exp.candidate_ids == []

    def test_touch(self):
        exp = ExperimentRecord()
        old_updated = exp.updated_at
        time.sleep(0.01)
        exp.touch()
        assert exp.updated_at > old_updated


class TestContrastiveMemoryBundle:
    def test_empty(self):
        bundle = ContrastiveMemoryBundle()
        assert bundle.successes == []
        assert bundle.failures == []
