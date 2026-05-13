"""Tests for Meta-Harness gated promotion."""

import pytest

from umbrella.meta_harness.models import (
    CandidateEval,
    CandidateManifest,
    CandidateStatus,
    MetaPromotionEligibility,
    TaskEvalResult,
)
from umbrella.meta_harness.promotion import (
    _check_gmas_changes,
    _check_scope,
    _detect_suspicious_hardcode,
    decide_candidate_promotion,
)
from umbrella.meta_harness.store import MetaHarnessStore


@pytest.fixture
def store(tmp_path):
    return MetaHarnessStore(tmp_path / "meta_harness")


class TestHardcodeDetection:
    def test_no_hardcode(self, tmp_path):
        cand_dir = tmp_path / "candidate"
        (cand_dir / "diffs").mkdir(parents=True)
        (cand_dir / "diffs" / "worktree.diff").write_text(
            "+result = compute_score(data)\n",
            encoding="utf-8",
        )
        suspicious = _detect_suspicious_hardcode(["file.py"], cand_dir)
        assert len(suspicious) == 0

    def test_detects_market_id(self, tmp_path):
        cand_dir = tmp_path / "candidate"
        (cand_dir / "diffs").mkdir(parents=True)
        (cand_dir / "diffs" / "worktree.diff").write_text(
            "+market_id = '0xabc123def456'\n",
            encoding="utf-8",
        )
        suspicious = _detect_suspicious_hardcode(["file.py"], cand_dir)
        assert len(suspicious) > 0

    def test_no_candidate_dir(self):
        suspicious = _detect_suspicious_hardcode(["file.py"], None)
        assert len(suspicious) == 0


class TestGmasChanges:
    def test_no_gmas(self):
        assert _check_gmas_changes(["umbrella/prompts/task.md"]) == []

    def test_gmas_detected(self):
        blocked = _check_gmas_changes(
            ["gmas/core/engine.py", "umbrella/evals/runner.py"]
        )
        assert len(blocked) == 1
        assert "gmas/core/engine.py" in blocked


class TestScopeCheck:
    def test_allowed_scope(self):
        assert (
            _check_scope(["umbrella/prompts/task.md", "ouroboros/tools/x.py"]) is True
        )

    def test_disallowed_scope(self):
        assert _check_scope(["random_dir/secret.py"]) is False

    def test_pyproject_allowed(self):
        assert _check_scope(["pyproject.toml"]) is True


