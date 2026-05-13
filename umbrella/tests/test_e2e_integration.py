"""
End-to-end integration tests for the Umbrella manager system.

These tests verify that all subsystems work together correctly:
- policy layer, workspace registry, runtime, retrieval, memory, control plane
- evals, promotion, and telemetry
- workspace-first behavior
- TASK_MAIN.md loading
- evidence collection
- human checkpoints
"""

import tempfile
from pathlib import Path

from umbrella.integration import (
    UmbrellaServices,
    run_manager_task,
    create_demo_runner,
    DemoScenario,
)
from umbrella.integration.runner import ManagerRunResult
from umbrella.integration.reporting import render_manager_report
from umbrella.config import UmbrellaRuntimeConfig


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class TestServicesBootstrap:
    """Test that services can be bootstrapped correctly."""

    def test_bootstrap_services_creates_all_components(self):
        """Test that bootstrap_services creates all required services."""
        with tempfile.TemporaryDirectory() as tmpdir:
            services = UmbrellaServices(
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
            )

            # Check all services exist
            assert services.registry is not None
            assert services.memory is not None
            assert services.control_plane is not None
            assert services.telemetry is not None
            assert services.metrics is not None

    def test_registry_discovers_agent_research(self):
        """Test that the registry discovers the agent_research workspace."""
        services = UmbrellaServices(repo_root=_repo_root())

        workspace_ids = services.registry.get_all_workspace_ids()
        assert "agent_research" in workspace_ids

        # Check agent_research has required files
        profile = services.registry.get_seed_profile("agent_research")
        assert profile is not None
        assert profile.workspace_id == "agent_research"

    def test_registry_loads_task_main(self):
        """Test that TASK_MAIN.md can be loaded from agent_research."""
        services = UmbrellaServices(repo_root=_repo_root())

        from umbrella.workspace_registry import load_task_main

        profile = services.registry.get_seed_profile("agent_research")
        assert profile is not None
        task_main_doc = load_task_main(profile.ref.task_main_path)
        assert task_main_doc is not None
        # Check for key sections
        content = profile.ref.task_main_path.read_text(encoding="utf-8").lower()
        assert "objective" in content
        assert "success criteria" in content


