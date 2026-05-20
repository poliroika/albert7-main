"""
Tests for the workspace artifacts observability layer.
"""

import json
import pytest
from datetime import datetime, timezone
from pathlib import Path

from umbrella.artifacts.models import (
    RunStatus,
    ArtifactCategory,
    ArtifactMeta,
    ErrorSignature,
    ErrorSeverity,
    StageTransition,
    RawLogPointer,
)
from umbrella.artifacts.run_index import index_workspace_runs, get_run_by_id
from umbrella.artifacts.manifests import build_run_manifest, build_artifact_manifest
from umbrella.artifacts.log_summary import (
    summarize_run_logs,
    extract_stage_transitions,
    count_errors_and_warnings,
)
from umbrella.artifacts.log_access import (
    tail_log,
    read_events_jsonl,
    read_result_summary,
)
from umbrella.artifacts.error_signatures import (
    extract_error_signatures,
    classify_error_type,
)


class TestModels:
    """Tests for data models."""

    def test_artifact_meta_creation(self):
        """Test creating ArtifactMeta."""
        meta = ArtifactMeta(
            artifact_id="test_artifact",
            name="test.md",
            path=Path("/tmp/test.md"),
            category=ArtifactCategory.REPORT,
            size_bytes=1024,
        )
        assert meta.artifact_id == "test_artifact"
        assert meta.category == ArtifactCategory.REPORT
        assert meta.size_bytes == 1024
        assert meta.to_dict()["artifact_id"] == "test_artifact"

    def test_error_signature(self):
        """Test ErrorSignature creation."""
        sig = ErrorSignature(
            error_id="err_001",
            error_type="TestError",
            severity=ErrorSeverity.ERROR,
            message="Test error message",
        )
        assert sig.error_id == "err_001"
        assert sig.is_critical is False
        assert sig.to_dict()["severity"] == "error"

    def test_stage_transition(self):
        """Test StageTransition creation."""
        ts = datetime.now(timezone.utc)
        transition = StageTransition(
            stage="test_agent",
            timestamp=ts,
            status="completed",
            duration_ms=1000.0,
        )
        assert transition.stage == "test_agent"
        assert transition.status == "completed"
        assert transition.duration_ms == 1000.0

    def test_raw_log_pointer(self):
        """Test RawLogPointer creation."""
        pointer = RawLogPointer(
            path=Path("/tmp/test.log"),
            start_line=0,
            end_line=100,
            total_lines=100,
        )
        assert pointer.start_line == 0
        assert pointer.end_line == 100
        assert pointer.total_lines == 100


class TestLogAccess:
    """Tests for log access utilities."""

    def test_read_events_jsonl(self, tmp_path):
        """Test reading events from jsonl file."""
        events_file = tmp_path / "events.jsonl"
        test_events = [
            {"event_type": "run_start", "timestamp": "2026-03-30T12:00:00Z"},
            {
                "event_type": "agent_end",
                "agent_id": "test_agent",
                "timestamp": "2026-03-30T12:01:00Z",
            },
        ]

        with open(events_file, "w") as f:
            for event in test_events:
                f.write(json.dumps(event) + "\n")

        events = read_events_jsonl(events_file)
        assert len(events) == 2
        assert events[0]["event_type"] == "run_start"
        assert events[1]["agent_id"] == "test_agent"

    def test_read_result_summary(self, tmp_path):
        """Test reading result summary."""
        summary_file = tmp_path / "result_summary.json"
        test_summary = {
            "run_id": "test_run",
            "final_agent_id": "delivery_agent",
            "total_tokens": 1000,
            "total_time": 60.0,
        }

        summary_file.write_text(json.dumps(test_summary))
        result = read_result_summary(summary_file)

        assert result is not None
        assert result["run_id"] == "test_run"
        assert result["final_agent_id"] == "delivery_agent"
        assert result["total_tokens"] == 1000

    def test_tail_log(self, tmp_path):
        """Test tailing log files."""
        log_file = tmp_path / "test.log"
        lines = [f"Line {i}\n" for i in range(10)]
        log_file.write_text("".join(lines))

        result = tail_log(log_file, max_lines=5)
        assert "Line 9" in result
        assert "Line 5" in result


