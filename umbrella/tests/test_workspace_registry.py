"""
Tests for the workspace registry.

Tests cover:
- Workspace discovery
- Loading workspace configs
- Seed profile loading
- Validation
- Selection
- Lineage tracking
"""

from pathlib import Path

from umbrella.workspace_registry.models import (
    WorkspaceRef,
    SeedWorkspaceProfile,
    TaskBrief,
    ValidationSeverity,
    WorkspaceType,
    WorkspaceCapability,
)
from umbrella.workspace_registry.discovery import (
    discover_workspaces,
    load_registry_manifest,
    load_workspace_config,
    load_seed_profile,
    load_task_instance_profile,
)
from umbrella.workspace_registry.validation import (
    validate_workspace,
)
from umbrella.workspace_registry.registry import (
    build_registry,
)
from umbrella.workspace_registry.lineage import (
    create_task_instance_record,
    LineageTracker,
)
from umbrella.workspace_registry.selectors import (
    score_workspace_for_task,
)
from umbrella.workspace_runtime.instances import create_task_instance

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestModels:
    """Tests for data models."""

    def test_workspace_ref_creation(self, tmp_path):
        """Test creating a workspace reference."""
        ref = WorkspaceRef(
            workspace_id="test_workspace",
            name="Test Workspace",
            description="A test workspace",
            path=tmp_path,
        )
        assert ref.workspace_id == "test_workspace"
        assert ref.name == "Test Workspace"

    def test_workspace_capability(self):
        """Test workspace capability."""
        cap = WorkspaceCapability(
            name="article_writing",
            description="Write articles",
            weight=1.5,
        )
        assert cap.name == "article_writing"
        assert cap.weight == 1.5

    def test_task_brief(self):
        """Test task brief."""
        brief = TaskBrief(
            description="Write an article about AI",
            task_class="article_writing",
        )
        assert brief.description == "Write an article about AI"
        assert brief.task_class == "article_writing"


class TestDiscovery:
    """Tests for workspace discovery."""

    def test_discover_workspaces(self, tmp_path):
        """Test workspace discovery."""
        # Create workspace directory structure
        workspace_dir = tmp_path / "workspaces" / "test_workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        # Create workspace.toml
        config_content = """
workspace_id = "test_workspace"
name = "Test Workspace"
description = "A test workspace"

[metadata]
engine = "gmas"
"""
        (workspace_dir / "workspace.toml").write_text(config_content)

        # Discover workspaces
        workspaces = discover_workspaces(tmp_path)
        assert len(workspaces) == 1
        assert workspaces[0].workspace_id == "test_workspace"

    def test_load_workspace_config(self, tmp_path):
        """Test loading workspace configuration."""
        workspace_dir = tmp_path / "test_workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        config_path = workspace_dir / "workspace.toml"
        config_content = """
workspace_id = "my_workspace"
name = "My Workspace"
description = "My test workspace"
task_main_file = "TASK_MAIN.md"

[metadata]
engine = "gmas"
engine_mutable = false
"""
        config_path.write_text(config_content)

        ref = load_workspace_config(config_path)
        assert ref is not None
        assert ref.workspace_id == "my_workspace"
        assert ref.name == "My Workspace"

    def test_discover_skips_invalid_workspace_toml(self, tmp_path):
        workspace_dir = tmp_path / "workspaces" / "broken"
        workspace_dir.mkdir(parents=True)
        (workspace_dir / "workspace.toml").write_text("this is not valid toml [[[")

        assert discover_workspaces(tmp_path) == []

    def test_load_workspace_config_missing_file(self, tmp_path):
        assert load_workspace_config(tmp_path / "nope.toml") is None

    def test_discover_materialized_instance_under_instances_dir(self, tmp_path):
        seed_dir = tmp_path / "workspaces" / "agent_research"
        seed_dir.mkdir(parents=True)
        (seed_dir / "workspace.toml").write_text(
            'workspace_id = "agent_research"\nname = "Agent Research"\ndescription = "Seed"\n',
            encoding="utf-8",
        )
        (seed_dir / "TASK_MAIN.md").write_text("# Task\n\nSeed", encoding="utf-8")
        (seed_dir / "seed_profile.toml").write_text(
            'primary_task_classes = ["article_writing"]\n', encoding="utf-8"
        )

        seed = load_seed_profile(seed_dir)
        assert seed is not None

        instance = create_task_instance(
            seed=seed,
            task=TaskBrief(
                description="Instance task", task_id="t-1", task_class="article_writing"
            ),
            instances_root=tmp_path / "workspaces" / "instances",
        )

        workspaces = discover_workspaces(tmp_path)
        workspace_ids = {workspace.workspace_id for workspace in workspaces}
        assert "agent_research" in workspace_ids
        assert instance.workspace_id in workspace_ids

    def test_load_task_instance_profile(self, tmp_path):
        workspace_dir = tmp_path / "workspaces" / "instances" / "task_ws"
        workspace_dir.mkdir(parents=True)
        (workspace_dir / "workspace.toml").write_text(
            'workspace_id = "task_ws"\nname = "Task WS"\ndescription = "Instance"\n',
            encoding="utf-8",
        )
        (workspace_dir / "instance_metadata.json").write_text(
            '{"instance_id":"task_ws","seed_workspace_id":"agent_research","task_id":"t-1","task_description":"test","task_class":"article_writing","status":"created","run_count":0,"lineage":{"creation_timestamp":"2026-03-30T00:00:00+00:00","creation_reason":"task"}}',
            encoding="utf-8",
        )

        profile = load_task_instance_profile(workspace_dir)
        assert profile is not None
        assert profile.workspace_type == WorkspaceType.INSTANCE
        assert profile.lineage.seed_workspace_id == "agent_research"
        assert profile.workspace_id == "task_ws"


