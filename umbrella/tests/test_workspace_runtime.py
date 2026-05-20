"""Tests for umbrella.workspace_runtime module."""

import json
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest
pytestmark = pytest.mark.skip(reason="Tests depend on deleted agent_research workspace")

from umbrella.workspace_runtime.models import (
    ArtifactRef,
    ArtifactType,
    PreparedWorkspace,
    WorkspaceInstance,
    WorkspaceRunRequest,
    WorkspaceRunResult,
    WorkspaceRunStatus,
    WorkspaceSnapshot,
    WorkspaceLifecycleStage,
)
from umbrella.workspace_runtime.adapters.base import BaseWorkspaceAdapter
from umbrella.workspace_runtime.adapters.agent_research import AgentResearchAdapter
from umbrella.workspace_runtime.adapters.generic import GenericWorkspaceAdapter
from umbrella.workspace_runtime.adapters.world_prediction import WorldPredictionAdapter
from umbrella.workspace_runtime.context_skills import (
    search_gmas_knowledge,
    search_workspace_context,
)
from umbrella.workspace_runtime.instances import (
    cleanup_detached_instances,
    create_task_instance,
    load_instance_metadata,
    prune_instance_storage,
)
from umbrella.workspace_runtime.checkpoints import CheckpointStore
from umbrella.workspace_runtime.artifacts import (
    ArtifactCollector,
    RunManifest,
    ArtifactStore,
)
from umbrella.workspace_runtime.events import (
    WorkspaceEventEmitter,
    WorkspaceEventType,
    WorkspaceEventLog,
    WorkspaceLifecycleEvent,
)
from umbrella.control_plane.workspace_patching import apply_workspace_patch
from umbrella.workspace_registry.models import (
    SeedWorkspaceProfile,
    TaskBrief,
    WorkspaceRef,
    WorkspaceType,
    WorkspaceMaturity,
)
from umbrella.workspace_registry.registry import build_registry
from umbrella.workspace_runtime import runner as workspace_runner
from umbrella.retrieval.models import RetrievalCard, RetrievalHit, HitType, SourceType


# Repository root fixture
@pytest.fixture
def repo_root():
    """Get the repository root path."""
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def agent_research_ref(repo_root):
    """Create a workspace reference for agent_research."""
    workspace_path = repo_root / "workspaces" / "agent_research"
    return WorkspaceRef(
        workspace_id="agent_research",
        name="Agent Research",
        description="Article pipeline workspace",
        path=workspace_path,
    )


@pytest.fixture
def agent_research_seed(agent_research_ref):
    """Create a seed profile for agent_research."""
    return SeedWorkspaceProfile(
        ref=agent_research_ref,
        workspace_type=WorkspaceType.SEED,
        maturity=WorkspaceMaturity.STABLE,
        primary_task_classes=["article_writing", "research"],
    )


@pytest.fixture
def world_prediction_ref(repo_root):
    """Create a workspace reference for world_prediction."""
    workspace_path = repo_root / "workspaces" / "world_prediction"
    return WorkspaceRef(
        workspace_id="world_prediction",
        name="World Prediction",
        description="Forecasting pipeline workspace",
        path=workspace_path,
    )


@pytest.fixture
def world_prediction_seed(world_prediction_ref):
    """Create a seed profile for world_prediction."""
    return SeedWorkspaceProfile(
        ref=world_prediction_ref,
        workspace_type=WorkspaceType.SEED,
        maturity=WorkspaceMaturity.STABLE,
        primary_task_classes=["forecasting", "research"],
    )


@pytest.fixture
def task_brief():
    """Create a sample task brief."""
    return TaskBrief(
        description="Test article about multi-agent systems",
        task_id="test-task-001",
        task_class="article_writing",
    )


@pytest.fixture
def temp_instances_dir(tmp_path):
    """Create a temporary directory for instances."""
    instances_dir = tmp_path / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    return instances_dir


# ============== Tests ==============