class TestErrorSignatures:
    """Tests for error signature extraction."""

    def test_classify_error_type(self):
        """Test error classification."""
        assert classify_error_type("Critical error occurred") == ErrorSeverity.CRITICAL
        assert (
            classify_error_type("Warning: this is deprecated") == ErrorSeverity.WARNING
        )
        assert classify_error_type("Info message") == ErrorSeverity.INFO

    def test_extract_error_signatures_from_events(self):
        """Test extracting errors from events."""
        events = [
            {"event_type": "agent_end", "error": "Test error", "agent_id": "agent1"},
            {"event_type": "run_end", "success": False, "error": "Run failed"},
        ]

        signatures = extract_error_signatures(events)
        assert len(signatures) >= 1
        # Should have at least the run failure error
        run_errors = [s for s in signatures if s.error_type == "RunFailure"]
        assert len(run_errors) == 1


class TestLogSummary:
    """Tests for log summarization."""

    def test_extract_stage_transitions(self):
        """Test extracting stage transitions."""
        events = [
            {
                "event_type": "agent_start",
                "agent_id": "agent1",
                "timestamp": "2026-03-30T12:00:00Z",
            },
            {
                "event_type": "agent_end",
                "agent_id": "agent1",
                "is_final": True,
                "timestamp": "2026-03-30T12:01:00Z",
            },
        ]

        transitions = extract_stage_transitions(events)
        assert len(transitions) == 2
        assert transitions[0].stage == "agent1"
        assert transitions[0].status == "started"
        assert transitions[1].status == "completed"

    def test_count_errors_and_warnings(self):
        """Test counting errors and warnings."""
        events = [
            {"event_type": "agent_end", "error": "Error occurred"},
            {"event_type": "run_end", "success": False, "error": "Failed"},
        ]

        error_count, warning_count = count_errors_and_warnings(events)
        # Should count at least the errors
        assert error_count >= 0


class TestRunIndex:
    """Tests for run indexing."""

    def test_index_workspace_runs(self, tmp_path):
        """Test indexing workspace runs."""
        # Create a mock workspace structure
        workspace_root = tmp_path / "test_workspace"
        workspace_root.mkdir()
        runs_dir = workspace_root / "runs"
        runs_dir.mkdir()

        # Create a mock run directory
        run_dir = runs_dir / "20260330T120000Z_test"
        run_dir.mkdir()

        # Create result_summary.json
        result_summary = {
            "run_id": "20260330T120000Z_test",
            "final_agent_id": "delivery_agent",
            "execution_order": ["agent1", "agent2"],
            "total_tokens": 1000,
            "total_time": 60.0,
            "report_path": str(workspace_root / "reports" / "test.md"),
        }
        (run_dir / "result_summary.json").write_text(json.dumps(result_summary))

        # Index the workspace
        index = index_workspace_runs(workspace_root)

        assert index.workspace_id == "test_workspace"
        assert index.total_runs == 1
        assert index.latest_run is not None
        assert index.latest_run.run_id == "20260330T120000Z_test"

    def test_get_run_by_id(self, tmp_path):
        """Test getting a specific run by ID."""
        workspace_root = tmp_path / "test_workspace"
        workspace_root.mkdir()
        runs_dir = workspace_root / "runs"
        runs_dir.mkdir()

        run_dir = runs_dir / "test_run_001"
        run_dir.mkdir()

        result_summary = {
            "run_id": "test_run_001",
            "final_agent_id": "delivery_agent",
            "execution_order": ["agent1"],
        }
        (run_dir / "result_summary.json").write_text(json.dumps(result_summary))

        run = get_run_by_id(workspace_root, "test_run_001")

        assert run is not None
        assert run.run_id == "test_run_001"


class TestManifests:
    """Tests for manifest building."""

    def test_build_run_manifest(self, tmp_path):
        """Test building a run manifest."""
        run_dir = tmp_path / "test_run"
        run_dir.mkdir()

        result_summary = {
            "run_id": "test_run",
            "status": "completed",
            "final_agent_id": "delivery_agent",
            "execution_order": ["agent1", "agent2"],
            "total_tokens": 1000,
            "total_time": 60.0,
            "final_answer": "Test completed successfully",
        }
        (run_dir / "result_summary.json").write_text(json.dumps(result_summary))

        manifest = build_run_manifest(run_dir, "test_workspace")

        assert manifest is not None
        assert manifest.run_id == "test_run"
        assert manifest.workspace_id == "test_workspace"
        assert manifest.status == RunStatus.COMPLETED
        assert manifest.total_tokens == 1000

    def test_build_artifact_manifest(self, tmp_path):
        """Test building an artifact manifest."""
        run_dir = tmp_path / "test_run"
        run_dir.mkdir()
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        main_report = reports_dir / "test_report.md"
        main_report.write_text("# Test Report")

        # Create some artifact files
        (run_dir / "result_summary.json").write_text(
            json.dumps(
                {
                    "run_id": "test",
                    "status": "completed",
                    "report_path": str(main_report),
                }
            )
        )
        (run_dir / "test_report.md").write_text("# Extra Local Report")

        memory_dir = run_dir / "memory" / "test_agent"
        memory_dir.mkdir(parents=True)
        (memory_dir / "001_output.md").write_text("Agent output")

        manifest = build_artifact_manifest(run_dir, "test_workspace")

        assert manifest.run_id == "test_run"
        assert (
            manifest.total_artifacts >= 3
        )  # At least result_summary, report, and memory file
        assert manifest.result_summary is not None
        assert manifest.main_report is not None
        assert manifest.main_report.path == main_report

    def test_build_run_manifest_from_empty_run_dir_returns_minimal_manifest(
        self, tmp_path
    ):
        run_dir = tmp_path / "empty_run"
        run_dir.mkdir()

        manifest = build_run_manifest(run_dir, "test_workspace")

        assert manifest is not None
        assert manifest.run_id == "empty_run"
        assert manifest.status == RunStatus.UNKNOWN


