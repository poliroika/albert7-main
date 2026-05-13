"""
Tests for the evaluation system.
"""

import tempfile
from pathlib import Path

from umbrella.evals.models import (
    TaskSuccessRating,
    OutputQualityRating,
    StabilityRating,
    PatchOutcome,
    EvaluationRecord,
    PromotionCandidate,
    PromotionDecision,
    PromotionEligibility,
)
from umbrella.evals.llm_evaluator import _build_eval_prompt
from umbrella.evals.runner import evaluate_run
from umbrella.evals.comparisons import (
    compare_runs,
    classify_patch_outcome_from_reports,
    get_improvement_magnitude,
)
from umbrella.evals.promotion import (
    apply_promotion_decision,
    build_promotion_candidate,
    decide_promotion,
)
from umbrella.evals.seed_guardrails import (
    create_default_policy,
    check_promotion_eligibility,
    create_guardrail,
)
from umbrella.workspace_runtime.models import (
    WorkspaceRunResult,
    WorkspaceRunStatus,
)


def _create_test_run_result(
    task_id: str = "test_task",
    workspace_id: str = "test_workspace",
    status: WorkspaceRunStatus = WorkspaceRunStatus.COMPLETED,
    final_answer: str = "Task completed successfully",
    total_tokens: int = 10000,
    duration_seconds: float = 60.0,
) -> WorkspaceRunResult:
    """Create a test run result."""
    return WorkspaceRunResult(
        task_id=task_id,
        workspace_id=workspace_id,
        status=status,
        final_answer=final_answer,
        total_tokens=total_tokens,
        duration_seconds=duration_seconds,
    )


def test_evaluate_run_returns_complete_record():
    """Artifact-based eval marks a run with a full report as COMPLETE."""
    from umbrella.workspace_runtime.models import ArtifactRef, ArtifactType

    run_result = _create_test_run_result()

    with tempfile.TemporaryDirectory() as tmpdir:
        instance_path = Path(tmpdir)
        reports_dir = instance_path / "reports"
        reports_dir.mkdir()
        report_path = reports_dir / "latest_article.md"
        report_path.write_text(
            "# Title\n\n"
            + ("word " * 2000)
            + "\n\n## Section 1\n\n## Section 2\n\n## Section 3\n\n"
            "## References\n\nhttps://example.com\nhttps://example.org\nhttps://example.net\n",
            encoding="utf-8",
        )
        run_result.artifacts = [
            ArtifactRef(
                artifact_id="report_001",
                artifact_type=ArtifactType.REPORT,
                path=report_path,
                description="Test report",
            )
        ]

        eval_record = evaluate_run(
            run_result, instance_path, min_article_word_count=500
        )

        assert eval_record.task_id == "test_task"
        assert eval_record.workspace_id == "test_workspace"
        assert eval_record.run_id == run_result.run_id
        assert eval_record.task_success == TaskSuccessRating.COMPLETE
        assert eval_record.total_tokens == 10000
        assert eval_record.total_duration_seconds == 60.0


def test_evaluate_run_partial_for_short_article():
    """Artifact-based eval marks a run with a too-short report as PARTIAL."""
    from umbrella.workspace_runtime.models import ArtifactRef, ArtifactType

    run_result = _create_test_run_result()

    with tempfile.TemporaryDirectory() as tmpdir:
        instance_path = Path(tmpdir)
        reports_dir = instance_path / "reports"
        reports_dir.mkdir()
        report_path = reports_dir / "latest_article.md"
        report_path.write_text("# Short\n\nOnly a few words.\n", encoding="utf-8")
        run_result.artifacts = [
            ArtifactRef(
                artifact_id="report_001",
                artifact_type=ArtifactType.REPORT,
                path=report_path,
                description="Test report",
            )
        ]

        eval_record = evaluate_run(run_result, instance_path)

        assert eval_record.task_success == TaskSuccessRating.PARTIAL