class TestValidation:
    """Tests for workspace validation."""

    def test_validate_missing_task_main(self, tmp_path):
        """Test validation when TASK_MAIN.md is missing."""
        ref = WorkspaceRef(
            workspace_id="test",
            name="Test",
            description="Test",
            path=tmp_path,
        )
        issues = validate_workspace(ref)
        assert any(i.severity == ValidationSeverity.ERROR for i in issues)

        assert any("TASK_MAIN" in i.message for i in issues)

    def test_validate_with_task_main(self, tmp_path):
        """Test validation when TASK_MAIN.md exists."""
        ref = WorkspaceRef(
            workspace_id="test",
            name="Test",
            description="Test",
            path=tmp_path,
        )

        # Create TASK_MAIN.md
        task_main = tmp_path / "TASK_MAIN.md"
        task_main.write_text(
            "# Task\n\nThis is a test task with enough content to pass validation."
        )

        issues = validate_workspace(ref)
        # Should not have errors about TASK_MAIN.md
        error_messages = [
            i.message for i in issues if i.severity == ValidationSeverity.ERROR
        ]
        assert not any("TASK_MAIN" in m for m in error_messages)


class TestSelection:
    """Tests for workspace selection."""

    def test_score_workspace(self, tmp_path):
        """Test scoring a workspace for a task."""
        ref = WorkspaceRef(
            workspace_id="test",
            name="Test",
            description="Test workspace",
            path=tmp_path,
        )
        profile = SeedWorkspaceProfile(
            ref=ref,
            primary_task_classes=["article_writing"],
            capabilities=[
                WorkspaceCapability(
                    name="article_writing", description="Write articles", weight=1.5
                ),
            ],
        )

        brief = TaskBrief(
            description="Write an article about AI",
            task_class="article_writing",
        )

        score = score_workspace_for_task(brief, profile)
        assert score > 0.0


class TestLineage:
    """Tests for lineage tracking."""

    def test_create_task_instance_record(self, tmp_path):
        """Test creating a task instance record."""
        ref = WorkspaceRef(
            workspace_id="seed",
            name="Seed",
            description="Seed workspace",
            path=tmp_path,
        )
        seed = SeedWorkspaceProfile(
            ref=ref,
            workspace_type=WorkspaceType.SEED,
        )

        brief = TaskBrief(
            description="Test task",
            task_id="task-123",
        )

        instance = create_task_instance_record(seed, brief)
        assert instance.workspace_type == WorkspaceType.INSTANCE
        assert instance.lineage.seed_workspace_id == "seed"
        assert instance.lineage.task_id == "task-123"

    def test_lineage_tracker(self):
        """Test lineage tracker."""
        tracker = LineageTracker()

        from umbrella.workspace_registry.models import WorkspaceLineageRecord

        record = WorkspaceLineageRecord(
            lineage_id="test-1",
            seed_workspace_id="seed",
            task_id="task-1",
        )

        tracker.save_record(record)
        records = tracker.get_records_for_seed("seed")
        assert len(records) == 1


