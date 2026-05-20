"""Umbrella domain policy primitives shared across phase tools."""

from __future__ import annotations

from dataclasses import dataclass
import re


LLM_RUNTIME_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("OUROBOROS_LLM_API_KEY", "LLM_API_KEY"),
    ("OUROBOROS_LLM_BASE_URL", "LLM_BASE_URL"),
    ("OUROBOROS_MODEL", "LLM_MODEL"),
)

UNSUPPORTED_LLM_ENV_ALIASES: dict[str, str] = {
    "LL_BASE_URL": "LLM_BASE_URL",
    "OUROBOROS_LLM_MODEL": "OUROBOROS_MODEL",
}


@dataclass(frozen=True)
class DomainPolicyIssue:
    code: str
    message: str


_INVALID_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(alias) for alias in UNSUPPORTED_LLM_ENV_ALIASES) + r")\b"
)


def unsupported_llm_env_alias_issues(
    text: str, *, subject: str = "plan", exclude_aliases: set[str] | None = None
) -> list[DomainPolicyIssue]:
    issues: list[DomainPolicyIssue] = []
    excluded = exclude_aliases or set()
    seen: set[str] = set()
    for match in _INVALID_ALIAS_RE.finditer(str(text or "")):
        alias = match.group(1)
        if alias in excluded:
            continue
        if alias in seen:
            continue
        seen.add(alias)
        replacement = UNSUPPORTED_LLM_ENV_ALIASES[alias]
        issues.append(
            DomainPolicyIssue(
                code="unsupported_llm_env_alias",
                message=(
                    f"{subject} uses unsupported LLM runtime env alias `{alias}`. "
                    f"Use `{replacement}` as part of the Umbrella/Ouroboros "
                    "runtime contract instead."
                ),
            )
        )
    return issues


__all__ = [
    "DomainPolicyIssue",
    "LLM_RUNTIME_ALIAS_GROUPS",
    "UNSUPPORTED_LLM_ENV_ALIASES",
    "unsupported_llm_env_alias_issues",
]
