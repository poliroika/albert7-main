"""Meta-Harness data models.

Defines schemas for experiments, candidates, evaluations, search sets,
promotion decisions, and contrastive memory bundles.
"""

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def generate_candidate_id() -> str:
    return f"cand_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def generate_experiment_id() -> str:
    return f"exp_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def generate_search_set_id() -> str:
    return f"ss_{int(time.time())}_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CandidateStatus(StrEnum):
    CAPTURED = "captured"
    EVALUATING = "evaluating"
    EVALUATED = "evaluated"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ERROR = "error"


class ExperimentStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"


class MetaPromotionEligibility(StrEnum):
    PROMOTE = "promote"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"
    INSUFFICIENT_DATA = "insufficient_data"


# ---------------------------------------------------------------------------
# Candidate Manifest
# ---------------------------------------------------------------------------


class CandidateManifest(BaseModel):
    candidate_id: str = Field(default_factory=generate_candidate_id)
    experiment_id: str = ""
    task_id: str = ""
    workspace_id: str = ""
    task_description: str = ""

    created_at: float = Field(default_factory=time.time)
    finished_at: float | None = None

    git_sha_before: str = ""
    git_sha_after: str = ""
    branch: str = ""
    instance_path: str = ""

    status: CandidateStatus = CandidateStatus.CAPTURED
    run_status: str = ""

    events_count: int = 0
    tool_calls: int = 0
    write_calls: int = 0
    changed_files: list[str] = Field(default_factory=list)
    promoted_files: list[str] = Field(default_factory=list)

    cost_usd: float = 0.0
    total_tokens: int = 0
    duration_seconds: float = 0.0

    final_message: str = ""
    error: str = ""

    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Search Sets
# ---------------------------------------------------------------------------


class SearchTask(BaseModel):
    task_id: str
    workspace_id: str
    task_text: str
    source: Literal["manual", "memory_failure", "workspace_run", "regression"] = (
        "manual"
    )
    expected_artifacts: list[str] = Field(default_factory=list)
    validation_commands: list[list[str]] = Field(default_factory=list)
    tags: set[str] = Field(default_factory=set)
    difficulty: int = 3


class SearchSet(BaseModel):
    id: str = Field(default_factory=generate_search_set_id)
    name: str = ""
    tasks: list[SearchTask] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)

    @property
    def size(self) -> int:
        return len(self.tasks)


# ---------------------------------------------------------------------------
# Candidate Evaluation
# ---------------------------------------------------------------------------


class TaskEvalResult(BaseModel):
    task_id: str
    workspace_id: str
    status: str = "unknown"
    score: float = 0.0
    task_success: float = 0.0
    artifact_quality: float = 0.0
    validation_pass: float = 0.0
    runtime_verification: float = 0.0
    runtime_verification_passed: bool = False
    runtime_verification_skipped: bool = False
    cost_usd: float = 0.0
    tokens: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = Field(default_factory=list)
    notes: str = ""
    verification_summary: str = ""


class CandidateEval(BaseModel):
    candidate_id: str
    experiment_id: str = ""
    search_set_id: str = ""

    task_results: list[TaskEvalResult] = Field(default_factory=list)

    tasks_total: int = 0
    tasks_complete: int = 0
    tasks_partial: int = 0
    tasks_failed: int = 0

    avg_score: float = 0.0
    median_score: float = 0.0
    weighted_score: float = 0.0

    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_duration_seconds: float = 0.0

    write_calls: int = 0
    tool_calls: int = 0

    regressions: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    raw_trace_paths: list[str] = Field(default_factory=list)

    notes: str = ""
    created_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Promotion Decision
# ---------------------------------------------------------------------------


class MetaPromotionDecision(BaseModel):
    candidate_id: str
    decision: MetaPromotionEligibility = MetaPromotionEligibility.INSUFFICIENT_DATA

    reasoning: str = ""
    evidence: list[str] = Field(default_factory=list)

    score_delta: float = 0.0
    baseline_score: float = 0.0
    candidate_score: float = 0.0

    passes_score_threshold: bool = False
    passes_heldout_check: bool = True
    passes_validation: bool = False
    passes_hardcode_audit: bool = True
    passes_scope_audit: bool = True
    passes_runtime_verification: bool = True

    suspicious_patterns: list[str] = Field(default_factory=list)
    blocked_files: list[str] = Field(default_factory=list)

    reviewed_by: Literal["auto", "human"] = "auto"
    reviewed_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Experiment Record
# ---------------------------------------------------------------------------


class ExperimentRecord(BaseModel):
    id: str = Field(default_factory=generate_experiment_id)
    name: str = ""
    workspace_id: str = ""
    search_set_id: str = ""
    heldout_set_id: str = ""

    status: ExperimentStatus = ExperimentStatus.ACTIVE
    candidate_ids: list[str] = Field(default_factory=list)
    baseline_candidate_id: str = ""
    best_candidate_id: str = ""
    best_score: float = 0.0

    iterations_completed: int = 0
    max_iterations: int = 0
    max_budget_usd: float = 0.0
    total_cost_usd: float = 0.0

    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = time.time()


# ---------------------------------------------------------------------------
# Contrastive Memory
# ---------------------------------------------------------------------------


class ContrastiveLessonEntry(BaseModel):
    lesson_id: str = ""
    workspace_id: str = ""
    conclusion: str = ""
    change_summary: str = ""
    observed_effect: str = ""
    tags: list[str] = Field(default_factory=list)
    score: float = 0.0
    raw_evidence_paths: list[str] = Field(default_factory=list)


class ContrastiveMemoryBundle(BaseModel):
    query: str = ""
    workspace_id: str = ""
    successes: list[ContrastiveLessonEntry] = Field(default_factory=list)
    failures: list[ContrastiveLessonEntry] = Field(default_factory=list)
    repeated_failures: list[ContrastiveLessonEntry] = Field(default_factory=list)
    challengers: list[ContrastiveLessonEntry] = Field(default_factory=list)
    avoid_tags: list[str] = Field(default_factory=list)
    repeat_tags: list[str] = Field(default_factory=list)