class TestRealRepoIntegration:
    """Against this repository's workspaces/ (Task 02 acceptance)."""

    def test_load_seed_profile_agent_research(self):
        profile = load_seed_profile(REPO_ROOT / "workspaces" / "agent_research")
        assert profile is not None
        assert profile.workspace_id == "agent_research"
        assert profile.workspace_type == WorkspaceType.SEED
        assert "article_writing" in profile.primary_task_classes
        assert len(profile.capabilities) >= 1

    def test_load_registry_manifest(self):
        manifest = load_registry_manifest(REPO_ROOT)
        assert manifest is not None
        assert "agent_research" in manifest.seeds

    def test_build_registry_agent_research_is_seed(self):
        registry = build_registry(REPO_ROOT)
        assert registry.get_workspace("agent_research") is not None
        seed = registry.get_seed_profile("agent_research")
        assert seed is not None
        assert seed.workspace_type == WorkspaceType.SEED
        assert registry.seed_count >= 1
        manifest_errors = [
            i
            for i in registry.get_validation_issues()
            if i.field == "seeds" and i.severity == ValidationSeverity.ERROR
        ]
        assert not manifest_errors

    def test_create_task_instance_via_registry(self):
        registry = build_registry(REPO_ROOT)
        seed = registry.get_seed_profile("agent_research")
        assert seed is not None
        brief = TaskBrief(
            description="Draft a short article about testing.",
            task_class="article_writing",
            task_id="integration-test-1",
        )
        instance = registry.create_task_instance(seed, brief)
        assert instance.workspace_type == WorkspaceType.INSTANCE
        assert instance.lineage.seed_workspace_id == "agent_research"
        assert instance.lineage.task_id == "integration-test-1"
        assert registry.get_task_instance(instance.workspace_id) is instance

    def test_match_workspaces_article_task(self):
        registry = build_registry(REPO_ROOT)
        brief = TaskBrief(
            description="Write an article about CI pipelines.",
            task_class="article_writing",
        )
        matches = registry.match(brief)
        assert matches
        assert matches[0].profile.workspace_id == "agent_research"


