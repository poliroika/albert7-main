"""Skill library registry for procedural memory packs."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import yaml


_TOKEN_RE = re.compile(r"[\w']+")


@dataclass(slots=True)
class SkillPack:
    """Parsed skill pack stored on disk."""

    slug: str
    path: Path
    name: str
    status: str
    domains: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    when_to_use: str = ""
    params: list[dict[str, Any]] = field(default_factory=list)
    body: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def searchable_text(self) -> str:
        return " ".join(
            [
                self.slug,
                self.name,
                self.when_to_use,
                " ".join(self.domains),
                " ".join(self.phases),
                self.body[:2000],
            ]
        )


def skill_library_root(repo_root: Path) -> Path:
    return (repo_root / "umbrella" / "skills" / "library").resolve()


def discover_skills(library_root: Path) -> list[SkillPack]:
    """Scan ``library_root`` and parse ``*/SKILL.md`` packs."""
    if not library_root.exists():
        return []
    skills: list[SkillPack] = []
    for skill_file in sorted(library_root.glob("*/SKILL.md")):
        parsed = parse_skill_file(skill_file)
        if parsed is not None:
            skills.append(parsed)
    return skills


def get_skill_by_slug(skills: list[SkillPack], slug: str) -> SkillPack | None:
    target = slug.strip().lower()
    for skill in skills:
        if skill.slug == target:
            return skill
    return None


def filter_by_phase(
    skills: list[SkillPack],
    phase: str,
    *,
    status: str | None = "active",
) -> list[SkillPack]:
    """Return skills whose ``phases`` list includes *phase* (or have no phase restriction)."""
    out: list[SkillPack] = []
    for skill in skills:
        if status and skill.status != status:
            continue
        if skill.phases and phase not in skill.phases:
            continue
        out.append(skill)
    return out


def filter_by_domain(
    skills: list[SkillPack],
    domains: set[str],
    *,
    status: str | None = "active",
) -> list[SkillPack]:
    """Filter skills by status and domain tags."""
    normalized_domains = {d.strip().lower() for d in domains if d and d.strip()}
    out: list[SkillPack] = []
    for skill in skills:
        if status and skill.status != status:
            continue
        if normalized_domains and not set(skill.domains).intersection(
            normalized_domains
        ):
            continue
        out.append(skill)
    return out


def match_for_task(
    task_text: str,
    skills: list[SkillPack],
    *,
    domains: set[str] | None = None,
    status: str | None = "active",
    limit: int = 3,
) -> list[SkillPack]:
    """Return top skill packs relevant to ``task_text``."""
    candidates = skills
    if domains is not None:
        candidates = filter_by_domain(candidates, domains, status=status)
    elif status:
        candidates = [s for s in candidates if s.status == status]

    query_tokens = _tokens(task_text)
    ranked: list[tuple[float, SkillPack]] = []
    for skill in candidates:
        score = _match_score(query_tokens, skill)
        ranked.append((score, skill))
    ranked.sort(key=lambda item: (item[0], item[1].name.lower()), reverse=True)
    sliced = [item[1] for item in ranked[: max(1, min(limit, 20))]]
    return [skill for skill in sliced if _match_score(query_tokens, skill) > 0]


def parse_skill_file(path: Path) -> SkillPack | None:
    """Parse a ``SKILL.md`` file into a structured ``SkillPack``."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    frontmatter, body = _split_frontmatter(raw)
    payload: dict[str, Any] = {}
    if frontmatter:
        try:
            loaded = yaml.safe_load(frontmatter) or {}
            if isinstance(loaded, dict):
                payload = loaded
        except yaml.YAMLError:
            payload = {}

    slug = path.parent.name.strip().lower()
    name = str(payload.get("name") or slug).strip() or slug
    status = str(payload.get("status") or "candidate").strip().lower()
    raw_domains = payload.get("domains") or []
    if isinstance(raw_domains, str):
        domains = [raw_domains.strip().lower()] if raw_domains.strip() else []
    elif isinstance(raw_domains, list):
        domains = [
            str(item).strip().lower() for item in raw_domains if str(item).strip()
        ]
    else:
        domains = []
    params = payload.get("params") if isinstance(payload.get("params"), list) else []
    raw_phases = payload.get("phases") or []
    if isinstance(raw_phases, str):
        phases = [raw_phases.strip().lower()] if raw_phases.strip() else []
    elif isinstance(raw_phases, list):
        phases = [str(p).strip().lower() for p in raw_phases if str(p).strip()]
    else:
        phases = []
    metadata = {
        key: value
        for key, value in payload.items()
        if key not in {"name", "status", "domains", "phases", "when_to_use", "params"}
    }
    return SkillPack(
        slug=slug,
        path=path,
        name=name,
        status=status,
        domains=domains,
        phases=phases,
        when_to_use=str(payload.get("when_to_use") or "").strip(),
        params=[item for item in params if isinstance(item, dict)],
        body=body.strip(),
        metadata=metadata,
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            frontmatter = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :])
            return frontmatter, body
    return "", text


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _match_score(query_tokens: set[str], skill: SkillPack) -> float:
    if not query_tokens:
        return 0.0
    haystack = skill.searchable_text.lower()
    skill_tokens = _tokens(haystack)
    overlap = len(query_tokens.intersection(skill_tokens))
    substring_hits = sum(1 for token in query_tokens if token in haystack)
    domain_bonus = 2 if any(token in skill.domains for token in query_tokens) else 0
    return float(overlap * 3 + substring_hits + domain_bonus)
