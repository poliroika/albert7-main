"""
Umbrella API for Ouroboros - Complete integration layer.

This module provides a clean API that Ouroboros can use to access
all of Umbrella's capabilities: retrieval, memory, metrics, control plane,
workspace operations, and evaluation.

Usage in Ouroboros:
    from umbrella.umbrella_api import UmbrellaAPI

    api = UmbrellaAPI(repo_root="/path/to/repo-checkout")

    # Get retrieval results
    results = api.retrieve("improve agent performance", max_results=10)

    # Get relevant lessons
    lessons = api.get_lessons(workspace_id="agent_research")

    # Get workspace insights
    insights = api.analyze_workspace("agent_research")

    # Make improvements
    patch_result = api.suggest_and_apply_patch(...)
"""

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, TypedDict

# Set Umbrella repo root for imports
repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in os.environ.get("PYTHONPATH", ""):
    import sys

    sys.path.insert(0, str(repo_root))

from umbrella.retrieval.service import RetrievalService
from umbrella.memory.store import MemoryStore, MemoryQuery, MemoryConfig
from umbrella.telemetry.store import TelemetryStore
from umbrella.telemetry.metrics import MetricsRegistry
from umbrella.workspace_runtime import create_instance_and_run
from umbrella.control_plane.code_analyzer import analyze_workspace_code
from umbrella.evals.runner import evaluate_run
from umbrella.control_plane.workspace_patching import WorkspacePatchResult
from umbrella.config import load_runtime_config

log = logging.getLogger(__name__)


class RetrievalResult(TypedDict):
    """Structured retrieval results."""

    query: str
    confidence: float
    recommended_pattern: str
    key_files: list[str]
    key_symbols: list[str]
    example_usage: list[str]
    anti_patterns: list[str]
    context_snippets: dict[str, str]


class WorkspaceInsight(TypedDict):
    """Workspace analysis results."""

    workspace_id: str
    path: Path
    issues: list[str]
    improvements: list[str]
    metrics: dict[str, Any]
    recent_runs: list[dict[str, Any]]


class ImprovementSuggestion(TypedDict):
    """Suggested improvement for a workspace."""

    workspace_id: str
    file_path: str
    current_code: str
    suggested_code: str
    reason: str
    expected_impact: str