class TestDecidePromotion:
    def test_promote_with_good_score(self, tmp_path, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(
            experiment_id=exp.id,
            run_status="complete",
            changed_files=["umbrella/prompts/task.md"],
        )
        store.save_candidate(manifest)

        ev = CandidateEval(
            candidate_id=manifest.candidate_id,
            avg_score=0.8,
            tasks_total=5,
            tasks_complete=4,
            tasks_failed=0,
        )
        store.save_eval(ev)

        # Create execution dir
        cand_dir = store.find_candidate_dir(manifest.candidate_id)
        (cand_dir / "execution").mkdir(parents=True, exist_ok=True)

        decision = decide_candidate_promotion(
            tmp_path,
            manifest.candidate_id,
            store=store,
        )
        assert decision.decision == MetaPromotionEligibility.PROMOTE

    def test_reject_low_score(self, tmp_path, store):
        exp = store.create_experiment(name="test")
        baseline = CandidateManifest(experiment_id=exp.id, run_status="complete")
        store.save_candidate(baseline)
        store.save_eval(
            CandidateEval(candidate_id=baseline.candidate_id, avg_score=0.8)
        )

        candidate = CandidateManifest(experiment_id=exp.id, run_status="complete")
        store.save_candidate(candidate)
        store.save_eval(
            CandidateEval(candidate_id=candidate.candidate_id, avg_score=0.7)
        )

        decision = decide_candidate_promotion(
            tmp_path,
            candidate.candidate_id,
            baseline_candidate_id=baseline.candidate_id,
            store=store,
        )
        assert decision.decision == MetaPromotionEligibility.REJECT

    def test_needs_review_for_gmas(self, tmp_path, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(
            experiment_id=exp.id,
            run_status="complete",
            changed_files=["gmas/core/engine.py"],
        )
        store.save_candidate(manifest)
        store.save_eval(
            CandidateEval(candidate_id=manifest.candidate_id, avg_score=0.9)
        )

        cand_dir = store.find_candidate_dir(manifest.candidate_id)
        (cand_dir / "execution").mkdir(parents=True, exist_ok=True)

        decision = decide_candidate_promotion(
            tmp_path,
            manifest.candidate_id,
            store=store,
        )
        assert decision.decision == MetaPromotionEligibility.NEEDS_REVIEW

    def test_candidate_not_found(self, tmp_path, store):
        decision = decide_candidate_promotion(
            tmp_path,
            "nonexistent",
            store=store,
        )
        assert decision.decision == MetaPromotionEligibility.INSUFFICIENT_DATA

    def test_candidate_status_updated(self, tmp_path, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(
            experiment_id=exp.id,
            run_status="complete",
            changed_files=["umbrella/prompts/task.md"],
        )
        store.save_candidate(manifest)
        store.save_eval(
            CandidateEval(candidate_id=manifest.candidate_id, avg_score=0.8)
        )

        cand_dir = store.find_candidate_dir(manifest.candidate_id)
        (cand_dir / "execution").mkdir(parents=True, exist_ok=True)

        decide_candidate_promotion(tmp_path, manifest.candidate_id, store=store)

        updated = store.find_candidate(manifest.candidate_id)
        assert updated.status == CandidateStatus.PROMOTED

    def test_reject_when_runtime_verification_fails(self, tmp_path, store):
        exp = store.create_experiment(name="test")
        manifest = CandidateManifest(
            experiment_id=exp.id,
            run_status="failed_verification",
            changed_files=["umbrella/prompts/task.md"],
        )
        store.save_candidate(manifest)

        ev = CandidateEval(
            candidate_id=manifest.candidate_id,
            avg_score=0.9,
            tasks_total=1,
            tasks_complete=0,
            tasks_failed=1,
            task_results=[
                TaskEvalResult(
                    task_id="t1",
                    workspace_id="ws1",
                    status="failed_verification",
                    score=0.1,
                    runtime_verification=0.0,
                    runtime_verification_passed=False,
                    runtime_verification_skipped=False,
                    verification_summary="Verification: FAIL\n- pytest -> failed",
                )
            ],
        )
        store.save_eval(ev)

        cand_dir = store.find_candidate_dir(manifest.candidate_id)
        (cand_dir / "execution").mkdir(parents=True, exist_ok=True)

        decision = decide_candidate_promotion(
            tmp_path,
            manifest.candidate_id,
            store=store,
        )
        assert decision.decision == MetaPromotionEligibility.REJECT
        assert decision.passes_runtime_verification is False
        assert "Runtime verification failed" in decision.reasoning


class TestApplyCandidatePatchTruncation:
    def test_skips_truncated_diff_without_running_git(
        self, tmp_path, store, monkeypatch
    ):
        from umbrella.meta_harness.capture import _DIFF_TRUNCATED_MARKER
        from umbrella.meta_harness import promotion as promotion_mod

        exp = store.create_experiment(name="trunc")
        manifest = CandidateManifest(
            experiment_id=exp.id,
            run_status="complete",
            changed_files=["umbrella/prompts/task.md"],
        )
        store.save_candidate(manifest)
        cand_dir = store.find_candidate_dir(manifest.candidate_id)
        (cand_dir / "diffs").mkdir(parents=True, exist_ok=True)
        (cand_dir / "diffs" / "worktree.diff").write_text(
            "diff --git a/x b/x\n--- a/x\n+++ b/x\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n"
            f"\n{_DIFF_TRUNCATED_MARKER}\n",
            encoding="utf-8",
        )

        called = {"git": False}

        def _fail_if_called(*args, **kwargs):
            called["git"] = True
            raise AssertionError("git apply must not be invoked on truncated diffs")

        monkeypatch.setattr(promotion_mod.subprocess, "run", _fail_if_called)

        ok = promotion_mod.apply_candidate_patch(
            tmp_path,
            manifest.candidate_id,
            store=store,
        )
        assert ok is False
        assert called["git"] is False
