"""Progressive skill rendering helpers (L1/L2/L3)."""

from pathlib import Path

from umbrella.llm_budget import estimate_tokens
from umbrella.skills.registry import SkillPack


def render_l1(skill: SkillPack) -> str:
    """Very compact skill hint for prompt bootstrap."""
    hint = skill.when_to_use or "Use when task context matches this pattern."
    return f"- {skill.slug}: {hint[:140]}"


def render_l2(skill: SkillPack, *, max_tokens: int = 220) -> str:
    """Medium detail card with metadata + short body excerpt."""
    lines: list[str] = [
        f"### {skill.name} (`{skill.slug}`)",
        f"- status: `{skill.status}`",
    ]
    if skill.domains:
        lines.append(f"- domains: {', '.join(skill.domains)}")
    if skill.when_to_use:
        lines.append(f"- when_to_use: {skill.when_to_use}")
    if skill.params:
        params = ", ".join(
            f"{p.get('name', 'param')}: {str(p.get('description', '')).strip()[:80]}"
            for p in skill.params[:6]
        )
        lines.append(f"- params: {params}")
    if skill.body:
        lines.append("")
        lines.append(_truncate_for_tokens(skill.body, max_tokens=max_tokens))
    return "\n".join(lines).strip()


def render_l3(skill: SkillPack) -> str:
    """Full skill payload from disk."""
    try:
        return skill.path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_skill_text(repo_root: Path, slug: str) -> str:
    """Read full ``SKILL.md`` by slug for explicit tool retrieval."""
    path = (repo_root / "umbrella" / "skills" / "library" / slug / "SKILL.md").resolve()
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _truncate_for_tokens(text: str, *, max_tokens: int) -> str:
    if estimate_tokens(text) <= max_tokens:
        return text
    max_chars = max(300, max_tokens * 4)
    return text[:max_chars].rstrip() + "\n\n[...truncated]"