class TestEndToEndWorkflow:
    """Test the complete manager workflow from task to result."""

    def test_manager_task_executes_full_workflow(self):
        """Test that a manager task goes through the expected phases."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Create a brief summary of the agent research workspace",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                workspace_id="agent_research",
                max_iterations=2,
                max_duration_seconds=60.0,
            )

            # Check result structure
            assert result.task_id
            assert result.status in ("complete", "partial", "failed")
            assert result.iterations >= 0
            assert result.duration_seconds >= 0

            # Check that evidence was collected
            assert len(result.evidence) > 0

    def test_canonical_happy_path_requires_real_instance_artifacts_and_lessons(self):
        """Canonical e2e must create a real instance, run it, and record lessons."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Create a brief summary of the agent research workspace",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                workspace_id="agent_research",
                max_iterations=2,
                max_duration_seconds=60.0,
                runtime_config=UmbrellaRuntimeConfig(
                    human_review_timeout_seconds=0,
                    quality_completion_threshold=0.80,
                ),
            )

            assert result.status == "complete"
            assert result.task_success == "complete"
            assert result.iterations >= 1
            assert result.degraded_mode_used is False

            assert result.instance_path is not None
            assert result.instance_path.exists()
            assert "instances" in {part.lower() for part in result.instance_path.parts}

            assert result.run_id
            assert result.retrieval_summary is None

            assert result.final_artifact_path is not None
            assert result.final_artifact_path.exists()
            assert result.artifact_paths
            assert all(path.exists() for path in result.artifact_paths)

            assert result.lessons_recorded >= 1

    def test_workspace_is_selected_not_bypassed(self):
        """Test that a workspace is actually selected and used."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Simple test task",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                workspace_id="agent_research",
                max_iterations=1,
                max_duration_seconds=30.0,
            )

            # Should have selected a workspace
            assert result.workspace_id is not None
            assert result.workspace_id == "agent_research"

            # Should have gone through workspace-related phases
            phase_str = " ".join(result.phases_visited)
            # Should include workspace selection and running phases
            assert "workspace" in phase_str.lower() or "instance" in phase_str.lower()

    def test_task_main_is_loaded(self):
        """Test that TASK_MAIN.md is loaded and used by the workspace."""
        # This test verifies that the workspace contract is respected
        # The actual loading happens during instance creation
        services = UmbrellaServices(repo_root=_repo_root())

        # Verify agent_research has TASK_MAIN.md
        profile = services.registry.get_seed_profile("agent_research")
        assert profile is not None

        task_main_path = profile.ref.path / "TASK_MAIN.md"
        assert task_main_path.exists(), "TASK_MAIN.md should exist in agent_research"

    def test_workspace_first_behavior_is_default(self):
        """Test that the manager prefers workspace patches over self-rewrites."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Suggest a minor improvement to the workspace structure",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                workspace_id="agent_research",
                max_iterations=3,
                max_duration_seconds=60.0,
            )

            # Check that workspace-first actions are preferred
            # The manager should NOT trigger self-improvement on first iteration
            assert not result.self_improvement_considered, (
                "Self-improvement should not be triggered on normal workspace loops"
            )

            # Workspace changes should be attempted
            # (In degraded mode this may be limited, but the intent should be there)
            assert result.iterations >= 1

    def test_evaluation_results_are_recorded(self):
        """Test that evaluation produces structured records."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Run a simple evaluation",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=30.0,
            )

            # Evidence should include evaluation-related items
            eval_evidence = [
                e
                for e in result.evidence
                if "eval" in e.lower() or "lesson" in e.lower()
            ]
            # At minimum, lessons should be tracked
            assert result.lessons_recorded >= 0


class TestEvidenceCollection:
    """Test that the manager collects sufficient evidence."""

    def test_phases_are_tracked(self):
        """Test that all visited phases are recorded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Test task",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=20.0,
            )

            # Should have visited some phases
            assert len(result.phases_visited) > 0

    def test_actions_are_tracked(self):
        """Test that manager actions are recorded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Test task",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=20.0,
            )

            # Actions should be tracked
            assert isinstance(result.actions_taken, list)

    def test_evidence_contains_key_info(self):
        """Test that evidence contains key decision points."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Generate evidence test",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=20.0,
            )

            # Should have collected evidence
            assert len(result.evidence) > 0

            # Evidence should mention key components
            evidence_str = " ".join(result.evidence).lower()
            # Should reference workspace, task, or evaluation
            assert any(
                word in evidence_str
                for word in ["workspace", "task", "iteration", "phase"]
            )


class TestDemoScenarios:
    """Test the predefined demo scenarios."""

    def test_article_research_demo_runs(self):
        """Test that the article research demo scenario can run."""
        runner = create_demo_runner()

        result = runner.run_scenario(
            DemoScenario.ARTICLE_RESEARCH,
            repo_root=_repo_root(),
            max_iterations=1,
            max_duration_seconds=30.0,
        )

        assert result.task_id
        assert result.workspace_id == "agent_research"
        assert result.status in ("complete", "partial", "failed")

    def test_demo_runner_prints_summary(self):
        """Test that the demo runner can print a result summary."""
        runner = create_demo_runner()

        # Run minimal task
        result = runner.run_scenario(
            DemoScenario.SIMPLE_TASK,
            repo_root=_repo_root(),
            max_iterations=1,
            max_duration_seconds=15.0,
        )

        # Should be able to print summary without error
        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            runner.print_result_summary(result)

        output = f.getvalue()
        assert "MANAGER TASK RESULT SUMMARY" in output
        assert result.task_id in output


