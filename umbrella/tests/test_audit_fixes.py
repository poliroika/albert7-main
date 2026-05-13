"""
Tests for audit-identified fixes.

Covers:
- snapshot_instance import in engine (patch loop reachable)
- PROMOTION_CONSIDERATION state transitions
- Retrieval card content in decision context
- Memory lesson bodies in decision context
- Self-improvement with real persistence
- Multi-workspace routing (evaluation vs agent_research)
- Demo mock_loops fairness
- CLI exit code semantics
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


from umbrella.control_plane.models import (
    DecisionContext,
    ManagerPhase,
    ManagerState,
    TaskBrief,
    TaskClass,
)
from umbrella.control_plane.state import ManagerStateMachine, _VALID_TRANSITIONS
from umbrella.control_plane.decision_policy import (
    classify_task,
    select_seed_workspace,
    build_decision_context,
)
from umbrella.control_plane.self_improvement import (
    ManagerCheckpoint,
    execute_self_improvement,
)
from umbrella.control_plane.workspace_patching import apply_workspace_patch
from umbrella.workspace_runtime.runner import _ADAPTER_BY_SEED_ID


# =========================================================================
# Fix 1: snapshot_instance is importable from engine
# =========================================================================


class TestSnapshotImport:
    def test_engine_can_import_snapshot_instance(self):
        from umbrella.control_plane.engine import snapshot_instance

        assert callable(snapshot_instance)


# =========================================================================
# Fix 2: PROMOTION_CONSIDERATION is reachable
# =========================================================================


class TestPromotionTransitions:
    def test_promotion_consideration_in_valid_transitions(self):
        assert ManagerPhase.PROMOTION_CONSIDERATION in _VALID_TRANSITIONS

    def test_lesson_recorded_can_transition_to_promotion(self):
        targets = _VALID_TRANSITIONS[ManagerPhase.LESSON_RECORDED]
        assert ManagerPhase.PROMOTION_CONSIDERATION in targets

    def test_patch_applied_can_transition_to_promotion(self):
        targets = _VALID_TRANSITIONS[ManagerPhase.PATCH_APPLIED]
        assert ManagerPhase.PROMOTION_CONSIDERATION in targets

    def test_promotion_can_transition_to_task_complete(self):
        targets = _VALID_TRANSITIONS[ManagerPhase.PROMOTION_CONSIDERATION]
        assert ManagerPhase.TASK_COMPLETE in targets

    def test_promotion_can_transition_to_escalated(self):
        targets = _VALID_TRANSITIONS[ManagerPhase.PROMOTION_CONSIDERATION]
        assert ManagerPhase.ESCALATED in targets

    def test_state_machine_accepts_promotion_transition(self):
        state = ManagerState(task_id="test")
        sm = ManagerStateMachine(state)
        sm.transition_to(ManagerPhase.WORKSPACE_SELECTED)
        sm.transition_to(ManagerPhase.INSTANCE_PREPARED)
        sm.transition_to(ManagerPhase.KNOWLEDGE_RETRIEVED)
        sm.transition_to(ManagerPhase.WORKSPACE_RUNNING)
        sm.transition_to(ManagerPhase.RUN_COMPLETE)
        sm.transition_to(ManagerPhase.INSPECTION_COMPLETE)
        sm.transition_to(ManagerPhase.LESSON_RECORDED)
        sm.transition_to(ManagerPhase.PROMOTION_CONSIDERATION)
        assert sm.state.phase == ManagerPhase.PROMOTION_CONSIDERATION


# =========================================================================
# Fix 3+4: Retrieval and memory content in decision context
# =========================================================================


class TestRetrievalAndMemoryInContext:
    def test_decision_context_has_retrieval_fields(self):
        ctx = DecisionContext(
            task_id="t1",
            task_brief=TaskBrief(
                task_id="t1",
                original_input="test",
                task_class=TaskClass.RESEARCH,
                summary="test",
            ),
            manager_state=ManagerState(task_id="t1"),
        )
        assert hasattr(ctx, "retrieval_recommended_pattern")
        assert hasattr(ctx, "retrieval_key_symbols")
        assert hasattr(ctx, "retrieval_key_files")
        assert hasattr(ctx, "retrieval_anti_patterns")
        assert hasattr(ctx, "retrieval_confidence")

    def test_decision_context_has_memory_fields(self):
        ctx = DecisionContext(
            task_id="t1",
            task_brief=TaskBrief(
                task_id="t1",
                original_input="test",
                task_class=TaskClass.RESEARCH,
                summary="test",
            ),
            manager_state=ManagerState(task_id="t1"),
        )
        assert hasattr(ctx, "relevant_lesson_summaries")
        assert hasattr(ctx, "active_gap_descriptions")

    def test_build_decision_context_with_memory_store(self):
        brief = TaskBrief(
            task_id="t1",
            original_input="test",
            task_class=TaskClass.RESEARCH,
            summary="test",
        )
        state = ManagerState(task_id="t1")

        mock_store = MagicMock()
        mock_store.get_stats.return_value = MagicMock(total_lessons=3, active_gaps=1)
        mock_store.query_lessons.return_value = []
        mock_store.get_active_gaps.return_value = []

        ctx = build_decision_context(
            brief, state, "agent_research", memory_store=mock_store
        )
        assert ctx.relevant_lessons == 3
        assert ctx.active_gaps == 1


# =========================================================================
# Fix 5: Self-improvement with real persistence
# =========================================================================


class TestSelfImprovementPersistence:
    def test_prompt_stack_rewrite_persists_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            checkpoint = ManagerCheckpoint(
                id="chk_test",
                task_id="t1",
                created_at=0.0,
                manager_state=ManagerState(task_id="t1"),
            )
            result = execute_self_improvement(
                checkpoint,
                "prompt_stack_rewrite",
                "Improve retrieval quality instructions",
                repo_root=repo_root,
            )
            assert result.outcome == "success"
            assert "annotation_path" in result.details
            annotation_path = Path(result.details["annotation_path"])
            assert annotation_path.exists()
            content = annotation_path.read_text(encoding="utf-8")
            assert "prompt_stack_rewrite" in content

    def test_retrieval_config_persists_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            checkpoint = ManagerCheckpoint(
                id="chk_test2",
                task_id="t2",
                created_at=0.0,
                manager_state=ManagerState(task_id="t2"),
            )
            result = execute_self_improvement(
                checkpoint,
                "retrieval_config",
                "Tune BM25 parameters",
                repo_root=repo_root,
            )
            assert result.outcome == "success"
            assert "note_path" in result.details

    def test_generic_type_without_repo_root_fails(self):
        checkpoint = ManagerCheckpoint(
            id="chk_test3",
            task_id="t3",
            created_at=0.0,
            manager_state=ManagerState(task_id="t3"),
        )
        result = execute_self_improvement(checkpoint, "general", "Something")
        assert result.outcome == "failure"


# =========================================================================
# Fix 6: Multi-workspace routing
# =========================================================================


class TestMultiWorkspaceRouting:
    def test_evaluation_adapter_registered(self):
        assert "evaluation" in _ADAPTER_BY_SEED_ID

    def test_agent_research_adapter_registered(self):
        assert "agent_research" in _ADAPTER_BY_SEED_ID

    def test_evaluation_task_routes_to_evaluation_workspace(self):
        brief = classify_task("evaluate the benchmark results", "t1")
        assert brief.task_class == TaskClass.EVALUATION
        workspace_id = select_seed_workspace(brief, registry=None)
        assert workspace_id == "evaluation"

    def test_research_task_routes_to_agent_research(self):
        brief = classify_task("research quantum computing advances", "t2")
        assert brief.task_class == TaskClass.RESEARCH
        workspace_id = select_seed_workspace(brief, registry=None)
        assert workspace_id == "agent_research"

    def test_different_tasks_get_different_workspaces(self):
        eval_brief = classify_task("test and evaluate the model performance", "t1")
        research_brief = classify_task("write an article about AI agents", "t2")
        eval_ws = select_seed_workspace(eval_brief, registry=None)
        research_ws = select_seed_workspace(research_brief, registry=None)
        assert eval_ws != research_ws


# =========================================================================
# Fix 8: Demo mock_loops fairness
# =========================================================================


class TestWorkspacePatchFairness:
    def test_patch_preserves_mock_loops_setting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_path = Path(tmpdir)
            (instance_path / "graph").mkdir()
            (instance_path / "reports").mkdir()
            metadata = {
                "runtime_overrides": {"mock_loops": True, "max_agent_executions": 12}
            }
            meta_path = instance_path / "instance_metadata.json"
            meta_path.write_text(json.dumps(metadata), encoding="utf-8")

            from umbrella.workspace_runtime.models import WorkspaceInstance

            instance = WorkspaceInstance(
                instance_id="test_inst",
                workspace_id="agent_research",
                path=instance_path,
            )

            result = apply_workspace_patch(
                instance,
                patch_description="test patch",
            )

            updated_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            assert updated_meta["runtime_overrides"]["mock_loops"] is True

    def test_patch_does_not_force_mock_loops_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance_path = Path(tmpdir)
            (instance_path / "graph").mkdir()
            (instance_path / "reports").mkdir()
            metadata = {"runtime_overrides": {"mock_loops": True}}
            meta_path = instance_path / "instance_metadata.json"
            meta_path.write_text(json.dumps(metadata), encoding="utf-8")

            from umbrella.workspace_runtime.models import WorkspaceInstance

            instance = WorkspaceInstance(
                instance_id="test_inst2",
                workspace_id="agent_research",
                path=instance_path,
            )

            result = apply_workspace_patch(
                instance,
                patch_description="test patch 2",
            )
            assert result.applied
            updated_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            assert updated_meta["runtime_overrides"]["mock_loops"] is True


# =========================================================================
# Fix 10: CLI exit code semantics
# =========================================================================


class TestCLIExitCodes:
    def test_complete_returns_zero(self):
        from umbrella.app import _exit_code_for_status

        assert _exit_code_for_status("complete") == 0

    def test_success_returns_zero(self):
        from umbrella.app import _exit_code_for_status

        assert _exit_code_for_status("success") == 0

    def test_partial_returns_nonzero(self):
        from umbrella.app import _exit_code_for_status

        assert _exit_code_for_status("partial") != 0

    def test_failed_returns_nonzero(self):
        from umbrella.app import _exit_code_for_status

        assert _exit_code_for_status("failed") != 0

    def test_partial_in_demo_mode_returns_nonzero(self):
        from umbrella.app import _exit_code_for_status

        assert _exit_code_for_status("partial", demo_mode=True) != 0
