"""Shared policy patterns and imports for Umbrella phase-control tools.

This is not a runtime stub: it is a compiled pattern registry for hard gates
(review loops, research handoffs, LLM env contracts, pytest claims). Patterns
are mostly English because control-plane tool names, env aliases, and pytest
output are ASCII. User-facing handoff prose may be any script; see
``phase_control_text_quality`` for encoding/script checks and
``phase_control_text_quality._HANDOFF_PLACEHOLDER_RE`` for placeholder detection.

Machine identifiers (``architecture_id``, evidence refs) stay ASCII slugs by
design; natural-language content belongs in ``notes`` / palace findings.
"""

import ast
import json
import datetime as dt
import os
import pathlib
import re
import time
import uuid
from typing import Any
from ouroboros.tools.registry import ToolContext, ToolEntry
_LLM_ENV_CONTEXT_RE = re.compile(r"(?i)\b(llm|gmas|agent|bot|model)\b")
_LLM_ENV_CONTRACT_REQUIRED_RE = re.compile(
    r"(?is)\b("
    r"(?:real|live)\s+llm|llm[-\s]?powered|llm\s+(?:client|calls?|"
    r"integration|reasoning)|inherited\s+real\s+runtime\s+env"
    r")\b"
)
_LLM_ENV_OMISSION_REQUIRED_RE = re.compile(
    r"(?i)\b(provider|credentials?|api[-_\s]?keys?|base[-_\s]?url|"
    r"model\s+(?:selection|provider|name)|\.env|env(?:ironment)?\s+"
    r"(?:vars?|variables?))\b"
)
_LLM_OUROBOROS_ENV_ALIASES = (
    "OUROBOROS_LLM_API_KEY",
    "OUROBOROS_LLM_BASE_URL",
    "OUROBOROS_MODEL",
)
_LLM_LEGACY_ENV_ALIASES = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
_LLM_ENV_ALIAS_RE = re.compile(
    r"\b(" + "|".join(re.escape(alias) for alias in _LLM_OUROBOROS_ENV_ALIASES) + r")\b"
)
_LLM_LEGACY_ENV_RE = re.compile(
    r"\b(" + "|".join(re.escape(alias) for alias in _LLM_LEGACY_ENV_ALIASES) + r")\b"
)
_UNSUPPORTED_OUROBOROS_MODEL_ALIAS_RE = re.compile(r"\bOUROBOROS_LLM_MODEL\b")
_OPENAI_KEY_RE = re.compile(r"\bOPENAI_API_KEY\b")
_OPENAI_REQUIRED_RE = re.compile(
    r"(?is)\b(?:must|require[sd]?|need(?:s|ed)?|expects?|set|configure|"
    r"only)\b.{0,80}\bOPENAI_API_KEY\b"
)
_WEB_SEARCH_ONLY_CONTEXT_RE = re.compile(
    r"(?is)\b(?:web\s+search|search\s+api|internet\s+search)\b"
)
_RESEARCH_ARCHITECTURE_ID_RE = re.compile(
    r"^(?:arch|architecture)-[A-Za-z0-9][A-Za-z0-9.-]*$",
    re.IGNORECASE,
)
_RESEARCH_ARCHITECTURE_ID_BAD_TOKEN_RE = re.compile(
    r"(?:^|[-_.:])(?:mock|fake|stub|dry[-_]?run|fallback|placeholder)(?:$|[-_.:])",
    re.IGNORECASE,
)
_RESEARCH_REVIEW_CODE_CLAIM_RE = re.compile(
    r"\b("
    r"importerror|traceback|pytest|test(?:s|ing)?|endpoint|api|http|500|"
    r"missing|required positional argument|constructor|signature|"
    r"import|class|def|__init__|frontend|backend|fastapi|react|typescript"
    r")\b",
    re.IGNORECASE,
)
_RESEARCH_SUMMARY_REL_PATH = ".memory/drive/state/research_summary_latest.json"
_RESEARCH_PATH_RE = re.compile(
    r"(?<![\w:/.-])((?:workspaces/[A-Za-z0-9_. -]+/)?"
    r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+"
    r"\.(?:pyi|tsx|jsx|mjs|cjs|yaml|scss|py|js|ts|json|toml|yml|md|html|css|txt))"
    r"(?![\w.-])"
)
_BAD_REVIEW_FALLBACK_RE = re.compile(
    r"(?is)("
    r"\b(?:fallback|fall[-\s]+back)\b.{0,140}\b(?:hardcoded|localhost\s+defaults?|"
    r"static|heuristics?|random|default|mock|stub|cached\s+decisions?|"
    r"cached\s+actions?|graceful\s+degradation|actions?|ai\s+decisions?|"
    r"behaviou?r|logic|handling|policy|strategy|rules?)\b|"
    r"\bfallback\s+mode\b|"
    r"\bfallback\s+model\s+strategy\b|"
    r"\btimeout\s+fallback\b|"
    r"\bsafe\s+minimal\s+actions?\b|"
    r"\bgraceful\s+degradation\b.{0,140}\b(?:llm|gmas|bot|agent|model|"
    r"ai|rule[-\s]?based|heuristics?|decisions?|actions?|mode|strategy|"
    r"behaviou?r|logic|policy|runtime|credentials?)\b|"
    r"\b(?:hardcoded|localhost\s+defaults?|static|heuristics?|random|default|"
    r"mock|stub|cached\s+decisions?|cached\s+actions?|graceful\s+degradation)"
    r"\b.{0,140}\b(?:fallback|fall[-\s]+back)\b"
    r")"
)
_BAD_REVIEW_LLM_TEST_DOUBLE_RE = re.compile(
    r"(?is)("
    r"\b(?:mock|fake|dry[-\s]?run|test\s+double)\b.{0,120}\b(?:llm|gmas|bot|agent)\b|"
    r"\b(?:llm|gmas|bot|agent)\b.{0,120}\b(?:mock|fake|dry[-\s]?run|test\s+double)\b"
    r")"
)
_BAD_REVIEW_PROVIDER_MODEL_RE = re.compile(
    r"(?is)\b(?:must|require[sd]?|add|specify|recommend(?:ed)?|document)\b"
    r".{0,140}\b(?:gpt-4o(?:-mini)?|openai)\b"
)
_BAD_REVIEW_MEMORY_EDIT_RE = re.compile(
    r"(?is)\b(?:update|modify|edit|rewrite|remove\s+from|change)\b"
    r".{0,120}\b(?:palace|memory\s+artifacts?|research/hall_events|"
    r"hall_events|drawers?|drawer_[a-z0-9]+)\b"
)
_BAD_REVIEW_NONPORTABLE_COMMAND_RE = re.compile(
    r"(?is)(?:"
    r"[`\"'][^`\"']*(?:\bgrep\b|\btimeout\s+\d+\b|\|\|\s*true|"
    r"\bps\s+aux\b|\bpkill\b)[^`\"']*[`\"']|"
    r"\b(?:grep\s+-q|timeout\s+\d+\b|ps\s+aux\b|pkill\b)\b|"
    r"\|\|\s*true"
    r")"
)
_ENV_ALIAS_FALLBACK_RE = re.compile(
    r"(?is)("
    r"\b(?:fallback|fall[-\s]+back|alias(?:es)?|precedence|priority|chain)\b"
    r".{0,140}(?:OUROBOROS_LLM_(?:API_KEY|BASE_URL|\*)|OUROBOROS_MODEL|"
    r"LLM_(?:API_KEY|BASE_URL|MODEL|\*)|"
    r"LLM_BASE_URL|LLM_MODEL|env(?:ironment)?\s+vars?|credential\s+aliases?|"
    r"runtime\s+aliases?)(?=$|[^A-Z0-9_])|"
    r"(?:OUROBOROS_LLM_(?:API_KEY|BASE_URL|\*)|OUROBOROS_MODEL|LLM_(?:API_KEY|BASE_URL|MODEL|\*)|LLM_API_KEY|LLM_BASE_URL|"
    r"LLM_MODEL|env(?:ironment)?\s+vars?|credential\s+aliases?|runtime\s+aliases?)"
    r"(?=$|[^A-Z0-9_]).{0,140}\b(?:fallback|fall[-\s]+back|alias(?:es)?|precedence|priority|chain)\b"
    r")"
)
_DANGEROUS_FALLBACK_RE = re.compile(
    r"(?is)\b(?:hardcoded|localhost\s+defaults?|static|heuristics?|random|"
    r"default(?:s)?|mock|stub|cached\s+decisions?|cached\s+actions?|"
    r"graceful\s+degradation|safe\s+minimal\s+actions?|ai\s+decisions?|"
    r"actions?|rules?|OPENAI_API_KEY|gpt-)"
)
_PYTEST_SKIP_ONLY_RE = re.compile(
    r"(?im)^=+\s*(?P<skipped>\d+)\s+skipped"
    r"(?:,\s*\d+\s+warnings?)?\s+in\s+[\d.]+s\s*=+\s*$"
)
_PYTEST_PASS_RE = re.compile(r"(?i)\b\d+\s+passed\b")
_PYTEST_FAILURE_RE = re.compile(r"(?i)\b\d+\s+(?:failed|errors?|xfailed)\b")
_PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD = 3
_PHASE_SUBTASK_COMMAND_TOOLS = {
    "shell",
    "run_workspace_command",
    "terminal_session",
    "run_subtask_proof",
}
_PHASE_SUBTASK_REPAIR_WRITE_TOOLS = {
    "apply_workspace_patch",
    "replace_workspace_file",
    "update_workspace_seed",
}
_PREFLIGHT_PLATFORM_BLOCKER_RE = re.compile(
    r"\b("
    r"api[-_ ]?key|credential|secret|token|env(?:ironment)? variable|"
    r"llm provider|base_url|model variable|mcp|palace|memory store|"
    r"workspace charter|task_main|human intervention|permission|"
    r"network unavailable|service unavailable"
    r")\b",
    re.IGNORECASE,
)
_PREFLIGHT_IMPLEMENTATION_ISSUE_RE = re.compile(
    r"\b("
    r"import ?error|syntax|compile|verification|test|pytest|collection|localhost|http|"
    r"endpoint|mock|scaffold|application code|source code|python import|"
    r"cannot import|module|package|game|gameengine|app|typeerror|"
    r"api_missing_argument|missing required|missing (?:required )?argument|"
    r"broken imports?|initialization error|codebase|not functional|functional|"
    r"cannot proceed with new development"
    r")\b",
    re.IGNORECASE,
)
_UNRESOLVED_PASS_BLOCKER_RE = re.compile(
    r"("
    r"not\s+blocking\s+verification|"
    r"outside\s+(?:the\s+)?scope|"
    r"requires?\s+fix(?:ing|es)?|"
    r"still\s+(?:broken|failing|fails)|"
    r"runtime\s+.*errors?\s+detected|"
    r"not\s+fully\s+playable|"
    r"not\s+playable|"
    r"cannot\s+be\s+used"
    r")",
    re.IGNORECASE,
)


