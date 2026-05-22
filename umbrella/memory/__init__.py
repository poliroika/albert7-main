"""
Umbrella Memory System.

Primary store: MemPalace (umbrella.memory.palace) — ChromaDB + SQLite graph/transient.
Secondary: PalaceBackend (umbrella.memory.palace_backend) — event-log facade over MemPalace.
JSONL flat files: ideas.jsonl, lessons.jsonl — written directly; indexed by MemPalace on ingest.

Model layer: MemoryType, LessonRecord, CompetencyGapRecord, MemoryQuery, MemoryConfig, etc.
"""

from umbrella.memory.models import (
    # Memory types
    MemoryType,
    WorkingMemoryRecord,
    WorkspaceMemoryRecord,
    ManagerMemoryRecord,
    CompetencyMemoryRecord,
    # Lesson records
    LessonRecord,
    WorkspaceLessonRecord,
    ManagerLessonRecord,
    # Competency tracking
    CompetencyGapRecord,
    CapabilitySignal,
    FailureSignature,
    # Query and summary
    MemoryQuery,
    MemorySummaryBundle,
    MemoryStats,
    # Config
    MemoryConfig,
)

from umbrella.memory.store import MemoryStore
from umbrella.memory.store import reprioritize_memory as _reprioritize_memory_impl
from umbrella.memory.palace_backend import PalaceBackend, get_palace_backend
from umbrella.memory.paths import (
    get_workspace_store,
    manager_memory_root,
    palace_path_for,
    workspace_memory_root,
)
def reprioritize_memory(store: MemoryStore) -> None:
    """Apply decay and priority adjustments to all lessons.

    Convenience wrapper for MemoryStore.reprioritize_memory().
    """
    _reprioritize_memory_impl(store)


from umbrella.memory.lessons import (
    record_workspace_lesson,
    record_manager_lesson,
    promote_log_evidence_to_lesson,
)
from umbrella.memory.competency import (
    record_competency_signal,
    open_competency_gap,
    update_competency_gap,
    get_active_gaps,
    check_capability_deficit,
)
from umbrella.memory.summarization import (
    summarize_workspace_run,
    summarize_manager_state,
    build_memory_summary_bundle,
)
from umbrella.memory.relevance import (
    query_relevant_lessons,
    score_relevance,
    deduplicate_lessons,
)
from umbrella.memory.context_builder import (
    build_manager_context_bundle,
    build_workspace_context_bundle,
)
from umbrella.memory.recall import RecallBundle, summarized_palace_for_prompt
from umbrella.memory.reflection import ReflectionResult, run_reflection_phase

__all__ = [
    # Models
    "MemoryType",
    "WorkingMemoryRecord",
    "WorkspaceMemoryRecord",
    "ManagerMemoryRecord",
    "CompetencyMemoryRecord",
    "LessonRecord",
    "WorkspaceLessonRecord",
    "ManagerLessonRecord",
    "CompetencyGapRecord",
    "CapabilitySignal",
    "FailureSignature",
    "MemoryQuery",
    "MemorySummaryBundle",
    "MemoryStats",
    "MemoryConfig",
    # Store
    "MemoryStore",
    "reprioritize_memory",
    "PalaceBackend",
    "get_palace_backend",
    "get_workspace_store",
    "manager_memory_root",
    "palace_path_for",
    "workspace_memory_root",
    # Lessons
    "record_workspace_lesson",
    "record_manager_lesson",
    "promote_log_evidence_to_lesson",
    # Competency
    "record_competency_signal",
    "open_competency_gap",
    "update_competency_gap",
    "get_active_gaps",
    "check_capability_deficit",
    # Summarization
    "summarize_workspace_run",
    "summarize_manager_state",
    "build_memory_summary_bundle",
    # Relevance
    "query_relevant_lessons",
    "score_relevance",
    "deduplicate_lessons",
    # Context
    "build_manager_context_bundle",
    "build_workspace_context_bundle",
    "RecallBundle",
    "summarized_palace_for_prompt",
    "ReflectionResult",
    "run_reflection_phase",
]