class TestModels:
    """Tests for workspace_runtime models."""

    def test_artifact_ref_creation(self):
        """Test ArtifactRef creation."""
        artifact = ArtifactRef(
            artifact_id="test-001",
            artifact_type=ArtifactType.REPORT,
            path=Path("/tmp/report.md"),
            description="Test report",
        )
        assert artifact.artifact_id == "test-001"
        assert artifact.artifact_type == ArtifactType.REPORT
        assert artifact.description == "Test report"

    def test_workspace_run_request_defaults(self):
        """Test WorkspaceRunRequest with defaults."""
        request = WorkspaceRunRequest(query="Test query")
        assert request.query == "Test query"
        assert request.live is False
        assert request.report_name == "latest_report.md"

    def test_workspace_run_result_creation(self):
        """Test WorkspaceRunResult creation."""
        result = WorkspaceRunResult(
            run_id="run-001",
            workspace_id="test-workspace",
            task_id="task-001",
            status=WorkspaceRunStatus.COMPLETED,
            start_timestamp=datetime.now(UTC),
        )
        assert result.run_id == "run-001"
        assert result.status == WorkspaceRunStatus.COMPLETED
        assert len(result.artifacts) == 0

    def test_workspace_instance_creation(self, agent_research_ref):
        """Test WorkspaceInstance creation."""
        instance = WorkspaceInstance(
            instance_id="inst-001",
            workspace_id="agent_research",
            path=agent_research_ref.path,
        )
        assert instance.instance_id == "inst-001"
        assert instance.workspace_id == "agent_research"


class TestInstances:
    """Tests for instance management."""

    def test_create_task_instance(
        self, agent_research_seed, task_brief, temp_instances_dir
    ):
        """Test creating a task instance from seed."""
        instance = create_task_instance(
            seed=agent_research_seed,
            task=task_brief,
            instances_root=temp_instances_dir,
        )
        assert instance is not None
        # workspace_id contains the full instance identifier
        assert "agent_research" in instance.workspace_id
        assert instance.instance_id.startswith("agent_research_")
        workspace_toml = (instance.path / "workspace.toml").read_text(encoding="utf-8")
        assert f'workspace_id = "{instance.workspace_id}"' in workspace_toml
        assert not (instance.path / "seed_profile.toml").exists()

    def test_load_instance_metadata(
        self, agent_research_seed, task_brief, temp_instances_dir
    ):
        """Test loading instance metadata."""
        instance = create_task_instance(
            seed=agent_research_seed,
            task=task_brief,
            instances_root=temp_instances_dir,
        )
        metadata = load_instance_metadata(instance.path)
        assert metadata is not None
        # Check that instance metadata was created
        assert "instance_id" in metadata
        assert metadata["seed_workspace_id"] == "agent_research"


class TestArtifacts:
    """Tests for artifact management."""

    def test_artifact_store(self, tmp_path):
        """Test ArtifactStore functionality."""
        run_dir = tmp_path / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        store = ArtifactStore(run_dir)

        artifact = ArtifactRef(
            artifact_id="test-artifact",
            artifact_type=ArtifactType.LOG,
            path=run_dir / "test.log",
            description="Test log file",
        )

        store.add_artifact(artifact)

        retrieved = store.get_artifact("test-artifact")
        assert retrieved is not None
        assert retrieved.artifact_id == "test-artifact"

    def test_run_manifest(self, tmp_path):
        """Test RunManifest creation and serialization."""
        result = WorkspaceRunResult(
            run_id="run-001",
            workspace_id="test-workspace",
            task_id="task-001",
            status=WorkspaceRunStatus.COMPLETED,
            start_timestamp=datetime.now(UTC),
        )

        manifest = RunManifest.from_run_result(result)
        assert manifest.run_id == "run-001"
        assert manifest.status.value == "completed"

        # Test serialization
        manifest_dict = manifest.to_dict()
        assert manifest_dict["run_id"] == "run-001"

    def test_artifact_collector(self, tmp_path):
        """Test ArtifactCollector functionality."""
        run_dir = tmp_path / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        # Create some test files
        logs_dir = run_dir / "logs"
        logs_dir.mkdir()
        (logs_dir / "test.log").write_text("Test log content")

        collector = ArtifactCollector(run_dir)
        artifacts = collector.collect_logs()

        assert len(artifacts) == 1
        assert artifacts[0].artifact_type == ArtifactType.LOG


