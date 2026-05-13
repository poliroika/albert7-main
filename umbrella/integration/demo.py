"""
Demo runner for Umbrella manager with realistic scenarios.

This module provides demo scenarios and a runner that demonstrates
the full manager workflow with the agent_research seed workspace.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from umbrella.integration.runner import run_manager_task, ManagerRunResult
from umbrella.control_plane.decision_policy import classify_task
from umbrella.control_plane.task_bridge import to_workspace_task_brief
from umbrella.control_plane.workspace_patching import apply_workspace_patch
from umbrella.evals import compare_runs, evaluate_run
from umbrella.retrieval.service import query_gmas
from umbrella.workspace_registry.discovery import load_seed_profile
from umbrella.workspace_runtime import snapshot_instance, run_workspace
from umbrella.workspace_runtime.instances import (
    create_task_instance,
    update_instance_metadata,
)
from umbrella.workspace_runtime.models import WorkspaceRunRequest, WorkspaceRunStatus

log = logging.getLogger(__name__)


class DemoScenario(StrEnum):
    """Available demo scenarios."""

    ARTICLE_RESEARCH = "article_research"
    PIPELINE_IMPROVEMENT = "pipeline_improvement"
    SIMPLE_TASK = "simple_task"
    WORKSPACE_IMPROVEMENT_CYCLE = "workspace_improvement_cycle"


@dataclass
class DemoConfig:
    """Configuration for demo runs."""

    # Task configuration
    task_input: str
    scenario: DemoScenario

    # Runtime configuration
    repo_root: Path | None = None
    control_state_dir: Path | None = None
    workspaces_root: Path | None = None
    workspace_id: str = "agent_research"  # Default to agent_research

    # Limits
    max_iterations: int | None = 5
    max_duration_seconds: float | None = 120.0
    heartbeat_interval_seconds: float = 30.0

    # LLM configuration (for live runs)
    use_live_llm: bool = False
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None


# Predefined demo tasks
DEMO_TASKS = {
    DemoScenario.ARTICLE_RESEARCH: (
        "Write an article about the impact of large language models on "
        "scientific research and discovery. Focus on how LLMs are changing "
        "the way scientists formulate hypotheses, analyze data, and collaborate."
    ),
    DemoScenario.PIPELINE_IMPROVEMENT: (
        "Analyze the current article research pipeline and suggest improvements "
        "to make it more efficient and produce higher quality articles. Focus on "
        "identifying bottlenecks and proposing concrete changes."
    ),
    DemoScenario.SIMPLE_TASK: (
        "Create a brief summary of what the agent research workspace does "
        "and how it could be used for a simple article writing task."
    ),
    DemoScenario.WORKSPACE_IMPROVEMENT_CYCLE: (
        "Run a simple task twice and show that the second run is better "
        "due to workspace improvements. This demonstrates the full "
        "workspace-first improvement cycle."
    ),
}


def create_demo_runner() -> "DemoRunner":
    """Create a demo runner instance."""
    return DemoRunner()


class DemoRunner:
    """Runner for demo scenarios with configuration and reporting."""

    def __init__(self):
        self.current_config: DemoConfig | None = None
        self.last_result: ManagerRunResult | None = None

    def run_scenario(
        self,
        scenario: DemoScenario,
        *,
        repo_root: Path | None = None,
        control_state_dir: Path | None = None,
        workspaces_root: Path | None = None,
        max_iterations: int | None = 5,
        max_duration_seconds: float | None = 120.0,
        use_live_llm: bool = False,
        heartbeat_interval_seconds: float = 30.0,
        progress_reporter: Any | None = None,
    ) -> ManagerRunResult:
        """Run a demo scenario.

        Args:
            scenario: Which demo scenario to run
            repo_root: Repository root
            control_state_dir: Control state directory
            workspaces_root: Workspaces root
            max_iterations: Max manager iterations. Use ``0``/``None`` for unlimited manager loop.
            max_duration_seconds: Max duration in seconds. Use ``0``/``None`` for unlimited manager loop.
            use_live_llm: Whether to use live LLM (vs degraded mode)
            heartbeat_interval_seconds: Heartbeat interval for long manager runs

        Returns:
            ManagerRunResult with full execution trace
        """
        task_input = DEMO_TASKS.get(scenario)
        if not task_input:
            raise ValueError(f"Unknown scenario: {scenario}")

        config = DemoConfig(
            task_input=task_input,
            scenario=scenario,
            repo_root=repo_root,
            control_state_dir=control_state_dir,
            workspaces_root=workspaces_root,
            max_iterations=max_iterations,
            max_duration_seconds=max_duration_seconds,
            use_live_llm=use_live_llm,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )

        self.current_config = config

        log.info(f"Running demo scenario: {scenario.value}")
        log.info(f"Task: {task_input[:100]}...")
        log.info(f"Live LLM: {config.use_live_llm}")

        # Run the task
        result = run_manager_task(
            task_input=task_input,
            repo_root=config.repo_root,
            control_state_dir=config.control_state_dir,
            workspaces_root=config.workspaces_root,
            workspace_id=config.workspace_id,
            max_iterations=config.max_iterations,
            max_duration_seconds=config.max_duration_seconds,
            use_live_llm=config.use_live_llm,
            heartbeat_interval_seconds=config.heartbeat_interval_seconds,
            progress_reporter=progress_reporter,
        )

        self.last_result = result
        return result

    def run_article_research_demo(
        self,
        repo_root: Path | None = None,
        use_live_llm: bool = False,
    ) -> ManagerRunResult:
        """Run the article research demo.

        This demonstrates the full manager workflow with the agent_research
        seed workspace for an article writing task.

        Args:
            repo_root: Repository root
            use_live_llm: Whether to use live LLM

        Returns:
            ManagerRunResult with execution trace
        """
        return self.run_scenario(
            DemoScenario.ARTICLE_RESEARCH,
            repo_root=repo_root,
            use_live_llm=use_live_llm,
        )

    def print_result_summary(self, result: ManagerRunResult | None = None) -> None:
        """Print a formatted summary of the result.

        Args:
            result: Result to summarize (uses last result if None)
        """
        result = result or self.last_result
        if not result:
            print("No result to summarize")
            return

        print("\n" + "=" * 60)
        print("MANAGER TASK RESULT SUMMARY")
        print("=" * 60)
        print(f"Task ID: {result.task_id}")
        print(f"Status: {result.status}")
        print(f"Task Success: {result.task_success}")
        print(f"Duration: {result.duration_str}")
        print(f"Iterations: {result.iterations}")
        print(f"Workspace: {result.workspace_id or 'None'}")

        if result.phases_visited:
            print(f"\nPhases Visited ({len(result.phases_visited)}):")
            for phase in result.phases_visited:
                print(f"  - {phase}")

        if result.actions_taken:
            print(f"\nActions Taken ({len(result.actions_taken)}):")
            for action in result.actions_taken:
                print(f"  - {action}")

        if result.workspace_changes:
            print(f"\nWorkspace Changes ({len(result.workspace_changes)}):")
            for change in result.workspace_changes[:5]:
                print(f"  - {change[:80]}")

        if result.evidence:
            print(f"\nEvidence ({len(result.evidence)} items):")
            for evidence in result.evidence[:10]:
                print(f"  - {evidence}")

        print(f"\nLessons Recorded: {result.lessons_recorded}")
        print(f"Self-Improvement Considered: {result.self_improvement_considered}")
        print(f"Self-Improvement Applied: {result.self_improvement_applied}")
        print(f"Human Checkpoints Requested: {result.human_checkpoints_requested}")
        print("=" * 60 + "\n")

    def run_workspace_improvement_cycle(
        self,
        *,
        repo_root: Path | None = None,
        control_state_dir: Path | None = None,
        workspaces_root: Path | None = None,
        use_live_llm: bool = False,
    ) -> dict[str, Any]:
        """
        Run a workspace improvement cycle demo.

        This demonstrates:
        1. Running a task on the workspace
        2. Collecting evidence and lessons
        3. Making a workspace patch
        4. Re-running with improved workspace
        5. Showing improvement in results

        Returns:
            Dictionary with "baseline" and "improved" run results
        """
        log.info("=== Workspace Improvement Cycle Demo ===")

        repo_root = repo_root or Path(__file__).resolve().parents[2]
        workspaces_root = workspaces_root or repo_root / "workspaces"
        seed_root = workspaces_root / "agent_research"
        seed_profile = load_seed_profile(seed_root)
        if seed_profile is None:
            raise RuntimeError(f"Could not load seed profile from {seed_root}")

        demo_task = DEMO_TASKS[DemoScenario.SIMPLE_TASK]
        control_brief = classify_task(demo_task, "demo_workspace_improvement")
        runtime_brief = to_workspace_task_brief(
            control_brief,
            preferred_workspace_id="agent_research",
        )
        retrieval_card = query_gmas(repo_root, demo_task, max_results=8)

        baseline_request = WorkspaceRunRequest(
            task_id="demo_workspace_improvement",
            query=demo_task,
            live=use_live_llm,
            mock_loops=True,
            max_agent_executions=12,
            metadata={
                "retrieval_context": "",
                "retrieval_hit_count": 0,
            },
        )

        log.info(
            "Step 1: Creating a real task instance with constrained baseline settings..."
        )
        instance = create_task_instance(
            seed_profile,
            runtime_brief,
            instances_root=workspaces_root / seed_profile.workspace_id / "instances",
            task_id=control_brief.task_id,
            copy_seed_files=True,
        )

        # Make the baseline constraint explicit on disk so the patch has a real before/after.
        update_instance_metadata(
            instance.path,
            {
                "runtime_overrides": {
                    "max_agent_executions": 12,
                    "mock_loops": True,
                }
            },
        )
        baseline_run = run_workspace(instance, baseline_request, prepare=True)
        baseline_eval = evaluate_run(
            baseline_run,
            instance.path,
            task_class=control_brief.task_class,
            previous_evals=[],
            repo_root=repo_root,
        )

        log.info("Step 2: Applying a real workspace patch to the same instance...")
        snapshot = snapshot_instance(
            instance,
            label="demo_pre_patch",
            include_artifacts=True,
        )
        patch_result = apply_workspace_patch(
            instance,
            patch_description="Expand runtime budget, disable forced mock loops, and add evidence refresh edge",
            retrieval_card=retrieval_card,
            inspection_data={
                "manifest": {"status": baseline_run.status.value},
                "error_signatures": list(baseline_run.errors),
            },
            snapshot_path=str(snapshot.snapshot_path),
        )

        log.info("Step 3: Re-running the patched instance...")
        improved_request = WorkspaceRunRequest(
            task_id="demo_workspace_improvement",
            query=demo_task,
            live=use_live_llm,
            mock_loops=True,
            max_agent_executions=12,
            metadata={
                "retrieval_context": (
                    f"Recommended pattern: {retrieval_card.recommended_pattern}\n"
                    + (
                        "\n".join(str(path) for path in retrieval_card.key_files[:5])
                        if retrieval_card.key_files
                        else ""
                    )
                ),
                "retrieval_hit_count": len(retrieval_card.hits),
            },
        )
        improved_run = run_workspace(instance, improved_request, prepare=False)
        improved_eval = evaluate_run(
            improved_run,
            instance.path,
            task_class=control_brief.task_class,
            previous_evals=[baseline_eval],
            repo_root=repo_root,
        )
        comparison = compare_runs(baseline_eval, improved_eval)

        baseline_result = self._runtime_result_to_demo_result(
            task_id=control_brief.task_id,
            run_result=baseline_run,
            eval_record=baseline_eval,
            instance_path=instance.path,
            retrieval_summary="No retrieval guidance injected during constrained baseline.",
            changed_files=[],
        )
        improved_result = self._runtime_result_to_demo_result(
            task_id=control_brief.task_id,
            run_result=improved_run,
            eval_record=improved_eval,
            instance_path=instance.path,
            retrieval_summary=retrieval_card.recommended_pattern,
            changed_files=patch_result.changed_files,
        )
        improved_result.evidence.append(
            f"Evaluation delta vs baseline: {comparison.score_delta:+.2f} ({comparison.overall_improvement.value})"
        )

        log.info("Step 4: Comparing results using evaluation deltas...")
        self._print_improvement_comparison(
            baseline_result,
            improved_result,
            changed_files=patch_result.changed_files,
            score_delta=comparison.score_delta,
            verdict=comparison.overall_improvement.value,
        )

        return {
            "baseline": baseline_result,
            "improved": improved_result,
            "changed_files": patch_result.changed_files,
            "instance_path": str(instance.path),
            "comparison": comparison,
        }

    def _runtime_result_to_demo_result(
        self,
        *,
        task_id: str,
        run_result,
        eval_record,
        instance_path: Path,
        retrieval_summary: str,
        changed_files: list[str],
    ) -> ManagerRunResult:
        """Convert a runtime/eval pair into the ManagerRunResult shape used by demos."""
        status = (
            "complete"
            if run_result.status == WorkspaceRunStatus.COMPLETED
            else "failed"
        )
        final_artifact = next(
            (
                artifact.path
                for artifact in run_result.artifacts
                if artifact.artifact_type.value == "report"
            ),
            None,
        )
        return ManagerRunResult(
            task_id=task_id,
            status=status,
            iterations=1,
            duration_seconds=run_result.duration_seconds,
            workspace_id=run_result.workspace_id,
            task_success=eval_record.task_success.value,
            final_artifact_path=final_artifact,
            instance_path=instance_path,
            run_id=run_result.run_id,
            artifact_paths=[artifact.path for artifact in run_result.artifacts],
            changed_files=list(changed_files),
            retrieval_summary=retrieval_summary,
            evaluation_score=eval_record.overall_score,
            evidence=[
                f"Run status: {run_result.status.value}",
                f"Evaluation score: {eval_record.overall_score:.2f}",
                f"Retrieval summary: {retrieval_summary}",
            ],
        )

    def _generate_improvement_suggestions(self, result: ManagerRunResult) -> list[str]:
        """Generate improvement suggestions based on run results."""
        suggestions = []

        # Analyze what went wrong or could be improved
        if result.status == "failed":
            suggestions.append("Fix workspace configuration issues")
        elif result.status == "partial":
            suggestions.append("Increase max_iterations for complete runs")
            suggestions.append("Add more robust error handling")

        if result.task_success == "unknown":
            suggestions.append("Improve task success evaluation")

        # Duration-based suggestions
        if result.duration_seconds > 20:
            suggestions.append("Optimize runtime performance")

        # Evidence-based suggestions
        if "DEGRADED" in " ".join(result.evidence):
            suggestions.append("Enable full workspace execution mode")

        if not result.workspace_changes:
            suggestions.append("Implement workspace patch mechanism")

        if result.lessons_recorded == 0:
            suggestions.append("Enhance lesson extraction from runs")

        return suggestions or [
            "Increase workspace test coverage",
            "Add more validation checkpoints",
            "Improve error recovery mechanisms",
        ]

    def _print_improvement_comparison(
        self,
        baseline: ManagerRunResult,
        improved: ManagerRunResult,
        *,
        changed_files: list[str],
        score_delta: float,
        verdict: str,
    ) -> None:
        """Print comparison between baseline and improved runs."""
        print("\n" + "=" * 60)
        print("WORKSPACE IMPROVEMENT COMPARISON")
        print("=" * 60)

        print(f"Baseline Status: {baseline.status}")
        print(f"Improved Status: {improved.status}")

        print(f"\nBaseline Duration: {baseline.duration_str}")
        print(f"Improved Duration: {improved.duration_str}")

        print(f"\nBaseline Iterations: {baseline.iterations}")
        print(f"Improved Iterations: {improved.iterations}")

        print(f"\nBaseline Evidence: {len(baseline.evidence)} items")
        print(f"Improved Evidence: {len(improved.evidence)} items")
        print(f"\nChanged Files: {len(changed_files)}")
        for path in changed_files:
            print(f"  - {path}")
        print(f"\nEvaluation Delta: {score_delta:+.2f}")
        print(f"Verdict: {verdict}")

        improvements = []
        if improved.status == "complete" and baseline.status != "complete":
            improvements.append("[OK] Status improved to complete")
        if score_delta > 0:
            improvements.append(f"[OK] Evaluation score improved by {score_delta:.2f}")
        if changed_files:
            improvements.append(
                f"[OK] Real workspace files changed ({len(changed_files)})"
            )

        if improvements:
            print("\nImprovements:")
            for improvement in improvements:
                print(f"  {improvement}")
        else:
            print(
                "\nNo significant improvement detected because no real causal gain was observed"
            )

        print("=" * 60 + "\n")