class UmbrellaAPI:
    """
    Complete Umbrella API for Ouroboros integration.

    This class provides a unified interface to all Umbrella capabilities:
    - Retrieval: RAG search across code/docs
    - Memory: Lessons, gaps, signals
    - Metrics: Performance data
    - Control Plane: Decision making, patching
    - Workspace Runtime: Running and inspecting workspaces
    - Evaluation: Assessing workspace performance
    """

    def __init__(
        self, repo_root: Path | None = None, control_state_dir: Path | None = None
    ):
        """Initialize Umbrella API.

        Args:
            repo_root: Repository root path
            control_state_dir: Control state directory (default: .umbrella/)
        """
        self.repo_root = (repo_root or Path.cwd()).resolve()
        self.control_state_dir = (
            control_state_dir or self.repo_root / ".umbrella"
        ).resolve()

        # Initialize services
        self._init_retrieval()
        self._init_memory()
        self._init_telemetry()
        self._init_runtime_config()

        log.info(f"Umbrella API initialized for {self.repo_root}")

    def _init_retrieval(self) -> None:
        """Initialize retrieval service."""
        try:
            self.retrieval = RetrievalService(self.repo_root)
            log.info("Retrieval service initialized")
        except Exception as e:
            log.warning(f"Retrieval init failed: {e}")
            self.retrieval = None

    def _init_memory(self) -> None:
        """Initialize memory store."""
        try:
            memory_root = self.control_state_dir / "memory"
            config = MemoryConfig(
                memory_root=memory_root,
                lessons_path=memory_root / "lessons.jsonl",
                gaps_path=memory_root / "gaps.jsonl",
                signals_path=memory_root / "signals.jsonl",
            )
            self.memory = MemoryStore(config)
            stats = self.memory.get_stats()
            log.info(f"Memory store initialized: {stats.total_lessons} lessons")
        except Exception as e:
            log.warning(f"Memory init failed: {e}")
            self.memory = None

    def _init_telemetry(self) -> None:
        """Initialize telemetry and metrics."""
        try:
            telemetry_dir = self.control_state_dir / "telemetry"
            self.telemetry = TelemetryStore(telemetry_dir)
            self.metrics = MetricsRegistry()
            log.info("Telemetry initialized")
        except Exception as e:
            log.warning(f"Telemetry init failed: {e}")
            self.telemetry = None
            self.metrics = None

    def _init_runtime_config(self) -> None:
        """Initialize runtime config."""
        try:
            self.runtime_config = load_runtime_config()
            log.info(
                f"Runtime config: quality_threshold={self.runtime_config.quality_completion_threshold}"
            )
        except Exception as e:
            log.warning(f"Runtime config init failed: {e}")
            self.runtime_config = None

    # =============================================================================
    # RETRIEVAL API
    # =============================================================================

    def retrieve(
        self, query: str, max_results: int = 10, workspace_id: str | None = None
    ) -> RetrievalResult:
        """
        Query Umbrella's retrieval service for relevant code/docs.

        Args:
            query: Search query
            max_results: Maximum number of results
            workspace_id: Optional workspace filter

        Returns:
            Structured retrieval results
        """
        if not self.retrieval:
            return RetrievalResult(
                query=query,
                confidence=0.0,
                recommended_pattern="",
                key_files=[],
                key_symbols=[],
                example_usage=[],
                anti_patterns=[],
                context_snippets={},
            )

        try:
            card = self.retrieval.search(query, max_results=max_results)

            # Get code snippets for key files
            snippets = {}
            for file_path in card.key_files[:5]:
                try:
                    full_path = self.repo_root / file_path
                    if full_path.exists() and full_path.is_file():
                        snippets[str(file_path)] = full_path.read_text(
                            encoding="utf-8"
                        )[:1000]
                except Exception:
                    pass

            return RetrievalResult(
                query=query,
                confidence=card.confidence,
                recommended_pattern=card.recommended_pattern,
                key_files=[str(f) for f in card.key_files],
                key_symbols=card.key_symbols,
                example_usage=card.example_usage or [],
                anti_patterns=card.anti_patterns or [],
                context_snippets=snippets,
            )
        except Exception as e:
            log.error(f"Retrieval failed: {e}")
            raise

    # =============================================================================
    # MEMORY API
    # =============================================================================

    def get_lessons(
        self,
        workspace_id: str | None = None,
        task_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Get relevant lessons from memory.

        Args:
            workspace_id: Filter by workspace
            task_id: Filter by task
            limit: Maximum lessons to return

        Returns:
            List of lesson dicts
        """
        if not self.memory:
            return []

        try:
            lessons = self.memory.query_lessons(
                MemoryQuery(
                    workspace_id=workspace_id,
                    task_id=task_id,
                    limit=limit,
                    include_stale=False,
                )
            )

            return [
                {
                    "id": lesson.id,
                    "workspace_id": lesson.workspace_id,
                    "change_summary": lesson.change_summary,
                    "conclusion": lesson.conclusion,
                    "lesson_type": lesson.lesson_type.value,
                    "priority": lesson.priority,
                    "tags": list(lesson.tags),
                    "created_at": lesson.created_at,
                }
                for lesson in lessons
            ]
        except Exception as e:
            log.error(f"Failed to get lessons: {e}")
            return []

    def get_active_gaps(self) -> list[dict[str, Any]]:
        """Get active competency gaps."""
        if not self.memory:
            return []

        try:
            gaps = self.memory.get_active_gaps()
            return [
                {
                    "id": gap.id,
                    "capability_area": gap.capability_area,
                    "description": gap.description,
                    "severity": gap.severity.value,
                    "first_seen": gap.first_seen,
                }
                for gap in gaps
            ]
        except Exception as e:
            log.error(f"Failed to get gaps: {e}")
            return []

    def record_lesson(
        self,
        workspace_id: str,
        change_summary: str,
        conclusion: str,
        expected_effect: str,
        observed_effect: str,
        tags: list[str] | None = None,
    ) -> str:
        """
        Record a lesson in memory.

        Args:
            workspace_id: Workspace identifier
            change_summary: What was changed
            conclusion: What was learned
            expected_effect: What we expected
            observed_effect: What actually happened
            tags: Optional tags for categorization

        Returns:
            Lesson ID
        """
        if not self.memory:
            return ""

        try:
            from umbrella.memory.models import WorkspaceLessonRecord, generate_lesson_id

            lesson = WorkspaceLessonRecord(
                id=generate_lesson_id(),
                workspace_id=workspace_id,
                change_summary=change_summary,
                expected_effect=expected_effect,
                observed_effect=observed_effect,
                conclusion=conclusion,
                tags=set(tags or []),
                created_at=time.time(),
            )

            self.memory.record_lesson(lesson)
            log.info(f"Recorded lesson {lesson.id}")
            return lesson.id
        except Exception as e:
            log.error(f"Failed to record lesson: {e}")
            return ""

    # =============================================================================
    # METRICS API
    # =============================================================================

    def get_metrics(self) -> dict[str, Any]:
        """Get all metrics snapshot."""
        if not self.metrics:
            return {}

        try:
            return self.metrics.get_all_metrics()
        except Exception as e:
            log.error(f"Failed to get metrics: {e}")
            return {}

    def get_workspace_metrics(self, workspace_id: str) -> dict[str, Any]:
        """Get metrics for a specific workspace."""
        all_metrics = self.get_metrics()
        run_metrics = all_metrics.get("run_metrics", {})
        return run_metrics.get(workspace_id, {})

    # =============================================================================
    # WORKSPACE ANALYSIS API
    # =============================================================================

    def analyze_workspace(self, workspace_id: str) -> WorkspaceInsight:
        """
        Analyze a workspace and return insights.

        Args:
            workspace_id: Workspace identifier

        Returns:
            Workspace analysis results
        """
        try:
            workspace_path = self.repo_root / "workspaces" / workspace_id

            if not workspace_path.exists():
                raise ValueError(f"Workspace not found: {workspace_id}")

            # Run code analyzer
            analysis = analyze_workspace_code(
                workspace_path=workspace_path,
                task_description="General analysis for improvement",
            )

            # Get metrics
            metrics = self.get_workspace_metrics(workspace_id)

            # Get recent lessons
            lessons = self.get_lessons(workspace_id, limit=5)

            return WorkspaceInsight(
                workspace_id=workspace_id,
                path=workspace_path,
                issues=analysis.get("issues", []),
                improvements=analysis.get("suggested_improvements", []),
                metrics=metrics,
                recent_runs=[
                    {
                        "summary": lesson.get("conclusion", ""),
                        "change": lesson.get("change_summary", ""),
                    }
                    for lesson in lessons
                ],
            )
        except Exception as e:
            log.error(f"Workspace analysis failed: {e}")
            raise

    # =============================================================================
    # IMPROVEMENT API
    # =============================================================================

    def suggest_improvements(
        self, workspace_id: str, issue_description: str
    ) -> list[ImprovementSuggestion]:
        """
        Suggest specific improvements for a workspace.

        Args:
            workspace_id: Workspace identifier
            issue_description: What to improve

        Returns:
            List of improvement suggestions
        """
        suggestions = []

        try:
            # Get retrieval context
            retrieval = self.retrieve(issue_description, workspace_id=workspace_id)

            # Analyze each key file
            for file_path in retrieval.key_files[:5]:
                try:
                    full_path = self.repo_root / file_path
                    if not full_path.exists():
                        continue

                    current_code = full_path.read_text(encoding="utf-8")

                    # Use AI to suggest improvements
                    from umbrella.control_plane.code_improver import (
                        suggest_code_improvements,
                    )

                    improvements = suggest_code_improvements(
                        file_path=str(full_path),
                        issue_description=issue_description,
                        retrieval_context=retrieval,
                    )

                    for improvement in improvements:
                        suggestions.append(
                            ImprovementSuggestion(
                                workspace_id=workspace_id,
                                file_path=str(file_path),
                                current_code=improvement.get("current_code", ""),
                                suggested_code=improvement.get("suggested_code", ""),
                                reason=improvement.get("reason", ""),
                                expected_impact=improvement.get("expected_impact", ""),
                            )
                        )
                except Exception as e:
                    log.warning(f"Failed to suggest improvements for {file_path}: {e}")
                    continue

            return suggestions
        except Exception as e:
            log.error(f"Failed to suggest improvements: {e}")
            return []

    def apply_patch(
        self,
        workspace_id: str,
        file_path: str,
        new_content: str,
        commit_message: str,
    ) -> WorkspacePatchResult:
        """
        Apply a patch to a workspace file.

        Args:
            workspace_id: Workspace identifier
            file_path: Path to file (relative to workspace root)
            new_content: New file content
            commit_message: Commit message

        Returns:
            Patch result
        """
        try:
            workspace_path = self.repo_root / "workspaces" / workspace_id
            full_path = workspace_path / file_path

            # Create patch result
            from umbrella.control_plane.workspace_patching import WorkspacePatchResult

            # Write new content
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(new_content, encoding="utf-8")

            # Track the change
            changed_files = [Path(file_path)]

            return WorkspacePatchResult(
                status="applied",
                changed_files=changed_files,
                summary=f"Updated {file_path}",
                diff=f"Modified: {file_path}",
            )
        except Exception as e:
            log.error(f"Failed to apply patch: {e}")
            raise

    # =============================================================================
    # RUN & EVALUATE API
    # =============================================================================

    def run_and_evaluate(
        self,
        workspace_id: str,
        task_input: str,
    ) -> dict[str, Any]:
        """
        Run a workspace task and evaluate the results.

        Args:
            workspace_id: Workspace identifier
            task_input: Task description

        Returns:
            Run and evaluation results
        """
        try:
            workspaces_root = self.repo_root / "workspaces"

            # Run workspace
            run_result = create_instance_and_run(
                workspaces_root=workspaces_root,
                workspace_id=workspace_id,
                task_input=task_input,
            )

            # Evaluate run
            eval_record = evaluate_run(
                repo_root=self.repo_root,
                workspaces_root=workspaces_root,
                workspace_id=workspace_id,
                run_record=run_result,
            )

            return {
                "run_status": run_result.status.value,
                "run_id": run_result.run_id,
                "eval_score": eval_record.overall_score,
                "task_success": eval_record.task_success.value,
                "output_quality": eval_record.output_quality.value,
                "improvement_suggested": eval_record.improvement_suggested,
            }
        except Exception as e:
            log.error(f"Run and evaluate failed: {e}")
            raise

    # =============================================================================
    # CONTEXT BUILDER
    # =============================================================================

    def build_task_context(
        self,
        task_input: str,
        workspace_id: str | None = None,
    ) -> str:
        """
        Build complete task context for Ouroboros.

        Includes:
        - Retrieval results
        - Relevant lessons
        - Workspace metrics
        - Active gaps

        Args:
            task_input: Task description
            workspace_id: Optional workspace filter

        Returns:
            Formatted context string
        """
        context_parts = [
            "# Umbrella Task Context",
            f"Task: {task_input}",
            f"Workspace: {workspace_id or 'auto'}",
            "",
        ]

        # Retrieval
        try:
            retrieval = self.retrieve(task_input, max_results=5)
            context_parts.extend(
                [
                    f"**Retrieval Confidence:** {retrieval.confidence:.2f}",
                    f"**Recommended Pattern:** {retrieval.recommended_pattern}",
                    "",
                ]
            )

            if retrieval.key_files:
                context_parts.append("**Relevant Files:**")
                for file_path in retrieval.key_files[:5]:
                    context_parts.append(f"  - {file_path}")
                context_parts.append("")
        except Exception:
            context_parts.append("**Retrieval:** (unavailable)")
            context_parts.append("")

        # Memory
        try:
            lessons = self.get_lessons(workspace_id, limit=3)
            if lessons:
                context_parts.append(f"**Relevant Lessons ({len(lessons)}):**")
                for lesson in lessons:
                    context_parts.append(f"  - {lesson.get('change_summary', '')[:80]}")
                context_parts.append("")
        except Exception:
            context_parts.append("**Memory:** (unavailable)")
            context_parts.append("")

        # Active gaps
        try:
            gaps = self.get_active_gaps()
            if gaps:
                context_parts.append(f"**Active Gaps ({len(gaps)}):**")
                for gap in gaps[:3]:
                    context_parts.append(
                        f"  - [{gap.get('severity')}] {gap.get('capability_area')}: {gap.get('description')[:80]}"
                    )
                context_parts.append("")
        except Exception:
            pass

        return "\n".join(context_parts)


# Global API instance
_api: UmbrellaAPI | None = None


def get_umbrella_api(repo_root: Path | None = None) -> UmbrellaAPI:
    """
    Get or create the global Umbrella API instance.

    Args:
        repo_root: Repository root path

    Returns:
        UmbrellaAPI instance
    """
    global _api

    effective_root = (repo_root or Path.cwd()).resolve()

    if _api is None or _api.repo_root != effective_root:
        _api = UmbrellaAPI(repo_root=effective_root)

    return _api


# For direct use in Ouroboros
def get_retrieval_for_task(task_input: str, repo_root: Path | None = None) -> str:
    """Quick retrieval helper for task context."""
    api = get_umbrella_api(repo_root)
    retrieval = api.retrieve(task_input, max_results=5)

    lines = [
        f"**Retrieval Results** (confidence: {retrieval.confidence:.2f})",
        f"Pattern: {retrieval.recommended_pattern}",
        "",
    ]

    if retrieval.key_files:
        lines.append("**Files:**")
        for f in retrieval.key_files[:5]:
            lines.append(f"  {f}")

    return "\n".join(lines)


def get_memory_context(workspace_id: str, repo_root: Path | None = None) -> str:
    """Quick memory helper for task context."""
    api = get_umbrella_api(repo_root)
    lessons = api.get_lessons(workspace_id, limit=5)

    if not lessons:
        return "(no lessons yet)"

    lines = [f"**Lessons for {workspace_id}**"]
    for lesson in lessons:
        lines.append(f"- {lesson.get('change_summary')}")
        lines.append(f"  → {lesson.get('conclusion')[:100]}")

    return "\n".join(lines)
