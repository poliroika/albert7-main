"""Skill lifecycle operations.

The Meta-Harness-based promotion gate was removed in the PhaseRunner refactor.
Skill activation now happens through manifest `allowed_skills` lists: any skill
that appears in a phase manifest is automatically considered active for that
phase. The ``promote_skill`` helper below is kept as a low-touch flag flip for
operator workflows.
"""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import yaml

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

    payload, body = _read_frontmatter(skill.path)
    payload["status"] = "active"
    _write_frontmatter(skill.path, payload, body)
    return SkillPromotionResult(
        status="promoted",
        message="Skill promoted to active",
        skill_slug=slug,
        decision="promote",
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
            body = "\n".join(lines[idx + 1:]).lstrip("\n")
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
        },
        ensure_ascii=False,
    )