class TestTaskMainContract:
    """Tests for TASK_MAIN.md workspace contract (Task 02a)."""

    def test_load_task_main_parses_sections(self, tmp_path):
        """Test that TASK_MAIN.md sections are parsed correctly."""
        from umbrella.workspace_registry.task_main import (
            load_task_main,
            TaskMainSection,
        )

        task_main = tmp_path / "TASK_MAIN.md"
        task_main.write_text("""# TASK_MAIN

## 1. Objective

Build a multi-agent system for article writing.

## 2. Final Deliverable

A working article pipeline.

## 3. Success Criteria

Articles are coherent and useful.

## 4. Constraints

Stay focused on article writing.

## 5. Starting Point

Initial workspace structure exists.

## 6. Human Checkpoints

Ask for review on important claims.

## 7. Long-Run Policy

Iterate until quality improves.
""")
        doc = load_task_main(task_main)
        assert doc is not None
        assert TaskMainSection.OBJECTIVE.value in doc.sections
        assert "multi-agent system" in doc.objective
        assert "article pipeline" in doc.final_deliverable
        assert doc.has_required_sections

    def test_validate_task_main_detects_missing_sections(self, tmp_path):
        """Test that missing required sections are detected."""
        from umbrella.workspace_registry.task_main import (
            load_task_main,
            validate_task_main_content,
        )

        task_main = tmp_path / "TASK_MAIN.md"
        task_main.write_text("""# TASK_MAIN

## 1. Objective

Some objective here.

## Notes

Extra notes.
""")
        doc = load_task_main(task_main)
        assert doc is not None
        issues = validate_task_main_content(doc)
        errors = [i for i in issues if i.severity == ValidationSeverity.ERROR]
        assert len(errors) >= 5  # Missing most core sections (Objective + Notes only)

    def test_build_task_brief_from_task_main(self, tmp_path):
        """Test that a TaskBrief can be built from TASK_MAIN.md."""
        from umbrella.workspace_registry.task_main import (
            load_task_main,
            build_task_brief_from_task_main,
        )

        task_main = tmp_path / "TASK_MAIN.md"
        task_main.write_text("""# TASK_MAIN

## 1. Objective

Write articles about technology topics using multi-agent research.

## 2. Final Deliverable

Final article in markdown format.

## 3. Success Criteria

Articles are coherent and useful.
""")
        doc = load_task_main(task_main)
        assert doc is not None
        brief = build_task_brief_from_task_main(doc)
        assert "technology" in brief.description.lower()
        assert brief.task_class is not None

    def test_render_task_main_template(self):
        """Test that template can be rendered."""
        from umbrella.workspace_registry.task_main import render_task_main_template

        content = render_task_main_template(
            title="Test Project",
            objective="Build something useful",
            final_deliverable="Working system",
            success_criteria="Tests pass",
        )
        assert "# Test Project" in content
        assert "Build something useful" in content
        assert "Working system" in content
        assert "Tests pass" in content

    def test_rendered_template_passes_required_section_validation(self, tmp_path):
        """Default template fills every required section (placeholders allowed)."""
        from umbrella.workspace_registry.task_main import (
            load_task_main,
            render_task_main_template,
            validate_task_main_content,
        )

        path = tmp_path / "TASK_MAIN.md"
        path.write_text(render_task_main_template(title="New workspace"))
        doc = load_task_main(path)
        assert doc is not None
        assert doc.has_required_sections
        errors = [
            i
            for i in validate_task_main_content(doc)
            if i.severity == ValidationSeverity.ERROR
        ]
        assert not errors

    def test_initialize_task_main_for_instance(self, tmp_path):
        """New task instance gets a TASK_MAIN.md that satisfies the contract."""
        from umbrella.workspace_registry.task_main import (
            initialize_task_main_for_instance,
            load_task_main,
            validate_task_main_content,
        )

        instance_root = tmp_path / "instance_ws"
        brief = TaskBrief(
            description="Produce a quarterly metrics summary for leadership",
            task_id="t-42",
            task_class="documentation",
            constraints={"deadline": "Friday"},
            required_capabilities=["file_search"],
        )
        written = initialize_task_main_for_instance(
            instance_root, None, brief, task_id="t-42"
        )
        assert written == instance_root / "TASK_MAIN.md"
        doc = load_task_main(written)
        assert doc is not None
        assert doc.has_required_sections
        errors = [
            i
            for i in validate_task_main_content(doc)
            if i.severity == ValidationSeverity.ERROR
        ]
        assert not errors
        assert "t-42" in doc.notes
        assert "quarterly metrics" in doc.objective.lower()

    def test_initialize_task_main_inherits_seed_sections(self, tmp_path):
        """When a seed document is provided, constraints and policies carry over."""
        from umbrella.workspace_registry.task_main import (
            initialize_task_main_for_instance,
            load_task_main,
        )

        seed_path = tmp_path / "seed" / "TASK_MAIN.md"
        seed_path.parent.mkdir(parents=True)
        seed_path.write_text("""# Seed

## 1. Objective

Seed objective.

## 2. Final Deliverable

Deliverable.

## 3. Success Criteria

Criteria.

## 4. Constraints

Do not use external APIs.

## 5. Starting Point

Repo checkout.

## 6. Human Checkpoints

Review before ship.

## 7. Long-Run Policy

Ship weekly.
""")
        seed_doc = load_task_main(seed_path)
        assert seed_doc is not None

        instance_root = tmp_path / "instance_ws"
        brief = TaskBrief(description="Instance-specific objective text")
        out = initialize_task_main_for_instance(
            instance_root, seed_doc, brief, task_id="i-1"
        )
        inst = load_task_main(out)
        assert inst is not None
        assert inst.final_deliverable == "Deliverable."
        assert "Criteria." in inst.success_criteria
        assert "external APIs" in inst.constraints
        assert "Review before ship" in inst.human_checkpoints
        assert "Ship weekly" in inst.long_run_policy
        assert inst.objective == "Instance-specific objective text"

    def test_agent_research_task_main_has_required_sections(self):
        """Test that agent_research TASK_MAIN.md has all required sections."""
        from umbrella.workspace_registry.task_main import (
            load_task_main,
            REQUIRED_SECTIONS,
        )

        task_main_path = REPO_ROOT / "workspaces" / "agent_research" / "TASK_MAIN.md"
        doc = load_task_main(task_main_path)
        assert doc is not None, "TASK_MAIN.md should exist in agent_research"
        for section in REQUIRED_SECTIONS:
            assert section.value in doc.sections, (
                f"Missing required section: {section.value}"
            )
