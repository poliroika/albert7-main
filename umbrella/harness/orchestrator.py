"""Staged multi-candidate harness orchestrator.

Harness mode is intentionally not a "run the whole task three times"
switch.  The useful shape is a repeated tournament:

1. choose the next work stage (research, a concrete subtask, remediation,
   verification, etc.);
2. fan that stage out to N isolated Ouroboros candidates;
3. score the candidates, apply the winning diff, prune the losers;
4. move to the next stage and repeat.

The Web Bridge consumes the structured events from this module to render
readable run timelines and memory-graph nodes.
"""

import json
import logging
import os
import re
import shutil
import threading
import uuid
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from collections.abc import Callable, Iterable

log = logging.getLogger(__name__)

__all__ = [
    "HarnessCandidateResult",
    "HarnessEvent",
    "HarnessOrchestrator",
    "HarnessResult",
    "HarnessStagePlan",
    "HarnessStageResult",
    "score_candidate",
]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class HarnessEvent:
    """Structured progress event surfaced to the Web Bridge UI."""

    type: str
    candidate_index: int | None = None
    candidate_id: str | None = None
    stage_index: int | None = None
    stage_id: str | None = None
    stage_title: str | None = None
    stage_kind: str | None = None
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


@dataclass
class HarnessStagePlan:
    """A stage that gets its own best-of-N harness tournament."""

    index: int
    stage_id: str
    title: str
    description: str
    success_check: str = ""
    kind: str = "subtask"
    source: str = "synthetic"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HarnessCandidateResult:
    """Per-candidate outcome inside one harness stage."""

    index: int
    candidate_id: str
    run_id: str
    model: str
    stage_index: int = 0
    stage_id: str = ""
    stage_title: str = ""
    stage_kind: str = ""
    strategy_id: str = ""
    strategy_title: str = ""
    strategy_summary: str = ""
    strategy_prompt: str = ""
    status: str = (
        "pending"  # pending | running | completed | recovered | failed | cancelled
    )
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    error: str = ""
    full_result: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    candidate_manifest_path: str = ""
    pruned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HarnessStageResult:
    """Outcome of one staged tournament."""

    index: int
    stage_id: str
    title: str
    description: str
    success_check: str = ""
    kind: str = "subtask"
    source: str = "synthetic"
    status: str = "pending"
    candidates: list[HarnessCandidateResult] = field(default_factory=list)
    winner_index: int | None = None
    winner_id: str = ""
    winner_run_id: str = ""
    winner_applied: bool = False
    pruned_candidate_ids: list[str] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidates"] = [c.to_dict() for c in self.candidates]
        return payload


@dataclass
class HarnessResult:
    """Top-level harness outcome."""

    harness_id: str
    workspace_id: str
    status: str  # completed | failed | cancelled
    stages: list[HarnessStageResult] = field(default_factory=list)
    candidates: list[HarnessCandidateResult] = field(default_factory=list)
    winner_index: int | None = None
    winner_applied: bool = False
    final_message: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["stages"] = [s.to_dict() for s in self.stages]
        payload["candidates"] = [c.to_dict() for c in self.candidates]
        return payload


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _max_parallel_default() -> int:
    raw = (os.environ.get("OUROBOROS_HARNESS_MAX_PARALLEL") or "").strip()
    try:
        return max(1, int(raw)) if raw else 3
    except ValueError:
        return 3


def _timeout_hours_default() -> float:
    raw = (os.environ.get("OUROBOROS_HARNESS_TIMEOUT_HOURS") or "").strip()
    try:
        return max(0.1, float(raw)) if raw else 4.0
    except ValueError:
        return 4.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_candidate(
    result: dict[str, Any],
    *,
    stage_kind: str = "subtask",
) -> tuple[float, dict[str, float]]:
    """Translate one Ouroboros result into a transparent stage score."""
    if not isinstance(result, dict):
        return 0.0, {"invalid_result": 0.0}

    breakdown: dict[str, float] = {}
    status = str(result.get("status") or "").lower()
    if status in {"complete", "completed", "verified", "ok", "success"}:
        breakdown["status_completed"] = 5.0
    elif status in {"failed", "failed_verification", "failed_hygiene", "error"}:
        breakdown["status_failed"] = -2.0
    elif status in {"cancelled", "stopped"}:
        breakdown["status_cancelled"] = -3.0

    verification = result.get("verification_report")
    if isinstance(verification, dict):
        results = verification.get("results") or []
        passed = sum(
            1 for r in results if isinstance(r, dict) and r.get("status") == "passed"
        )
        failed = sum(
            1
            for r in results
            if isinstance(r, dict)
            and r.get("status") != "passed"
            and not r.get("optional")
        )
        breakdown["verification_passed"] = float(passed)
        breakdown["verification_failed"] = -2.0 * failed

    changes = result.get("promoted_files") or result.get("changes_made") or []
    if isinstance(changes, list):
        n = len(changes)
        if n == 0:
            if stage_kind in {
                "planning",
                "research",
                "review",
                "verification",
                "final",
            }:
                breakdown["no_change_ok"] = 0.0
            else:
                breakdown["no_changes"] = -1.0
        elif n <= 5:
            breakdown["focused_changes"] = 1.5
        elif n <= 20:
            breakdown["medium_changes"] = 0.5
        else:
            breakdown["sprawling_changes"] = -0.5

    rounds = result.get("total_rounds") or result.get("rounds") or 0
    try:
        rounds_int = int(rounds)
        if rounds_int:
            breakdown["efficiency_bonus"] = max(0.0, 2.0 - rounds_int / 50.0)
    except (TypeError, ValueError):
        pass

    if result.get("error"):
        breakdown["has_error"] = -3.0

    return sum(breakdown.values()), breakdown