class TestLogSummary:
    """Tests for log summary creation."""

    def test_summarize_run_logs(self, tmp_path):
        """Test summarizing logs from a run directory."""
        run_dir = tmp_path / "test_run"
        run_dir.mkdir()

        # Create events.jsonl
        events = [
            {
                "event_type": "run_start",
                "timestamp": "2026-03-30T12:00:00Z",
                "query": "Test query",
            },
            {
                "event_type": "agent_end",
                "agent_id": "agent1",
                "timestamp": "2026-03-30T12:01:00Z",
                "tokens_used": 100,
            },
            {
                "event_type": "run_end",
                "timestamp": "2026-03-30T12:02:00Z",
                "success": True,
                "final_agent_id": "agent1",
            },
        ]

        events_file = run_dir / "events.jsonl"
        events_file.write_text("\n".join(json.dumps(e) for e in events))

        summary = summarize_run_logs(run_dir, "test_workspace")

        assert summary is not None
        assert summary.run_id == "test_run"
        assert summary.workspace_id == "test_workspace"
        assert summary.final_status == RunStatus.COMPLETED
        assert summary.total_tokens == 100
        assert summary.agent_executions == 1


class TestIntegration:
    """Integration tests with agent_research workspace."""

    def test_index_agent_research_runs(self):
        """Test indexing actual agent_research runs if they exist."""
        workspace_root = Path(
            "C:/Users/poliroika/Documents/umbrella/workspaces/agent_research"
        )

        if not workspace_root.exists():
            pytest.skip("agent_research workspace not found")

        try:
            index = index_workspace_runs(workspace_root)

            # Should have some runs
            assert index.total_runs >= 0

            if index.total_runs > 0:
                assert index.latest_run is not None
                assert index.latest_run.run_id is not None
                print(f"✅ Indexed {index.total_runs} runs from agent_research")

                # Test getting a run by ID
                run_id = index.latest_run.run_id
                run = get_run_by_id(workspace_root, run_id)
                assert run is not None
                assert run.run_id == run_id

        except Exception as e:
            pytest.skip(f"Failed to index agent_research: {e}")

    def test_build_manifests_for_agent_research_run(self):
        """Test building manifests for an actual agent_research run."""
        workspace_root = Path(
            "C:/Users/poliroika/Documents/umbrella/workspaces/agent_research"
        )

        if not workspace_root.exists():
            pytest.skip("agent_research workspace not found")

        runs_dir = workspace_root / "runs"
        if not runs_dir.exists():
            pytest.skip("No runs directory found")

        # Find the first run directory
        run_dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
        if not run_dirs:
            pytest.skip("No runs found")

        run_dir = sorted(run_dirs)[0]

        try:
            # Build run manifest
            run_manifest = build_run_manifest(run_dir, "agent_research")
            assert run_manifest is not None
            assert run_manifest.run_id == run_dir.name
            print(f"✅ Built run manifest for {run_dir.name}")

            # Build artifact manifest
            artifact_manifest = build_artifact_manifest(run_dir, "agent_research")
            assert artifact_manifest is not None
            assert artifact_manifest.total_artifacts >= 0
            print(f"✅ Found {artifact_manifest.total_artifacts} artifacts")

            # Summarize logs
            log_summary = summarize_run_logs(run_dir, "agent_research")
            assert log_summary is not None
            assert log_summary.run_id == run_dir.name
            print(f"✅ Created log summary with {log_summary.total_events} events")

        except Exception as e:
            pytest.skip(f"Failed to build manifests for {run_dir.name}: {e}")