class TestCheckpoints:
    """Tests for checkpoint management."""

    def test_checkpoint_store_creation(self, tmp_path):
        """Test CheckpointStore creation."""
        checkpoints_dir = tmp_path / "checkpoints"
        store = CheckpointStore(checkpoints_dir)
        assert store.checkpoints_dir == checkpoints_dir

    def test_create_checkpoint(self, tmp_path):
        """Test checkpoint creation."""
        checkpoints_dir = tmp_path / "checkpoints"
        store = CheckpointStore(checkpoints_dir)

        checkpoint = store.create_checkpoint(
            instance_id="inst-001",
            run_id="run-001",
            stage=WorkspaceLifecycleStage.RUN,
            state={"progress": 50},
        )

        assert checkpoint is not None
        assert checkpoint.instance_id == "inst-001"
        assert checkpoint.run_id == "run-001"

    def test_load_checkpoint(self, tmp_path):
        """Test checkpoint loading."""
        checkpoints_dir = tmp_path / "checkpoints"
        store = CheckpointStore(checkpoints_dir)

        checkpoint = store.create_checkpoint(
            instance_id="inst-001",
            run_id="run-001",
            stage=WorkspaceLifecycleStage.RUN,
            state={"progress": 50},
        )

        loaded = store.load_checkpoint(checkpoint.checkpoint_id)
        assert loaded is not None
        assert loaded.checkpoint_id == checkpoint.checkpoint_id


class TestEvents:
    """Tests for event management."""

    def test_event_creation(self):
        """Test WorkspaceLifecycleEvent creation."""
        event = WorkspaceLifecycleEvent(
            event_type=WorkspaceEventType.CREATED,
            workspace_id="test-workspace",
            message="Workspace created",
        )
        assert event.event_type == WorkspaceEventType.CREATED
        assert event.workspace_id == "test-workspace"
        assert event.message == "Workspace created"

    def test_event_serialization(self):
        """Test event serialization."""
        event = WorkspaceLifecycleEvent(
            event_type=WorkspaceEventType.COMPLETED,
            workspace_id="test-workspace",
            run_id="run-001",
        )

        event_dict = event.to_dict()
        assert event_dict["event_type"] == "completed"
        assert event_dict["workspace_id"] == "test-workspace"

    def test_event_log(self, tmp_path):
        """Test WorkspaceEventLog functionality."""
        log_path = tmp_path / "events.jsonl"
        event_log = WorkspaceEventLog(log_path)

        event = WorkspaceLifecycleEvent(
            event_type=WorkspaceEventType.RUNNING,
            workspace_id="test-workspace",
        )

        event_log.log(event)

        events = event_log.get_events(workspace_id="test-workspace")
        assert len(events) == 1

    def test_event_emitter(self, tmp_path):
        """Test WorkspaceEventEmitter functionality."""
        log_path = tmp_path / "events.jsonl"
        event_log = WorkspaceEventLog(log_path)
        emitter = WorkspaceEventEmitter(event_log)

        emitter.set_context(workspace_id="test-workspace")

        event = emitter.emit_created()
        assert event.event_type == WorkspaceEventType.CREATED
        assert event.workspace_id == "test-workspace"


