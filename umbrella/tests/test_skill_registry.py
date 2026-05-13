from pathlib import Path

from umbrella.skills.loader import render_l1, render_l2
from umbrella.skills.registry import discover_skills, filter_by_domain, match_for_task


def _write_skill(root: Path, slug: str, status: str, when_to_use: str) -> None:
    skill_dir = root / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {slug}\n"
            f"status: {status}\n"
            "domains: [multi_agent_gmas]\n"
            f"when_to_use: {when_to_use}\n"
            "---\n\n"
            "## Steps\n"
            "1. Do the thing.\n"
        ),
        encoding="utf-8",
    )


def test_discover_filter_match_skills(tmp_path: Path) -> None:
    library = tmp_path / "library"
    _write_skill(
        library,
        "gmas-rolegraph",
        "active",
        "Use when role graph orchestration is needed",
    )
    _write_skill(
        library, "gmas-streaming", "candidate", "Use when streaming pipeline is needed"
    )

    skills = discover_skills(library)
    assert len(skills) == 2

    active = filter_by_domain(skills, {"multi_agent_gmas"}, status="active")
    assert [s.slug for s in active] == ["gmas-rolegraph"]

    matched = match_for_task(
        "need role graph and orchestration",
        skills,
        domains={"multi_agent_gmas"},
        status="active",
        limit=2,
    )
    assert matched
    assert matched[0].slug == "gmas-rolegraph"
    assert "gmas-rolegraph" in render_l1(matched[0])
    assert "when_to_use" in render_l2(matched[0])
