"""Symbol/import claim patterns for research contradiction checks (opt-in hot path)."""

import re

_NON_BLOCKING_RESEARCH_REVISE_RE = re.compile(
    r"\b("
    r"minor\s+discrepanc(?:y|ies)|prior[-\s]?art|novelty|credit(?:ing)?|"
    r"repository\s+urls?|wording|phrasing|nuance|citation|cite|summary\s+text|"
    r"external\s+files?\s+cannot\s+be\s+read|workspace\s+is\s+intentionally\s+empty|"
    r"sufficient\s+for\s+planning|ready\s+for\s+planning"
    r")\b",
    re.IGNORECASE,
)
_BLOCKING_RESEARCH_REVISE_RE = re.compile(
    r"\b("
    r"no\s+viable\s+architecture|lacks?\s+(?:architecture|evidence|mcp|skill|tool)|"
    r"missing\s+mandatory|contradicts?\s+(?:the\s+)?workspace\s+charter|"
    r"unsupported\s+current\s+code|contradicted\s+by\s+current\s+file|"
    r"unread\s+current\s+workspace|fabricated|invented\s+finding|"
    r"not\s+accepted\s+by\s+palace_add|cannot\s+plan|planning\s+speculative|"
    r"critical\s+unknown|blocking\s+gap|policy[-\s]?violating|"
    r"unsafe\s+(?:hot\s+)?memory|unsafe\s+(?:research\s+)?finding|"
    r"forbidden\s+llm\s+fallback|mock/simulation\s+mode|"
    r"graceful\s+degradation"
    r")\b",
    re.IGNORECASE,
)
_IMPLEMENTATION_OWNED_RESEARCH_REVISE_RE = re.compile(
    r"\b("
    r"implementation\s+(?:details?|patterns?|algorithms?)|"
    r"exact\s+(?:schemas?|runtime\s+traces?|function\s+names?|imports?|"
    r"file\s+names?|test\s+commands?|contracts?)|"
    r"complete\s+code|full\s+code|code\s+snippets?|"
    r"pydantic\s+model(?:s|\s+definitions?)?|json\s+schema|"
    r"api\s+contracts?|endpoint\s+contracts?|message\s+protocol(?:s)?|"
    r"module\s+lists?|deployment\s+commands?|"
    r"turn(?:-based)?\s+game\s+loop|timeout\s+mechanics?|"
    r"deadline\s+mechanics?|state\s+serialization|"
    r"concurrent\s+(?:ai\s+)?scheduling|phase\s+transition\s+triggers?|"
    r"websocket\s+server\s+manages|pytest|python\s+-c|"
    r"http\s+requests?|localhost\s+servers?|import\s+checks?"
    r")\b",
    re.IGNORECASE,
)
_RESEARCH_SUMMARY_REL_PATH = ".memory/drive/state/research_summary_latest.json"
_NEGATIVE_SYMBOL_CLAIM_RE = re.compile(
    r"(?is)"
    r"(?:cannot\s+import\s+(?:name\s+)?[`'\"]?(?P<import_symbol>[A-Za-z_]\w*)[`'\"]?"
    r"(?:\s+from\s+[`'\"](?P<module>[A-Za-z_][\w.]+)[`'\"])?|"
    r"missing\s+import\b\s*:?\s*(?:(?:the|a|an)\s+)?[`'\"]?"
    r"(?P<missing_import_symbol>[A-Za-z_]\w*)[`'\"]?|"
    r"(?:add|fix|resolve|repair)?\s*missing\s+(?:(?:the|a|an)\s+)?[`'\"]?"
    r"(?P<missing_import_or_impl_symbol>[A-Za-z_]\w*)[`'\"]?"
    r"\s+import\s+or\s+implement\b|"
    r"(?:add|fix|resolve|repair)?\s*missing\s+(?:(?:the|a|an)\s+)?[`'\"]?"
    r"(?P<missing_import_after_symbol>[A-Za-z_]\w*)[`'\"]?"
    r"\s+import\b|"
    r"(?:implement|add|create|define)\s+missing\s+(?:(?:the|a|an)\s+)?[`'\"]?"
    r"(?P<implement_missing_symbol>[A-Za-z_]\w*)[`'\"]?|"
    r"(?:missing|lacks?|no)\s+(?:(?:the|a|an)\s+)?[`'\"]?(?P<missing_symbol>[A-Za-z_]\w*)[`'\"]?"
    r"\s+(?:export|function|method|class|definition)|"
    r"(?:doesn['’]?t|does\s+not)\s+export\s+[`'\"]?"
    r"(?P<not_exported_symbol>[A-Za-z_]\w*)[`'\"]?|"
    r"(?:fix|update|change)\s+[^;\n]{0,160}?\bto\s+export\s+[`'\"]?"
    r"(?P<fix_export_symbol>[A-Za-z_]\w*)[`'\"]?|"
    r"(?:fix|update|change|ensure|make)\s+[^;\n]{0,160}?\b[`'\"]?"
    r"(?P<ensure_export_symbol>[A-Za-z_]\w*)[`'\"]?"
    r"\s+(?:(?:is|are|be)\s+)?(?:properly\s+)?exported\b|"
    r"(?:fix|resolve|repair)\s+[^.\n;]{0,120}?[`'\"]?"
    r"(?P<fix_import_error_symbol>[A-Za-z_]\w*)[`'\"]?"
    r"\s+import\s+(?:error|failure|issue|bug)|"
    r"(?:fix|resolve|repair)\s+[^.\n;]{0,120}?\bimport\s+(?:(?:the|a|an)\s+)?[`'\"]?"
    r"(?P<fix_import_symbol_after>[A-Za-z_]\w*)[`'\"]?|"
    r"(?<![./])[`'\"]?(?:[A-Za-z_]\w*\.)*(?P<missing_subject_symbol>[A-Za-z_]\w*)[`'\"]?"
    r"(?:\s+(?:function|method|class|definition|symbol))?"
    r"\s+(?:is|are)\s+(?:missing|absent|unavailable)|"
    r"(?<![./])(?P<unimportable_symbol>[A-Za-z_]\w*)"
    r"(?:\s+(?:function|method|class|definition))?"
    r"[^.\n;]{0,120}?\bnot\s+available\s+for\s+import)"
)
_NEGATIVE_SYMBOL_IN_FILE_RE = re.compile(
    r"(?is)(?:missing|lacks?|no)\s+(?:(?:the|a|an)\s+)?[`'\"]?(?P<symbol>[A-Za-z_]\w*)[`'\"]?"
    r"\s+(?:in|from)\s+[`'\"]?(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py)[`'\"]?"
)
_NEGATIVE_PARAM_CLAIM_RE = re.compile(
    r"(?is)"
    r"(?P<target>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*\(\)\s+"
    r"[^.\n;]{0,80}?(?:missing|lacks?|needs|required)\s+"
    r"(?:\d+\s+)?(?:required\s+)?"
    r"(?:(?:positional\s+)?(?:argument|parameter)s?\s*:?\s*)?"
    r"(?:(?:the|a|an)\s+)?"
    r"[`'\"]?(?P<param>[A-Za-z_]\w*)[`'\"]?"
)
_WITHOUT_PARAM_CLAIM_RE = re.compile(
    r"(?s)"
    r"(?P<target>[A-Z]\w*(?:\.[A-Za-z_]\w*)?)"
    r"(?:\s+(?i:class|constructor|endpoint|call|calls|instance|instances|"
    r"object|objects))?"
    r"[^.\n;]{0,160}?\b(?i:without|not)\s+(?i:passing)\s+"
    r"(?:(?i:the|a|an)\s+)?"
    r"[`'\"]?(?P<param>[A-Za-z_]\w*)[`'\"]?"
)
_CONSTRUCTOR_PARAM_CLAIM_RE = re.compile(
    r"(?is)"
    r"(?P<target>[A-Z]\w*(?:\.[A-Za-z_]\w*)?)"
    r"[^.\n;]{0,160}?\b(?:constructor|initiali[sz]ation|init|integration)"
    r"[^.\n;]{0,160}?\b(?:missing|lacks?|requires?|expects?)\s+"
    r"(?:(?:the|a|an)\s+)?"
    r"[`'\"]?(?P<param>[A-Za-z_]\w*)[`'\"]?\s+"
    r"(?:parameter|argument)\b"
)
_DIRECT_PARAM_CLAIM_RE = re.compile(
    r"(?is)"
    r"\b(?P<target>[A-Z]\w*(?:\.[A-Za-z_]\w*)?)"
    r"(?:\s+(?:constructor|initiali[sz]ation|init|__init__|calls?|endpoint))?"
    r"[^.\n;]{0,120}?\b(?:missing|lacks?|requires?|expects?)\s+"
    r"(?:(?:the|a|an|required|positional)\s+)*"
    r"[`'\"]?(?P<param>[A-Za-z_]\w*)[`'\"]?\s+"
    r"(?:parameter|argument)\b"
)
_MISSING_PARAM_IN_TARGET_RE = re.compile(
    r"(?is)"
    r"\b(?:missing|lacks?|requires?|expects?)\s+"
    r"(?:(?:the|a|an)\s+)?"
    r"[`'\"]?(?P<param>[A-Za-z_]\w*)[`'\"]?\s+"
    r"(?:parameter|argument)\b"
    r"[^.\n;]{0,160}?\b(?:in|from|on|for|to)\s+"
    r"[`'\"]?(?P<target>[A-Z]\w*(?:\.[A-Za-z_]\w*)?)(?:\s*\(\))?[`'\"]?"
)
_INCLUDE_PARAM_IN_TARGET_RE = re.compile(
    r"(?is)"
    r"\b(?:fix|repair|update|change|ensure|make)\b"
    r"[^.\n;]{0,180}?\b(?P<target>[A-Z]\w*(?:\.[A-Za-z_]\w*)?)(?!-)"
    r"(?:\s+(?:calls?|call\s+sites?|constructor|initiali[sz]ation|init|"
    r"endpoint|path))?"
    r"[^.\n;]{0,180}?\b(?:include|add|pass|provide|supply)\s+"
    r"(?:(?:the|a|an)\s+)?[`'\"]?(?P<param>[A-Za-z_]\w*)[`'\"]?"
    r"\b(?!-)\s+(?:parameter|argument)\b"
)
_FIX_TARGET_WITH_PARAM_RE = re.compile(
    r"(?is)"
    r"\b(?:fix|repair|update|change|ensure|make)\b"
    r"[^.\n;]{0,160}?\b(?P<target>[A-Z]\w*(?:\.[A-Za-z_]\w*)?)"
    r"[^.\n;]{0,160}?\b(?:constructor|initiali[sz]ation|init|__init__|calls?)\b"
    r"[^.\n;]{0,120}?\bwith\s+(?:(?:the|a|an)\s+)?[`'\"]?"
    r"(?P<param>[A-Za-z_]\w*)[`'\"]?\s+(?:parameter|argument)\b"
)
_HANDLE_PARAM_IN_TARGET_RE = re.compile(
    r"(?is)"
    r"\b(?:fix|repair|update|change|ensure|make)\b"
    r"[^.\n;]{0,180}?\b(?P<target>[A-Z]\w*(?:\.[A-Za-z_]\w*)?)"
    r"[^.\n;]{0,180}?\b(?:handles?|accepts?|supports?)\s+"
    r"(?:(?:the|a|an)\s+)?(?:optional\s+)?[`'\"]?"
    r"(?P<param>[A-Za-z_]\w*)[`'\"]?\s+(?:parameter|argument)\b"
)
_PASS_DEPENDENCY_TO_TARGET_RE = re.compile(
    r"(?is)"
    r"\b(?:fix|repair|update|change|ensure|make)\b"
    r"[^.\n;]{0,220}?\bpass(?:ing)?\s+[`'\"]?"
    r"(?:(?:the|a|an)\s+)?"
    r"(?P<dependency>[A-Z]\w*)[`'\"]?\s+(?:to|into|for)\s+[`'\"]?"
    r"(?P<target>[A-Z]\w*(?:\.[A-Za-z_]\w*)?)(?:\s+constructor)?"
)
_SYMBOL_EXPECTATION_MISMATCH_RE = re.compile(
    r"(?is)"
    r"\bexpects?\s+[`'\"]?(?P<symbol>[A-Za-z_]\w*)[`'\"]?\s+"
    r"(?:export|function|method|class|definition)"
    r"[^.\n;]{0,180}?\bbut\b[^\n;]{0,120}?"
    r"(?:contains|has|defines)\b"
)
_NEGATIVE_FILE_EXISTENCE_CLAIM_RE = re.compile(
    r"(?is)"
    r"(?:"
    r"(?P<path1>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:pyi|tsx|jsx|mjs|cjs|yaml|scss|py|js|ts|json|toml|yml|md|html|css|txt))"
    r"[^.\n;]{0,140}?\b(?:doesn['’]?t|does\s+not|didn['’]?t|did\s+not)\s+exist|"
    r"\b(?:no|missing)\s+[`'\"]?"
    r"(?P<path2>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:pyi|tsx|jsx|mjs|cjs|yaml|scss|py|js|ts|json|toml|yml|md|html|css|txt))"
    r"[`'\"]?\s+file\s+exists?|"
    r"\b(?:no|missing)\s+file\s+[`'\"]?"
    r"(?P<path3>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:pyi|tsx|jsx|mjs|cjs|yaml|scss|py|js|ts|json|toml|yml|md|html|css|txt))"
    r"[`'\"]?"
    r")"
)
_POSITIVE_CLASS_CLAIM_RE = re.compile(
    r"(?is)"
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py)"
    r"[^.\n;]{0,220}?\b(?:contains|has|defines|provides|exports)\b"
    r"[^.\n;]{0,220}?\b(?P<symbol>[A-Z][A-Za-z_]\w*)\s+class\b"
)
_IGNORED_PARAM_CLAIM_WORDS = {
    "actual",
    "argument",
    "arguments",
    "live",
    "parameter",
    "parameters",
    "positional",
    "real",
    "required",
    "runtime",
    "error",
    "errors",
    "issue",
    "failure",
}
_STALE_CLAIM_CONTEXT_RE = re.compile(
    r"(?i)\b("
    r"stale|old|earlier|previous|cached|no\s+longer\s+applicable|"
    r"not\s+applicable|not\s+required|already\s+(?:shows|has|defines)"
    r")\b"
)
_STALE_CLAIM_NEGATION_RE = re.compile(
    r"(?i)\b("
    r"not\s+(?:due\s+to\s+|from\s+|a\s+)?stale|"
    r"is\s+not\s+(?:a\s+)?stale|isn['’]?t\s+(?:a\s+)?stale|"
    r"not\s+old|not\s+previous"
    r")\b"
)
_SOURCE_FILE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
}
_SOURCE_SCAN_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".memory",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "venv",
    ".venv",
    "dist",
    "build",
    "external",
}