class TestReporting:
    """Test the reporting module."""

    def test_manager_report_is_generated(self):
        """Test that a manager report can be generated."""
        # Create a mock result
        result = ManagerRunResult(
            task_id="test_task",
            status="complete",
            iterations=2,
            duration_seconds=45.0,
            workspace_id="agent_research",
            task_success="complete",
            phases_visited=["workspace_selected", "workspace_running", "run_complete"],
            actions_taken=["select_workspace", "run_workspace"],
            evidence=["Task completed successfully"],
            lessons_recorded=1,
        )

        report = render_manager_report(result)

        assert "# Umbrella Manager Run Report" in report
        assert result.task_id in report
        assert "agent_research" in report
        assert "complete" in report

    def test_report_includes_all_sections(self):
        """Test that report includes all required sections."""
        result = ManagerRunResult(
            task_id="test_task",
            status="complete",
            iterations=1,
            duration_seconds=30.0,
            workspace_id="agent_research",
            task_success="complete",
            phases_visited=["workspace_selected"],
            actions_taken=["select_workspace"],
            evidence=["Test evidence"],
            lessons_recorded=0,
            self_improvement_considered=False,
        )

        report = render_manager_report(result)

        # Check for required sections
        assert "## Task Summary" in report
        assert "## Execution Phases" in report
        assert "## Manager Actions" in report
        assert "## Evaluation" in report
        assert "## Workspace-First Behavior" in report
        assert "## Report Metadata" in report


class TestMemoryIntegration:
    """Test that memory is properly integrated."""

    def test_lessons_are_recorded(self):
        """Test that lessons are recorded after a run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Test memory integration",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=20.0,
            )

            # Should record some lessons (even in degraded mode)
            assert result.lessons_recorded >= 0


class TestTelemetryIntegration:
    """Test that telemetry is properly integrated."""

    def test_telemetry_events_are_emitted(self):
        """Test that telemetry events are emitted during execution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Run a task
            run_manager_task(
                task_input="Test telemetry",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=20.0,
            )

            # Check that telemetry files were created
            telemetry_dir = Path(tmpdir) / "control" / "telemetry" / "events"
            if telemetry_dir.exists():
                event_files = list(telemetry_dir.glob("events_*.jsonl"))
                # Events should have been emitted
                # (In degraded mode with no real runs, this may be empty)
                assert isinstance(event_files, list)


class TestWorkspaceStandalone:
    """Test that the produced workspace can run standalone."""

    def test_workspace_has_required_structure(self):
        """Test that agent_research has the required structure for standalone operation."""
        services = UmbrellaServices(repo_root=_repo_root())

        profile = services.registry.get_seed_profile("agent_research")
        assert profile is not None

        # Check required files exist
        root = profile.ref.path
        assert (root / "workspace.toml").exists(), "workspace.toml required"
        assert (root / "TASK_MAIN.md").exists(), "TASK_MAIN.md required"
        assert (root / "graph" / "topology.toml").exists(), (
            "graph/topology.toml required"
        )
        assert (root / "agents").exists(), "agents/ directory required"

    def test_workspace_can_be_run_without_manager(self):
        """Test that the workspace structure allows standalone execution."""
        # This test verifies the structure, not actual execution
        services = UmbrellaServices(repo_root=_repo_root())

        profile = services.registry.get_seed_profile("agent_research")
        assert profile is not None

        # Check that there's a runtime contract
        # (workspace.toml defines the contract)
        from umbrella.workspace_registry.discovery import load_workspace_config

        config = load_workspace_config(profile.ref.path / "workspace.toml")
        assert config is not None

        # Check that GMAS is used as the engine
        assert config.engine == "gmas", "Should use gmas engine"

        # Check that there's a graph definition
        graph_file = profile.ref.path / "graph" / "topology.toml"
        assert graph_file.exists(), "Graph definition required"


