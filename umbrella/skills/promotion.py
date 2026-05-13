"""Skill lifecycle operations with Meta-Harness-based promotion gate."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import yaml

from umbrella.meta_harness.evaluator import evaluate_candidate_on_search_set
from umbrella.meta_harness.promotion import decide_candidate_promotion
from umbrella.meta_harness.store import get_default_store
from umbrella.skills.registry import (
    SkillPack,
    discover_skills,
    get_skill_by_slug,
    skill_library_root,
)


@dataclass(slots=True)
class SkillPromotionResult:
    status: str
    message: str
    skill_slug: str
    baseline_score: float = 0.0
    candidate_score: float = 0.0
    decision: str = ""


def list_skills(repo_root: Path, *, status: str | None = None) -> list[SkillPack]:
    skills = discover_skills(skill_library_root(repo_root))
    if status:
        return [skill for skill in skills if skill.status == status]
    return skills


def retire_skill(repo_root: Path, slug: str) -> SkillPromotionResult:
    skill_path = _skill_file(repo_root, slug)
    if not skill_path.exists():
        return SkillPromotionResult("not_found", "Skill file not found", slug)
    payload, body = _read_frontmatter(skill_path)
    payload["status"] = "retired"
    _write_frontmatter(skill_path, payload, body)
    return SkillPromotionResult("retired", "Skill marked as retired", slug)


def promote_skill(repo_root: Path, slug: str) -> SkillPromotionResult:
    library = skill_library_root(repo_root)
    skills = discover_skills(library)
    skill = get_skill_by_slug(skills, slug)
    if skill is None:
        return SkillPromotionResult("not_found", "Skill not found", slug)
    if skill.status == "active":
        return SkillPromotionResult("already_active", "Skill is already active", slug)

    store = get_default_store(repo_root)
    experiment = store.get_latest_experiment()
    if experiment is None:
        return SkillPromotionResult(
            "insufficient_data", "No Meta-Harness experiment found", slug
        )
    search_set = store.get_search_set(experiment.id)
    if search_set is None or not search_set.tasks:
        return SkillPromotionResult(
            "insufficient_data", "No search set for latest experiment", slug
        )

    candidate_id = experiment.best_candidate_id or (
        experiment.candidate_ids[-1] if experiment.candidate_ids else ""
    )
    if not candidate_id:
        return SkillPromotionResult(
            "insufficient_data", "No candidate available for evaluation", slug
        )

    candidate_eval = evaluate_candidate_on_search_set(
        repo_root,
        candidate_id,
        search_set,
        store=store,
    )

    baseline_score = 0.0
    if experiment.baseline_candidate_id:
        baseline_eval = evaluate_candidate_on_search_set(
            repo_root,
            experiment.baseline_candidate_id,
            search_set,
            store=store,
        )
        baseline_score = baseline_eval.avg_score

    decision = decide_candidate_promotion(
        repo_root,
        candidate_id,
        baseline_candidate_id=experiment.baseline_candidate_id or None,
        search_eval=candidate_eval,
        store=store,
    )
    candidate_score = candidate_eval.avg_score
    approved = (
        decision.decision.value == "promote" and decision.passes_runtime_verification
    )

    if not approved:
        return SkillPromotionResult(
            status="rejected",
            message=decision.reasoning,
            skill_slug=slug,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            decision=decision.decision.value,
        )

    payload, body = _read_frontmatter(skill.path)
    payload["status"] = "active"
    verified_on = payload.get("verified_on")
    if not isinstance(verified_on, list):
        verified_on = []
    verified_on.append(
        {
            "experiment_id": experiment.id,
            "candidate_id": candidate_id,
            "workspace_id": experiment.workspace_id,
            "passed": True,
            "baseline_score": round(baseline_score, 5),
            "candidate_score": round(candidate_score, 5),
            "decision": decision.decision.value,
        }
    )
    payload["verified_on"] = verified_on
    _write_frontmatter(skill.path, payload, body)
    return SkillPromotionResult(
        status="promoted",
        message="Skill promoted to active",
        skill_slug=slug,
        baseline_score=baseline_score,
        candidate_score=candidate_score,
        decision=decision.decision.value,
    )


def _skill_file(repo_root: Path, slug: str) -> Path:
    return skill_library_root(repo_root) / slug.strip().lower() / "SKILL.md"


def _read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return {}, raw
    lines = raw.splitlines()
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            header = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :]).lstrip("\n")
            loaded = yaml.safe_load(header) or {}
            if not isinstance(loaded, dict):
                loaded = {}
            return loaded, body
    return {}, raw


def _write_frontmatter(path: Path, payload: dict[str, Any], body: str) -> None:
    header = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).strip()
    text = f"---\n{header}\n---\n\n{body.rstrip()}\n"
    path.write_text(text, encoding="utf-8")


def format_cli_payload(result: SkillPromotionResult) -> str:
    return json.dumps(
        {
            "status": result.status,
            "message": result.message,
            "skill_slug": result.skill_slug,
            "decision": result.decision,
            "baseline_score": result.baseline_score,
            "candidate_score": result.candidate_score,
        },
        ensure_ascii=False,
    )