__all__ = [
    "Any",
    "ToolContext",
    "ToolEntry",
    "_BAD_REVIEW_FALLBACK_RE",
    "_BAD_REVIEW_LLM_TEST_DOUBLE_RE",
    "_BAD_REVIEW_MEMORY_EDIT_RE",
    "_BAD_REVIEW_NONPORTABLE_COMMAND_RE",
    "_BAD_REVIEW_PROVIDER_MODEL_RE",
    "_DANGEROUS_FALLBACK_RE",
    "_ENV_ALIAS_FALLBACK_RE",
    "_LLM_ENV_ALIAS_RE",
    "_LLM_ENV_CONTEXT_RE",
    "_LLM_ENV_CONTRACT_REQUIRED_RE",
    "_LLM_ENV_OMISSION_REQUIRED_RE",
    "_LLM_LEGACY_ENV_ALIASES",
    "_LLM_LEGACY_ENV_RE",
    "_LLM_OUROBOROS_ENV_ALIASES",
    "_OPENAI_KEY_RE",
    "_OPENAI_REQUIRED_RE",
    "_PHASE_SUBTASK_COMMAND_TOOLS",
    "_PHASE_SUBTASK_REPAIR_WRITE_TOOLS",
    "_PHASE_SUBTASK_RETRY_ESCALATION_THRESHOLD",
    "_PREFLIGHT_IMPLEMENTATION_ISSUE_RE",
    "_PREFLIGHT_PLATFORM_BLOCKER_RE",
    "_PYTEST_FAILURE_RE",
    "_PYTEST_PASS_RE",
    "_PYTEST_SKIP_ONLY_RE",
    "_RESEARCH_ARCHITECTURE_ID_BAD_TOKEN_RE",
    "_RESEARCH_ARCHITECTURE_ID_RE",
    "_RESEARCH_PATH_RE",
    "_RESEARCH_REVIEW_CODE_CLAIM_RE",
    "_RESEARCH_SUMMARY_REL_PATH",
    "_UNRESOLVED_PASS_BLOCKER_RE",
    "_UNSUPPORTED_OUROBOROS_MODEL_ALIAS_RE",
    "_WEB_SEARCH_ONLY_CONTEXT_RE",
    "ast",
    "dt",
    "json",
    "os",
    "pathlib",
    "re",
    "time",
    "uuid",
]