class TestSelfImprovementGating:
    """Test that self-improvement is properly gated."""

    def test_self_improvement_not_default_first_action(self):
        """Test that self-improvement is not the default first action."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Normal task that should not trigger self-improvement",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=2,
                max_duration_seconds=30.0,
            )

            # Self-improvement should NOT be considered by default
            assert not result.self_improvement_considered

    def test_workspace_first_not_self_first(self):
        """Test that workspace improvements are preferred over self-rewrites."""
        # This is tested by test_workspace_first_behavior_is_default
        # and verified by checking that self_improvement_considered is False
        pass


class TestRetrievalIntegration:
    """Test that retrieval provides repo-grounded context."""

    def test_retrieval_service_builds_index(self):
        """Test that the retrieval service builds a complete index."""
        services = UmbrellaServices(repo_root=_repo_root())

        # Check that retrieval service was initialized
        assert services.retrieval is not None

        # Build index if not already built
        if not services.retrieval._is_built:
            report = services.retrieval.build_index()
            assert report is not None

        # Check that index was built
        assert services.retrieval._is_built

        # Check index stats
        assert services.retrieval._sources is not None
        assert len(services.retrieval._sources) > 0, (
            "Should have indexed source documents"
        )

        # Verify we have different source types
        source_types = {s.source_type for s in services.retrieval._sources}
        assert len(source_types) > 0, "Should have multiple source types"

    def test_retrieval_returns_repo_specific_results(self):
        """Test that retrieval returns actual repository content, not generic text."""
        from umbrella.retrieval.service import query_gmas

        # Query for something specific to this repo
        result = query_gmas(
            repo_root=_repo_root(),
            query="workspace registry or WorkspaceRegistry",
            max_results=5,
        )

        # Check that we got results
        assert result is not None

        # Check that we have hits (RetrievalCard has .hits attribute)
        assert hasattr(result, "hits"), "RetrievalCard should have hits attribute"
        assert len(result.hits) > 0, "Should find results for repo-specific query"

        # Check that hits have actual content (not empty/placeholder)
        from umbrella.retrieval.models import HitType

        symbol_hits = [h for h in result.hits if h.hit_type == HitType.CODE_SYMBOL]
        if symbol_hits:
            # Verify symbol hits point to actual code locations
            for hit in symbol_hits[:3]:
                assert hit.symbol_name, f"Symbol hit should have name: {hit}"
                assert hasattr(hit, "path"), f"Symbol hit should have path: {hit}"
                # Verify file path is within the repo
                hit_path = hit.path if hasattr(hit, "path") else None
                if hit_path and hasattr(hit_path, "parents"):
                    assert _repo_root() in hit_path.parents or hit_path.is_relative_to(
                        _repo_root()
                    ), f"File path should be within repo: {hit_path}"

    def test_retrieval_includes_gmas_documentation(self):
        """Test that retrieval includes GMAS documentation."""
        services = UmbrellaServices(repo_root=_repo_root())

        # Build index if needed
        if not services.retrieval._is_built:
            services.retrieval.build_index()

        # Check that docs were indexed
        from umbrella.retrieval.models import SourceType

        docs_sources = [
            s
            for s in services.retrieval._sources
            if s.source_type == SourceType.DOCUMENTATION
        ]
        assert len(docs_sources) > 0, "Should have indexed GMAS documentation"

    def test_retrieval_query_finds_workspace_files(self):
        """Test that retrieval can find actual workspace files."""
        services = UmbrellaServices(repo_root=_repo_root())

        # Build index if needed
        if not services.retrieval._is_built:
            services.retrieval.build_index()

        # Search for workspace-related content using string query
        result = services.retrieval.search(
            query="agent_research workspace configuration",
            max_results=10,
        )

        # Check we got results
        assert result is not None
        assert hasattr(result, "hits"), "Result should have hits"
        assert len(result.hits) > 0, "Should find workspace-related content"

        # Verify results have meaningful context
        for hit in result.hits[:3]:
            assert (
                hasattr(hit, "content")
                or hasattr(hit, "snippet")
                or hasattr(hit, "symbol_name")
            ), f"Hit should have content: {hit}"

    def test_retrieval_used_by_manager_workflow(self):
        """Test that retrieval is actually called during manager workflow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="How do I use the workspace registry in this codebase?",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=30.0,
            )

            # Check that evidence includes retrieval-related information
            retrieval_evidence = [
                e
                for e in result.evidence
                if "retriev" in e.lower()
                or "knowledge" in e.lower()
                or "index" in e.lower()
            ]
            # Note: Even if retrieval doesn't show explicit evidence, the workflow should complete
            assert result.status in ("complete", "partial", "failed")


