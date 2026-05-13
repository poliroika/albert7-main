"""Umbrella workspace skills: detect task domains, surface existing tools.

Skills do **not** duplicate knowledge that already lives in the repo. Their
job is to:

1. Recognise *what kind of task* the user is asking for, in any language,
   without hardcoded keyword lists tied to one human language.
2. When a domain is detected, make Umbrella dispatch the **already existing**
   tooling for that domain (e.g. ``umbrella.retrieval.gmas_context``) on
   behalf of the agent, so the agent always starts with a fresh, focused
   context dump instead of being asked to "please call this tool".

Public API:

- :func:`detect_task_domains` -- bilingual / language-agnostic detection
  with an LLM-classifier-first strategy and a narrow keyword fallback.
- :func:`summarize_domains` -- short banner for ``active_skills.md``.

Domains live in :class:`Domain` and are extended by adding entries to the
classifier prompt and the keyword fallback. No giant per-domain knowledge
packs are shipped from this module by design.
"""

from umbrella.skills.domain_detection import (
    Domain,
    classify_with_keywords,
    classify_with_llm,
    detect_task_domains,
    summarize_domains,
)
from umbrella.skills.registry import (
    SkillPack,
    discover_skills,
    filter_by_domain,
    get_skill_by_slug,
    match_for_task,
    parse_skill_file,
    skill_library_root,
)
from umbrella.skills.loader import (
    load_skill_text,
    render_l1,
    render_l2,
    render_l3,
)
from umbrella.skills.promotion import (
    SkillPromotionResult,
    list_skills,
    promote_skill,
    retire_skill,
)

__all__ = [
    "Domain",
    "classify_with_keywords",
    "classify_with_llm",
    "detect_task_domains",
    "summarize_domains",
    "SkillPack",
    "skill_library_root",
    "discover_skills",
    "parse_skill_file",
    "filter_by_domain",
    "get_skill_by_slug",
    "match_for_task",
    "render_l1",
    "render_l2",
    "render_l3",
    "load_skill_text",
    "SkillPromotionResult",
    "list_skills",
    "promote_skill",
    "retire_skill",
]
