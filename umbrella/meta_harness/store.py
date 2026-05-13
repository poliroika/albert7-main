"""Filesystem-backed store for Meta-Harness experiments and candidates.

Layout:
    .umbrella/meta_harness/
        experiments/
            <experiment_id>/
                experiment.json
                search_set.json
                heldout_set.json
                candidates/
                    <candidate_id>/
                        manifest.json
                        hypothesis.md
                        diagnosis.md
                        prompt_snapshot/
                        policy_snapshot/
                        source_snapshot/
                        memory_input/
                        execution/
                        evaluation/
                        diffs/
                        promotion/
                index.json
"""

import json
import logging
from pathlib import Path
from typing import Any

from umbrella.meta_harness.models import (
    CandidateEval,
    CandidateManifest,
    CandidateStatus,
    ExperimentRecord,
    ExperimentStatus,
    MetaPromotionDecision,
    SearchSet,
    generate_experiment_id,
)

log = logging.getLogger(__name__)


class MetaHarnessStore:
    """Filesystem-backed store for meta-harness data."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.experiments_dir = self.root / "experiments"
        self._ensure_layout()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _ensure_layout(self) -> None:
        self.experiments_dir.mkdir(parents=True, exist_ok=True)

    def _experiment_dir(self, experiment_id: str) -> Path:
        return self.experiments_dir / experiment_id

    def _candidate_dir(self, experiment_id: str, candidate_id: str) -> Path:
        return self._experiment_dir(experiment_id) / "candidates" / candidate_id

    def _ensure_candidate_dirs(self, experiment_id: str, candidate_id: str) -> Path:
        cand_dir = self._candidate_dir(experiment_id, candidate_id)
        for sub in (
            "prompt_snapshot",
            "policy_snapshot",
            "source_snapshot",
            "memory_input",
            "execution",
            "evaluation",
            "diffs",
            "promotion",
        ):
            (cand_dir / sub).mkdir(parents=True, exist_ok=True)
        return cand_dir

    # ------------------------------------------------------------------
    # JSON helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").strip().split("\n"):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("Skipping malformed JSONL line in %s", path)
        return records

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        *,
        name: str = "",
        workspace_id: str = "",
        search_set: SearchSet | None = None,
        heldout_set: SearchSet | None = None,
        max_iterations: int = 0,
        max_budget_usd: float = 0.0,
    ) -> ExperimentRecord:
        exp = ExperimentRecord(
            id=generate_experiment_id(),
            name=name,
            workspace_id=workspace_id,
            search_set_id=search_set.id if search_set else "",
            heldout_set_id=heldout_set.id if heldout_set else "",
            max_iterations=max_iterations,
            max_budget_usd=max_budget_usd,
        )
        exp_dir = self._experiment_dir(exp.id)
        exp_dir.mkdir(parents=True, exist_ok=True)
        (exp_dir / "candidates").mkdir(exist_ok=True)

        self._write_json(exp_dir / "experiment.json", exp.model_dump(mode="json"))

        if search_set:
            self._write_json(
                exp_dir / "search_set.json", search_set.model_dump(mode="json")
            )
        if heldout_set:
            self._write_json(
                exp_dir / "heldout_set.json", heldout_set.model_dump(mode="json")
            )

        self._rebuild_index(exp.id)
        return exp

    def get_experiment(self, experiment_id: str) -> ExperimentRecord | None:
        path = self._experiment_dir(experiment_id) / "experiment.json"
        if not path.exists():
            return None
        try:
            return ExperimentRecord(**self._read_json(path))
        except Exception:
            log.warning("Failed to load experiment %s", experiment_id, exc_info=True)
            return None

    def update_experiment(self, experiment: ExperimentRecord) -> None:
        experiment.touch()
        exp_dir = self._experiment_dir(experiment.id)
        self._write_json(
            exp_dir / "experiment.json", experiment.model_dump(mode="json")
        )

    def list_experiments(self) -> list[ExperimentRecord]:
        experiments = []
        if not self.experiments_dir.exists():
            return experiments
        for exp_dir in sorted(self.experiments_dir.iterdir()):
            path = exp_dir / "experiment.json"
            if path.exists():
                try:
                    experiments.append(ExperimentRecord(**self._read_json(path)))
                except Exception:
                    log.warning(
                        "Skipping malformed experiment %s", exp_dir.name, exc_info=True
                    )
        return experiments

    def get_latest_experiment(self) -> ExperimentRecord | None:
        experiments = self.list_experiments()
        if not experiments:
            return None
        return max(experiments, key=lambda e: e.created_at)

    def get_or_create_experiment(self, **kwargs: Any) -> ExperimentRecord:
        latest = self.get_latest_experiment()
        if latest and latest.status == ExperimentStatus.ACTIVE:
            return latest
        return self.create_experiment(**kwargs)

    # ------------------------------------------------------------------
    # Candidates
    # ------------------------------------------------------------------

    def save_candidate(self, manifest: CandidateManifest) -> Path:
        exp_id = manifest.experiment_id or "_default"
        cand_dir = self._ensure_candidate_dirs(exp_id, manifest.candidate_id)
        self._write_json(cand_dir / "manifest.json", manifest.model_dump(mode="json"))

        exp = self.get_experiment(exp_id)
        if exp and manifest.candidate_id not in exp.candidate_ids:
            exp.candidate_ids.append(manifest.candidate_id)
            self.update_experiment(exp)

        self._rebuild_index(exp_id)
        return cand_dir

    def get_candidate(
        self, experiment_id: str, candidate_id: str
    ) -> CandidateManifest | None:
        path = self._candidate_dir(experiment_id, candidate_id) / "manifest.json"
        if not path.exists():
            return None
        try:
            return CandidateManifest(**self._read_json(path))
        except Exception:
            log.warning(
                "Failed to load candidate %s/%s",
                experiment_id,
                candidate_id,
                exc_info=True,
            )
            return None

    def find_candidate(self, candidate_id: str) -> CandidateManifest | None:
        """Find a candidate across all experiments."""
        for exp_dir in self.experiments_dir.iterdir():
            path = exp_dir / "candidates" / candidate_id / "manifest.json"
            if path.exists():
                try:
                    return CandidateManifest(**self._read_json(path))
                except Exception:
                    continue
        return None

    def find_candidate_dir(self, candidate_id: str) -> Path | None:
        for exp_dir in self.experiments_dir.iterdir():
            cand_dir = exp_dir / "candidates" / candidate_id
            if (cand_dir / "manifest.json").exists():
                return cand_dir
        return None

    def list_candidates(self, experiment_id: str) -> list[CandidateManifest]:
        candidates_dir = self._experiment_dir(experiment_id) / "candidates"
        if not candidates_dir.exists():
            return []
        results = []
        for cand_dir in sorted(candidates_dir.iterdir()):
            path = cand_dir / "manifest.json"
            if path.exists():
                try:
                    results.append(CandidateManifest(**self._read_json(path)))
                except Exception:
                    log.warning(
                        "Skipping malformed candidate %s", cand_dir.name, exc_info=True
                    )
        return results

    def get_candidate_dir(self, experiment_id: str, candidate_id: str) -> Path:
        return self._candidate_dir(experiment_id, candidate_id)

    # ------------------------------------------------------------------
    # Evaluations
    # ------------------------------------------------------------------

    def save_eval(self, evaluation: CandidateEval) -> None:
        cand_dir = self.find_candidate_dir(evaluation.candidate_id)
        if cand_dir is None:
            log.warning(
                "Cannot save eval: candidate %s not found", evaluation.candidate_id
            )
            return
        self._write_json(
            cand_dir / "evaluation" / "eval.json",
            evaluation.model_dump(mode="json"),
        )

    def get_eval(self, candidate_id: str) -> CandidateEval | None:
        cand_dir = self.find_candidate_dir(candidate_id)
        if cand_dir is None:
            return None
        path = cand_dir / "evaluation" / "eval.json"
        if not path.exists():
            return None
        try:
            return CandidateEval(**self._read_json(path))
        except Exception:
            log.warning("Failed to load eval for %s", candidate_id, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Promotion Decisions
    # ------------------------------------------------------------------

    def save_promotion_decision(self, decision: MetaPromotionDecision) -> None:
        cand_dir = self.find_candidate_dir(decision.candidate_id)
        if cand_dir is None:
            log.warning(
                "Cannot save promotion: candidate %s not found", decision.candidate_id
            )
            return
        self._write_json(
            cand_dir / "promotion" / "decision.json",
            decision.model_dump(mode="json"),
        )

    def get_promotion_decision(self, candidate_id: str) -> MetaPromotionDecision | None:
        cand_dir = self.find_candidate_dir(candidate_id)
        if cand_dir is None:
            return None
        path = cand_dir / "promotion" / "decision.json"
        if not path.exists():
            return None
        try:
            return MetaPromotionDecision(**self._read_json(path))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Search Sets
    # ------------------------------------------------------------------

    def get_search_set(self, experiment_id: str) -> SearchSet | None:
        path = self._experiment_dir(experiment_id) / "search_set.json"
        if not path.exists():
            return None
        try:
            return SearchSet(**self._read_json(path))
        except Exception:
            return None

    def get_heldout_set(self, experiment_id: str) -> SearchSet | None:
        path = self._experiment_dir(experiment_id) / "heldout_set.json"
        if not path.exists():
            return None
        try:
            return SearchSet(**self._read_json(path))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Execution traces
    # ------------------------------------------------------------------

    def save_execution_events(
        self, experiment_id: str, candidate_id: str, events: list[dict[str, Any]]
    ) -> Path:
        cand_dir = self._ensure_candidate_dirs(experiment_id, candidate_id)
        path = cand_dir / "execution" / "events.jsonl"
        self._write_jsonl(path, events)
        return path

    def get_execution_events(self, candidate_id: str) -> list[dict[str, Any]]:
        cand_dir = self.find_candidate_dir(candidate_id)
        if cand_dir is None:
            return []
        return self._read_jsonl(cand_dir / "execution" / "events.jsonl")

    # ------------------------------------------------------------------
    # Snapshot helpers
    # ------------------------------------------------------------------

    def save_text_snapshot(
        self,
        experiment_id: str,
        candidate_id: str,
        category: str,
        filename: str,
        content: str,
    ) -> Path:
        cand_dir = self._ensure_candidate_dirs(experiment_id, candidate_id)
        path = cand_dir / category / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    def _rebuild_index(self, experiment_id: str) -> None:
        exp_dir = self._experiment_dir(experiment_id)
        candidates = self.list_candidates(experiment_id)
        index = {
            "experiment_id": experiment_id,
            "candidate_count": len(candidates),
            "candidates": [
                {
                    "candidate_id": c.candidate_id,
                    "status": c.status,
                    "created_at": c.created_at,
                    "workspace_id": c.workspace_id,
                    "run_status": c.run_status,
                    "write_calls": c.write_calls,
                }
                for c in candidates
            ],
        }
        self._write_json(exp_dir / "candidates" / "index.json", index)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def top_candidates(
        self, experiment_id: str, *, n: int = 10, sort_by: str = "score"
    ) -> list[tuple[CandidateManifest, CandidateEval | None]]:
        candidates = self.list_candidates(experiment_id)
        pairs: list[tuple[CandidateManifest, CandidateEval | None]] = []
        for c in candidates:
            ev = self.get_eval(c.candidate_id)
            pairs.append((c, ev))

        if sort_by == "score":
            pairs.sort(key=lambda p: p[1].avg_score if p[1] else 0.0, reverse=True)
        elif sort_by == "cost":
            pairs.sort(key=lambda p: p[1].total_cost_usd if p[1] else float("inf"))
        else:
            pairs.sort(key=lambda p: p[0].created_at, reverse=True)

        return pairs[:n]

    def get_failures(
        self, experiment_id: str, *, workspace_id: str = ""
    ) -> list[tuple[CandidateManifest, CandidateEval | None]]:
        candidates = self.list_candidates(experiment_id)
        results = []
        for c in candidates:
            if workspace_id and c.workspace_id != workspace_id:
                continue
            if c.status in (CandidateStatus.ERROR, CandidateStatus.REJECTED):
                ev = self.get_eval(c.candidate_id)
                results.append((c, ev))
            elif c.run_status in ("error", "incomplete"):
                ev = self.get_eval(c.candidate_id)
                results.append((c, ev))
        return results


def get_default_store(repo_root: Path) -> MetaHarnessStore:
    return MetaHarnessStore(repo_root / ".umbrella" / "meta_harness")