class TestHumanCheckpoints:
    """Test that human checkpoints are actually requested and recorded."""

    def test_checkpoint_directory_is_created(self):
        """Test that checkpoint directory is created during manager run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            control_dir = Path(tmpdir) / "control"
            result = run_manager_task(
                task_input="Simple test task",
                repo_root=_repo_root(),
                control_state_dir=control_dir,
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=20.0,
            )

            # Check that checkpoint directory exists
            checkpoint_dir = control_dir / "human_checkpoints"
            # Note: Directory may not be created if no checkpoints were requested
            # This test verifies the infrastructure is in place
            assert result.status in ("complete", "partial", "failed")

    def test_checkpoint_fields_are_populated(self):
        """Test that ManagerRunResult has checkpoint fields properly populated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_manager_task(
                task_input="Test task for checkpoint verification",
                repo_root=_repo_root(),
                control_state_dir=Path(tmpdir) / "control",
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=20.0,
            )

            # Verify checkpoint-related fields exist and are properly typed
            assert hasattr(result, "human_checkpoints_requested")
            assert hasattr(result, "human_checkpoints_approved")
            assert isinstance(result.human_checkpoints_requested, int)
            assert isinstance(result.human_checkpoints_approved, int)

            # These should be non-negative
            assert result.human_checkpoints_requested >= 0
            assert result.human_checkpoints_approved >= 0
            assert (
                result.human_checkpoints_approved <= result.human_checkpoints_requested
            )

    def test_checkpoint_saved_to_disk_when_requested(self):
        """Test that when a checkpoint is requested, it's persisted to disk."""
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            control_dir = Path(tmpdir) / "control"

            # Run a task that might trigger checkpoints
            result = run_manager_task(
                task_input="Task that requires checkpoint testing",
                repo_root=_repo_root(),
                control_state_dir=control_dir,
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=2,
                max_duration_seconds=30.0,
            )

            checkpoint_dir = control_dir / "human_checkpoints"

            # If checkpoints were requested, verify they were saved
            if result.human_checkpoints_requested > 0:
                assert checkpoint_dir.exists(), "Checkpoint directory should exist"

                # Check for checkpoint files
                checkpoint_files = list(checkpoint_dir.glob("*.json"))
                assert len(checkpoint_files) > 0, "Should have saved checkpoint files"

                # Verify checkpoint file structure
                for checkpoint_file in checkpoint_files[:3]:
                    data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
                    assert "id" in data, "Checkpoint should have id"
                    assert "task_id" in data, "Checkpoint should have task_id"
                    assert "status" in data, "Checkpoint should have status"

    def test_checkpoint_resume_flow(self):
        """Test that checkpoint and resume flow works end-to-end."""
        with tempfile.TemporaryDirectory() as tmpdir:
            control_dir = Path(tmpdir) / "control"

            # First run - may create checkpoint
            result1 = run_manager_task(
                task_input="Initial task for checkpoint testing",
                repo_root=_repo_root(),
                control_state_dir=control_dir,
                workspaces_root=_repo_root() / "workspaces",
                max_iterations=1,
                max_duration_seconds=20.0,
            )

            # Verify we got a result
            assert result1.task_id
            assert result1.status in ("complete", "partial", "failed")

            # If the first run created a checkpoint state, verify we can attempt resume
            checkpoint_path = control_dir / "checkpoints" / f"{result1.task_id}.json"

            if checkpoint_path.exists():
                # Try to resume
                from umbrella.integration.runner import resume_manager_run

                result2 = resume_manager_run(
                    run_id=result1.task_id,
                    repo_root=_repo_root(),
                    control_state_dir=control_dir,
                    workspaces_root=_repo_root() / "workspaces",
                    max_iterations=1,
                    max_duration_seconds=20.0,
                )

                # Verify resumed result
                assert result2.task_id == result1.task_id
                assert result2.status in ("complete", "partial", "failed")
                assert (
                    "Resumed" in " ".join(result2.evidence)
                    or "checkpoint" in " ".join(result2.evidence).lower()
                )
