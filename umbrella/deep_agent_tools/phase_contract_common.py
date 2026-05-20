"""Shared imports and constants for Umbrella phase-contract tools."""

import ast
import importlib
import json
import os
import pathlib
import platform
import re
import sys
import time
import uuid
from typing import Any, Iterator
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools import umbrella_tools
from umbrella.deep_agent_tools.phase_control_tools import (
    _llm_cached_decision_handoff_issue,
    _llm_fallback_handoff_issue,
    _llm_test_double_handoff_issue,
    _negative_claim_contradiction_issue,
    _tool_log_rows_for_task,
    _unread_existing_workspace_path_issue,
)
_UNRESOLVED_PASS_BLOCKER_RE = re.compile(
    r"(?i)("
    r"not\s+blocking\s+verification|"
    r"outside\s+(?:the\s+)?scope|"
    r"requires?\s+fix(?:ing|es)?|"
    r"still\s+(?:broken|failing|fails)|"
    r"runtime\s+.*errors?\s+detected|"
    r"not\s+fully\s+playable|"
    r"not\s+playable|"
    r"cannot\s+be\s+used"
    r")"
)
_ROOT_PLAN_NOISE_RE = re.compile(
    r"(?i)^(?:"
    r"test_.*\.py|.*_test\.py|"
    r"check_.*\.py|verify_.*\.py|validate_.*\.py|"
    r"inspect_.*\.py|analyze_.*\.py|debug_.*\.py|"
    r"find_.*\.py|scan_.*\.py|real_test_.*\.py|"
    r"test_minimal_.*\.py"
    r")$"
)
_PLAN_STUB_INTENT_RE = re.compile(
    r"(?i)\b("
    r"implement\s+or\s+stub|"
    r"stub\s+(?:the\s+)?missing|"
    r"stub\s+(?:the\s+)?function|"
    r"placeholder\s+implementation|"
    r"mock\s+implementation|"
    r"(?:fallback|fall[-\s]+back)\s+to\s+(?:a\s+)?(?:deterministic\s+)?heuristics?"
    r")\b"
)
_PLAN_LLM_FALLBACK_RE = re.compile(
    r"(?is)("
    r"\b(?:llm|gmas|agent|bot|model)\b.{0,240}\b(?:fallback|fall[-\s]+back)\b"
    r".{0,160}\b(?:heuristics?|deterministic|static|hardcoded|mock|random|"
    r"rule[-\s]?based|default|valid\s+action|cached\s+decisions?|"
    r"cached\s+actions?|graceful\s+degradation|conservative\s+strategy)\b|"
    r"\b(?:llm|gmas|agent|bot|model)\b.{0,240}\b(?:heuristics?|"
    r"deterministic|static|hardcoded|mock|random|rule[-\s]?based|"
    r"cached\s+decisions?|cached\s+actions?|graceful\s+degradation|"
    r"conservative\s+strategy)\s+"
    r"(?:(?:ai|bot|agent|model|llm|gmas)\s+)?"
    r"(?:fallback|replacement|decision|action)\b|"
    r"\b(?:fallback|fall[-\s]+back)\b.{0,160}\b(?:heuristics?|deterministic|"
    r"static|hardcoded|mock|random|rule[-\s]?based|default|valid\s+action|"
    r"cached\s+decisions?|cached\s+actions?|graceful\s+degradation|"
    r"conservative\s+strategy)"
    r"\b.{0,240}\b(?:llm|gmas|agent|bot|model)\b"
        r")"
)
_PLAN_LLM_CONTEXT_RE = re.compile(r"(?i)\b(llm|gmas|agent|bot|model)\b")
_PLAN_LLM_CACHED_DECISION_RE = re.compile(
    r"(?is)\b(?:decision|action|response)\s+caching\b|"
    r"\bcach(?:e|ed|ing)\s+(?:llm\s+|gmas\s+|ai\s+|bot\s+|agent\s+)?"
    r"(?:common\s+)?[^.;\n]{0,80}"
    r"(?:decisions?|actions?|responses?|outputs?|reasoning)\b|"
    r"\breuse\s+cached\s+(?:decisions?|actions?|responses?|outputs?|reasoning)\b"
)
_PLAN_GENERIC_FALLBACK_RE = re.compile(
    r"(?i)\b(?:fallback|fall[-\s]+back)\b(?:\s+(?:logic|handling|policy|"
    r"path|mode|strategy|rules?|behavior))?\b"
)
_PLAN_BAD_FALLBACK_REPLACEMENT_RE = re.compile(
    r"(?i)\b(heuristics?|deterministic|static|hardcoded|mock|random|"
    r"rule[-\s]?based|default|valid\s+action|cached\s+decisions?|"
    r"cached\s+actions?|graceful\s+degradation|conservative\s+strategy)\b"
)
_PLAN_ENV_ALIAS_FALLBACK_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:fallback|fall[-\s]+back)\b.{0,160}"
    r"(?:(?:llm_(?:api_key|base_url|model|\*)|"
    r"ouroboros_llm_(?:api_key|base_url|\*)|ouroboros_model)|\baliases?\b|"
    r"\bruntime\s+aliases?\b|\benv(?:ironment)?(?:\s+vars?)?\b)"
    r"|"
    r"(?:(?:llm_(?:api_key|base_url|model|\*)|"
    r"ouroboros_llm_(?:api_key|base_url|\*)|ouroboros_model)|\baliases?\b|"
    r"\bruntime\s+aliases?\b|\benv(?:ironment)?(?:\s+vars?)?\b)"
    r".{0,160}\b(?:fallback|fall[-\s]+back)\b"
    r")"
)
_PLAN_TOOL_DECLARATION_KEYS = {
    "allowed_tools",
    "required_tools",
    "tools",
    "tools_required",
    "phase_tools",
    "tool_names",
}
_PLAN_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLAN_WORK_ITEM_PATH_RE = re.compile(r"(?:^|\.)(?:subtasks|steps|phases)\[\d+\]$")
_PLAN_PATH_RE = re.compile(
    r"(?i)(?:^|[\s`'\"(])("
    r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+"
    r")(?:$|[\s`'\"),.;:])"
)
_PLAN_CODE_EXTENSIONS = {
    ".py",
    ".pyw",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".php",
    ".rb",
    ".swift",
}
_PLAN_NON_IMPL_ROOTS = {
    ".github",
    ".memory",
    ".umbrella",
    ".venv",
    "__pycache__",
    "assets",
    "build",
    "dist",
    "doc",
    "docs",
    "e2e",
    "node_modules",
    "public",
    "reports",
    "scripts",
    "test",
    "tests",
    "tmp",
    "venv",
}
_PLAN_MIGRATION_WORD_RE = re.compile(
    r"(?i)\b("
    r"migrate|migration|move|rename|refactor|replace|consolidate|"
    r"delete|remove|deprecate|obsolete|cleanup|clean up|"
    r"перенести|мигрир|рефактор|заменить|удалить|убрать"
    r")\b"
)
_PLAN_REBUILD_EXISTING_RE = re.compile(
    r"(?i)("
    r"\b(?:setup|set\s+up|initialize|scaffold|create)\s+"
    r"(?:the\s+)?(?:project\s+structure|full[-\s]?stack\s+project|"
    r"backend|frontend|application|app)\b|"
    r"\b(?:backend|frontend|application|app|project)\s+(?:project\s+)?setup\b|"
    r"\b(?:from\s+scratch|greenfield)\b|"
    r"\b(?:directories?|folders?)\s+created\b|"
    r"\b(?:pyproject\.toml|package\.json)\s+has\s+.*dependencies\b"
    r")"
)
_PLAN_EXISTING_REPAIR_WORD_RE = re.compile(
    r"(?i)\b("
    r"fix|repair|debug|diagnos|verify|wire|integrat|reuse|extend|update|"
    r"correct|adapt|align|refactor|migrate|cleanup|remove|delete|"
    r"почин|исправ|диагност|провер|интегр|переиспольз|обнов|рефактор|удал"
    r")"
)
_PLAN_CHILD_KEYS = frozenset({"subtasks", "ordered_subtasks", "steps", "phases"})
_PLAN_SUCCESS_TEST_KEYS = frozenset(
    {
        "success_test",
        "success_check",
        "success_checks",
        "acceptance_command",
        "verification_command",
        "verification_commands",
        "verification",
        "test_strategy",
        "test",
    }
)
_GENERIC_SUCCESS_TEST_TOOLS = frozenset(
    {
        "run_workspace_verify",
        "run_unit_tests",
        "harness_run",
        "run_real_e2e",
        "http_boot",
        "behavioral_http",
    }
)
_GENERIC_SUCCESS_TOOL_WITH_ARGS_RE = re.compile(
    r"(?i)^\s*(?:run_workspace_verify|run_unit_tests|harness_run|run_real_e2e)"
    r"(?:\s+\S|[:(])"
)
_SUCCESS_TEST_PROSE_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:run|verify|check|assert|command)\s*:"
)
_DESCRIPTIVE_SUCCESS_TEST_RE = re.compile(
    r"(?i)("
    r"\s+-\s+(?:must|should|verify|validate|checks?|assert|contains?|"
    r"exit\s+code|return\s+code|expected\s+exit)\b|"
    r"\bwith\s+(?:schema|http|behavioral)\s+verification\b|"
    r"\bmust\s+(?:instantiate|test|validate|run|start|assert|launch|create|verify)\b"
    r")"
)
_DESCRIPTIVE_SUCCESS_OUTCOME_RE = re.compile(
    r"(?ix)("
    r"\b(?:pytest|python\s+-m\s+pytest|npm|npx|vitest|playwright|curl)\b"
    r"[^;\n]{0,240}\b(?:succeeds?|passes?|fails?|skips?)\b|"
    r";\s*(?:without|with|if|when)\b[^;\n]{0,240}"
    r"\b(?:succeeds?|passes?|fails?|skips?)\b|"
    r"\b(?:succeeds?|passes?|fails?|skips?)\b\s*(?:;|,|\band\b)"
    r")"
)
_DESCRIPTIVE_SUCCESS_PAREN_RE = re.compile(
    r"(?ix)"
    r"\b(?:pytest|python\s+-m\s+pytest|npm|npx|vitest|playwright|curl)\b"
    r"[^\n]{0,260}\([^)]*\b(?:testclient|with|uses?|if|when|missing|"
    r"skips?|fails?|succeeds?|passes?|manual|websocket)\b[^)]*\)\s*$"
)
_FILE_EXISTENCE_ONLY_SUCCESS_TEST_RE = re.compile(
    r"(?ix)("
    r"os\.path\.exists|"
    r"(?:pathlib\.)?Path\s*\([^)]*\)\.exists|"
    r"\.(?:exists|is_file|is_dir)\s*\(|"
    r"fs\.existsSync|"
    r"Test-Path|"
    r"\[\s+-[efs]\b"
    r")"
)
_BEHAVIORAL_SUCCESS_TEST_RE = re.compile(
    r"(?ix)\b("
    r"pytest|python\s+-m\s+pytest|npm\s+(?:run\s+)?(?:test|build)|"
    r"pnpm|yarn|npx\s+vitest|vitest\s+run|curl\s+-f|"
    r"run_workspace_verify|run_unit_tests|harness_run|http_boot|"
    r"behavioral_http|playwright"
    r")\b"
)
_DESCRIPTIVE_BROWSER_SUCCESS_TEST_RE = re.compile(
    r"(?ix)("
    r"\b(?:browser|page)\s+(?:opens?|loads?|shows?|displays?|navigates?)\b|"
    r"\b(?:open|load|visit|navigate)\s+(?:the\s+)?(?:browser|page|app|ui)\b|"
    r"\bhuman\s+player\b|"
    r"\bnetwork\s+inspector\b|"
    r"\bconsole\s+(?:has|shows|contains|reports)\s+(?:zero|no)\s+errors?\b|"
    r"\bwebsocket\s+messages?\s+(?:show|appear|visible)\b|"
    r"\bserver\s+starts?\s+cleanly\b"
    r")"
)
_CONCRETE_BROWSER_AUTOMATION_RE = re.compile(
    r"(?ix)\b("
    r"playwright|selenium|pytest|python\s+-m\s+pytest|npx\s+playwright|"
    r"npm\s+(?:run\s+)?(?:test|build)|pnpm|yarn|node|run_real_e2e|"
    r"run_workspace_verify|harness_run|http_boot|behavioral_http|curl\s+-f"
    r")\b"
)
_LLM_MOCK_SUCCESS_TEST_RE = re.compile(
    r"(?ix)("
    r"--mock\b|"
    r"--mock[-_]?llm\b|"
    r"--use[-_]?llm[-_]?mock[-_]?env\b|"
    r"--use[-_]?mock[-_]?llm[-_]?env\b|"
    r"--dry[-_]?run\b|"
    r"\bmock[-_\s]?llm\b|"
    r"\bmocked\s+llm\b|"
    r"\bfake[-_\s]?llm\b|"
    r"\bllm[-_\s]?mock[-_\s]?env\b|"
    r"\bmock[-_\s]?llm[-_\s]?env\b|"
    r"\bdry[-_\s]?run[-_\s]?llm\b"
    r")"
)
_LLM_WORK_ITEM_CONTEXT_RE = re.compile(r"(?i)\b(llm|gmas|agent|bot|model)\b")
_MOCKED_PROOF_WORK_ITEM_CONTEXT_RE = re.compile(
    r"(?i)\b("
    r"llm|gmas|agent|bot|model|"
    r"end[-\s]?to[-\s]?end|e2e|integration|acceptance|"
    r"live[-\s]?runtime|real[-\s]?runtime|real[-\s]?llm"
    r")\b"
)
_LLM_ERROR_AS_SUCCESS_RE = re.compile(
    r"(?is)\bassert\b.{0,300}(?:\bor\b|\|\|).{0,300}"
    r"\b(?:error_llm|llm_error|error|exception|failed)\b|"
    r"\b(?:error_llm|llm_error|error|exception|failed)\b.{0,300}"
    r"(?:\bor\b|\|\|).{0,300}\bassert\b"
)
_LLM_ERROR_PROTECTIVE_RE = re.compile(
    r"(?is)\b(?:not|never|without|forbid(?:s|den)?|reject(?:s|ed)?|"
    r"fail(?:s|ed)?\s+if)\b.{0,80}"
    r"\b(?:error_llm|llm_error|error|exception|failed)\b|"
    r"\b(?:error_llm|llm_error|error|exception|failed)\b.{0,80}"
    r"\b(?:not|forbidden|rejected|disallowed|absent)\b"
)
_SUCCESS_TEST_AUTOMATION_RE = re.compile(
    r"(?ix)"
    r"("
    r"\b(run_workspace_verify|run_unit_tests|harness_run|run_real_e2e|"
    r"run_workspace_command|shell)\b|"
    r"\b(http_boot|behavioral_http|playwright|selenium)\b|"
    r"\b(pytest|python|npm|pnpm|yarn|node|npx|uv|ruff|mypy|tsc|vite|curl|"
    r"powershell|pwsh|bash|sh|go|cargo|dotnet|mvn|gradle|java|make|cmake|"
    r"docker(?:\s+compose)?)\b|"
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+/[^\s]+|"
    r"https?://"
    r")"
)
_SUCCESS_TEST_VAGUE_RE = re.compile(
    r"(?ix)"
    r"("
    r"\b(document(?:ation|ed)?|memory\s+artifact|artifact\s+with|"
    r"notes?|analysis|clear\s+understanding|understand(?:ing)?|"
    r"evidence\s+recorded|summary|checklist)\b|"
    r"\b(all\s+tests\s+pass|tests\s+pass|no\s+errors?|works|complete|done)\b"
    r")"
)
_SUCCESS_TEST_WORKSPACE_CD_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*)cd\s+[\"']?(?:\.?[\\/])?workspaces[\\/][^;&|\"'\s]+"
)
_SUCCESS_TEST_FAILURE_MASK_RE = re.compile(
    r"(?ix)("
    r"\|\||"
    r"(?:^|[;&|]\s*)true\s*$"
    r")"
)
_SUCCESS_TEST_ALIAS_KEYS = (
    "success_check",
    "success_checks",
    "acceptance_command",
    "verification_command",
    "verification_commands",
    "verification",
    "test_strategy",
    "test",
)
_PYTHON_INLINE_ALLOWED_IMPORT_ROOTS = frozenset(
    getattr(sys, "stdlib_module_names", frozenset())
) | {"__future__"}
_DIRECT_PYTHON_TEST_MODULE_RE = re.compile(
    r"(?ix)"
    r"(?<![\w.-])"
    r"(?:python|py)(?:\.exe)?\s+"
    r"(?!-m\s+pytest\b)"
    r"(?:-[A-Za-z]\s+)*"
    r"(?P<target>[^\s;&|]*?(?:^|[\\/])?(?:test_[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+_test)\.py)"
    r"(?=$|\s|::|[;&|])"
)
_NON_PORTABLE_SHELL_RE = re.compile(
    r"(?ix)("
    r"(?:^|[;&|]\s*)[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]+"
        r"(?:\s+[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]+)*\s+[\w./\\-]+|"
    r"\b(?:bash|sh)\s+-c\b|"
    r"\b(?:bash|sh)\s+[^\s;&|]+\.sh\b|"
    r"(?:^|[\s;&|])(?:\.{0,2}[\\/])?[\w.-]+(?:[\\/][\w.-]+)*\.sh(?=$|\s|[;&|])|"
    r"(?:^|\s)\d?>\s*[^\s;&|]+|"
    r"/dev/null\b|"
    r"(?:^|[;&|]\s*)test\s+-[efs]\b|"
    r"\[\s+-[efs]\b|"
    r"\bps\s+aux\b|"
        r"\bpkill\b|"
        r"\breadlink\b|"
        r"\bexit\s+(?:\$\?|\d+\b)|"
        r"(?:^|[;&|]\s*)if\s+\[|"
        r";\s*(?:then|else|fi)\b|"
        r"\bStart-Job\b|"
        r"\bgrep\b|"
        r"\bsed\b|"
        r"\bawk\b|"
    r"\bhead\s+-\d+\b|"
    r"\btail\s+-\d+\b|"
    r"\|\s*grep\b"
    r")"
)
_JS_EMPTY_TEST_BYPASS_RE = re.compile(
    r"(?i)(?:^|\s)--(?:passWithNoTests|allowEmpty|allowNoTests)\b"
)
_PYTEST_COLLECT_ONLY_SUCCESS_TEST_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*)(?:python\s+-m\s+)?pytest\b[^;&|]*--collect-only\b"
)
_PYTEST_CD_SRC_SUCCESS_TEST_RE = re.compile(
    r"(?ix)"
    r"(?:^|[;&|]\s*)"
    r"cd\s+[\"']?\.?[\\/]?src[\"']?\s*(?:&&|;)\s*"
    r"(?:(?:python|py)(?:\.exe)?\s+-m\s+)?pytest\b"
)
_PYTEST_SRC_TESTLIKE_SUCCESS_TEST_RE = re.compile(
    r"(?ix)"
    r"(?:^|[;&|]\s*)"
    r"(?:python\s+-m\s+)?pytest\b"
    r"[^;&|\n]*"
    r"\bsrc[\\/][^;&|\n\s]*?"
    r"(?:"
    r"[\\/](?:tests?|verify)[\\/][^;&|\n\s]+\.py|"
    r"[\\/](?:test_[^;&|\n\s\\/]+|[^;&|\n\s\\/]+_test|"
    r"verify_[^;&|\n\s\\/]+|[^;&|\n\s\\/]+_verify)\.py"
    r")"
)
_PLAN_LLM_TEST_DOUBLE_RE = re.compile(
    r"(?is)("
    r"\b(?:mock|fake|dry[-\s]?run|test\s+double)\b.{0,140}\b(?:llm|gmas|bot|agent|model)\b|"
    r"\b(?:llm|gmas|bot|agent|model)\b.{0,140}\b(?:mock|fake|dry[-\s]?run|test\s+double)\b"
    r")"
)
_LLM_ENV_CONTEXT_RE = re.compile(r"(?i)\b(llm|gmas|agent|bot|model)\b")
_LLM_ENV_CONTRACT_REQUIRED_RE = re.compile(
    r"(?is)\b("
    r"(?:real|live)\s+llm|llm[-\s]?(?:powered|driven|backed|based)|"
    r"llm\s+(?:ai\s+design|agents?|bots?|client|calls?|decisions?|"
    r"decision[-\s]?making|game\s+ai|integration|reasoning|strategy)|"
    r"inherited\s+real\s+runtime\s+env"
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
    r"missing|validate|check)\b.{0,120}\bOPENAI_API_KEY\b|"
    r"\bOPENAI_API_KEY\b.{0,120}\b(?:must|required|needed|expected|set|"
    r"configured|missing|validat(?:e|ion)|check)\b"
)
_WEB_SEARCH_ONLY_CONTEXT_RE = re.compile(
    r"(?i)\b(web[_ -]?search|public web search|search provider)\b"
)
_LLM_PROVIDER_DEFAULT_PLAN_RE = re.compile(
    r"(?i)\b(?:openai/)?gpt-[a-z0-9_.:-]+\b|https://api\.openai\.com"
)
_EMPTY_TEST_SKELETON_RE = re.compile(
    r"(?is)("
    r"\b(?:empty|blank|import[-\s]?only|basic\s+imports?)\b.{0,120}"
    r"\b(?:test|tests|pytest|skeleton|shell|file|files)\b|"
    r"\b(?:test|tests|pytest|skeleton|shell|file|files)\b.{0,120}"
    r"\b(?:empty|blank|import[-\s]?only|basic\s+imports?)\b"
    r")"
)
_EMPTY_TEST_PROTECTIVE_RE = re.compile(
    r"(?is)\b(?:no|not|never|without|avoid|reject(?:s|ed)?|"
    r"forbid(?:s|den)?|disallow(?:s|ed)?|do\s+not|must\s+not|"
    r"should\s+not|cannot|can't)\b"
)
_EMPTY_TEST_BEHAVIORAL_PROOF_RE = re.compile(
    r"(?is)\b(?:executable\s+assertions?|assertions?|fixtures?|"
    r"can\s+fail|fail\s+for\s+real\s+behavior|real\s+behavior|"
    r"behavioral\s+(?:proof|evidence|test)|non[-\s]?empty)\b"
)
_EMPTY_TEST_DIRECT_PROTECTIVE_RE = re.compile(
    r"(?is)\b(?:no|not|never|without|avoid|reject(?:s|ed)?|"
    r"forbid(?:s|den)?|disallow(?:s|ed)?|do\s+not|must\s+not|"
    r"should\s+not|cannot|can't)\b"
    r"(?:\s+\w+){0,4}\s+"
    r"(?:empty|blank|import[-\s]?only|basic\s+imports?|"
    r"test|tests|pytest|skeleton|shell|file|files)\b"
)
_REVISION_SUBTASK_RE = re.compile(
    r"\b(?:subtasks?|st|task)(?:[_-]|\s+)?0*\d+(?:\.\d+)?[a-z0-9_]*"
    r"(?:\s*-\s*0*\d+(?:\.\d+)?[a-z0-9_]*)?\b",
    re.IGNORECASE,
)
_REVISION_PHASE_REF_RE = re.compile(
    r"(?is)\bphase\s+0*\d+(?:\.\d+)?"
    r"(?:\s*\([^)]{0,120}\))?\s*[:\-)]?"
)
_REVISION_RENAME_RE = re.compile(
    r"(?is)\brename\b\s+[`\"']?([a-z0-9_.-]{3,})[`\"']?\s+"
    r"\bto\b\s+[`\"']?([a-z0-9_.-]{3,})[`\"']?"
)
_REVISION_QUOTED_RE = re.compile(r"`[^`]{1,240}`|\"[^\"]{1,240}\"|'[^']{1,240}'")
_REVISION_STOP_WORDS = frozenset(
    {
        "a",
        "add",
        "added",
        "adds",
        "an",
        "and",
        "are",
        "as",
        "be",
        "by",
        "for",
        "file",
        "files",
        "fix",
        "from",
        "has",
        "have",
        "in",
        "include",
        "including",
        "command",
        "commands",
        "concrete",
        "convert",
        "create",
        "created",
        "creation",
        "equivalent",
        "equivalently",
        "explain",
        "explaining",
        "provide",
        "proper",
        "instead",
        "into",
        "item",
        "level",
        "move",
        "moved",
        "name",
        "names",
        "new",
        "must",
        "not",
        "of",
        "or",
        "phase",
        "plan",
        "replace",
        "review",
        "section",
        "should",
        "specify",
        "statements",
        "strategy",
        "success",
        "subtask",
        "task",
        "test",
        "tests",
        "test_strategy",
        "that",
        "the",
        "this",
        "to",
        "unit_tests",
        "update",
        "use",
        "using",
        "verify",
        "variable",
        "variables",
        "with",
        "без",
        "для",
        "добавить",
        "если",
        "задача",
        "как",
        "или",
        "нужно",
        "обновить",
        "подзадача",
        "это",
        "чтобы",
    }
)
_REVISION_ILLUSTRATIVE_NUMBER_EXAMPLES_RE = re.compile(
    r"(?is)\b(?:like|such\s+as|for\s+example|e\.g\.)\s+"
    r"(?:subtasks?\s+)?"
    r"(?:[`\"']?(?:subtasks?|st|task)?(?:[_-]|\s+)?0*\d+(?:\.\d+)?[a-z0-9_]*[`\"']?"
    r"\s*(?:,|\band\b|\bor\b)?\s*)+"
)
_REVISION_BUDGET_AMOUNT_RE = re.compile(
    r"(?is)(?:[$€£]\s*\d+(?:\.\d+)?(?:\s*(?:usd|eur|gbp|dollars?))?|"
    r"\b\d+(?:\.\d+)?\s*(?:usd|eur|gbp|dollars?|budget)\b)"
)
_PLAN_READ_FILE_KEYS = frozenset(
    {"file_to_read", "files_to_read", "read_file", "read_files", "files_to_inspect"}
)
_PLAN_CHANGE_FILE_KEYS = frozenset(
    {
        "file_to_change",
        "files_to_change",
        "files_to_modify",
        "files_affected",
        "target_file",
        "target_files",
    }
)
_PLAN_CREATE_FILE_KEYS = frozenset(
    {"file_to_create", "files_to_create", "new_file", "new_files", "files_to_add"}
)
_PLAN_FILE_FIELD_KEYS = (
    _PLAN_READ_FILE_KEYS | _PLAN_CHANGE_FILE_KEYS | _PLAN_CREATE_FILE_KEYS
)
_PLAN_GREENFIELD_ALLOWED_ROOT_PY = {
    "asgi.py",
    "conftest.py",
    "manage.py",
    "setup.py",
    "wsgi.py",
}
_LOCALHOST_E2E_RE = re.compile(
    r"(?i)("
    r"\blocalhost\b|127\.0\.0\.1|web\s*ui|browser|playwright|selenium|"
    r"\bfrontend\b|\bvite\b|\breact\b|"
    r"брауз|интерфейс|кнопк|локалхост|подними|запусти"
    r")"
)
_LOCALHOST_PROOF_RE = re.compile(
    r"(?i)(localhost|127\.0\.0\.1|http://|https://).*(playwright|selenium|browser|curl|requests|httpx|fetch)|"
    r"(playwright|selenium|browser|curl|requests|httpx|fetch).*(localhost|127\.0\.0\.1|http://|https://)"
)