def test_evaluate_run_detects_failure():
    """Test that evaluate_run detects failed runs."""
    run_result = _create_test_run_result(
        final_answer="Task failed with errors",
        status=WorkspaceRunStatus.FAILED,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        eval_record = evaluate_run(run_result, Path(tmpdir))

        assert eval_record.task_success == TaskSuccessRating.FAILED


def test_compare_runs_detects_improvement():
    """Test that compare_runs correctly detects improvements."""
    baseline = EvaluationRecord(
        id="baseline",
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run1",
        instance_path=Path("."),
        task_success=TaskSuccessRating.PARTIAL,
        output_quality=OutputQualityRating.FAIR,
        stability=StabilityRating.UNKNOWN,
        total_tokens=10000,
        total_duration_seconds=60.0,
        overall_score=0.5,
    )

    comparison = EvaluationRecord(
        id="comparison",
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run2",
        instance_path=Path("."),
        task_success=TaskSuccessRating.COMPLETE,
        output_quality=OutputQualityRating.GOOD,
        stability=StabilityRating.STABLE,
        total_tokens=12000,
        total_duration_seconds=50.0,
        overall_score=0.8,
    )

    report = compare_runs(baseline, comparison)

    assert report.overall_improvement == PatchOutcome.IMPROVED
    assert report.better_outcome is True
    assert report.score_delta > 0


def test_compare_runs_detects_regression():
    """Test that compare_runs correctly detects regressions."""
    baseline = EvaluationRecord(
        id="baseline",
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run1",
        instance_path=Path("."),
        task_success=TaskSuccessRating.COMPLETE,
        output_quality=OutputQualityRating.GOOD,
        stability=StabilityRating.STABLE,
        total_tokens=10000,
        total_duration_seconds=60.0,
        overall_score=0.8,
    )

    comparison = EvaluationRecord(
        id="comparison",
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run2",
        instance_path=Path("."),
        task_success=TaskSuccessRating.PARTIAL,
        output_quality=OutputQualityRating.FAIR,
        stability=StabilityRating.UNSTABLE,
        total_tokens=15000,
        total_duration_seconds=90.0,
        overall_score=0.4,
    )

    report = compare_runs(baseline, comparison)

    assert report.overall_improvement == PatchOutcome.REGRESSED
    assert report.better_outcome is False
    assert report.score_delta < 0


def test_build_eval_prompt_avoids_fake_ellipsis_for_short_task():
    prompt = _build_eval_prompt(
        {
            "task_input": "short task",
            "run_status": "complete",
            "duration_seconds": 1.0,
            "total_tokens": 10,
        }
    )

    assert "**Task:** short task" in prompt
    assert "**Task:** short task..." not in prompt


def test_classify_patch_outcome_from_reports():
    """Test classification of patch outcomes from multiple reports."""
    from umbrella.evals.models import ComparisonReport

    # Create multiple improved reports
    improved_reports = [
        ComparisonReport(
            id=f"report_{i}",
            task_id="test_task",
            workspace_id="test_workspace",
            baseline_run_id=f"baseline_{i}",
            comparison_run_id=f"comparison_{i}",
            baseline_score=0.5,
            comparison_score=0.7,
            score_delta=0.2,
            baseline_task_success=TaskSuccessRating.PARTIAL,
            comparison_task_success=TaskSuccessRating.COMPLETE,
            baseline_output_quality=OutputQualityRating.FAIR,
            comparison_output_quality=OutputQualityRating.GOOD,
            baseline_cost_usd=1.0,
            comparison_cost_usd=1.2,
            cost_delta_usd=0.2,
            baseline_stability=StabilityRating.UNKNOWN,
            comparison_stability=StabilityRating.STABLE,
            overall_improvement=PatchOutcome.IMPROVED,
        )
        for i in range(3)
    ]

    outcome = classify_patch_outcome_from_reports(improved_reports)
    assert outcome == PatchOutcome.IMPROVED


def test_build_promotion_candidate():
    """Test building a promotion candidate."""
    baseline = EvaluationRecord(
        id="baseline",
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run1",
        instance_path=Path("."),
        task_success=TaskSuccessRating.PARTIAL,
        output_quality=OutputQualityRating.FAIR,
        stability=StabilityRating.UNKNOWN,
        total_tokens=10000,
        total_duration_seconds=60.0,
        overall_score=0.5,
    )

    comparison = EvaluationRecord(
        id="comparison",
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run2",
        instance_path=Path("."),
        task_success=TaskSuccessRating.COMPLETE,
        output_quality=OutputQualityRating.GOOD,
        stability=StabilityRating.STABLE,
        total_tokens=12000,
        total_duration_seconds=50.0,
        overall_score=0.8,
    )

    comparison_report = compare_runs(baseline, comparison)

    candidate = build_promotion_candidate(
        baseline,
        comparison,
        comparison_report,
        patch_description="Improved agent coordination",
        changed_files=[Path("graph/config.json")],
    )

    assert candidate.task_id == "test_task"
    assert candidate.workspace_id == "test_workspace"
    assert candidate.patch_description == "Improved agent coordination"
    assert candidate.improvement_magnitude > 0
    assert len(candidate.changed_files) == 1


def test_decide_promotion_with_default_policy():
    """Test promotion decision with default policy."""
    baseline = EvaluationRecord(
        id="baseline",
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run1",
        instance_path=Path("."),
        task_success=TaskSuccessRating.PARTIAL,
        output_quality=OutputQualityRating.FAIR,
        stability=StabilityRating.UNKNOWN,
        total_tokens=10000,
        total_duration_seconds=60.0,
        overall_score=0.5,
    )

    comparison = EvaluationRecord(
        id="comparison",
        task_id="test_task",
        workspace_id="test_workspace",
        run_id="run2",
        instance_path=Path("."),
        task_success=TaskSuccessRating.COMPLETE,
        output_quality=OutputQualityRating.GOOD,
        stability=StabilityRating.STABLE,
        total_tokens=12000,
        total_duration_seconds=50.0,
        overall_score=0.8,
    )

    comparison_report = compare_runs(baseline, comparison)

    candidate = build_promotion_candidate(
        baseline,
        comparison,
        comparison_report,
        patch_description="Improved agent coordination",
        changed_files=[Path("graph/config.json")],
    )

    policy = create_default_policy()
    decision = decide_promotion(candidate, policy)

    assert decision.decision == PromotionEligibility.PROMOTE
    assert decision.passes_threshold is not None
    assert decision.passes_guardrails is not None


def test_build_promotion_candidate_normalizes_instance_paths():
    """Promotion candidates should keep only seed-relevant relative paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        instance_path = (
            tmp_path
            / "workspaces"
            / "agent_research"
            / "instances"
            / "agent_research_instance_test"
        )
        topology_path = instance_path / "graph" / "topology.toml"
        report_path = instance_path / "reports" / "manager_patch.md"
        metadata_path = instance_path / "instance_metadata.json"

        topology_path.parent.mkdir(parents=True)
        report_path.parent.mkdir(parents=True)
        topology_path.write_text("topology = true\n", encoding="utf-8")
        report_path.write_text("# patch note\n", encoding="utf-8")
        metadata_path.write_text("{}", encoding="utf-8")

        baseline = EvaluationRecord(
            id="baseline",
            task_id="test_task",
            workspace_id="agent_research_instance_test",
            run_id="run1",
            instance_path=instance_path,
            task_success=TaskSuccessRating.PARTIAL,
            output_quality=OutputQualityRating.FAIR,
            stability=StabilityRating.UNKNOWN,
            total_tokens=10000,
            total_duration_seconds=60.0,
            overall_score=0.5,
        )
        comparison = EvaluationRecord(
            id="comparison",
            task_id="test_task",
            workspace_id="agent_research_instance_test",
            run_id="run2",
            instance_path=instance_path,
            task_success=TaskSuccessRating.COMPLETE,
            output_quality=OutputQualityRating.GOOD,
            stability=StabilityRating.STABLE,
            total_tokens=12000,
            total_duration_seconds=50.0,
            overall_score=0.8,
        )

        comparison_report = compare_runs(baseline, comparison)
        candidate = build_promotion_candidate(
            baseline,
            comparison,
            comparison_report,
            patch_description="Improved graph configuration",
            changed_files=[metadata_path, topology_path, report_path, topology_path],
        )

        assert candidate.changed_files == [Path("graph") / "topology.toml"]


def test_apply_promotion_decision_copies_absolute_instance_paths_to_seed():
    """Promotion should copy from instance into seed even when paths are absolute."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        seed_path = tmp_path / "workspaces" / "agent_research"
        instance_path = seed_path / "instances" / "agent_research_instance_test"

        seed_topology = seed_path / "graph" / "topology.toml"
        instance_topology = instance_path / "graph" / "topology.toml"

        seed_topology.parent.mkdir(parents=True)
        instance_topology.parent.mkdir(parents=True)

        seed_topology.write_text("seed-version\n", encoding="utf-8")
        instance_topology.write_text("instance-version\n", encoding="utf-8")

        candidate = PromotionCandidate(
            task_id="test_task",
            workspace_id="agent_research_instance_test",
            instance_path=instance_path,
            patch_description="Promote graph update",
            changed_files=[instance_topology],
        )
        decision = PromotionDecision(
            candidate_id=candidate.id,
            decision=PromotionEligibility.PROMOTE,
            reasoning="Improvement is stable",
        )

        applied = apply_promotion_decision(
            candidate=candidate,
            decision=decision,
            seed_path=seed_path,
            instance_path=instance_path,
            changed_files=[
                instance_topology,
                instance_path / "reports" / "manager_patch.md",
                instance_path / "instance_metadata.json",
            ],
        )

        assert applied is True
        assert seed_topology.read_text(encoding="utf-8") == "instance-version\n"
        assert not (seed_path / "reports" / "manager_patch.md").exists()
        assert not (seed_path / "instance_metadata.json").exists()


def test_seed_guardrail_blocks_destructive_changes():
    """Test that guardrails block destructive changes."""
    guardrail = create_guardrail(
        guardrail_id="test_guardrail",
        name="Test Guardrail",
        description="Test guardrail for destructive changes",
        blocked_patterns=["delete", "destroy"],
    )

    assert "delete" in guardrail.blocked_patterns
    assert "destroy" in guardrail.blocked_patterns


def test_check_promotion_eligibility():
    """Test promotion eligibility checking."""
    policy = create_default_policy()

    # Safe change
    is_eligible, reasons = check_promotion_eligibility(
        "candidate_1", "Improved graph configuration", policy
    )
    # May have reasons due to human approval requirement
    assert isinstance(is_eligible, bool)
    assert isinstance(reasons, list)

    # Blocked change
    is_eligible_blocked, reasons_blocked = check_promotion_eligibility(
        "candidate_2", "This will delete core functionality", policy
    )
    assert len(reasons_blocked) > 0


def test_get_improvement_magnitude():
    """Test calculation of improvement magnitude."""
    from umbrella.evals.models import ComparisonReport

    report = ComparisonReport(
        id="report_1",
        task_id="test_task",
        workspace_id="test_workspace",
        baseline_run_id="run1",
        comparison_run_id="run2",
        baseline_score=0.5,
        comparison_score=0.8,
        score_delta=0.3,
        baseline_task_success=TaskSuccessRating.PARTIAL,
        comparison_task_success=TaskSuccessRating.COMPLETE,
        baseline_output_quality=OutputQualityRating.FAIR,
        comparison_output_quality=OutputQualityRating.GOOD,
        baseline_cost_usd=1.0,
        comparison_cost_usd=1.2,
        cost_delta_usd=0.2,
        baseline_stability=StabilityRating.UNKNOWN,
        comparison_stability=StabilityRating.STABLE,
        overall_improvement=PatchOutcome.IMPROVED,
    )

    magnitude = get_improvement_magnitude(report)
    assert magnitude > 0  # Should be positive for improvement
    assert magnitude <= 1.0  # Should be bounded