_CANDIDATE_STRATEGIES: tuple[dict[str, str], ...] = (
    {
        "id": "evidence_first",
        "title": "Evidence-first",
        "summary": "Starts by collecting requirements, docs, examples, and concrete success criteria before changing files.",
        "prompt": (
            "Bias toward evidence and constraints first. Search MCP/GitHub/docs/deep-search only when useful, "
            "capture concise findings, then make the smallest stage-appropriate move."
        ),
    },
    {
        "id": "minimal_risk",
        "title": "Minimal-risk",
        "summary": "Prefers the smallest reversible patch with focused verification and low blast radius.",
        "prompt": (
            "Bias toward narrow, low-risk changes. Avoid broad rewrites. Prefer one coherent patch and explicit checks."
        ),
    },
    {
        "id": "integration_first",
        "title": "Integration-first",
        "summary": "Looks for end-to-end wiring gaps, UI/run/log visibility, and places where components disagree.",
        "prompt": (
            "Bias toward integration behavior. Trace how UI, bridge, memory, logs, and runtime artifacts connect, "
            "then fix the stage's most important broken handoff."
        ),
    },
    {
        "id": "adversarial_reviewer",
        "title": "Reviewer",
        "summary": "Acts as a skeptical bug finder, testing assumptions and looking for regressions.",
        "prompt": (
            "Bias toward finding failure modes and hidden regressions. Validate before trusting prior work, "
            "then patch the clearest bug found in this stage."
        ),
    },
)


def _candidate_strategy(index: int) -> dict[str, str]:
    return _CANDIDATE_STRATEGIES[index % len(_CANDIDATE_STRATEGIES)]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