class TestRunner:
    """Tests for unified runner entrypoints."""

    def test_get_adapter_for_instance(
        self, agent_research_seed, task_brief, temp_instances_dir
    ):
        instance = create_task_instance(
            seed=agent_research_seed,
            task=task_brief,
            instances_root=temp_instances_dir,
        )
        adapter = workspace_runner.get_adapter_for_instance(instance)
        assert isinstance(adapter, AgentResearchAdapter)

    def test_registry_prefers_task_main_when_seed_contract_exists(self, repo_root):
        registry = build_registry(repo_root)
        profile = registry.get_seed_profile("world_prediction")
        assert profile is not None
        assert profile.ref.task_main_file == "TASK_MAIN.md"

    def test_unknown_seed_raises(self, tmp_path):
        instance = WorkspaceInstance(
            instance_id="orphan",
            workspace_id="no_such_seed",
            seed_workspace_id="unknown_workspace_xyz",
            path=tmp_path,
        )
        with pytest.raises(ValueError, match="No workspace runtime adapter"):
            workspace_runner.get_adapter_for_instance(instance)

    def test_unknown_seed_uses_generic_adapter_when_workspace_exists(self, tmp_path):
        workspace_root = tmp_path / "custom_workspace"
        (workspace_root / "experiments").mkdir(parents=True, exist_ok=True)
        (workspace_root / "workspace.toml").write_text(
            'workspace_id = "custom_workspace"\nname = "Custom Workspace"\n',
            encoding="utf-8",
        )
        (workspace_root / "TASK_MAIN.md").write_text(
            "# Task\n\nTest task.\n", encoding="utf-8"
        )
        (workspace_root / "experiments" / "run_pipeline.py").write_text(
            """
async def run_pipeline(query, **kwargs):
    return {
        "status": "completed",
        "run_id": "custom_run_001",
        "run_dir": kwargs["workspace_root"] / "runs" / "custom_run_001",
        "report_path": str(kwargs["workspace_root"] / "reports" / "latest_report.md"),
        "final_agent_id": "delivery",
        "final_answer": query,
        "execution_order": ["delivery"],
        "total_tokens": 7,
    }
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (workspace_root / "reports").mkdir(parents=True, exist_ok=True)
        (workspace_root / "reports" / "latest_report.md").write_text(
            "custom report\n", encoding="utf-8"
        )

        instance = WorkspaceInstance(
            instance_id="generic",
            workspace_id="custom_workspace_instance",
            seed_workspace_id="custom_workspace",
            path=workspace_root,
        )

        adapter = workspace_runner.get_adapter_for_instance(instance)
        assert isinstance(adapter, GenericWorkspaceAdapter)

    def test_register_adapter_supports_additional_seed(self, tmp_path):
        instance = WorkspaceInstance(
            instance_id="custom",
            workspace_id="custom_instance",
            seed_workspace_id="code_lab",
            path=tmp_path,
        )

        class CodeLabAdapter(BaseWorkspaceAdapter):
            def run(self, request):
                return WorkspaceRunResult(
                    workspace_id=instance.workspace_id,
                    status=WorkspaceRunStatus.COMPLETED,
                    final_answer="code lab completed",
                )

            def snapshot(self, instance, label, include_artifacts=True):
                snapshot_path = instance.path / "snapshots" / label
                snapshot_path.mkdir(parents=True, exist_ok=True)
                return WorkspaceSnapshot(
                    instance_id=instance.instance_id,
                    workspace_id=instance.workspace_id,
                    label=label,
                    snapshot_path=snapshot_path,
                    source_path=instance.path,
                )

            def supports_workspace(self, workspace_id: str) -> bool:
                return workspace_id == "code_lab"

            def get_supported_workspace_types(self) -> list[str]:
                return ["code_lab"]

        original = workspace_runner._ADAPTER_BY_SEED_ID.copy()
        try:
            workspace_runner.register_adapter("code_lab", CodeLabAdapter)
            adapter = workspace_runner.get_adapter_for_instance(instance)
            assert isinstance(adapter, CodeLabAdapter)
        finally:
            workspace_runner._ADAPTER_BY_SEED_ID.clear()
            workspace_runner._ADAPTER_BY_SEED_ID.update(original)

    def test_prepare_instance(
        self, agent_research_seed, task_brief, temp_instances_dir
    ):
        instance = create_task_instance(
            seed=agent_research_seed,
            task=task_brief,
            instances_root=temp_instances_dir,
        )
        prepared = workspace_runner.prepare_instance(instance)
        assert prepared.ready is True
        assert prepared.config_valid is True

    def test_get_adapter_for_world_prediction_instance(
        self,
        world_prediction_seed,
        task_brief,
        temp_instances_dir,
    ):
        instance = create_task_instance(
            seed=world_prediction_seed,
            task=task_brief,
            instances_root=temp_instances_dir,
        )
        adapter = workspace_runner.get_adapter_for_instance(instance)
        assert isinstance(adapter, WorldPredictionAdapter)

    def test_run_workspace_fails_fast_when_not_ready(self, tmp_path):
        bad_root = tmp_path / "empty_ws"
        bad_root.mkdir()
        instance = WorkspaceInstance(
            instance_id="bad",
            workspace_id="bad",
            seed_workspace_id="agent_research",
            path=bad_root,
        )
        req = WorkspaceRunRequest(query="q")
        result = workspace_runner.run_workspace(instance, req, prepare=True)
        assert result.status == WorkspaceRunStatus.FAILED
        assert result.errors

    def test_build_task_brief_for_instance(
        self, agent_research_seed, task_brief, temp_instances_dir
    ):
        instance = create_task_instance(
            seed=agent_research_seed,
            task=task_brief,
            instances_root=temp_instances_dir,
        )
        brief = workspace_runner.build_task_brief_for_instance(instance)
        assert brief is not None
        assert brief.description

    def test_package_import_exports_runner(self):
        import umbrella.workspace_runtime as wr

        assert callable(wr.run_workspace)
        assert callable(wr.create_instance_and_run)

    def test_run_workspace_does_not_write_instance_metadata_into_seed(
        self, tmp_path, monkeypatch
    ):
        seed_path = tmp_path / "workspaces" / "agent_research"
        seed_path.mkdir(parents=True)
        instance = WorkspaceInstance(
            instance_id="seed-run",
            workspace_id="agent_research",
            path=seed_path,
        )

        class DummyAdapter:
            def prepare(self):
                return PreparedWorkspace(
                    instance=instance, ready=True, config_valid=True
                )

            def run(self, request):
                return WorkspaceRunResult(
                    run_id="run-001",
                    workspace_id=instance.workspace_id,
                    status=WorkspaceRunStatus.COMPLETED,
                    start_timestamp=datetime.now(UTC),
                )

        monkeypatch.setattr(
            workspace_runner, "get_adapter_for_instance", lambda _: DummyAdapter()
        )
        result = workspace_runner.run_workspace(
            instance, WorkspaceRunRequest(query="seed run"), prepare=True
        )

        assert result.status == WorkspaceRunStatus.COMPLETED
        assert not (seed_path / "instance_metadata.json").exists()

    def test_workspace_patch_changes_instance_graph(
        self, agent_research_seed, task_brief, temp_instances_dir
    ):
        instance = create_task_instance(
            seed=agent_research_seed,
            task=task_brief,
            instances_root=temp_instances_dir,
        )
        seed_topology = (
            agent_research_seed.path / "graph" / "topology.toml"
        ).read_text(encoding="utf-8")

        patch_result = apply_workspace_patch(
            instance,
            patch_description="Insert evidence rewrite loop",
        )

        instance_topology_path = instance.path / "graph" / "topology.toml"
        instance_topology = instance_topology_path.read_text(encoding="utf-8")

        assert patch_result.applied is True
        assert "draft_rewrite_evidence_loop_v1" in instance_topology
        if "draft_rewrite_evidence_loop_v1" in seed_topology:
            assert patch_result.graph_changed is False
            assert str(instance_topology_path) not in patch_result.changed_files
            assert instance_topology == seed_topology
        else:
            assert patch_result.graph_changed is True
            assert str(instance_topology_path) in patch_result.changed_files
            assert instance_topology != seed_topology

    def test_workspace_patch_uses_generic_strategy_for_world_prediction(
        self,
        world_prediction_seed,
        task_brief,
        temp_instances_dir,
    ):
        instance = create_task_instance(
            seed=world_prediction_seed,
            task=task_brief,
            instances_root=temp_instances_dir,
        )
        seed_topology = (
            world_prediction_seed.path / "graph" / "topology.toml"
        ).read_text(encoding="utf-8")

        patch_result = apply_workspace_patch(
            instance,
            patch_description="Tune forecasting instance",
        )

        metadata = load_instance_metadata(instance.path) or {}
        runtime_overrides = metadata.get("runtime_overrides", {})
        instance_topology_path = instance.path / "graph" / "topology.toml"
        instance_topology = instance_topology_path.read_text(encoding="utf-8")

        assert patch_result.applied is True
        assert patch_result.graph_changed is False
        assert str(instance_topology_path) not in patch_result.changed_files
        assert instance_topology == seed_topology
        assert (
            "forecasting guidance"
            in str(runtime_overrides.get("query_suffix", "")).lower()
        )

    def test_prune_instance_storage_keeps_latest_artifacts(
        self, agent_research_seed, task_brief, temp_instances_dir
    ):
        instance = create_task_instance(
            seed=agent_research_seed,
            task=task_brief,
            instances_root=temp_instances_dir,
        )

        old_run = instance.path / "runs" / "run_old"
        mid_run = instance.path / "runs" / "run_mid"
        new_run = instance.path / "runs" / "run_new"
        for idx, run_dir in enumerate([old_run, mid_run, new_run], start=1):
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "result.txt").write_text(f"run {idx}\n", encoding="utf-8")

        old_snapshot = instance.path / "snapshots" / "snap_old"
        new_snapshot = instance.path / "snapshots" / "snap_new"
        for idx, snap_dir in enumerate([old_snapshot, new_snapshot], start=1):
            snap_dir.mkdir(parents=True, exist_ok=True)
            (snap_dir / "snapshot_metadata.json").write_text(
                json.dumps({"snapshot_id": f"s{idx}"}),
                encoding="utf-8",
            )

        latest_report = instance.path / "reports" / "latest_report.md"
        older_report = instance.path / "reports" / "20240101_report.md"
        newer_report = instance.path / "reports" / "20240102_report.md"
        latest_report.write_text("latest\n", encoding="utf-8")
        older_report.write_text("old\n", encoding="utf-8")
        newer_report.write_text("new\n", encoding="utf-8")

        old_log = instance.path / "logs" / "old.log"
        new_log = instance.path / "logs" / "new.log"
        old_log.write_text("old\n", encoding="utf-8")
        new_log.write_text("new\n", encoding="utf-8")

        for timestamp, path in enumerate(
            [
                old_run,
                mid_run,
                new_run,
                old_snapshot,
                new_snapshot,
                older_report,
                newer_report,
                latest_report,
                old_log,
                new_log,
            ],
            start=1,
        ):
            os.utime(path, (timestamp, timestamp))

        removed = prune_instance_storage(
            instance.path,
            keep_run_dirs=2,
            keep_snapshots=1,
            keep_report_files=1,
            keep_log_files=1,
        )

        assert str(old_run) in removed
        assert str(old_snapshot) in removed
        assert str(older_report) in removed
        assert str(old_log) in removed
        assert not old_run.exists()
        assert not old_snapshot.exists()
        assert not older_report.exists()
        assert not old_log.exists()
        assert mid_run.exists()
        assert new_run.exists()
        assert latest_report.exists()
        assert newer_report.exists()
        metadata = load_instance_metadata(instance.path) or {}
        assert metadata.get("storage_pruned_entries", 0) >= 4

    def test_cleanup_detached_instances_keeps_active_and_latest(
        self, temp_instances_dir
    ):
        def make_instance(name: str, timestamp: int) -> Path:
            path = temp_instances_dir / name
            path.mkdir(parents=True, exist_ok=True)
            (path / "instance_metadata.json").write_text(
                json.dumps({"instance_id": name, "status": "complete"}),
                encoding="utf-8",
            )
            os.utime(path, (timestamp, timestamp))
            return path

        oldest = make_instance("agent_research_instance_old", 1)
        active = make_instance("agent_research_instance_active", 2)
        newest = make_instance("agent_research_instance_new", 3)

        removed = cleanup_detached_instances(
            temp_instances_dir,
            active_instance_paths=[active],
            keep_latest_detached=1,
        )

        assert str(oldest) in removed
        assert not oldest.exists()
        assert active.exists()
        assert newest.exists()


class TestContextSkills:
    """Tests for optional on-demand context skill helpers."""

    @staticmethod
    def _write_minimal_docx(path: Path, text: str) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
            )
            archive.writestr(
                "_rels/.rels",
                """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>""",
            )
            archive.writestr(
                "word/document.xml",
                f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{text}</w:t></w:r></w:p>
  </w:body>
</w:document>""",
            )

    def test_search_workspace_context_can_read_specific_file(self, tmp_path):
        workspace_root = tmp_path / "workspace"
        logs_dir = workspace_root / "runs" / "run-001"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "events.log"
        log_path.write_text("line one\nline two\n", encoding="utf-8")

        output = search_workspace_context(
            workspace_root,
            read_file="runs/run-001/events.log",
        )

        assert (
            "runs\\run-001\\events.log" in output or "runs/run-001/events.log" in output
        )
        assert "line one" in output

    def test_search_workspace_context_previews_docx_as_text(self, tmp_path):
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        self._write_minimal_docx(workspace_root / "brief.docx", "docx preview text")

        output = search_workspace_context(
            workspace_root,
            read_file="brief.docx",
        )

        assert "docx preview text" in output
        assert "office_docx" in output
        assert "PK" not in output

    def test_search_workspace_context_can_search_logs_and_code(self, tmp_path):
        workspace_root = tmp_path / "workspace"
        (workspace_root / "logs").mkdir(parents=True, exist_ok=True)
        (workspace_root / "logs" / "app.log").write_text(
            "ERROR: failed to route task\n", encoding="utf-8"
        )
        (workspace_root / "prompts").mkdir(parents=True, exist_ok=True)
        (workspace_root / "prompts" / "lead.md").write_text(
            "route task to evidence branch\n", encoding="utf-8"
        )

        output = search_workspace_context(
            workspace_root,
            pattern="*",
            query="route task",
        )

        assert "logs" in output or "prompts" in output
        assert "route task" in output.lower()

    def test_search_gmas_knowledge_formats_card(self, tmp_path):
        fake_card = RetrievalCard(
            query="How do I build a graph?",
            recommended_pattern="Use GraphBuilder, then connect workflow edges before building.",
            key_symbols=["gmas.builder.graph_builder.GraphBuilder"],
            key_files=["gmas/src/gmas/builder/graph_builder.py"],
            example_usage=[
                "builder.add_agent(...); builder.add_workflow_edge(...); builder.build()"
            ],
            anti_patterns=["Do not mutate the graph schema directly after build()."],
            hits=[
                RetrievalHit(
                    hit_id="hit-001",
                    hit_type=HitType.DOCUMENT_CHUNK,
                    score=0.9,
                    source_id="graph_builder",
                    source_type=SourceType.SOURCE_CODE,
                    title="GraphBuilder",
                    excerpt="builder.add_agent(...) and builder.add_workflow_edge(...)",
                    path=tmp_path
                    / "gmas"
                    / "src"
                    / "gmas"
                    / "builder"
                    / "graph_builder.py",
                    line_number=10,
                )
            ],
            confidence=0.9,
        )

        with patch(
            "umbrella.workspace_runtime.context_skills.query_gmas",
            return_value=fake_card,
        ):
            output = search_gmas_knowledge(tmp_path, "How do I build a graph?")

        assert "Recommended pattern" in output
        assert "GraphBuilder" in output
        assert "graph_builder.py" in output