__all__ = [
    'Any',
    'Iterator',
    'ToolContext',
    'ToolEntry',
    '_BEHAVIORAL_SUCCESS_TEST_RE',
    '_CONCRETE_BROWSER_AUTOMATION_RE',
    '_DESCRIPTIVE_BROWSER_SUCCESS_TEST_RE',
    '_DESCRIPTIVE_SUCCESS_OUTCOME_RE',
    '_DESCRIPTIVE_SUCCESS_PAREN_RE',
    '_DESCRIPTIVE_SUCCESS_TEST_RE',
    '_DIRECT_PYTHON_TEST_MODULE_RE',
    '_EMPTY_TEST_BEHAVIORAL_PROOF_RE',
    '_EMPTY_TEST_DIRECT_PROTECTIVE_RE',
    '_EMPTY_TEST_PROTECTIVE_RE',
    '_EMPTY_TEST_SKELETON_RE',
    '_FILE_EXISTENCE_ONLY_SUCCESS_TEST_RE',
    '_GENERIC_SUCCESS_TEST_TOOLS',
    '_GENERIC_SUCCESS_TOOL_WITH_ARGS_RE',
    '_JS_EMPTY_TEST_BYPASS_RE',
    '_LLM_ENV_ALIAS_RE',
    '_LLM_ENV_CONTEXT_RE',
    '_LLM_ENV_CONTRACT_REQUIRED_RE',
    '_LLM_ENV_OMISSION_REQUIRED_RE',
    '_LLM_ERROR_AS_SUCCESS_RE',
    '_LLM_ERROR_PROTECTIVE_RE',
    '_LLM_LEGACY_ENV_ALIASES',
    '_LLM_LEGACY_ENV_RE',
    '_LLM_MOCK_SUCCESS_TEST_RE',
    '_LLM_OUROBOROS_ENV_ALIASES',
    '_LLM_PROVIDER_DEFAULT_PLAN_RE',
    '_LLM_WORK_ITEM_CONTEXT_RE',
    '_LOCALHOST_E2E_RE',
    '_LOCALHOST_PROOF_RE',
    '_MOCKED_PROOF_WORK_ITEM_CONTEXT_RE',
    '_NON_PORTABLE_SHELL_RE',
    '_OPENAI_KEY_RE',
    '_OPENAI_REQUIRED_RE',
    '_PLAN_BAD_FALLBACK_REPLACEMENT_RE',
    '_PLAN_CHANGE_FILE_KEYS',
    '_PLAN_CHILD_KEYS',
    '_PLAN_CODE_EXTENSIONS',
    '_PLAN_CREATE_FILE_KEYS',
    '_PLAN_ENV_ALIAS_FALLBACK_RE',
    '_PLAN_EXISTING_REPAIR_WORD_RE',
    '_PLAN_FILE_FIELD_KEYS',
    '_PLAN_GENERIC_FALLBACK_RE',
    '_PLAN_GREENFIELD_ALLOWED_ROOT_PY',
    '_PLAN_LLM_CACHED_DECISION_RE',
    '_PLAN_LLM_CONTEXT_RE',
    '_PLAN_LLM_FALLBACK_RE',
    '_PLAN_LLM_TEST_DOUBLE_RE',
    '_PLAN_MIGRATION_WORD_RE',
    '_PLAN_NON_IMPL_ROOTS',
    '_PLAN_PATH_RE',
    '_PLAN_READ_FILE_KEYS',
    '_PLAN_REBUILD_EXISTING_RE',
    '_PLAN_STUB_INTENT_RE',
    '_PLAN_SUCCESS_TEST_KEYS',
    '_PLAN_TOOL_DECLARATION_KEYS',
    '_PLAN_TOOL_NAME_RE',
    '_PLAN_WORK_ITEM_PATH_RE',
    '_PYTEST_CD_SRC_SUCCESS_TEST_RE',
    '_PYTEST_COLLECT_ONLY_SUCCESS_TEST_RE',
    '_PYTEST_SRC_TESTLIKE_SUCCESS_TEST_RE',
    '_PYTHON_INLINE_ALLOWED_IMPORT_ROOTS',
    '_REVISION_BUDGET_AMOUNT_RE',
    '_REVISION_ILLUSTRATIVE_NUMBER_EXAMPLES_RE',
    '_REVISION_PHASE_REF_RE',
    '_REVISION_QUOTED_RE',
    '_REVISION_RENAME_RE',
    '_REVISION_STOP_WORDS',
    '_REVISION_SUBTASK_RE',
    '_ROOT_PLAN_NOISE_RE',
    '_SUCCESS_TEST_ALIAS_KEYS',
    '_SUCCESS_TEST_AUTOMATION_RE',
    '_SUCCESS_TEST_FAILURE_MASK_RE',
    '_SUCCESS_TEST_PROSE_PREFIX_RE',
    '_SUCCESS_TEST_VAGUE_RE',
    '_SUCCESS_TEST_WORKSPACE_CD_RE',
    '_UNRESOLVED_PASS_BLOCKER_RE',
    '_UNSUPPORTED_OUROBOROS_MODEL_ALIAS_RE',
    '_WEB_SEARCH_ONLY_CONTEXT_RE',
    '_llm_fallback_handoff_issue',
    '_llm_cached_decision_handoff_issue',
    '_llm_test_double_handoff_issue',
    '_negative_claim_contradiction_issue',
    '_tool_log_rows_for_task',
    '_unread_existing_workspace_path_issue',
    'ast',
    'importlib',
    'json',
    'os',
    'pathlib',
    'platform',
    're',
    'sys',
    'time',
    'umbrella_tools',
    'uuid',
]