EventCallback = Callable[[HarnessEvent], None]


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class HarnessOrchestrator:
    """Runs a stage-by-stage best-of-N harness tournament."""

    def __init__(
        self,
        *,
        repo_root: Path,
        workspace_id: str,
        task_description: str,
        harness_id: str | None = None,
        num_candidates: int = 3,
        model_pool: Iterable[str] | None = None,
        max_rounds: int = 0,
        max_verify_retries: int = 0,
        on_event: EventCallback | None = None,
        run_fn: Callable[..., dict[str, Any]] | None = None,
        apply_fn: Callable[[Path, str], bool] | None = None,
        max_parallel: int | None = None,
        timeout_seconds: float | None = None,
        stages: list[HarnessStagePlan | dict[str, Any]] | None = None,
    ) -> None:
        if num_candidates < 1:
            raise ValueError("num_candidates must be >= 1")
        self.repo_root = Path(repo_root).resolve()
        self.workspace_id = workspace_id
        self.task_description = task_description
        self.harness_id = harness_id or f"harness_{uuid.uuid4().hex[:10]}"
        self.num_candidates = num_candidates
        models = list(model_pool or [])
        if not models:
            default_model = (
                os.environ.get("OUROBOROS_MODEL") or os.environ.get("LLM_MODEL") or ""
            ).strip() or "default"
            models = [default_model]
        self.candidate_models: list[str] = [
            models[i % len(models)] for i in range(num_candidates)
        ]
        self.max_rounds = max_rounds
        self.max_verify_retries = max_verify_retries
        self.on_event = on_event or (lambda _evt: None)
        self.max_parallel = max_parallel or _max_parallel_default()
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else _timeout_hours_default() * 3600.0
        )

        if run_fn is None:
            from umbrella.control_plane.ouroboros_integration import (
                run_ouroboros_improvement_sync as _default_run,
            )

            run_fn = _default_run
        self._run_fn = run_fn
        if apply_fn is None:
            try:
                from umbrella.meta_harness.promotion import (
                    apply_candidate_patch as _apply,
                )

                apply_fn = _apply
            except Exception:  # pragma: no cover - defensive
                apply_fn = None
        self._apply_fn = apply_fn

        self.stage_plans = (
            self._coerce_stage_plans(stages) if stages else self._derive_stage_plans()
        )
        self._cancel_event = threading.Event()
        self._futures: list[Future] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        """Request a cooperative cancel of the harness."""
        self._cancel_event.set()
        try:
            stop_path = (
                self.repo_root
                / "workspaces"
                / self.workspace_id
                / ".memory"
                / "drive"
                / "state"
                / "stop_requested.json"
            )
            stop_path.parent.mkdir(parents=True, exist_ok=True)
            stop_path.write_text(
                json.dumps(
                    {"workspace_id": self.workspace_id, "harness_id": self.harness_id}
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    @property
    def candidate_run_ids(self) -> list[str]:
        ids: list[str] = []
        for stage in self.stage_plans:
            for i in range(self.num_candidates):
                ids.append(self._candidate_run_id(stage, i))
        return ids

    def run(self) -> HarnessResult:
        """Run every harness stage, applying each stage winner before continuing."""
        self._emit(
            HarnessEvent(
                type="harness_started",
                message=(
                    f"starting staged harness: {len(self.stage_plans)} stages, "
                    f"{self.num_candidates} candidates per stage"
                ),
                data={
                    "harness_id": self.harness_id,
                    "workspace_id": self.workspace_id,
                    "mode": "staged",
                    "num_stages": len(self.stage_plans),
                    "num_candidates": self.num_candidates,
                    "models": self.candidate_models,
                    "candidate_run_ids": self.candidate_run_ids,
                    "stages": [s.to_dict() for s in self.stage_plans],
                },
                ts=_now_iso(),
            )
        )

        stage_results: list[HarnessStageResult] = []
        flattened: list[HarnessCandidateResult] = []
        status = "completed"
        error = ""

        for stage in self.stage_plans:
            if self._cancel_event.is_set():
                status = "cancelled"
                break
            stage_result = self._run_stage(stage, stage_results)
            stage_results.append(stage_result)
            flattened.extend(stage_result.candidates)
            if stage_result.status == "cancelled":
                status = "cancelled"
                break
            if stage_result.status == "failed":
                status = "failed"
                error = stage_result.error
                break

        if self._cancel_event.is_set() and status != "failed":
            status = "cancelled"

        last_winner: HarnessCandidateResult | None = None
        for stage in reversed(stage_results):
            if stage.winner_index is None:
                continue
            if 0 <= stage.winner_index < len(stage.candidates):
                last_winner = stage.candidates[stage.winner_index]
                break
        winner_index = last_winner.index if last_winner else None
        winner_applied = any(stage.winner_applied for stage in stage_results)
        final_message = self._build_final_message(stage_results, status, error)
        result = HarnessResult(
            harness_id=self.harness_id,
            workspace_id=self.workspace_id,
            status=status,
            stages=stage_results,
            candidates=flattened,
            winner_index=winner_index,
            winner_applied=winner_applied,
            final_message=final_message,
            error=error,
        )
        self._emit(
            HarnessEvent(
                type="harness_finished",
                message=final_message,
                data={
                    "status": status,
                    "stage_count": len(stage_results),
                    "winner_index": winner_index,
                    "winner_applied": winner_applied,
                    "error": error,
                },
                ts=_now_iso(),
            )
        )
        return result

    # ------------------------------------------------------------------
    # Stage planning
    # ------------------------------------------------------------------

    def _coerce_stage_plans(
        self,
        stages: list[HarnessStagePlan | dict[str, Any]],
    ) -> list[HarnessStagePlan]:
        plans: list[HarnessStagePlan] = []
        for idx, raw in enumerate(stages):
            if isinstance(raw, HarnessStagePlan):
                plan = raw
            elif isinstance(raw, dict):
                plan = HarnessStagePlan(
                    index=int(
                        raw.get("index") if raw.get("index") is not None else idx
                    ),
                    stage_id=str(raw.get("stage_id") or f"s{idx + 1}"),
                    title=str(raw.get("title") or f"Stage {idx + 1}"),
                    description=str(raw.get("description") or ""),
                    success_check=str(raw.get("success_check") or ""),
                    kind=str(raw.get("kind") or "subtask"),
                    source=str(raw.get("source") or "provided"),
                )
            else:
                continue
            plans.append(
                HarnessStagePlan(
                    index=len(plans),
                    stage_id=plan.stage_id or f"s{len(plans) + 1}",
                    title=plan.title,
                    description=plan.description,
                    success_check=plan.success_check,
                    kind=plan.kind or "subtask",
                    source=plan.source or "provided",
                )
            )
        return plans or self._fallback_stage_plans([])

    def _derive_stage_plans(self) -> list[HarnessStagePlan]:
        plan_stages = self._load_existing_plan_stages()
        if plan_stages:
            return plan_stages
        parsed = self._parse_task_subtasks(self.task_description)
        return self._fallback_stage_plans(parsed)

    def _load_existing_plan_stages(self) -> list[HarnessStagePlan]:
        plans_dir = (
            self.repo_root
            / "workspaces"
            / self.workspace_id
            / ".memory"
            / "drive"
            / "task_plans"
        )
        if not plans_dir.exists():
            return []
        task_tokens = self._token_set(self.task_description)
        candidates = sorted(
            plans_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        for path in candidates[:8]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if (
                str(payload.get("workspace_id") or self.workspace_id)
                != self.workspace_id
            ):
                continue
            subtasks = payload.get("subtasks")
            if not isinstance(subtasks, list) or not subtasks:
                continue
            digest = str(payload.get("objective_digest") or "")
            if (
                task_tokens
                and self._token_overlap(task_tokens, self._token_set(digest)) < 0.25
            ):
                continue
            stages: list[HarnessStagePlan] = [
                self._stage(
                    "planning",
                    "Evidence and plan alignment",
                    (
                        "Inspect workspace context and any needed MCP/GitHub/documentation evidence. "
                        "Align the next candidate tournaments with the existing task plan."
                    ),
                    "Existing plan and external evidence are captured or judged unnecessary.",
                    source=f"task_plan:{path.name}",
                )
            ]
            for raw in subtasks[:5]:
                if not isinstance(raw, dict):
                    continue
                stages.append(
                    self._stage(
                        "subtask",
                        str(raw.get("title") or f"Subtask {len(stages)}"),
                        str(raw.get("description") or ""),
                        str(raw.get("success_check") or ""),
                        source=f"task_plan:{path.name}",
                    )
                )
            stages.append(
                self._stage(
                    "remediation",
                    "Bug fix and remediation pass",
                    "Find regressions, broken assumptions, tool errors, and incomplete subtasks; fix them.",
                    "Known failures are fixed or explicitly recorded as blockers.",
                    source=f"task_plan:{path.name}",
                )
            )
            stages.append(
                self._stage(
                    "verification",
                    "Final verification",
                    "Run focused verification and prepare the handoff summary.",
                    "Verification evidence is recorded and the final workspace is coherent.",
                    source=f"task_plan:{path.name}",
                )
            )
            return self._renumber_stages(stages[:7])
        return []

    def _fallback_stage_plans(
        self, parsed_subtasks: list[dict[str, str]]
    ) -> list[HarnessStagePlan]:
        stages: list[HarnessStagePlan] = [
            self._stage(
                "planning",
                "Evidence search and work plan",
                (
                    "Map the task to concrete intermediate work. Use MCP discovery, "
                    "GitHub project search, docs, or deep search only when they add evidence. "
                    "Persist useful findings before implementation begins."
                ),
                "The next implementation/big-fix stages have concrete evidence and success criteria.",
            )
        ]

        if parsed_subtasks:
            for raw in parsed_subtasks[:4]:
                stages.append(
                    self._stage(
                        "subtask",
                        raw.get("title") or f"Subtask {len(stages)}",
                        raw.get("description") or raw.get("title") or "",
                        raw.get("success_check")
                        or "The subtask has concrete code or evidence.",
                    )
                )
        else:
            kind = (
                "bugfix"
                if self._looks_like_bugfix(self.task_description)
                else "subtask"
            )
            title = "Focused bug fix" if kind == "bugfix" else "Focused implementation"
            stages.append(
                self._stage(
                    kind,
                    title,
                    (
                        "Implement the highest-value concrete change for this task only. "
                        "Do not attempt final polish or broad verification in this stage."
                    ),
                    "A focused candidate change is captured and ready for remediation.",
                )
            )

        stages.append(
            self._stage(
                "remediation",
                "Bug fix and integration pass",
                (
                    "Run a fresh bug-fix tournament over the current workspace state. "
                    "Fix mistakes from the previous winner, merge loose ends, and remove incomplete paths."
                ),
                "Regressions and incomplete paths are fixed or recorded as explicit blockers.",
            )
        )
        stages.append(
            self._stage(
                "verification",
                "Final verification and handoff",
                (
                    "Verify the selected work, run targeted tests/checks, and produce final evidence. "
                    "This stage should not reopen broad implementation unless verification finds a bug."
                ),
                "Verification passes or the remaining blocker is precise and actionable.",
            )
        )
        return self._renumber_stages(stages[:7])

    def _stage(
        self,
        kind: str,
        title: str,
        description: str,
        success_check: str,
        *,
        source: str = "synthetic",
    ) -> HarnessStagePlan:
        return HarnessStagePlan(
            index=0,
            stage_id="",
            title=title.strip()[:160] or "Harness stage",
            description=description.strip(),
            success_check=success_check.strip(),
            kind=kind.strip() or "subtask",
            source=source,
        )

    def _renumber_stages(
        self, stages: list[HarnessStagePlan]
    ) -> list[HarnessStagePlan]:
        out: list[HarnessStagePlan] = []
        for idx, stage in enumerate(stages):
            out.append(
                HarnessStagePlan(
                    index=idx,
                    stage_id=f"s{idx + 1}",
                    title=stage.title,
                    description=stage.description,
                    success_check=stage.success_check,
                    kind=stage.kind,
                    source=stage.source,
                )
            )
        return out

    @staticmethod
    def _token_set(text: str) -> set[str]:
        return {
            token.lower()
            for token in re.findall(
                r"[\w\u0400-\u04ff]{4,}", text or "", flags=re.UNICODE
            )
        }

    @staticmethod
    def _token_overlap(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / max(1, min(len(a), len(b)))

    @staticmethod
    def _looks_like_bugfix(text: str) -> bool:
        lowered = (text or "").lower()
        markers = (
            "bug",
            "fix",
            "failed",
            "ошиб",
            "баг",
            "исправ",
            "слом",
            "regression",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _parse_task_subtasks(text: str) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        in_planish_block = False
        for line in (text or "").splitlines():
            stripped = line.strip()
            heading = stripped.lower().lstrip("#").strip()
            if stripped.startswith("#"):
                in_planish_block = any(
                    marker in heading
                    for marker in (
                        "subtask",
                        "plan",
                        "todo",
                        "steps",
                        "этап",
                        "шаг",
                        "задач",
                    )
                )
                continue
            match = re.match(
                r"^(?:[-*]\s+(?:\[[ xX]\]\s*)?|\d+[\.)]\s+)(.+)$", stripped
            )
            if not match:
                continue
            title = re.sub(r"\s+", " ", match.group(1)).strip()
            if not in_planish_block and len(rows) == 0 and len(title) < 24:
                continue
            if len(title) < 8 or title.startswith("http"):
                continue
            if len(rows) >= 5:
                break
            rows.append(
                {
                    "title": title[:140],
                    "description": title,
                    "success_check": "The described subtask is complete and verified with concrete evidence.",
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Stage execution
    # ------------------------------------------------------------------

    def _run_stage(
        self,
        stage: HarnessStagePlan,
        previous_stages: list[HarnessStageResult],
    ) -> HarnessStageResult:
        stage_result = HarnessStageResult(
            index=stage.index,
            stage_id=stage.stage_id,
            title=stage.title,
            description=stage.description,
            success_check=stage.success_check,
            kind=stage.kind,
            source=stage.source,
            status="running",
            started_at=_now_iso(),
        )
        stage_result.candidates = [
            self._build_candidate(stage, i) for i in range(self.num_candidates)
        ]

        self._emit_stage_event(
            "stage_started",
            stage,
            f"stage {stage.index + 1}: split into {self.num_candidates} candidates",
            data={
                "stage": stage.to_dict(),
                "candidate_run_ids": [c.run_id for c in stage_result.candidates],
                "candidates": [c.to_dict() for c in stage_result.candidates],
                "num_candidates": self.num_candidates,
            },
        )

        executor = ThreadPoolExecutor(
            max_workers=min(self.max_parallel, self.num_candidates),
            thread_name_prefix=f"{self.harness_id}-{stage.stage_id}",
        )
        try:
            future_to_index: dict[Future, int] = {}
            for i, candidate in enumerate(stage_result.candidates):
                if self._cancel_event.is_set():
                    candidate.status = "cancelled"
                    continue
                task_description = self._build_stage_task_description(
                    stage, previous_stages, candidate
                )
                future = executor.submit(
                    self._run_candidate, candidate, task_description, stage
                )
                future_to_index[future] = i
                self._futures.append(future)

            try:
                for future in as_completed(
                    future_to_index, timeout=self.timeout_seconds
                ):
                    idx = future_to_index[future]
                    candidate = stage_result.candidates[idx]
                    try:
                        future.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        candidate.status = "failed"
                        candidate.error = str(exc)
                        log.error(
                            "harness candidate %s crashed",
                            candidate.run_id,
                            exc_info=True,
                        )
                    self._emit_candidate_completed(stage, candidate)
                    if self._cancel_event.is_set():
                        break
            except FuturesTimeoutError:
                self._cancel_event.set()
                for candidate in stage_result.candidates:
                    if candidate.status in {"pending", "running"}:
                        candidate.status = "cancelled"
                        candidate.error = candidate.error or "harness_timeout"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        for candidate in stage_result.candidates:
            if candidate.full_result and not candidate.score_breakdown:
                candidate.score, candidate.score_breakdown = score_candidate(
                    candidate.full_result,
                    stage_kind=stage.kind,
                )

        completed = [
            c for c in stage_result.candidates if c.status in {"completed", "recovered"}
        ]
        if self._cancel_event.is_set():
            stage_result.status = "cancelled"
            stage_result.completed_at = _now_iso()
            self._emit_stage_event(
                "stage_finished", stage, "stage cancelled", data=stage_result.to_dict()
            )
            return stage_result

        if not completed:
            recovered = self._recover_stage_candidate(stage, stage_result)
            if recovered is not None:
                completed = [recovered]
            else:
                stage_result.status = "failed"
                stage_result.error = "no candidate completed this stage"
                stage_result.completed_at = _now_iso()
                self._emit_stage_event(
                    "stage_finished",
                    stage,
                    stage_result.error,
                    data=stage_result.to_dict(),
                )
                return stage_result

        best = max(completed, key=lambda c: c.score)
        stage_result.winner_index = best.index
        stage_result.winner_id = best.candidate_id
        stage_result.winner_run_id = best.run_id
        scores = [
            {
                "candidate_id": c.candidate_id,
                "run_id": c.run_id,
                "stage_id": c.stage_id,
                "score": c.score,
                "breakdown": c.score_breakdown,
                "status": c.status,
            }
            for c in stage_result.candidates
        ]
        self._emit_stage_event(
            "stage_candidates_scored",
            stage,
            f"stage {stage.index + 1}: winner {best.candidate_id} score={best.score:.2f}",
            data={
                "scores": scores,
                "winner_index": best.index,
                "winner_id": best.candidate_id,
            },
        )
        losers = [c for c in stage_result.candidates if c.index != best.index]
        self._emit_stage_event(
            "stage_winner_selected",
            stage,
            f"selected {best.candidate_id} for {stage.title}",
            data={
                "winner": best.to_dict(),
                "losers": [c.to_dict() for c in losers],
            },
        )

        winner_applied = self._apply_stage_winner(stage, best)
        stage_result.winner_applied = winner_applied
        pruned = self._prune_losers(losers)
        stage_result.pruned_candidate_ids = pruned
        self._emit_stage_event(
            "stage_losers_pruned",
            stage,
            f"pruned {len(pruned)} losing candidate artifact(s)",
            data={
                "pruned_candidate_ids": pruned,
                "loser_run_ids": [c.run_id for c in losers],
            },
        )
        stage_result.status = "completed"
        stage_result.completed_at = _now_iso()
        self._emit_stage_event(
            "stage_finished", stage, "stage completed", data=stage_result.to_dict()
        )
        return stage_result

    def _recover_stage_candidate(
        self,
        stage: HarnessStagePlan,
        stage_result: HarnessStageResult,
    ) -> HarnessCandidateResult | None:
        """Salvage a useful partial candidate instead of killing the whole harness.

        This is intentionally conservative: a candidate must have a captured
        result, a positive score, and either changed files or a meta-harness
        candidate bundle that can be promoted. The UI sees this as an explicit
        recovery event, not as a silent success.
        """
        recoverable: list[HarnessCandidateResult] = []
        for candidate in stage_result.candidates:
            if candidate.status == "cancelled" or not candidate.full_result:
                continue
            changes = (
                candidate.full_result.get("changes_made")
                or candidate.full_result.get("promoted_files")
                or []
            )
            has_changes = isinstance(changes, list) and bool(changes)
            has_bundle = bool(candidate.full_result.get("candidate_id"))
            if candidate.score <= 0 or not (has_changes or has_bundle):
                continue
            recoverable.append(candidate)
        if not recoverable:
            return None
        best = max(recoverable, key=lambda item: item.score)
        best.status = "recovered"
        if "recovered_partial" not in best.score_breakdown:
            best.score_breakdown["recovered_partial"] = -0.5
            best.score -= 0.5
        stage_result.error = "recovered from failed candidates"
        self._emit_stage_event(
            "stage_recovered_candidate_selected",
            stage,
            f"recovered with {best.candidate_id} after all candidates missed clean completion",
            candidate=best,
            data={
                "winner": best.to_dict(),
                "reason": "all candidates missed clean completion, but this one produced promotable work",
                "recoverable_candidate_ids": [c.candidate_id for c in recoverable],
            },
        )
        return best

    def _build_candidate(
        self, stage: HarnessStagePlan, index: int
    ) -> HarnessCandidateResult:
        strategy = _candidate_strategy(index)
        return HarnessCandidateResult(
            index=index,
            candidate_id=f"{stage.stage_id}-c{index + 1}",
            run_id=self._candidate_run_id(stage, index),
            model=self.candidate_models[index],
            stage_index=stage.index,
            stage_id=stage.stage_id,
            stage_title=stage.title,
            stage_kind=stage.kind,
            strategy_id=strategy["id"],
            strategy_title=strategy["title"],
            strategy_summary=strategy["summary"],
            strategy_prompt=strategy["prompt"],
        )

    def _candidate_run_id(self, stage: HarnessStagePlan, index: int) -> str:
        return f"{self.harness_id}__{stage.stage_id}__c{index + 1}"

    def _build_stage_task_description(
        self,
        stage: HarnessStagePlan,
        previous_stages: list[HarnessStageResult],
        candidate: HarnessCandidateResult,
    ) -> str:
        previous = []
        for prev in previous_stages[-5:]:
            if prev.winner_id:
                previous.append(
                    f"- Stage {prev.index + 1} [{prev.kind}] {prev.title}: "
                    f"winner={prev.winner_id}, applied={prev.winner_applied}, "
                    f"status={prev.status}"
                )
        previous_text = "\n".join(previous) or "- No previous stages yet."
        return (
            "[HARNESS_STAGE]\n"
            f"Stage {stage.index + 1}/{len(self.stage_plans)}\n"
            f"Kind: {stage.kind}\n"
            f"Title: {stage.title}\n"
            f"Description: {stage.description}\n"
            f"Success check: {stage.success_check or 'Use concrete evidence.'}\n\n"
            "[CANDIDATE_STRATEGY]\n"
            f"ID: {candidate.strategy_id}\n"
            f"Name: {candidate.strategy_title}\n"
            f"Difference: {candidate.strategy_summary}\n"
            f"Instruction: {candidate.strategy_prompt}\n"
            "[END_CANDIDATE_STRATEGY]\n\n"
            "You are one candidate in a staged harness tournament. Work ONLY on this "
            "stage. Do not try to finish the whole original task unless this stage is "
            "the final verification/handoff stage. Later harness stages will handle "
            "remediation, verification, and final polish.\n\n"
            "For research/planning stages, prefer evidence capture: MCP discovery, "
            "GitHub project search, documentation/deep search when useful, and concise "
            "workspace memory notes. For implementation or bugfix stages, produce a "
            "focused patch. For verification stages, run targeted checks and report "
            "the evidence.\n\n"
            "[PREVIOUS_STAGE_WINNERS]\n"
            f"{previous_text}\n"
            "[END_PREVIOUS_STAGE_WINNERS]\n\n"
            "[ORIGINAL_TASK]\n"
            f"{self.task_description.strip()}\n"
            "[END_ORIGINAL_TASK]\n"
        )

    def _run_candidate(
        self,
        candidate: HarnessCandidateResult,
        task_description: str,
        stage: HarnessStagePlan,
    ) -> None:
        if self._cancel_event.is_set():
            candidate.status = "cancelled"
            return
        candidate.status = "running"
        self._emit(
            HarnessEvent(
                type="stage_candidate_started",
                candidate_index=candidate.index,
                candidate_id=candidate.candidate_id,
                stage_index=stage.index,
                stage_id=stage.stage_id,
                stage_title=stage.title,
                stage_kind=stage.kind,
                message=f"{candidate.candidate_id} started",
                data={
                    "model": candidate.model,
                    "run_id": candidate.run_id,
                    "stage": stage.to_dict(),
                    "candidate": candidate.to_dict(),
                    "strategy": {
                        "id": candidate.strategy_id,
                        "title": candidate.strategy_title,
                        "summary": candidate.strategy_summary,
                    },
                },
                ts=_now_iso(),
            )
        )
        import time as _time

        started = _time.monotonic()
        previous_model = os.environ.get("OUROBOROS_MODEL")
        if candidate.model and candidate.model != "default":
            os.environ["OUROBOROS_MODEL"] = candidate.model
        try:
            result = self._run_fn(
                repo_root=self.repo_root,
                task_description=task_description,
                workspace_id=self.workspace_id,
                experiment_id=f"{self.harness_id}__{stage.stage_id}",
                candidate_isolation=True,
                task_id=candidate.run_id,
                timeout_seconds=self.timeout_seconds,
                verification_remediation_attempts=self.max_verify_retries,
                verify=stage.kind not in {"planning", "research", "review"},
                require_instance=False,
            )
        except Exception as exc:
            candidate.status = "failed"
            candidate.error = str(exc)
            log.error("candidate %s raised", candidate.run_id, exc_info=True)
            return
        finally:
            if previous_model is None:
                os.environ.pop("OUROBOROS_MODEL", None)
            else:
                os.environ["OUROBOROS_MODEL"] = previous_model
            candidate.duration_ms = int((_time.monotonic() - started) * 1000)

        if not isinstance(result, dict):
            candidate.status = "failed"
            candidate.error = f"unexpected result type: {type(result).__name__}"
            return

        candidate.full_result = result
        candidate.candidate_manifest_path = str(
            result.get("candidate_manifest_path") or ""
        )
        normalized = str(result.get("status") or "").lower()
        if normalized in {"complete", "completed", "verified", "ok", "success"}:
            candidate.status = "completed"
        elif normalized in {"cancelled", "stopped"}:
            candidate.status = "cancelled"
        else:
            candidate.status = "failed"
            candidate.error = str(
                result.get("error") or result.get("final_message") or normalized or ""
            )[:400]

    def _emit_candidate_completed(
        self, stage: HarnessStagePlan, candidate: HarnessCandidateResult
    ) -> None:
        self._emit(
            HarnessEvent(
                type="stage_candidate_completed",
                candidate_index=candidate.index,
                candidate_id=candidate.candidate_id,
                stage_index=stage.index,
                stage_id=stage.stage_id,
                stage_title=stage.title,
                stage_kind=stage.kind,
                message=f"{candidate.candidate_id} {candidate.status}",
                data={
                    "run_id": candidate.run_id,
                    "status": candidate.status,
                    "score": candidate.score,
                    "duration_ms": candidate.duration_ms,
                    "error": candidate.error,
                    "candidate": candidate.to_dict(),
                },
                ts=_now_iso(),
            )
        )

    def _apply_stage_winner(
        self, stage: HarnessStagePlan, winner: HarnessCandidateResult
    ) -> bool:
        winner_applied = False
        full_result = winner.full_result or {}
        candidate_meta_id = str(full_result.get("candidate_id") or "").strip()
        if candidate_meta_id and self._apply_fn is not None:
            try:
                winner_applied = bool(self._apply_fn(self.repo_root, candidate_meta_id))
            except Exception:  # pragma: no cover - defensive
                log.error(
                    "apply_candidate_patch failed for %s", winner.run_id, exc_info=True
                )
                winner_applied = False
        self._emit_stage_event(
            "stage_patch_applied" if winner_applied else "stage_patch_skipped",
            stage,
            "winner patch applied"
            if winner_applied
            else "winner patch could not be applied",
            candidate=winner,
            data={
                "candidate_meta_id": candidate_meta_id,
                "winner_applied": winner_applied,
            },
        )
        return winner_applied

    def _prune_losers(self, losers: list[HarnessCandidateResult]) -> list[str]:
        pruned: list[str] = []
        root = (self.repo_root / ".umbrella" / "meta_harness" / "experiments").resolve()
        for loser in losers:
            manifest = str(loser.candidate_manifest_path or "").strip()
            if not manifest:
                continue
            cand_dir = Path(manifest)
            if not cand_dir.is_absolute():
                cand_dir = self.repo_root / cand_dir
            cand_dir = cand_dir.parent
            try:
                resolved = cand_dir.resolve()
                if not str(resolved).startswith(str(root)):
                    continue
                if resolved.exists() and resolved.is_dir():
                    shutil.rmtree(resolved)
                    loser.pruned = True
                    pruned.append(loser.candidate_id)
            except Exception:
                log.debug(
                    "failed to prune loser candidate %s", loser.run_id, exc_info=True
                )
        return pruned

    # ------------------------------------------------------------------
    # Events and final text
    # ------------------------------------------------------------------

    def _emit(self, event: HarnessEvent) -> None:
        if not event.ts:
            event.ts = _now_iso()
        try:
            self.on_event(event)
        except Exception:  # pragma: no cover - never crash the orchestrator on UI bugs
            log.exception("on_event callback raised")

    def _emit_stage_event(
        self,
        event_type: str,
        stage: HarnessStagePlan,
        message: str,
        *,
        candidate: HarnessCandidateResult | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self._emit(
            HarnessEvent(
                type=event_type,
                candidate_index=candidate.index if candidate else None,
                candidate_id=candidate.candidate_id if candidate else None,
                stage_index=stage.index,
                stage_id=stage.stage_id,
                stage_title=stage.title,
                stage_kind=stage.kind,
                message=message,
                data=data or {},
                ts=_now_iso(),
            )
        )

    def _build_final_message(
        self,
        stages: list[HarnessStageResult],
        status: str,
        error: str,
    ) -> str:
        if status == "cancelled":
            return (
                f"Harness cancelled after {len(stages)}/{len(self.stage_plans)} "
                f"stage(s)."
            )
        if status == "failed":
            return (
                f"Harness failed at stage {len(stages)}/{len(self.stage_plans)}: "
                f"{error or 'no candidate completed'}"
            )
        winners = [
            f"{stage.index + 1}. {stage.title}: {stage.winner_id}"
            for stage in stages
            if stage.winner_id
        ]
        suffix = "; ".join(winners[-4:])
        return (
            f"Harness completed {len(stages)} staged tournament(s); "
            f"winners: {suffix or 'none'}."
        )