class TestAdapters:
    """Tests for workspace adapters."""

    def test_base_adapter_protocol(self):
        """Test that adapters follow the protocol."""
        # BaseWorkspaceAdapter is abstract, so we test the interface
        from umbrella.workspace_runtime.adapters.base import BaseWorkspaceAdapter

        assert hasattr(BaseWorkspaceAdapter, "prepare")
        assert hasattr(BaseWorkspaceAdapter, "run")
        assert hasattr(BaseWorkspaceAdapter, "inspect")

    def test_agent_research_adapter_exists(self):
        """Test that AgentResearchAdapter exists and has required methods."""
        assert hasattr(AgentResearchAdapter, "workspace_type")
        assert AgentResearchAdapter.workspace_type == "agent_research"

    def test_world_prediction_adapter_runs_stub_pipeline(self, tmp_path):
        workspace_root = tmp_path / "world_prediction_instance"
        (workspace_root / "graph").mkdir(parents=True, exist_ok=True)
        (workspace_root / "agents").mkdir(parents=True, exist_ok=True)
        (workspace_root / "experiments").mkdir(parents=True, exist_ok=True)
        (workspace_root / "reports").mkdir(parents=True, exist_ok=True)
        (workspace_root / "snapshots").mkdir(parents=True, exist_ok=True)
        (workspace_root / "workspace.toml").write_text(
            'workspace_id = "world_prediction"\nname = "World Prediction"\n',
            encoding="utf-8",
        )
        (workspace_root / "TASK_MAIN.md").write_text(
            "# Task\n\nTest forecast.\n", encoding="utf-8"
        )
        (workspace_root / "graph" / "topology.toml").write_text(
            "agents = []\n", encoding="utf-8"
        )
        (workspace_root / "instance_metadata.json").write_text(
            json.dumps(
                {
                    "runtime_overrides": {
                        "query_suffix": "Focus on base rates.",
                        "max_agent_executions": 64,
                    }
                }
            ),
            encoding="utf-8",
        )
        (workspace_root / "experiments" / "run_prediction_pipeline.py").write_text(
            """
import json
from pathlib import Path


async def run_pipeline(
    query,
    *,
    live,
    live_model,
    live_api_key,
    live_base_url,
    live_temperature,
    live_max_tokens,
    live_tool_choice,
    max_agent_executions,
):
    workspace_root = Path(__file__).resolve().parents[1]
    run_dir = workspace_root / "runs" / "stub_run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text(json.dumps({"event_type": "run_start"}) + "\\n", encoding="utf-8")
    snapshot_slug = workspace_root / "snapshots" / "forecast_stub"
    snapshot_slug.mkdir(parents=True, exist_ok=True)
    (snapshot_slug / "stub_run_001_graph.json").write_text("{}", encoding="utf-8")
    report_dir = workspace_root / "reports" / "forecast_stub"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "latest_prediction.md"
    report_path.write_text(f"# Stub Prediction\\n\\n{query}\\n\\nsteps={max_agent_executions}\\n", encoding="utf-8")
    return {
        "status": "completed",
        "forecast_id": "forecast_stub",
        "run_id": "stub_run_001",
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "final_agent_id": "delivery_agent",
        "final_answer": f"Stub answer for: {query}",
        "execution_order": ["query_analyzer", "delivery_agent"],
        "total_tokens": 42,
    }
""".strip()
            + "\n",
            encoding="utf-8",
        )

        instance = WorkspaceInstance(
            instance_id="wp_inst",
            workspace_id="world_prediction_instance",
            seed_workspace_id="world_prediction",
            path=workspace_root,
        )
        adapter = WorldPredictionAdapter(instance)
        prepared = adapter.prepare()
        assert prepared.ready is True

        result = adapter.run(
            WorkspaceRunRequest(
                query="Will inflation fall this year?",
                metadata={
                    "retrieval_context": "Use historical calibration data.",
                    "retrieval_hit_count": 3,
                },
            )
        )

        assert result.status == WorkspaceRunStatus.COMPLETED
        assert result.run_manifest_path is not None
        manifest = json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"
        assert "historical calibration data" in manifest["query"].lower()
        assert "focus on base rates" in manifest["query"].lower()
        assert result.metrics["max_agent_executions_effective"] == 64
        assert result.get_artifacts_by_type(ArtifactType.REPORT)

    def test_generic_adapter_runs_convention_workspace(self, tmp_path):
        workspace_root = tmp_path / "custom_workspace"
        (workspace_root / "graph").mkdir(parents=True, exist_ok=True)
        (workspace_root / "agents").mkdir(parents=True, exist_ok=True)
        (workspace_root / "experiments").mkdir(parents=True, exist_ok=True)
        (workspace_root / "reports").mkdir(parents=True, exist_ok=True)
        (workspace_root / "workspace.toml").write_text(
            'workspace_id = "custom_workspace"\nname = "Custom Workspace"\ngraph_file = "graph/topology.toml"\nagents_dir = "agents"\n',
            encoding="utf-8",
        )
        (workspace_root / "TASK_MAIN.md").write_text(
            "# Task\n\nSummarize this workspace.\n", encoding="utf-8"
        )
        (workspace_root / "graph" / "topology.toml").write_text(
            "agents = []\n", encoding="utf-8"
        )
        (workspace_root / "instance_metadata.json").write_text(
            json.dumps({"runtime_overrides": {"query_suffix": "Stay concrete."}}),
            encoding="utf-8",
        )
        (workspace_root / "experiments" / "run_pipeline.py").write_text(
            """
from pathlib import Path


async def run_pipeline(query, *, workspace_root, max_agent_executions, metadata, **kwargs):
    run_dir = Path(workspace_root) / "runs" / "generic_run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.jsonl").write_text("{\\"event_type\\": \\"run_start\\"}\\n", encoding="utf-8")
    report_path = Path(workspace_root) / "reports" / "latest_report.md"
    report_path.write_text(
        f"# Generic Report\\n\\n{query}\\n\\nmax_steps={max_agent_executions}\\n\\nmetadata={metadata}\\n",
        encoding="utf-8",
    )
    return {
        "status": "completed",
        "run_id": "generic_run_001",
        "run_dir": str(run_dir),
        "report_path": str(report_path),
        "final_agent_id": "generic_delivery",
        "final_answer": f"done: {query}",
        "execution_order": ["generic_delivery"],
        "total_tokens": 11,
    }
""".strip()
            + "\n",
            encoding="utf-8",
        )

        instance = WorkspaceInstance(
            instance_id="custom_inst",
            workspace_id="custom_workspace_instance",
            seed_workspace_id="custom_workspace",
            path=workspace_root,
        )
        adapter = GenericWorkspaceAdapter(instance)
        prepared = adapter.prepare()
        assert prepared.ready is True

        result = adapter.run(
            WorkspaceRunRequest(
                query="Summarize this workspace",
                metadata={
                    "retrieval_context": "Use repo structure.",
                    "retrieval_hit_count": 2,
                },
            )
        )

        assert result.status == WorkspaceRunStatus.COMPLETED
        assert result.run_manifest_path is not None
        manifest = json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"
        assert "use repo structure" in manifest["query"].lower()
        assert "stay concrete" in manifest["query"].lower()
        assert result.metrics["generic_adapter_used"] is True
        assert result.get_artifacts_by_type(ArtifactType.REPORT)
