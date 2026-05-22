"""Umbrella domain policy primitives shared across phase tools."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import re


PUBLIC_LLM_ENV_ALIASES: tuple[str, ...] = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
)

HOST_LLM_ENV_BRIDGE_ALIASES: dict[str, str] = {
    "OUROBOROS_LLM_API_KEY": "LLM_API_KEY",
    "OUROBOROS_LLM_BASE_URL": "LLM_BASE_URL",
    "OUROBOROS_MODEL": "LLM_MODEL",
}

LLM_RUNTIME_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("LLM_API_KEY", "OUROBOROS_LLM_API_KEY"),
    ("LLM_BASE_URL", "OUROBOROS_LLM_BASE_URL"),
    ("LLM_MODEL", "OUROBOROS_MODEL"),
)

UNSUPPORTED_LLM_ENV_ALIASES: dict[str, str] = {
    "LL_BASE_URL": "LLM_BASE_URL",
    "OUROBOROS_LLM_MODEL": "LLM_MODEL",
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
                    f"Use `{replacement}` as part of the generated workspace's "
                    "public LLM runtime contract instead."
                ),
            )
        )
    return issues


def public_workspace_llm_env_bridge(
    env: Mapping[str, str | None],
) -> dict[str, str]:
    """Return public LLM_* env values bridged from Umbrella host aliases.

    Generated projects should read the provider-neutral ``LLM_*`` aliases.
    Umbrella may itself be launched with ``OUROBOROS_*`` aliases, so workspace
    command runners normalize those host names into ``LLM_*`` before running
    generated code/tests.
    """

    bridged: dict[str, str] = {}
    for host_alias, public_alias in HOST_LLM_ENV_BRIDGE_ALIASES.items():
        public_value = str(env.get(public_alias) or "").strip()
        if public_value:
            continue
        host_value = str(env.get(host_alias) or "").strip()
        if host_value:
            bridged[public_alias] = host_value
    return bridged


__all__ = [
    "DomainPolicyIssue",
    "HOST_LLM_ENV_BRIDGE_ALIASES",
    "LLM_RUNTIME_ALIAS_GROUPS",
    "PUBLIC_LLM_ENV_ALIASES",
    "UNSUPPORTED_LLM_ENV_ALIASES",
    "public_workspace_llm_env_bridge",
    "unsupported_llm_env_alias_issues",
]