__all__ = [
    "_NON_BLOCKING_RESEARCH_REVISE_RE",
    "_BLOCKING_RESEARCH_REVISE_RE",
    "_IMPLEMENTATION_OWNED_RESEARCH_REVISE_RE",
    "_RESEARCH_SUMMARY_REL_PATH",
    "_NEGATIVE_SYMBOL_CLAIM_RE",
    "_NEGATIVE_SYMBOL_IN_FILE_RE",
    "_NEGATIVE_PARAM_CLAIM_RE",
    "_WITHOUT_PARAM_CLAIM_RE",
    "_CONSTRUCTOR_PARAM_CLAIM_RE",
    "_DIRECT_PARAM_CLAIM_RE",
    "_MISSING_PARAM_IN_TARGET_RE",
    "_INCLUDE_PARAM_IN_TARGET_RE",
    "_FIX_TARGET_WITH_PARAM_RE",
    "_HANDLE_PARAM_IN_TARGET_RE",
    "_PASS_DEPENDENCY_TO_TARGET_RE",
    "_SYMBOL_EXPECTATION_MISMATCH_RE",
    "_NEGATIVE_FILE_EXISTENCE_CLAIM_RE",
    "_POSITIVE_CLASS_CLAIM_RE",
    "_IGNORED_PARAM_CLAIM_WORDS",
    "_STALE_CLAIM_CONTEXT_RE",
    "_STALE_CLAIM_NEGATION_RE",
    "_SOURCE_FILE_EXTENSIONS",
    "_SOURCE_SCAN_SKIP_DIRS",
]
