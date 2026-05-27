"""Merged phase-contract policy (success tests, paths, revisions)."""

from umbrella.deep_agent_tools.phase_contract_common import *
from umbrella.deep_agent_tools.phase_contract_base import *
from umbrella.deep_agent_tools.phase_contract_declarations import *
from umbrella.deep_agent_tools.phase_control_common import (
    _LLM_ENV_ALIAS_RE,
    _LLM_ENV_CONTEXT_RE,
    _LLM_ENV_CONTRACT_REQUIRED_RE,
    _LLM_ENV_OMISSION_REQUIRED_RE,
    _LLM_LEGACY_ENV_ALIASES,
    _LLM_LEGACY_ENV_RE,
    _LLM_OUROBOROS_ENV_ALIASES,
    _OPENAI_KEY_RE,
    _OPENAI_REQUIRED_RE,
    _UNSUPPORTED_OUROBOROS_MODEL_ALIAS_RE,
    _WEB_SEARCH_ONLY_CONTEXT_RE,
)
from umbrella.deep_agent_tools.phase_control_base import _tool_log_rows_for_task
import ast
import json
import pathlib
import re

_LLM_PROVIDER_DEFAULT_PLAN_RE = re.compile(
    r"(?is)\b(?:openai/)?gpt-[a-z0-9_.:-]+\b|https://api\.openai\.com"
)
_EMPTY_TEST_SKELETON_RE = re.compile(
    r"(?is)\b(?:empty|blank|basic[-\s]?import|import[-\s]?only)\b"
    r".{0,160}\b(?:tests?|pytest|test files?)\b|"
    r"\b(?:tests?|pytest|test files?)\b.{0,160}"
    r"\b(?:empty|blank|basic[-\s]?import|import[-\s]?only)\b"
)
_EMPTY_TEST_DIRECT_PROTECTIVE_RE = re.compile(
    r"(?is)\b(?:no|not|never|without|avoid|reject|forbid|forbidden|"
    r"disallow|prohibit|prohibited)\b.{0,100}"
    r"\b(?:empty|blank|skeleton|basic[-\s]?import|import[-\s]?only)\b"
)
_EMPTY_TEST_PROTECTIVE_RE = _EMPTY_TEST_DIRECT_PROTECTIVE_RE
_EMPTY_TEST_BEHAVIORAL_PROOF_RE = re.compile(
    r"(?is)\b(?:assert|assertion|behavior|behaviour|real|fail|regression|"
    r"fixture|executable)\b"
)
_PLAN_CHILD_KEYS = {"subtasks", "tasks", "steps", "phases", "ordered_subtasks"}
_PLAN_CREATE_FILE_KEYS = {"files_to_create", "create_files", "files_created"}
_PLAN_CHANGE_FILE_KEYS = {
    "files_to_change",
    "files_to_modify",
    "files_affected",
    "changed_files",
}
_PLAN_FILE_FIELD_KEYS = _PLAN_CREATE_FILE_KEYS | _PLAN_CHANGE_FILE_KEYS | {
    "files_under_test",
    "changed_files_expected",
}
_PLAN_SUCCESS_TEST_KEYS = {
    "success_test",
    "success_tests",
    "success_check",
    "success_checks",
    "verification_command",
    "verification_commands",
    "verification",
    "acceptance_command",
    "test",
    "test_strategy",
    "proof_command",
}
_SUCCESS_TEST_ALIAS_KEYS = tuple(_PLAN_SUCCESS_TEST_KEYS - {"success_test"})
_GENERIC_SUCCESS_TEST_TOOLS = {
    "run_workspace_verify",
    "run_unit_tests",
    "harness_run",
    "http_boot",
    "behavioral_http",
    "run_real_e2e",
}
_GENERIC_SUCCESS_TEST_ALLOWED_RE = re.compile(
    r"(?is)\b(?:final|overall|workspace|gate|full|suite|verify|verification)\b"
)
_SUCCESS_TEST_PROSE_PREFIX_RE = re.compile(
    r"(?is)^\s*(?:run|verify|check|assert|command|success(?:\s+test)?)\s*:"
)
_SUCCESS_TEST_WORKSPACE_CD_RE = re.compile(
    r"(?is)\b(?:cd|pushd)\s+(?:[a-z]:[\\/]|/workspace\b|/workspaces\b|"
    r"[^\s;&|]*\bworkspaces[\\/])"
)
_JS_EMPTY_TEST_BYPASS_RE = re.compile(
    r"(?is)\b(?:--passWithNoTests|--allowEmpty|pass\s+with\s+no\s+tests)\b"
)
_PYTEST_COLLECT_ONLY_SUCCESS_TEST_RE = re.compile(r"(?is)\b--collect-only\b")
_PYTEST_SRC_TESTLIKE_SUCCESS_TEST_RE = re.compile(
    r"(?is)\bpytest\b[^;\n]*\bsrc[\\/][^;\n]*(?:test_|_test|verify_|_e2e|_integration)"
)
_FILE_EXISTENCE_ONLY_SUCCESS_TEST_RE = re.compile(
    r"(?is)\b(?:existsSync|exists\s*\(|is_file\s*\(|os\.path\.exists|"
    r"pathlib\.Path|Path\s*\(|\[\s*-f\b|\btest\s+-f\b)"
)
_BEHAVIORAL_SUCCESS_TEST_RE = re.compile(
    r"(?is)\b(?:pytest|playwright|npm\s+(?:run\s+)?(?:test|build)|pnpm|"
    r"yarn|vitest|jest|curl\b|invoke-webrequest|requests\.(?:get|post)|"
    r"selenium|browser|http_boot|behavioral_http|run_real_e2e)\b"
)
_GENERIC_SUCCESS_TOOL_WITH_ARGS_RE = re.compile(
    r"(?is)^\s*(?:run_workspace_verify|run_unit_tests|harness_run)(?:\s+\S+|:)"
)
_DESCRIPTIVE_SUCCESS_OUTCOME_RE = re.compile(
    r"(?is)\b(?:passes?|fails?|exit\s+code|returns?)\b[^;\n]{0,120}$"
)
_DESCRIPTIVE_SUCCESS_PAREN_RE = re.compile(
    r"(?is)\s+\([^)]*(?:verif|check|pass|fail|manual|browser|human|expected|"
    r"must|should)[^)]*\)\s*$"
)
_DESCRIPTIVE_SUCCESS_TEST_RE = re.compile(
    r"(?is)\s+-\s+(?:must|should|verif(?:y|ies)|validates?|checks?|exit\s+code)\b"
)
_DESCRIPTIVE_BROWSER_SUCCESS_TEST_RE = re.compile(
    r"(?is)\b(?:browser|user|human|manual|visual|console|network inspector|"
    r"websocket messages?)\b"
)
_CONCRETE_BROWSER_AUTOMATION_RE = re.compile(
    r"(?is)\b(?:playwright|selenium|puppeteer|browser_automation|"
    r"run_real_e2e|http_boot|behavioral_http|python\s+-m\s+pytest|npx\s+playwright)\b"
)
_SUCCESS_TEST_AUTOMATION_RE = re.compile(
    r"(?is)\b(?:python|py|pytest|node|npm|pnpm|yarn|npx|uv|ruff|mypy|"
    r"playwright|curl|invoke-webrequest|run_workspace_verify|run_unit_tests|"
    r"harness_run|http_boot|behavioral_http|run_real_e2e)\b|^\s*\./"
)
_SUCCESS_TEST_VAGUE_RE = re.compile(
    r"(?is)\b(?:verify|check|ensure|confirm|validate|test)\b"
)
_DIRECT_PYTHON_TEST_MODULE_RE = re.compile(
    r"(?is)\b(?:python|py)(?:\.exe)?\s+(?P<target>"
    r"[^\s;&|]*(?:^|[\\/])(?:test_[^\\/;\s|&]*|[^\\/;\s|&]+_test)\.py"
    r"(?:\:\:[^\s;&|]+)?|[^\s;&|]+\.py\:\:[^\s;&|]+)"
)
_NON_PORTABLE_SHELL_RE = re.compile(
    r"(?is)^\s*(?:bash|sh|zsh|fish|cmd(?:\.exe)?|powershell|pwsh)(?:\.exe)?\b|"
    r"(?:^|[\s;&|])(?:start-job|start-process|nohup|timeout|sleep)\b|"
    r"(?:^|[\s;&|])(?:\.\/)?[^\s;&|]+\.sh\b|`"
)
_PYTHON_INLINE_ALLOWED_IMPORT_ROOTS = {
    "argparse",
    "asyncio",
    "contextlib",
    "datetime",
    "importlib",
    "json",
    "math",
    "os",
    "pathlib",
    "re",
    "shlex",
    "subprocess",
    "sys",
    "tempfile",
    "time",
    "typing",
    "unittest",
}
_LLM_MOCK_SUCCESS_TEST_RE = re.compile(
    r"(?is)\b(?:mock|fake|stub|dry[-\s]?run|simulat(?:e|ed|ion)|fallback)\b"
)
_MOCKED_PROOF_WORK_ITEM_CONTEXT_RE = re.compile(
    r"(?is)\b(?:llm|gmas|agent|bot|model|e2e|integration|runtime)\b"
)
_LLM_WORK_ITEM_CONTEXT_RE = re.compile(
    r"(?is)\b(?:llm|gmas|agent|bot|model)\b"
)
_LLM_ERROR_AS_SUCCESS_RE = re.compile(
    r"(?is)\b(?:error|exception|failure|unavailable|missing\s+(?:key|env|config))\b"
    r".{0,120}\b(?:pass|success|ok|expected)\b|"
    r"\b(?:pass|success|ok|expected)\b.{0,120}"
    r"\b(?:error|exception|failure|unavailable|missing\s+(?:key|env|config))\b"
)
_LLM_ERROR_PROTECTIVE_RE = re.compile(
    r"(?is)\b(?:must\s+fail|fails?|raises?|exits?\s+non[-\s]?zero|"
    r"rejects?|blocks?)\b"
)
_PLAN_READ_FILE_KEYS = {
    "files_to_read",
    "read_files",
    "files_to_inspect",
    "existing_files",
    "context_files",
}
_PLAN_REBUILD_EXISTING_RE = re.compile(
    r"(?is)\b(?:scaffold|bootstrap|create\s+from\s+scratch|new\s+project|"
    r"initialize|initialise)\b"
)
_PLAN_EXISTING_REPAIR_WORD_RE = re.compile(
    r"(?is)\b(?:repair|fix|reuse|extend|refactor|preserve|existing)\b"
)
_PLAN_MIGRATION_WORD_RE = re.compile(r"(?is)\b(?:migrate|migration|port)\b")
_PLAN_STUB_INTENT_RE = re.compile(
    r"(?is)\b(?:stub|placeholder|todo|mock|fake|dummy|hardcoded)\b"
)
_REVISION_STOP_WORDS = {
    "the",
    "and",
    "or",
    "with",
    "for",
    "from",
    "into",
    "that",
    "this",
    "must",
    "should",
    "provide",
    "platform",
    "appropriate",
    "command",
    "subtask",
    "phase",
}
_PLAN_CODE_SUFFIXES = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".css",
    ".scss",
    ".html",
}
_PLAN_GREENFIELD_ALLOWED_ROOT_PY = {"conftest.py", "setup.py"}
_PLAN_NON_IMPL_ROOTS = {
    "docs",
    "doc",
    "tests",
    "test",
    "frontend",
    "public",
    "assets",
    "scripts",
}
_ROOT_PLAN_NOISE_RE = re.compile(
    r"(?i)^(?:verify|debug|probe|smoke|run|serve|start|stop).*\.py$"
)


def _workspace_root_for_policy(ctx: ToolContext | None) -> pathlib.Path | None:
    if ctx is None:
        return None
    workspace_id = _workspace_id(ctx)
    if not workspace_id:
        return None
    repo_root = pathlib.Path(getattr(ctx, "host_repo_root", None) or ctx.repo_dir)
    return repo_root / "workspaces" / workspace_id


def _workspace_existing_impl_roots(ctx: ToolContext | None) -> set[str]:
    root = _workspace_root_for_policy(ctx)
    if root is None or not root.exists():
        return set()
    impl_suffixes = _PLAN_CODE_SUFFIXES | {
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".cs",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".vue",
        ".svelte",
    }
    ignored_dirs = {
        ".git",
        ".memory",
        ".pytest_cache",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "docs",
        "doc",
        "tests",
        "test",
        "assets",
        "public",
    }
    roots: set[str] = set()
    for child in root.iterdir():
        name = child.name
        lowered = name.lower()
        if lowered in ignored_dirs or name.startswith("."):
            continue
        if child.is_dir():
            if any(
                path.is_file() and path.suffix.lower() in impl_suffixes
                for path in child.rglob("*")
            ):
                roots.add(name)
            continue
        if child.is_file() and child.suffix.lower() in impl_suffixes:
            if lowered in _PLAN_GREENFIELD_ALLOWED_ROOT_PY:
                continue
            if lowered.startswith(("test_", "verify_", "check_")):
                continue
            if ".config." in lowered or lowered.endswith(("_config.py", ".d.ts")):
                continue
            roots.add(name)
    return roots


def _path_looks_like_code(path: str) -> bool:
    rel = str(path or "").replace("\\", "/").strip("/")
    if not rel:
        return False
    return pathlib.PurePosixPath(rel).suffix.lower() in _PLAN_CODE_SUFFIXES


def _extract_plan_paths(plan: Any) -> list[str]:
    paths: list[str] = []
    for text in _iter_plan_strings(plan):
        for match in re.finditer(
            r"(?<![\w:/.-])((?:[A-Za-z0-9_.-]+/)+"
            r"[A-Za-z0-9_.-]+\."
            r"(?:pyi|tsx|jsx|mjs|cjs|yaml|scss|py|js|ts|json|toml|"
            r"yml|md|html|css|txt))(?![\w.-])",
            text,
        ):
            paths.append(match.group(1))
    return paths

# --- phase_contract_success.py ---
def _plan_item_has_success_test(item: dict[str, Any]) -> bool:
    for key in _PLAN_SUCCESS_TEST_KEYS:
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            return True
        if isinstance(raw, dict) and any(str(value or "").strip() for value in raw.values()):
            return True
        if isinstance(raw, (list, tuple, set, frozenset)) and any(
            str(value or "").strip() for value in raw
        ):
            return True
    return False


def _iter_plan_child_dicts(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        return [item for item in raw.values() if isinstance(item, dict)]
    return []


def _iter_plan_subtask_leaves(item: dict[str, Any]) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for key in _PLAN_CHILD_KEYS:
        for child in _iter_plan_child_dicts(item.get(key)):
            children.extend(_iter_plan_subtask_leaves(child))
    if children:
        # A phase/umbrella wrapper may carry narrative verification metadata such
        # as ``test_strategy``.  Execution leaves are still the nested subtasks;
        # treating the wrapper as a leaf makes planning loop on phase prose.
        return children
    return [item]


def _iter_plan_subtasks(value: Any) -> list[dict[str, Any]]:
    subtasks: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _PLAN_CHILD_KEYS:
                for item in _iter_plan_child_dicts(child):
                    subtasks.extend(_iter_plan_subtask_leaves(item))
            else:
                subtasks.extend(_iter_plan_subtasks(child))
    elif isinstance(value, list):
        for child in value:
            subtasks.extend(_iter_plan_subtasks(child))
    return subtasks


def _success_test_text_from_raw(raw: Any) -> str:
    if isinstance(raw, dict):
        parts: list[str] = []
        for name in (
            "value",
            "command",
            "commands",
            "cmd",
            "command_line",
            "pytest_id",
            "verification",
            "checks",
            "description",
            "text",
        ):
            value = raw.get(name)
            if isinstance(value, (dict, list, tuple, set, frozenset)):
                text = _success_test_text_from_raw(value)
            else:
                text = str(value or "").strip()
            if text:
                parts.append(text)
        return " ".join(part for part in parts if part)
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts: list[str] = []
        for value in raw:
            if isinstance(value, dict):
                text = _success_test_text_from_raw(value)
            else:
                text = str(value).strip()
            if text:
                parts.append(text)
        return "; ".join(parts)
    return str(raw or "").strip()


def _bare_success_test_tool(value: str) -> str:
    text = str(value or "").strip().strip("`").lower()
    return text if text in _GENERIC_SUCCESS_TEST_TOOLS else ""


def _plan_item_success_test_raw(item: dict[str, Any]) -> Any:
    raw_success = item.get("success_test")
    success_text = _success_test_text_from_raw(raw_success)
    if success_text and _bare_success_test_tool(success_text):
        for key in (
            "verification_command",
            "verification_commands",
            "verification",
            "acceptance_command",
            "success_check",
            "success_checks",
            "test",
            "test_strategy",
        ):
            raw = item.get(key)
            text = _success_test_text_from_raw(raw)
            if text and not _bare_success_test_tool(text):
                return raw
    if success_text:
        return raw_success
    for key in (
        "success_check",
        "success_checks",
        "acceptance_command",
        "verification_command",
        "verification_commands",
        "verification",
        "test_strategy",
        "test",
    ):
        raw = item.get(key)
        if _success_test_text_from_raw(raw):
            return raw
    return None


def _plan_item_success_test_text(item: dict[str, Any]) -> str:
    return _success_test_text_from_raw(_plan_item_success_test_raw(item))


def _plan_item_non_success_context_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in item.items():
        if str(key).strip().lower() in _PLAN_SUCCESS_TEST_KEYS:
            continue
        parts.extend(_iter_plan_strings(value))
    return "\n".join(parts)


def _llm_mock_success_test_issue(item: dict[str, Any], success_text: str) -> str:
    if not _LLM_MOCK_SUCCESS_TEST_RE.search(success_text):
        return ""
    if not _MOCKED_PROOF_WORK_ITEM_CONTEXT_RE.search(
        _plan_item_non_success_context_text(item)
    ):
        return ""
    return (
        "success test uses a mocked path or dry-run path for an LLM/e2e/"
        "integration proof; "
        "required behavior must be proved with the inherited real runtime env "
        "or fail/skip explicitly when that env is absent"
    )


def _llm_error_as_success_issue(item: dict[str, Any], success_text: str) -> str:
    if not _LLM_WORK_ITEM_CONTEXT_RE.search(_plan_item_non_success_context_text(item)):
        return ""
    if not _LLM_ERROR_AS_SUCCESS_RE.search(success_text):
        return ""
    if _LLM_ERROR_PROTECTIVE_RE.search(success_text):
        return ""
    return (
        "success test treats an LLM/GMAS error path as a passing outcome; with "
        "the inherited real runtime env, the proof must require a successful "
        "real LLM decision and reserve explicit error/skip behavior only for "
        "missing or failing configuration"
    )


def _success_test_automation_issue(value: str) -> str:
    raw_text = str(value or "").strip()
    text = raw_text.lower()
    if not text:
        return "success test is empty"
    if _SUCCESS_TEST_PROSE_PREFIX_RE.match(raw_text):
        return (
            "success test is descriptive text with a prefix; put only the "
            "exact command/tool target in `success_test`, without `Run:`, "
            "`Verify:`, `Check:`, `Assert:`, or `Command:` prose."
        )
    if _SUCCESS_TEST_WORKSPACE_CD_RE.search(raw_text):
        return (
            "success test hard-codes a host workspace path; phase success "
            "tests run from the active workspace root, so use a workspace-"
            "relative command such as `python -m pytest ...` or "
            "`cd backend && python -m pytest ...`"
        )
    quote_issue = _command_quote_issue(raw_text)
    if quote_issue:
        return quote_issue
    python_issue = _python_inline_syntax_issue(raw_text)
    if python_issue:
        return python_issue
    python_test_issue = _python_test_module_invocation_issue(raw_text)
    if python_test_issue:
        return python_test_issue
    shell_segment_issue = _shell_command_segment_issue(raw_text)
    if shell_segment_issue:
        return shell_segment_issue
    portability_issue = _command_portability_issue(raw_text)
    if portability_issue:
        return portability_issue
    if _JS_EMPTY_TEST_BYPASS_RE.search(raw_text):
        return (
            "success test allows an empty JavaScript test suite "
            "(`--passWithNoTests`/`--allowEmpty`); write a real checked-in "
            "test or use a build/typecheck command that fails on regressions"
        )
    if _PYTEST_COLLECT_ONLY_SUCCESS_TEST_RE.search(raw_text):
        return (
            "success test only collects pytest tests (`--collect-only`); "
            "use a real checked-in test, build, smoke, HTTP/browser proof, "
            "or verification script that executes behavior and can fail for "
            "the implemented feature"
        )
    if _PYTEST_SRC_TESTLIKE_SUCCESS_TEST_RE.search(raw_text):
        return (
            "success test runs pytest against test-like Python modules under "
            "`src/`; greenfield pytest tests and verification pytest modules "
            "must live under `tests/`. Move `test_*.py`, `*_test.py`, "
            "`verify_*.py`, or `verify/` pytest targets out of `src/`, or run "
            "a non-pytest application entrypoint if this is production code"
        )
    if _FILE_EXISTENCE_ONLY_SUCCESS_TEST_RE.search(
        raw_text
    ) and not _BEHAVIORAL_SUCCESS_TEST_RE.search(raw_text):
        return (
            "success test only checks file/path existence; move file presence "
            "into acceptance criteria and use a checked-in unit/integration "
            "test, build command, HTTP/browser proof, or verification script "
            "that exercises behavior"
        )
    if _GENERIC_SUCCESS_TOOL_WITH_ARGS_RE.search(raw_text):
        return (
            "success test uses a generic Umbrella tool name with pseudo-arguments; "
            "use the bare tool only for final gates or write the exact underlying "
            "command such as `python -m pytest ... -q`, `npm test`, or a checked-in "
            "verification script"
        )
    if _DESCRIPTIVE_SUCCESS_OUTCOME_RE.search(raw_text):
        return (
            "success test mixes an executable command with descriptive pass/fail "
            "outcome prose; put alternate env/error expectations in a checked-in "
            "test or acceptance criteria, and leave success_test as one exact "
            "command"
        )
    if _DESCRIPTIVE_SUCCESS_PAREN_RE.search(raw_text):
        return (
            "success test appends parenthetical explanatory prose to an executable "
            "command; move the note into goal/acceptance_criteria and leave "
            "success_test as one exact command"
        )
    if _DESCRIPTIVE_SUCCESS_TEST_RE.search(raw_text):
        return (
            "success test mixes an executable with descriptive acceptance text; "
            "move the prose into goal/acceptance_criteria and leave success_test "
            "as one exact command or tool target"
        )
    human_required = re.search(
        r"\b(user reports?|human reports?|ask the user|by hand)\b", text
    )
    manual_required = re.search(
        r"\b(manual|manually|manual smoke|visual inspection|visually inspect)\b",
        text,
    )
    if human_required or manual_required:
        return (
            "success test depends on a human/manual report; replace it with "
            "an agent-run command, HTTP/browser automation, or verification tool"
        )
    if _DESCRIPTIVE_BROWSER_SUCCESS_TEST_RE.search(
        raw_text
    ) and not _CONCRETE_BROWSER_AUTOMATION_RE.search(raw_text):
        return (
            "success test describes browser/user observation instead of an "
            "automated proof; use a concrete command/tool such as "
            "`npx playwright test`, `python -m pytest ...`, `run_real_e2e`, "
            "`http_boot`, or `behavioral_http`"
        )
    if not _SUCCESS_TEST_AUTOMATION_RE.search(text):
        return (
            "success test is not an executable proof; use an exact command, "
            "`run_workspace_verify`, `run_unit_tests`, `harness_run`, "
            "`http_boot`/`behavioral_http`, or browser automation"
        )
    if _SUCCESS_TEST_VAGUE_RE.search(text) and not re.search(
        r"\b(run_workspace_verify|run_unit_tests|harness_run|http_boot|"
        r"behavioral_http|pytest|python\s+-m\s+pytest|npm\s+(run\s+)?test|"
        r"npm(?:\s+\S+){0,4}\s+(?:run\s+)?test|npm\s+run\s+build|pnpm|"
        r"npm(?:\s+\S+){0,6}\s+exec\s+\S+|vitest|yarn|playwright|browser)\b",
        text,
    ):
        return (
            "success test is too vague; include the concrete command or "
            "automation target that produces pass/fail evidence"
        )
    return ""


def _success_test_shape_issue(raw: Any) -> str:
    if isinstance(raw, (list, tuple, set, frozenset)):
        return (
            "success_test must be a single executable string/object, not a "
            "list of commands; split the work into separate subtasks or call "
            "a checked-in test script"
        )
    if isinstance(raw, dict):
        text = _success_test_text_from_raw(raw)
        if re.match(r"^\s*-\w", text):
            return (
                "success_test command is missing an executable; include the "
                "full command such as `python -m pytest ...` instead of an "
                "option-only command like `-m pytest ...`"
            )
    return ""


def _success_test_alias_shape_issue(item: dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    if "success_test" in item:
        return ""
    for key in _SUCCESS_TEST_ALIAS_KEYS:
        if key not in item:
            continue
        raw = item.get(key)
        if isinstance(raw, (list, tuple, set, frozenset)):
            return (
                f"`{key}` is a list; phase plans must put one exact executable "
                "command/object in top-level `success_test`. Split multiple "
                "checks into separate subtasks or call a checked-in verification "
                "script."
            )
        if isinstance(raw, str) and _SUCCESS_TEST_PROSE_PREFIX_RE.match(raw):
            return (
                f"`{key}` is descriptive text with a prefix; put only the exact "
                "command/tool target in top-level `success_test`, without "
                "`Run:`, `Verify:`, `Check:`, `Assert:`, or `Command:` prose."
            )
    return ""


def _command_quote_issue(value: str) -> str:
    text = str(value or "")
    escaped = False
    double_quotes = 0
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            double_quotes += 1
    if double_quotes % 2:
        return (
            "success test command has unbalanced double quotes; provide a "
            "valid executable command"
        )
    return ""


def _iter_python_inline_snippets(value: str) -> list[str]:
    text = str(value or "")
    snippets: list[str] = []
    pos = 0
    pattern = re.compile(r"(?i)\b(?:python|py)(?:\.exe)?\s+-c\s*([\"'])")
    while True:
        match = pattern.search(text, pos)
        if not match:
            return snippets
        quote = match.group(1)
        idx = match.end()
        escaped = False
        chars: list[str] = []
        while idx < len(text):
            ch = text[idx]
            if escaped:
                chars.append("\\" + ch)
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                break
            else:
                chars.append(ch)
            idx += 1
        if idx >= len(text):
            return snippets
        snippet = "".join(chars).replace(r"\"", '"').replace(r"\'", "'")
        snippets.append(snippet)
        pos = idx + 1


def _python_inline_import_only_issue(tree: ast.AST) -> str:
    if not isinstance(tree, ast.Module):
        return ""
    if not tree.body:
        return ""

    def is_print_expr(stmt: ast.stmt) -> bool:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            return False
        func = stmt.value.func
        return isinstance(func, ast.Name) and func.id == "print"

    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if is_print_expr(stmt):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        return ""
    return (
        "success test `python -c` only imports modules and/or prints text; "
        "use assertions, instantiate/call the behavior, or run a real test "
        "command so the subtask proof can fail when the implementation is "
        "wrong"
    )


def _python_inline_workspace_import_issue(tree: ast.AST) -> str:
    if not isinstance(tree, ast.Module):
        return ""
    for node in ast.walk(tree):
        roots: list[str] = []
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                return (
                    "success test `python -c` imports workspace/application "
                    "modules; put this behavioral check in a checked-in "
                    "pytest/node/browser test or verification script"
                )
            module = str(node.module or "").strip()
            if module:
                roots.append(module.split(".", 1)[0])
        for root in roots:
            if root and root not in _PYTHON_INLINE_ALLOWED_IMPORT_ROOTS:
                return (
                    "success test `python -c` imports workspace/application "
                    "modules; put this behavioral check in a checked-in "
                    "pytest/node/browser test or verification script"
                )
    return ""


def _python_inline_complexity_issue(snippet: str) -> str:
    text = str(snippet or "").strip()
    if (
        "\n" in text
        or "\r" in text
        or len(text) > 280
        or text.count(";") > 5
        or re.search(r"\b(subprocess|time\.sleep|requests|urllib\.request)\b", text)
    ):
        return (
            "success test `python -c` is too complex for reliable phase-plan "
            "verification; put the behavior in a checked-in pytest/node/browser "
            "test or verification script and use that command as success_test"
        )
    return ""


def _python_inline_docs_content_issue(snippet: str) -> str:
    text = str(snippet or "")
    lowered = text.lower()
    if (
        ".read(" in lowered
        and re.search(r"\bopen\s*\([^)]*(?:readme\.md|docs[\\/])", lowered)
        and re.search(r"\bassert\b|'\s+in\s+open|\"\s+in\s+open", lowered)
    ):
        return (
            "success test uses documentation/content inline checks; put "
            "documentation assertions in a checked-in verifier or focus "
            "success_test on executable behavioral proof"
        )
    return ""


def _python_inline_syntax_issue(value: str) -> str:
    for snippet in _iter_python_inline_snippets(value):
        try:
            tree = ast.parse(snippet)
        except SyntaxError as exc:
            detail = exc.msg or "invalid syntax"
            return (
                "success test contains invalid `python -c` code "
                f"({detail}); provide a syntactically valid command"
            )
        lowered = snippet.lower()
        has_failure_print = bool(
            re.search(r"else\s+['\"](?:fail|failed|error)['\"]", lowered)
        )
        has_real_failure = any(
            token in lowered for token in ("assert ", "raise ", "sys.exit")
        )
        if has_failure_print and not has_real_failure:
            return (
                "success test `python -c` only prints FAIL/ERROR while still "
                "exiting successfully; use assert, raise, sys.exit, or a real "
                "test command so failure changes the exit code"
            )
        import_only_issue = _python_inline_import_only_issue(tree)
        if import_only_issue:
            return import_only_issue
        docs_content_issue = _python_inline_docs_content_issue(snippet)
        if docs_content_issue:
            return docs_content_issue
        complexity_issue = _python_inline_complexity_issue(snippet)
        if complexity_issue:
            return complexity_issue
        workspace_import_issue = _python_inline_workspace_import_issue(tree)
        if workspace_import_issue:
            return workspace_import_issue
    return ""


def _python_test_module_invocation_issue(value: str) -> str:
    match = _DIRECT_PYTHON_TEST_MODULE_RE.search(str(value or ""))
    if not match:
        return ""
    target = match.group("target") or "test module"
    return (
        f"success test invokes pytest module `{target}` with `python ...`; "
        "run test modules through `python -m pytest <path>[::test] -q` or "
        "`pytest <path>[::test] -q`, or use a checked-in verification script "
        "whose name is not a pytest test module"
    )


def _shell_command_segment_issue(value: str) -> str:
    for segment in _split_shell_command_segments(str(value or "")):
        stripped = segment.strip()
        if not stripped:
            continue
        if re.match(r"(?i)^assert\b", stripped):
            return (
                "success test contains a bare Python `assert` as a shell command; "
                "put assertions in `python -c \"assert ...\"`, a checked-in pytest, "
                "or a verification script"
            )
        if re.match(
            r"(?i)^(?:os\.path\.exists|fs\.existsSync|pathlib\.Path\(|Path\()",
            stripped,
        ):
            return (
                "success test contains a bare file-existence expression as a "
                "shell command; put it in a real pytest/python/node assertion "
                "or use a verification script"
            )
    return ""


def _split_shell_command_segments(value: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    text = str(value or "")
    while index < len(text):
        ch = text[index]
        if escaped:
            current.append(ch)
            escaped = False
            index += 1
            continue
        if ch == "\\":
            current.append(ch)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            index += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            current.append(ch)
            index += 1
            continue
        if text.startswith("&&", index) or text.startswith("||", index):
            segments.append("".join(current))
            current = []
            index += 2
            continue
        if ch == ";":
            segments.append("".join(current))
            current = []
            index += 1
            continue
        current.append(ch)
        index += 1
    segments.append("".join(current))
    return segments


def _has_background_shell_operator(value: str) -> bool:
    return _has_shell_control_operator(value, background_only=True)


def _has_shell_control_operator(
    value: str,
    *,
    background_only: bool = False,
) -> bool:
    text = str(value or "")
    quote: str | None = None
    escaped = False
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if escaped:
            escaped = False
            idx += 1
            continue
        if ch == "\\":
            escaped = True
            idx += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            idx += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            idx += 1
            continue
        if text.startswith("&&", idx) or text.startswith("||", idx):
            if not background_only:
                return True
            idx += 2
            continue
        if ch == "&":
            return True
        if not background_only and ch in {";", "|"}:
            return True
        idx += 1
    return False


def _command_portability_issue(value: str) -> str:
    text = str(value or "")
    if _NON_PORTABLE_SHELL_RE.search(text) or _has_shell_control_operator(text):
        return (
            "success test uses non-portable or unmanaged shell/process-control "
            "syntax that is not a reliable Umbrella workspace proof on this "
            "host; use Python/pytest/node/npm, a checked-in verification "
            "script, or a managed HTTP/browser verification gate that starts "
            "and stops services cleanly"
        )
    return ""


def _phase_plan_success_test_issues(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for idx, subtask in enumerate(_iter_plan_subtasks(plan), start=1):
        if not isinstance(subtask, dict) or not _plan_item_has_success_test(subtask):
            continue
        issue = _success_test_alias_shape_issue(subtask)
        if not issue:
            issue = _success_test_shape_issue(subtask.get("success_test"))
        text = ""
        if not issue:
            text = _plan_item_success_test_text(subtask)
            issue = _success_test_automation_issue(text)
        if not issue:
            issue = _llm_mock_success_test_issue(subtask, text)
        if not issue:
            issue = _llm_error_as_success_issue(subtask, text)
        if not issue:
            continue
        subtask_id = (
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or f"subtask #{idx}"
        )
        issues.append(f"subtask `{subtask_id}` has non-automatable success_test: {issue}")
    return issues


def _phase_plan_generic_success_test_issues(plan: dict[str, Any]) -> list[str]:
    subtasks = _iter_plan_subtasks(plan)
    if not subtasks:
        return []
    generic_ids: list[str] = []
    inappropriate: list[str] = []
    for idx, subtask in enumerate(subtasks, start=1):
        text = _plan_item_success_test_text(subtask)
        tool = _bare_success_test_tool(text)
        if not tool:
            continue
        subtask_id = str(
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or subtask.get("name")
            or f"subtask #{idx}"
        )
        generic_ids.append(subtask_id)
        label = " ".join(
            str(subtask.get(key) or "")
            for key in ("id", "subtask_id", "title", "name", "goal", "description")
        )
        if tool in {"run_workspace_verify", "run_unit_tests"} and not (
            _GENERIC_SUCCESS_TEST_ALLOWED_RE.search(label)
        ):
            inappropriate.append(subtask_id)
    issues: list[str] = []
    if inappropriate:
        issues.append(
            "bare `run_workspace_verify`/`run_unit_tests` is too generic for "
            "implementation subtask(s); use a concrete local command or an explicit "
            "HTTP/browser/tool proof for: "
            + ", ".join(inappropriate[:8])
        )
    if len(subtasks) >= 6 and len(generic_ids) > max(2, len(subtasks) // 2):
        issues.append(
            "phase plan overuses bare verification tool names as success tests; "
            "reserve `run_workspace_verify`/`run_unit_tests` for final/"
            "integration/config gates and give implementation subtasks "
            "concrete commands"
        )
    return issues


def _phase_plan_structure_issues(plan: dict[str, Any]) -> list[str]:
    if not isinstance(plan, dict) or not plan:
        return [
            (
                "propose_phase_plan requires a non-empty `plan` object. Send a "
                "compact object with a top-level `subtasks` array; each leaf "
                "needs `id`, `title`, `goal`, files, and typed `proof`."
            )
        ]
    if isinstance(plan.get("plan"), str) and plan.get("plan_truncated") is True:
        return [
            (
                "phase plan was submitted as truncated serialized text in "
                "`plan.plan`; send a compact JSON object directly with real "
                "`subtasks`, `steps`, or `phases` leaves. Do not wrap the plan "
                "as a string, digest, diff, or partial artifact."
            )
        ]
    if isinstance(plan.get("plan"), str) and not _iter_plan_subtasks(plan):
        return [
            (
                "phase plan is embedded as serialized text in `plan.plan`; "
                "send the plan as a JSON object with real `subtasks`, `steps`, "
                "or `phases` leaves, not as a string or truncated digest"
            )
        ]
    if not _iter_plan_subtasks(plan):
        return [
            (
                "phase plan has no executable subtasks/steps/phases. Do not "
                "submit only `title`, `decision_policies`, `risk_mitigation`, "
                "notes, or an empty tool call. Re-emit a compact object with "
                "`subtasks: [{id, title, goal, files_to_create/files_to_change/"
                "files_affected, proof}, ...]`."
            )
        ]
    return []


def _phase_plan_placeholder_issues(plan: dict[str, Any]) -> list[str]:
    issues: list[str] = []

    def walk(value: Any, path: str = "plan") -> None:
        if isinstance(value, dict):
            if value.get("_depth_limit") is True:
                issues.append(
                    f"plan field `{path}` is a depth-limit placeholder, not an "
                    "executable subtask; provide the real leaf object with id, "
                    "title, goal, files, and typed proof"
                )
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, f"{path}[{idx}]")

    walk(plan)
    return issues


def _phase_plan_llm_fallback_issues(plan: dict[str, Any]) -> list[str]:
    del plan
    return []


def _phase_plan_llm_test_double_issues(plan: dict[str, Any]) -> list[str]:
    del plan
    return []


def _provider_default_match_is_protective(text: str) -> bool:
    value = str(text or "")
    provider_ref = r"(?:\b(?:openai/)?gpt-[a-z0-9_.:-]+\b|https://api\.openai\.com)"
    negative_before = (
        r"\b(?:no|not|never|without|avoid|reject|forbid(?:s|den)?|"
        r"disallow(?:s|ed)?|do\s+not|must\s+not)\b"
    )
    negative_after = (
        r"\b(?:not\s+allowed|forbidden|disallowed|rejected|must\s+not|"
        r"do\s+not|avoid(?:ed)?)\b"
    )
    return bool(
        re.search(rf"(?is){negative_before}.{{0,80}}{provider_ref}", value)
        or re.search(rf"(?is){provider_ref}.{{0,80}}{negative_after}", value)
    )


def _phase_plan_llm_provider_default_issues(plan: dict[str, Any]) -> list[str]:
    text = "\n".join(_iter_plan_strings(plan))
    if not _LLM_ENV_CONTEXT_RE.search(text):
        return []
    for match in _LLM_PROVIDER_DEFAULT_PLAN_RE.finditer(text):
        start = max(0, match.start() - 80)
        end = min(len(text), match.end() + 80)
        if _provider_default_match_is_protective(text[start:end]):
            continue
        return [
            (
                "plan hardcodes provider/model-specific LLM defaults. Use the "
                "inherited Umbrella/Ouroboros runtime model/provider from "
                "`OUROBOROS_MODEL`/`LLM_MODEL` and env-driven base URL instead "
                "of `gpt-*` or `https://api.openai.com` in generated code, "
                "tests, docs, cost estimates, or acceptance criteria."
            )
        ]
    return []


def _phase_plan_empty_test_skeleton_issues(plan: dict[str, Any]) -> list[str]:
    first_unprotected: str | None = None
    for text in _iter_plan_strings(plan):
        if _plan_string_is_identifier_like(text):
            continue
        for match in _EMPTY_TEST_SKELETON_RE.finditer(text):
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 100)
            context = text[start:end]
            direct_start = max(0, match.start() - 80)
            direct_end = min(len(text), match.end() + 40)
            if _EMPTY_TEST_DIRECT_PROTECTIVE_RE.search(text[direct_start:direct_end]):
                continue
            if _EMPTY_TEST_PROTECTIVE_RE.search(context) and (
                _EMPTY_TEST_BEHAVIORAL_PROOF_RE.search(context)
                or _EMPTY_TEST_PROTECTIVE_RE.search(match.group(0))
            ):
                continue
            first_unprotected = " ".join(match.group(0).split())
            break
        if first_unprotected is not None:
            break
    if first_unprotected is None:
        return []
    return [
        (
            "plan asks for empty/basic-import test skeletons. Test "
            "infrastructure must contain executable assertions or fixtures that "
            "can fail for real behavior; do not plan empty shells, import-only "
            "tests, or basic-import placeholders as a completion step. Matched "
            f"text: `{first_unprotected}`."
        )
    ]


def _missing_llm_runtime_aliases(text: str) -> list[str]:
    return [
        alias
        for alias in _LLM_LEGACY_ENV_ALIASES
        if not re.search(rf"\b{re.escape(alias)}\b", str(text or ""))
    ]


def _llm_env_contract_issue_from_text(text: str, *, subject: str) -> str:
    if not _LLM_ENV_CONTEXT_RE.search(text):
        return ""
    if _UNSUPPORTED_OUROBOROS_MODEL_ALIAS_RE.search(text):
        return (
            f"{subject} uses unsupported model env alias "
            "`OUROBOROS_LLM_MODEL`. The inherited Umbrella/Ouroboros model "
            "alias is `OUROBOROS_MODEL` (with `LLM_MODEL` as the workspace "
            "alias); do not invent `OUROBOROS_LLM_MODEL` in generated code, "
            "docs, tests, env examples, or research memory."
        )
    if re.search(r"\bLL_BASE_URL\b", text):
        return (
            f"{subject} uses unsupported LLM env alias `LL_BASE_URL`; use "
            "`LLM_BASE_URL` as the generated workspace public base-url "
            "alias."
        )
    has_ouroboros_alias = bool(_LLM_ENV_ALIAS_RE.search(text))
    legacy_mentions = _LLM_LEGACY_ENV_RE.findall(text)
    openai_required = bool(_OPENAI_REQUIRED_RE.search(text))
    openai_mentions = bool(_OPENAI_KEY_RE.search(text))
    web_search_only = bool(_WEB_SEARCH_ONLY_CONTEXT_RE.search(text)) and not legacy_mentions
    missing_aliases = _missing_llm_runtime_aliases(text)
    has_any_runtime_alias = len(missing_aliases) < (
        len(_LLM_LEGACY_ENV_ALIASES)
    )
    if has_ouroboros_alias:
        return (
            f"{subject} leaks Umbrella host LLM aliases into generated "
            "workspace contract. Generated projects must expose the public "
            "`LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` aliases only; "
            "Umbrella maps host launch env into those aliases before running "
            "workspace commands."
        )
    if has_any_runtime_alias and not missing_aliases:
        return ""
    if (
        has_any_runtime_alias
        or legacy_mentions
        or (openai_mentions and not web_search_only)
        or openai_required
    ):
        missing_text = (
            " Missing aliases: "
            + ", ".join(f"`{alias}`" for alias in missing_aliases)
            + "."
            if missing_aliases
            else ""
        )
        return (
            f"{subject} uses an LLM credential contract that is too narrow for "
            "Umbrella/Ouroboros runtime. Generated workspace code/tests and "
            "phase memory must support the public runtime aliases "
            "`LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`; do not require "
            "`OPENAI_API_KEY` as the only way to run real LLM/e2e "
            "behavior. `OPENAI_API_KEY` is only one possible provider/"
            "web-search credential, not the universal project LLM contract."
            f"{missing_text}"
        )
    if not (
        _LLM_ENV_OMISSION_REQUIRED_RE.search(text)
        or _LLM_ENV_CONTRACT_REQUIRED_RE.search(text)
    ):
        return ""
    return (
        f"{subject} omits the standalone LLM runtime env contract for "
        "LLM/GMAS/bot work. The handoff must explicitly require generated "
        "workspace code/tests to resolve `LLM_API_KEY`, `LLM_BASE_URL`, and "
        "`LLM_MODEL` (Umbrella maps host launch env into those public aliases), "
        "and to fail/skip/pause clearly when real LLM credentials are absent."
    )


def _phase_plan_llm_env_issues(plan: dict[str, Any]) -> list[str]:
    issue = _llm_env_contract_issue_from_text(
        "\n".join(_iter_plan_strings(plan)),
        subject="plan",
    )
    return [issue] if issue else []


def _plan_string_is_identifier_like(value: str) -> bool:
    text = str(value or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9_.:/\\-]{1,160}", text))


__all__ = [
    '_plan_item_has_success_test',
    '_iter_plan_child_dicts',
    '_iter_plan_subtask_leaves',
    '_iter_plan_subtasks',
    '_success_test_text_from_raw',
    '_bare_success_test_tool',
    '_plan_item_success_test_raw',
    '_plan_item_success_test_text',
    '_plan_item_non_success_context_text',
    '_llm_mock_success_test_issue',
    '_llm_error_as_success_issue',
    '_success_test_automation_issue',
    '_success_test_shape_issue',
    '_success_test_alias_shape_issue',
    '_command_quote_issue',
    '_iter_python_inline_snippets',
    '_python_inline_import_only_issue',
    '_python_inline_workspace_import_issue',
    '_python_inline_complexity_issue',
    '_python_inline_syntax_issue',
    '_python_test_module_invocation_issue',
    '_shell_command_segment_issue',
    '_split_shell_command_segments',
    '_has_background_shell_operator',
    '_command_portability_issue',
    '_phase_plan_success_test_issues',
    '_phase_plan_generic_success_test_issues',
    '_phase_plan_structure_issues',
    '_phase_plan_placeholder_issues',
    '_phase_plan_llm_fallback_issues',
    '_phase_plan_llm_test_double_issues',
    '_provider_default_match_is_protective',
    '_phase_plan_llm_provider_default_issues',
    '_phase_plan_empty_test_skeleton_issues',
    '_missing_llm_runtime_aliases',
    '_llm_env_contract_issue_from_text',
    '_phase_plan_llm_env_issues',
    '_plan_string_is_identifier_like',
]

# --- phase_contract_paths.py ---
def _iter_path_values(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, str):
        stripped = value.strip().strip("`'\"")
        if stripped:
            paths.append(stripped)
    elif isinstance(value, dict):
        for key in ("path", "file", "file_path", "name"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                paths.append(raw.strip().strip("`'\""))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_iter_path_values(item))
    return paths


def _normalise_plan_path(
    raw: str, *, workspace_root: pathlib.Path, workspace_id: str
) -> str:
    text = raw.replace("\\", "/").strip().strip("/").strip()
    if not text or text.startswith(("http://", "https://")):
        return ""
    if any(ch in text for ch in ("*", "?", "\n", "\r")):
        return ""
    if re.search(r"\s", text):
        return ""
    marker = f"workspaces/{workspace_id}/"
    if marker in text:
        text = text.split(marker, 1)[1]
    prefix = f"{workspace_id}/"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    try:
        path_obj = pathlib.Path(text)
        if path_obj.is_absolute():
            resolved = path_obj.resolve()
            try:
                return resolved.relative_to(workspace_root.resolve()).as_posix()
            except ValueError:
                return ""
    except OSError:
        return ""
    return text


def _plan_value_has_workspace_prefix(raw: str, workspace_id: str) -> bool:
    wid = str(workspace_id or "").strip().strip("/\\")
    if not wid:
        return False
    text = str(raw or "").replace("\\", "/").strip().strip("`'\"()[]{}").lstrip("./")
    if not text:
        return False
    lowered = text.lower()
    wid_l = wid.lower()
    return lowered == wid_l or lowered.startswith(f"{wid_l}/") or lowered.startswith(
        f"workspaces/{wid_l}/"
    )


def _phase_plan_workspace_prefix_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    workspace_id = _workspace_id(ctx) if ctx is not None else ""
    if not workspace_id:
        return []
    bad_paths: list[str] = []
    for _, _, raw in _iter_plan_file_field_refs(plan):
        if _plan_value_has_workspace_prefix(raw, workspace_id):
            bad_paths.append(raw)
    quoted = re.escape(workspace_id.strip("/\\"))
    cd_re = re.compile(
        rf"(?i)(?:^|[;&|]\s*)cd\s+[\"']?(?:\.?[\\/])?"
        rf"(?:workspaces[\\/])?{quoted}(?:[\\/]|[\"'\s;&|]|$)"
    )
    bad_commands = [
        text.strip()
        for text in _iter_plan_strings(plan)
        if cd_re.search(text)
    ]
    issues: list[str] = []
    if bad_paths:
        issues.append(
            "phase plans must use workspace-relative file paths, not paths "
            f"prefixed with the workspace id `{workspace_id}` or "
            f"`workspaces/{workspace_id}`; fix: {bad_paths[:8]}"
        )
    if bad_commands:
        issues.append(
            "phase success tests already run from the active workspace root; "
            f"do not `cd {workspace_id}` or `cd workspaces/{workspace_id}`. "
            "Use paths relative to the workspace root; offending command(s): "
            + ", ".join(bad_commands[:4])
        )
    return issues


def _iter_plan_file_field_refs(
    value: Any, *, path: str = "plan"
) -> list[tuple[str, str, str]]:
    refs: list[tuple[str, str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            lowered = str(key).lower()
            if lowered in _PLAN_FILE_FIELD_KEYS:
                for raw_path in _iter_path_values(child):
                    refs.append((child_path, lowered, raw_path))
            else:
                refs.extend(_iter_plan_file_field_refs(child, path=child_path))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            refs.extend(_iter_plan_file_field_refs(child, path=f"{path}[{idx}]"))
    return refs


def _phase_plan_file_reference_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    workspace_root = _workspace_root_for_policy(ctx)
    if workspace_root is None:
        return []
    workspace_id = _workspace_id(ctx) if ctx is not None else ""
    refs = _iter_plan_file_field_refs(plan)
    if not refs:
        return []
    existing_roots = _workspace_existing_impl_roots(ctx)
    existing_impl = bool(existing_roots)
    existing_roots_lc = {root.lower() for root in existing_roots}
    create_paths = {
        _normalise_plan_path(raw, workspace_root=workspace_root, workspace_id=workspace_id)
        for _, key, raw in refs
        if key in _PLAN_CREATE_FILE_KEYS
    }
    create_paths.discard("")
    issues: list[str] = []
    for field_path, key, raw in refs:
        rel = _normalise_plan_path(
            raw, workspace_root=workspace_root, workspace_id=workspace_id
        )
        if not rel:
            continue
        candidate = (workspace_root / rel).resolve()
        try:
            candidate.relative_to(workspace_root.resolve())
        except ValueError:
            continue
        exists = candidate.exists()
        if key in _PLAN_READ_FILE_KEYS and not exists:
            issues.append(
                f"plan field `{field_path}` references non-existent file `{raw}` "
                "as a file to read; run `list_files`/`read_file` and plan against "
                "the actual workspace layout"
            )
        elif (
            key in _PLAN_CHANGE_FILE_KEYS
            and existing_impl
            and not exists
            and rel not in create_paths
        ):
            issues.append(
                f"plan field `{field_path}` references non-existent file `{raw}` "
                "as an existing file to change/affect; use `files_to_create` for "
                "new files or correct the path from workspace inspection"
            )
        elif key in _PLAN_CREATE_FILE_KEYS and existing_impl and not exists:
            pure = pathlib.PurePosixPath(rel.replace("\\", "/"))
            parts = [part for part in pure.parts if part and part != "."]
            if not parts or pure.suffix.lower() != ".py":
                continue
            top = parts[0].lower()
            name = parts[-1].lower()
            if (
                top in existing_roots_lc
                or top in {"src", "tests", "test", "docs", "doc", "frontend"}
                or (len(parts) == 1 and name in _PLAN_GREENFIELD_ALLOWED_ROOT_PY)
            ):
                continue
            issues.append(
                f"plan field `{field_path}` creates new top-level Python path "
                f"`{raw}` while existing implementation root(s) "
                f"{sorted(existing_roots)} are present; put new modules under "
                "the existing root or `src/<package>/...`, or change an "
                "existing file instead of creating a parallel root"
            )
    return issues


_PLAN_ALLOWED_ENV_EXAMPLE_BASENAMES = {
    ".env.example",
    ".env.sample",
    ".env.template",
}
_PLAN_FORBIDDEN_SECRET_DIRS = {"secret", "secrets", "credential", "credentials"}
_PLAN_FORBIDDEN_CONTROL_DIRS = {".memory", ".umbrella", ".umbrella_scratch"}


def _phase_plan_forbidden_file_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    workspace_root = _workspace_root_for_policy(ctx)
    if workspace_root is None:
        return []
    workspace_id = _workspace_id(ctx) if ctx is not None else ""
    forbidden: list[str] = []
    for field_path, _key, raw in _iter_plan_file_field_refs(plan):
        rel = _normalise_plan_path(
            raw,
            workspace_root=workspace_root,
            workspace_id=workspace_id,
        )
        if not rel:
            continue
        parts = [
            part.lower()
            for part in pathlib.PurePosixPath(rel.replace("\\", "/")).parts
            if part and part != "."
        ]
        if not parts:
            continue
        basename = parts[-1]
        if parts[0] in _PLAN_FORBIDDEN_CONTROL_DIRS:
            forbidden.append(f"{field_path}: {raw}")
            continue
        if basename.startswith(".env") and basename not in _PLAN_ALLOWED_ENV_EXAMPLE_BASENAMES:
            forbidden.append(f"{field_path}: {raw}")
            continue
        if any(part in _PLAN_FORBIDDEN_SECRET_DIRS for part in parts[:-1]):
            forbidden.append(f"{field_path}: {raw}")
            continue
    if not forbidden:
        return []
    return [
        "phase plan references protected secret/env workspace path(s) or "
        "workspace/control path(s); do not "
        "create or modify `.memory`, `.umbrella`, real `.env` files, or "
        "secret/credential directories from generated workspace tasks. Use "
        "phase tools for memory/control-plane signals, documented env "
        "contracts, `.env.example`, or tests that inherit Umbrella runtime "
        "aliases instead. Offending path(s): "
        + ", ".join(forbidden[:8])
    ]


def _phase_plan_subtask_rebuild_existing_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    existing = _workspace_existing_impl_roots(ctx)
    if not existing:
        return []
    issues: list[str] = []
    for idx, subtask in enumerate(_iter_plan_subtasks(plan), start=1):
        subtask_text = "\n".join(_iter_plan_strings(subtask))
        if not _PLAN_REBUILD_EXISTING_RE.search(subtask_text):
            continue
        mentions_existing = any(root in subtask_text for root in existing)
        has_repair_language = bool(
            _PLAN_EXISTING_REPAIR_WORD_RE.search(subtask_text)
            or _PLAN_MIGRATION_WORD_RE.search(subtask_text)
        )
        if mentions_existing and has_repair_language:
            continue
        subtask_id = (
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or f"subtask #{idx}"
        )
        issues.append(
            "subtask "
            f"`{subtask_id}` proposes setup/scaffold/create-from-scratch work "
            f"while existing implementation root(s) {sorted(existing)} are "
            "already present; rewrite it as verify/repair/reuse/refactor work "
            "against the current codebase, or explicitly state a migration and "
            "cleanup/removal contract"
        )
    return issues


def _phase_plan_greenfield_layout_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    workspace_root = _workspace_root_for_policy(ctx)
    if workspace_root is None:
        return []
    from umbrella.workspace_registry.charter import load_workspace_charter

    charter = load_workspace_charter(workspace_root)
    policies = charter.get("policies") if isinstance(charter.get("policies"), dict) else {}
    if not policies.get("greenfield_python_src_layout"):
        return []
    if _workspace_existing_impl_roots(ctx):
        return []
    workspace_id = _workspace_id(ctx) if ctx is not None else ""
    subtasks = _iter_plan_subtasks(plan)
    plan_text = "\n".join(_iter_plan_strings(plan)).lower()

    refs = _iter_plan_file_field_refs(plan)
    paths: set[str] = set()
    for _, _, raw in refs:
        rel = _normalise_plan_path(
            raw, workspace_root=workspace_root, workspace_id=workspace_id
        )
        if rel:
            paths.add(rel.replace("\\", "/").strip("/"))
    for raw in _extract_plan_paths(plan):
        rel = _normalise_plan_path(
            raw, workspace_root=workspace_root, workspace_id=workspace_id
        )
        if rel:
            paths.add(rel.replace("\\", "/").strip("/"))

    code_paths = {p for p in paths if _path_looks_like_code(p)}
    if not code_paths:
        return []

    has_python = any(pathlib.PurePosixPath(p).suffix.lower() == ".py" for p in code_paths)
    has_frontend = any(
        pathlib.PurePosixPath(p).suffix.lower() in {".tsx", ".jsx", ".ts", ".js"}
        or p.startswith("frontend/")
        for p in code_paths
    )
    has_project_config = any(
        pathlib.PurePosixPath(p).name.lower() in {"pyproject.toml", "package.json"}
        for p in paths
    )
    has_agent_llm = bool(
        re.search(r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|bot)\b", plan_text)
    )
    complex_greenfield = (
        len(subtasks) >= 3
        or has_project_config
        or (has_python and has_frontend)
        or has_agent_llm
    )
    has_test_like_non_tests_path = any(
        pathlib.PurePosixPath(p).suffix.lower() == ".py"
        and not p.startswith(("tests/", "test/"))
        and re.search(
            r"(?:^|/)(?:test_|verify_|check_)|(?:_test|_verify|_e2e|_integration)\.py$",
            p,
        )
        for p in code_paths
    )
    if not complex_greenfield and not has_test_like_non_tests_path:
        return []

    issues: list[str] = []
    disallowed_python: list[str] = []
    disallowed_src_python: list[str] = []
    src_python_roots: set[str] = set()
    disallowed_python_tests: list[str] = []
    disallowed_python_scripts: list[str] = []
    disallowed_docs_python: list[str] = []
    for rel in sorted(code_paths):
        pure = pathlib.PurePosixPath(rel)
        if pure.suffix.lower() != ".py":
            continue
        parts = [part for part in pure.parts if part and part != "."]
        if not parts:
            continue
        lowered = [part.lower() for part in parts]
        top = lowered[0]
        name = lowered[-1]
        is_test_path = (
            name.startswith("test_")
            or name.endswith("_test.py")
            or name.startswith(("verify_", "check_"))
            or name.endswith(("_verify.py", "_e2e.py", "_integration.py"))
            or any(part in {"test", "tests"} for part in lowered)
        )
        if is_test_path and top not in {"tests", "test", "scripts"}:
            disallowed_python_tests.append(rel)
            continue
        if top == "src":
            if len(parts) < 3:
                disallowed_src_python.append(rel)
            else:
                src_python_roots.add(parts[1])
            continue
        if top in {"docs", "doc"}:
            disallowed_docs_python.append(rel)
            continue
        if top in {"tests", "test", "docs", "doc", "frontend"}:
            continue
        if top == "scripts" and _ROOT_PLAN_NOISE_RE.match(name):
            disallowed_python_scripts.append(rel)
            continue
        if name.startswith("test_") or "tests" in lowered or "test" in lowered:
            continue
        if len(parts) == 1 and name in _PLAN_GREENFIELD_ALLOWED_ROOT_PY:
            continue
        if top in _PLAN_NON_IMPL_ROOTS:
            continue
        disallowed_python.append(rel)

    if disallowed_python_tests:
        issues.append(
            "test-like Python modules under `src/` or docs are not "
            "production code; greenfield Python pytest/test modules must "
            "live under `tests/`; "
            "move "
            f"{disallowed_python_tests[:8]} under `tests/` or make them "
            "non-pytest verification scripts with non-test filenames"
        )

    if disallowed_src_python:
        issues.append(
            "greenfield Python application/library code must use a package "
            "inside `src/<package>/...`; move "
            f"{disallowed_src_python[:8]} under a real package directory such "
            "as `src/<package>/...`, not bare `src/*.py` or `src/__init__.py`"
        )

    if len(src_python_roots) > 1:
        issues.append(
            "greenfield Python application/library code under `src/` must use "
            "one canonical package root (`src/<package>/...`); found multiple "
            f"roots {sorted(src_python_roots)[:8]}. Move modules under one "
            "project package, for example `src/<package>/api/...` and "
            "`src/<package>/agents/...`."
        )

    if disallowed_python:
        issues.append(
            "greenfield Python application/library code must be planned under "
            "`src/<package>/...` instead of top-level package roots; move "
            f"{disallowed_python[:8]} under `src/` and keep tests under `tests/`"
        )

    if disallowed_python_scripts:
        issues.append(
            "greenfield Python verify/check/debug/probe helpers must not be "
            "planned under root `scripts/`; put reusable Python code under "
            "`src/<package>/...`, put pytest verification under `tests/`, or "
            "use a non-Python launch script only when it is a real deliverable. "
            f"Move or remove {disallowed_python_scripts[:8]}"
        )

    if disallowed_docs_python:
        issues.append(
            "Python files do not belong under `docs/`; keep documentation as "
            "Markdown/text and put executable verification under `tests/`, "
            "`scripts/` helpers, or `src/<package>/...` runtime code. "
            f"Move or remove {disallowed_docs_python[:8]}"
        )

    requires_docs = (
        (has_agent_llm and (len(subtasks) >= 4 or has_project_config))
        or (has_python and has_frontend)
        or len(subtasks) >= 6
    )
    has_docs = any(p.startswith("docs/") for p in paths)
    if requires_docs and not has_docs:
        issues.append(
            "complex greenfield/LLM project plans must include durable docs "
            "under `docs/` (for example `docs/architecture.md` or "
            "`docs/agent_topology.md`) instead of relying only on README notes"
        )
    return issues


def _subtask_has_file_contract(subtask: dict[str, Any]) -> bool:
    for key in _PLAN_CREATE_FILE_KEYS | _PLAN_CHANGE_FILE_KEYS:
        if key not in subtask:
            continue
        raw = subtask.get(key)
        if isinstance(raw, str) and raw.strip():
            return True
        if isinstance(raw, dict) and _iter_path_values(raw):
            return True
        if isinstance(raw, (list, tuple, set, frozenset)) and any(
            _iter_path_values(item) if isinstance(item, dict) else str(item or "").strip()
            for item in raw
        ):
            return True
    return False


def _phase_plan_missing_leaf_file_field_issues(plan: dict[str, Any]) -> list[str]:
    subtasks = _iter_plan_subtasks(plan)
    if not subtasks:
        return []
    plan_text = "\n".join(_iter_plan_strings(plan)).lower()
    has_agent_llm = bool(
        re.search(r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|bot)\b", plan_text)
    )
    complex_plan = len(subtasks) >= 6 or (has_agent_llm and len(subtasks) >= 4)
    if not complex_plan:
        return []
    missing: list[str] = []
    for idx, subtask in enumerate(subtasks, start=1):
        if not isinstance(subtask, dict) or not _plan_item_has_success_test(subtask):
            continue
        if _subtask_has_file_contract(subtask):
            continue
        subtask_id = str(
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or subtask.get("name")
            or f"subtask #{idx}"
        )
        missing.append(subtask_id)
    if not missing:
        return []
    return [
        "complex phase plan leaf subtask(s) missing `files_to_create`, "
        "`files_to_change`, or `files_affected`: " + ", ".join(missing[:10])
    ]


def _phase_plan_compactness_issues(plan: dict[str, Any]) -> list[str]:
    subtasks = _iter_plan_subtasks(plan)
    if len(subtasks) <= 16:
        return []
    plan_text = "\n".join(_iter_plan_strings(plan)).lower()
    looks_like_large_greenfield = bool(
        re.search(
            r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|frontend|backend|"
            r"websocket|fastapi|react|typescript|civilization|game)\b",
            plan_text,
        )
    )
    if not looks_like_large_greenfield:
        return []
    return [
        "phase plan has "
        f"{len(subtasks)} executable leaves; keep large greenfield Umbrella "
        "plans compact at roughly 8-16 leaves by grouping related work into "
        "vertical slices with one real typed proof each"
    ]


def _phase_plan_item_file_paths(
    ctx: ToolContext | None, item: dict[str, Any]
) -> set[str]:
    workspace_root = _workspace_root_for_policy(ctx)
    workspace_id = _workspace_id(ctx) if ctx is not None else ""
    paths: set[str] = set()
    for _, _, raw in _iter_plan_file_field_refs(item):
        if workspace_root is not None:
            rel = _normalise_plan_path(
                raw, workspace_root=workspace_root, workspace_id=workspace_id
            )
        else:
            rel = str(raw or "").replace("\\", "/").strip().strip("/").strip("`'\"")
        if rel:
            paths.add(rel.replace("\\", "/").strip("/"))
    return paths


def _phase_plan_broad_leaf_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    subtasks = _iter_plan_subtasks(plan)
    if len(subtasks) < 6:
        return []
    plan_text = "\n".join(_iter_plan_strings(plan)).lower()
    looks_like_large_greenfield = bool(
        re.search(
            r"\b(?:gmas|llm|multi[-\s]?agent|agent graph|frontend|backend|"
            r"websocket|fastapi|react|typescript|civilization|game)\b",
            plan_text,
        )
    )
    if not looks_like_large_greenfield:
        return []

    too_broad: list[str] = []
    for idx, subtask in enumerate(subtasks, start=1):
        label = " ".join(
            str(subtask.get(key) or "")
            for key in ("id", "subtask_id", "title", "name", "goal", "description", "mode")
        ).lower()
        if re.search(
            r"\b(?:setup|initiali[sz]e|scaffold|project structure|"
            r"documentation|docs|final|e2e|smoke|verification|launch)\b",
            label,
        ):
            continue
        paths = sorted(_phase_plan_item_file_paths(ctx, subtask))
        if len(paths) <= 4:
            continue
        code_paths = [path for path in paths if _path_looks_like_code(path)]
        if len(code_paths) <= 3:
            continue
        subtask_id = str(
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or subtask.get("name")
            or f"subtask #{idx}"
        )
        too_broad.append(f"{subtask_id} ({len(paths)} files)")

    if not too_broad:
        return []
    return [
        "phase plan has implementation subtask(s) that are too broad for a "
        "bounded Umbrella execute loop: "
        + ", ".join(too_broad[:8])
        + ". Split large greenfield/full-stack leaves into narrower vertical "
        "subtasks of about 2-4 files each, with one behavior-focused "
        "typed proof per leaf, instead of packing multiple domains or "
        "frontend/backend surfaces behind one pytest/build command."
    ]


def _phase_plan_policy_issues(
    plan: dict[str, Any], ctx: ToolContext | None = None, notes: str = ""
) -> list[str]:
    issues: list[str] = []
    issues.extend(_phase_plan_structure_issues(plan))
    issues.extend(_phase_plan_placeholder_issues(plan))
    issues.extend(_phase_plan_workspace_prefix_issues(ctx, plan))
    plan_with_notes = {"plan": plan, "notes": notes} if notes else plan
    issues.extend(_phase_plan_llm_fallback_issues(plan_with_notes))
    issues.extend(_phase_plan_llm_test_double_issues(plan_with_notes))
    issues.extend(_phase_plan_llm_env_issues(plan_with_notes))
    issues.extend(_phase_plan_llm_provider_default_issues(plan_with_notes))
    issues.extend(_phase_plan_empty_test_skeleton_issues(plan_with_notes))
    if ctx is not None:
        from umbrella.discovery.external_catalog import plan_external_memory_issues

        issues.extend(plan_external_memory_issues(plan, ctx))
        from umbrella.deep_agent_tools.phase_control_research import (
            _negative_claim_contradiction_issue,
        )

        rows = _tool_log_rows_for_task(ctx, str(getattr(ctx, "task_id", "") or ""))
        contradiction = _negative_claim_contradiction_issue(
            ctx,
            rows=rows,
            text=json.dumps(
                {"plan": plan, "notes": notes} if notes else plan,
                ensure_ascii=False,
            ),
            label="phase plan",
        )
        if contradiction:
            issues.append(contradiction.removeprefix("ERROR: ").strip())
    for text in _iter_plan_strings(plan):
        stripped = text.strip()
        normalised = stripped.replace("\\", "/")
        if (
            re.fullmatch(r"[\w./* -]+\.py", normalised)
            and "/" not in normalised.strip("./")
            and _ROOT_PLAN_NOISE_RE.match(pathlib.PurePosixPath(normalised).name)
        ):
            issues.append(
                f"root diagnostic/test file `{stripped}` must be under tests/ or removed"
            )
        if _PLAN_STUB_INTENT_RE.search(stripped):
            issues.append(
                "plan proposes stub/mock/placeholder implementation for required behavior"
            )
    known_tools = _known_phase_tool_names()
    for path, tool_name in _iter_declared_phase_tools(plan):
        if tool_name not in known_tools:
            issues.append(
                f"plan field `{path}` declares unknown phase tool `{tool_name}`; "
                "use phase-manifest tool names from `list_available_tools`"
            )
    issues.extend(_phase_plan_parallel_root_issues(ctx, plan))
    issues.extend(_phase_plan_rebuild_existing_issues(ctx, plan))
    issues.extend(_phase_plan_subtask_rebuild_existing_issues(ctx, plan))
    issues.extend(_phase_plan_forbidden_file_issues(ctx, plan))
    issues.extend(_phase_plan_file_reference_issues(ctx, plan))
    issues.extend(_phase_plan_greenfield_layout_issues(ctx, plan))
    issues.extend(_phase_plan_missing_leaf_file_field_issues(plan))
    issues.extend(_phase_plan_compactness_issues(plan))
    issues.extend(_phase_plan_broad_leaf_issues(ctx, plan))
    issues.extend(_phase_plan_success_test_issues(plan))
    issues.extend(_phase_plan_generic_success_test_issues(plan))
    issues.extend(_phase_plan_revision_contract_issues(ctx, plan))
    return list(dict.fromkeys(issues))


__all__ = [
    '_iter_path_values',
    '_normalise_plan_path',
    '_plan_value_has_workspace_prefix',
    '_phase_plan_workspace_prefix_issues',
    '_iter_plan_file_field_refs',
    '_phase_plan_file_reference_issues',
    '_phase_plan_forbidden_file_issues',
    '_phase_plan_subtask_rebuild_existing_issues',
    '_phase_plan_greenfield_layout_issues',
    '_subtask_has_file_contract',
    '_phase_plan_missing_leaf_file_field_issues',
    '_phase_plan_compactness_issues',
    '_phase_plan_broad_leaf_issues',
    '_phase_plan_policy_issues',
]

# --- phase_contract_revisions.py ---
def _phase_plan_required_changes(ctx: ToolContext | None) -> list[dict[str, Any]]:
    if ctx is None:
        return []
    overlays = getattr(ctx, "context_overlays", {}) or {}
    phase_node = overlays.get("phase_node") if isinstance(overlays, dict) else None
    overlay = phase_node.get("overlay") if isinstance(phase_node, dict) else None
    if not isinstance(overlay, dict):
        return []
    contract = overlay.get("revision_contract")
    if not isinstance(contract, dict):
        return []
    raw = contract.get("required_plan_changes") or ()
    changes: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            changes.append(item)
    return changes


def _subtask_payload_by_id(plan: dict[str, Any], subtask_id: str) -> dict[str, Any] | None:
    target = _revision_subtask_aliases(subtask_id)
    for subtask in _iter_plan_subtasks(plan):
        sid = str(
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("name")
            or ""
        )
        if _revision_subtask_aliases(sid) & target:
            return subtask
    return None


def _typed_revision_compliance_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    if _umbrella_phase_id(ctx) != "plan":
        return []
    issues: list[str] = []
    for change in _phase_plan_required_changes(ctx):
        subtask_id = str(change.get("subtask_id") or "").strip()
        field = str(change.get("field") or "").strip()
        action = str(change.get("action") or "set").strip().lower()
        if not subtask_id or not field:
            continue
        subtask = _subtask_payload_by_id(plan, subtask_id)
        if subtask is None:
            issues.append(
                f"required_plan_change targets missing subtask `{subtask_id}`"
            )
            continue
        blob = json.dumps(subtask, ensure_ascii=False).lower()
        if action in {"remove", "delete"}:
            if field.lower() in blob:
                issues.append(
                    f"required_plan_change expected `{field}` removed from `{subtask_id}`"
                )
            continue
        if field.lower() not in blob:
            issues.append(
                f"required_plan_change field `{field}` not reflected in subtask `{subtask_id}`"
            )
    return issues


def _phase_plan_revision_items(ctx: ToolContext | None) -> list[str]:
    if ctx is None:
        return []
    overlays = getattr(ctx, "context_overlays", {}) or {}
    phase_node = overlays.get("phase_node") if isinstance(overlays, dict) else None
    overlay = phase_node.get("overlay") if isinstance(phase_node, dict) else None
    if not isinstance(overlay, dict):
        return []
    contract = overlay.get("revision_contract")
    if isinstance(contract, dict):
        revisions = contract.get("revisions")
        if isinstance(revisions, list):
            return list(
                dict.fromkeys(str(item).strip() for item in revisions if str(item).strip())
            )
        return []
    reason = str(overlay.get("retry_reason") or "").strip()
    if not reason.lower().startswith("micro review requested revisions"):
        return []
    _, _, details = reason.partition(":")
    return list(
        dict.fromkeys(
            part.strip(" .")
            for part in re.split(r";|\n", details)
            if part.strip(" .")
        )
    )


def _revision_positive_clause(revision: str) -> str:
    text = str(revision or "")
    lower = text.lower()
    if "->" in text:
        text = text.rsplit("->", 1)[1]
        lower = text.lower()
    elif re.search(r"\breplace\b", lower) and " with " in lower:
        start = lower.rfind(" with ") + len(" with ")
        text = text[start:]
        lower = text.lower()
    elif re.search(r"\bremove\b", lower) and re.search(r"\buse\b", lower):
        use_match = list(re.finditer(r"\buse\b", lower))[-1]
        text = text[use_match.end() :]
        lower = text.lower()
    for marker in (" instead of ", " rather than "):
        pos = lower.find(marker)
        if pos >= 0:
            text = text[:pos]
            lower = text.lower()
    optional_parenthetical = re.search(
        r"(?i)\(\s*or\s+(?:add|create|move|note|provide|use)\b", text
    )
    if optional_parenthetical:
        optional_end = text.find(")", optional_parenthetical.start())
        if optional_end >= 0:
            text = (
                text[: optional_parenthetical.start()]
                + " "
                + text[optional_end + 1 :]
            )
        else:
            text = text[: optional_parenthetical.start()]
        lower = text.lower()
    for marker in (
        " or equivalent",
        " or provide ",
        " or use ",
        " or note ",
        " or create ",
    ):
        pos = lower.find(marker)
        if pos >= 0:
            text = text[:pos]
            lower = text.lower()
    return text.replace("`", " ").replace('"', " ").replace("'", " ")


def _revision_keywords(text: str) -> list[str]:
    tokens = [
        match.group(0).lower()
        for match in re.finditer(r"[a-z0-9_]+|[а-яё0-9_]+", str(text or "").lower())
    ]
    keywords: list[str] = []
    for token in tokens:
        if len(token) < 3:
            continue
        if token in _REVISION_STOP_WORDS:
            continue
        if token.startswith("subtask_") or token.startswith("subtask-"):
            continue
        if token.isdigit():
            continue
        if token not in keywords:
            keywords.append(token)
    return keywords


_REVISION_SUBTASK_RE = re.compile(
    r"(?is)\b(?:subtasks?|subtask|st|task)(?:[_-]|\s+)?"
    r"0*\d+(?:\.\d+)?[a-z0-9_]*(?:\s*-\s*0*\d+(?:\.\d+)?)?\b"
)
_REVISION_PHASE_REF_RE = re.compile(r"(?is)\bphase\s+0*\d+\b")
_REVISION_BUDGET_AMOUNT_RE = re.compile(r"(?is)\b\d+(?:\.\d+)?\s*(?:h|hr|hour|hours|min|minutes)\b")
_REVISION_ILLUSTRATIVE_NUMBER_EXAMPLES_RE = re.compile(
    r"(?is)\b(?:react|python|node|vite|fastapi)\s+\d+(?:\.\d+)?\b"
)
_REVISION_RENAME_RE = re.compile(
    r"(?is)\brename\s+(?P<old>(?:subtask|st|task)[\s_-]*\d+[a-z0-9_]*)"
    r"\s+(?:to|as)\s+(?P<new>[a-z0-9_.-]+)"
)


def _normalise_revision_subtask_ref(value: str) -> str:
    text = re.sub(r"\s+", "_", str(value or "").strip().lower()).replace("-", "_")
    text = re.sub(r"^subtasks?_", "subtask_", text)
    text = re.sub(r"^(?:st|task)_", "subtask_", text)
    if re.fullmatch(r"0*\d+", text):
        return f"subtask_{int(text)}"
    if re.fullmatch(r"0*\d+(?:\.\d+)+", text):
        return ".".join(str(int(part)) for part in text.split("."))
    bare_match = re.match(r"0*(\d+(?:\.\d+)?)([a-z][a-z0-9_]*)$", text)
    if bare_match:
        number = ".".join(str(int(part)) for part in bare_match.group(1).split("."))
        suffix = str(bare_match.group(2) or "").strip("_")
        return f"subtask_{number}" + (f"_{suffix}" if suffix else "")
    match = re.match(r"subtask_?0*(\d+(?:\.\d+)?)([a-z0-9_]*)$", text)
    if not match:
        return text
    number = ".".join(str(int(part)) for part in match.group(1).split("."))
    suffix = str(match.group(2) or "").strip("_")
    if "." in number and not suffix:
        return number
    return f"subtask_{number}" + (f"_{suffix}" if suffix else "")


def _normalise_decimal_revision_number(raw: str) -> str:
    text = str(raw or "").strip().lower().replace("_", ".").replace("-", ".")
    match = re.match(r"0*(\d+(?:\.\d+)*)", text)
    if not match:
        return ""
    return ".".join(str(int(part)) for part in match.group(1).split(".") if part)


def _revision_decimal_aliases(value: str) -> set[str]:
    aliases: set[str] = set()
    text = str(value or "").strip().lower()
    for candidate in {text, text.replace("_", "."), text.replace("-", ".")}:
        for match in re.finditer(r"(?:subtask[_.-]?)?0*(\d+(?:[._-]\d+)+)", candidate):
            number = _normalise_decimal_revision_number(match.group(1))
            if not number:
                continue
            aliases.add(number)
            aliases.add(f"subtask_{number}")
            underscored = number.replace(".", "_")
            dashed = number.replace(".", "-")
            aliases.add(underscored)
            aliases.add(f"subtask_{underscored}")
            aliases.add(dashed)
            aliases.add(f"subtask_{dashed}")
            root = number.split(".", 1)[0]
            aliases.add(root)
            aliases.add(f"subtask_{root}")
    return aliases


def _revision_subtask_aliases(value: str) -> set[str]:
    normalised = _normalise_revision_subtask_ref(value)
    raw = str(value or "").strip().lower().replace(" ", "_")
    aliases = {normalised}
    if raw:
        aliases.add(raw)
        aliases.add(raw.replace("-", "_"))
        aliases.add(raw.replace("_", "-"))
    aliases.update(_revision_decimal_aliases(value))
    aliases.update(_revision_decimal_aliases(normalised))
    if normalised.startswith("subtask_"):
        bare = normalised.removeprefix("subtask_")
        aliases.add(bare)
        compact = bare.replace("_", "")
        dashed = bare.replace("_", "-")
        aliases.update({compact, dashed, f"st_{compact}", f"st-{compact}"})
        aliases.update({f"subtask_{compact}", f"subtask-{compact}"})
        aliases.update(
            {
                f"st_{bare}",
                f"st-{dashed}",
                f"task_{bare}",
                f"task-{dashed}",
            }
        )
        bare_lead = re.match(r"0*(\d+)(?:[_.-].*)?$", bare)
        if bare_lead:
            number = str(int(bare_lead.group(1)))
            aliases.add(number)
            aliases.add(f"subtask_{number}")
    else:
        aliases.add(f"subtask_{normalised}")
    lead = re.match(r"0*(\d+)(?:[_.-].*)?$", normalised)
    if lead:
        number = str(int(lead.group(1)))
        aliases.add(number)
        aliases.add(f"subtask_{number}")
    return aliases


def _revision_subtask_ref_numbers(value: str) -> list[str]:
    text = str(value or "")
    text = re.sub(r"(?i)\bsubtasks?(?:[_-]|\s+)?", "", text, count=1)
    numbers: list[str] = []
    for match in re.finditer(r"\b0*\d+(?:\.\d+)?[a-z0-9_]*\b", text):
        number = _normalise_decimal_revision_number(match.group(0))
        if number not in numbers:
            numbers.append(number)
    return numbers


def _revision_number_tokens(text: str) -> list[str]:
    values: list[str] = []
    for match in _REVISION_SUBTASK_RE.finditer(str(text or "")):
        for token in _revision_subtask_ref_numbers(match.group(0)):
            if token not in values:
                values.append(token)
    for match in re.finditer(r"\b0*\d+(?:\.\d+)+[a-z_][a-z0-9_]*\b", str(text or "")):
        token = _normalise_decimal_revision_number(match.group(0))
        if token and token not in values:
            values.append(token)
    for match in re.finditer(r"\b\d+(?:\.\d+)?\b", str(text or "")):
        raw = match.group(0)
        try:
            number = float(raw)
        except ValueError:
            token = raw
        else:
            token = str(int(number)) if number.is_integer() else str(number)
        if token not in values:
            values.append(token)
    return values


def _revision_number_tokens_without_examples(text: str) -> list[str]:
    cleaned = _REVISION_ILLUSTRATIVE_NUMBER_EXAMPLES_RE.sub("", str(text or ""))
    return _revision_number_tokens(cleaned)


def _revision_semantic_number_tokens(text: str) -> list[str]:
    cleaned = _REVISION_ILLUSTRATIVE_NUMBER_EXAMPLES_RE.sub("", str(text or ""))
    cleaned = _REVISION_BUDGET_AMOUNT_RE.sub(" ", cleaned)
    cleaned = _REVISION_PHASE_REF_RE.sub(" ", cleaned)
    cleaned = _REVISION_SUBTASK_RE.sub(" ", cleaned)
    return _revision_number_tokens(cleaned)


def _revision_number_present(
    required: str,
    present_numbers: set[str],
    positive_clause: str,
) -> bool:
    if required in present_numbers:
        return True
    prefix = f"{required}."
    if any(number.startswith(prefix) for number in present_numbers):
        return True
    if "." in required:
        return False
    if not re.search(rf"(?i)\bphase\s+{re.escape(required)}\b", positive_clause):
        return False
    return any(number.startswith(prefix) for number in present_numbers)


def _revision_is_meta_test_strategy_instruction(revision: str) -> bool:
    text = str(revision or "").lower()
    return (
        "test strategy section" in text
        or "convert high-level statements to concrete executable commands" in text
    )


def _revision_is_optional_instruction(revision: str) -> bool:
    text = str(revision or "").strip().lower()
    return bool(
        re.match(
            r"^(?:optional|polish|nice[-\s]?to[-\s]?have|non[-\s]?blocking|consider|could|may)\b",
            text,
        )
    )


def _revision_is_non_actionable_budget_comment(revision: str) -> bool:
    text = str(revision or "").strip().lower()
    if not re.search(r"(?:[$€£]|\bbudget\b|\busd\b|\bdollars?\b|\bresources?\b)", text):
        return False
    if not re.search(
        r"\b(insufficient|too\s+low|not\s+enough|requires?\s+more\s+resources?|"
        r"cannot\s+realistically|can't\s+realistically|misaligned\s+with\s+scope)\b",
        text,
    ):
        return False
    return not bool(
        re.search(
            r"\b(reduce|increase|set|cap|limit|track|add|remove|change|split|"
            r"scope\s+down|document|allocate|implement|create)\b",
            text,
        )
    )


def _revision_is_success_test_quality_instruction(revision: str) -> bool:
    text = str(revision or "").lower()
    return (
        (
            "success test" in text
            and (
                "file existence" in text
                or "verify behavior" in text
                or "verifies behavior" in text
                or "not just file" in text
            )
        )
        or (
            "success test" in text
            and (
                "cross-platform" in text
                or "cross platform" in text
                or "platform-appropriate" in text
                or "portable" in text
                or "non-portable" in text
                or "portability" in text
                or "python -c" in text
                or "checked-in" in text
                or "checked in" in text
            )
        )
        or (
            "test creation" in text
            and "validation" in text
            and ("split" in text or "separate" in text)
        )
        or (
            "create test" in text
            and "validate" in text
            and ("split" in text or "separate" in text)
        )
        or (
            "empty" in text
            and (
                "test" in text
                or "tests" in text
                or "assertion" in text
                or "assertions" in text
            )
        )
        or "passwithnotests" in text
        or "allowempty" in text
        or "allow no tests" in text
        or (
            "functional tests" in text
            and (
                "assertion" in text
                or "assertions" in text
                or "real assertions" in text
            )
        )
        or (
            "file existence" in text
            or "verify behavior" in text
            or "verifies behavior" in text
            or "not just file" in text
        )
    )


def _revision_rename_issue(plan: dict[str, Any], revision: str) -> str | None:
    match = _REVISION_RENAME_RE.search(str(revision or ""))
    if not match:
        return None
    old_name = match.group(1).strip("`\"'").lower()
    new_name = match.group(2).strip("`\"'").lower()
    plan_text = json.dumps(plan, ensure_ascii=False).lower()
    if new_name not in plan_text:
        return (
            "review rename revision appears unaddressed: "
            f"`{revision}`; missing renamed target `{new_name}`"
        )
    if old_name in plan_text:
        return (
            "review rename revision appears unaddressed: "
            f"`{revision}`; old target `{old_name}` is still present"
        )
    return ""


def _expand_revision_range(start: str, end: str) -> list[str]:
    start_number = _normalise_decimal_revision_number(start)
    end_number = _normalise_decimal_revision_number(end)
    if not start_number or not end_number:
        return []
    start_parts = start_number.split(".")
    end_parts = end_number.split(".")
    if len(start_parts) != len(end_parts) or start_parts[:-1] != end_parts[:-1]:
        return [start_number, end_number]
    try:
        first = int(start_parts[-1])
        last = int(end_parts[-1])
    except ValueError:
        return [start_number, end_number]
    if first > last or last - first > 50:
        return [start_number, end_number]
    prefix = ".".join(start_parts[:-1])
    return [
        f"{prefix}.{idx}" if prefix else str(idx)
        for idx in range(first, last + 1)
    ]


def _revision_target_ids(revision: str) -> list[str]:
    target_ids: list[str] = []

    def add(value: str) -> None:
        target_id = _normalise_revision_subtask_ref(value)
        if target_id and target_id not in target_ids:
            target_ids.append(target_id)

    text = str(revision or "")
    if re.search(r"(?is)\badd\b.{0,160}\bafter\s+subtasks?\b", text):
        return []
    for phase_match in re.finditer(
        r"(?i)\ball\s+phase\s+0*(\d+)\s+subtasks?\b", text
    ):
        add(phase_match.group(1))
    for match in _REVISION_SUBTASK_RE.finditer(text):
        raw_ref = match.group(0)
        range_match = re.search(
            r"(?is)\b(?:subtasks?|st|task)(?:[_-]|\s+)?"
            r"(0*\d+(?:\.\d+)?)\s*-\s*(0*\d+(?:\.\d+)?)\b",
            raw_ref,
        )
        if range_match:
            for number in _expand_revision_range(
                range_match.group(1), range_match.group(2)
            ):
                add(number)
            continue
        add(raw_ref)
    for match in re.finditer(r"(?is)\bsubtasks?\b(?P<body>.{0,260})", text):
        body = re.split(r"\s+-\s+|;|\n", match.group("body"), maxsplit=1)[0]
        if not re.match(
            r"(?is)^\s*(?::|,|\(|\)|\[|\]|\s)*(?:0*\d|subtasks?\b|st\b|task\b)",
            body,
        ):
            continue
        for range_match in re.finditer(
            r"\b(0*\d+(?:\.\d+)?)\s*-\s*(0*\d+(?:\.\d+)?)\b",
            body,
        ):
            for number in _expand_revision_range(range_match.group(1), range_match.group(2)):
                add(number)
        for ref_match in re.finditer(
            r"\b(?:subtasks?|st|task)(?:[_-]|\s+)?0*\d+(?:\.\d+)?[a-z0-9_]*\b",
            body,
            re.IGNORECASE,
        ):
            add(ref_match.group(0))
        for ref_match in re.finditer(r"\b0*\d+(?:\.\d+)?[a-z0-9_]*\b", body):
            add(ref_match.group(0))
    return target_ids


def _plan_text_for_revision_target(plan: dict[str, Any], revision: str) -> tuple[str, str]:
    target_ids = _revision_target_ids(revision)
    target_aliases = [_revision_subtask_aliases(target_id) for target_id in target_ids]
    if not target_ids:
        return json.dumps(plan, ensure_ascii=False), ""
    target_texts: list[str] = []
    matched_ids: list[str] = []
    subtasks = _iter_plan_subtasks(plan)
    for subtask in _iter_plan_subtasks(plan):
        subtask_id = str(
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("name")
            or ""
        )
        aliases = _revision_subtask_aliases(subtask_id)
        if any(aliases & target for target in target_aliases):
            matched = next(
                target_id for target_id, target in zip(target_ids, target_aliases) if aliases & target
            )
            target_texts.append(json.dumps(subtask, ensure_ascii=False))
            if matched not in matched_ids:
                matched_ids.append(matched)
    if target_texts:
        return "\n".join(target_texts), ", ".join(matched_ids)
    for target_id in target_ids:
        if not re.fullmatch(r"(?:subtask_)?\d+", target_id):
            continue
        prefix = target_id.removeprefix("subtask_") + "."
        coarse_texts: list[str] = []
        for subtask in subtasks:
            subtask_id = str(
                subtask.get("id")
                or subtask.get("subtask_id")
                or subtask.get("name")
                or ""
            )
            aliases = _revision_subtask_aliases(subtask_id)
            if any(alias.startswith(prefix) for alias in aliases):
                coarse_texts.append(json.dumps(subtask, ensure_ascii=False))
        if coarse_texts:
            return "\n".join(coarse_texts), target_id
    return "", target_ids[0]


def _phase_plan_revision_contract_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    if _umbrella_phase_id(ctx) != "plan":
        return []
    typed_issues = _typed_revision_compliance_issues(ctx, plan)
    if typed_issues:
        return typed_issues
    issues: list[str] = []
    for revision in _phase_plan_revision_items(ctx):
        rename_issue = _revision_rename_issue(plan, revision)
        if rename_issue is not None:
            if rename_issue:
                issues.append(rename_issue)
            continue
        if _revision_is_optional_instruction(revision):
            continue
        if _revision_is_non_actionable_budget_comment(revision):
            continue
        if _revision_is_meta_test_strategy_instruction(
            revision
        ) or _revision_is_success_test_quality_instruction(revision):
            continue
        positive_clause = _revision_positive_clause(revision)
        keywords = _revision_keywords(positive_clause)
        if not keywords:
            continue
        target_text, target_id = _plan_text_for_revision_target(plan, revision)
        if target_id and not target_text:
            issues.append(
                f"review revision targets `{target_id}` but the new phase plan has no matching subtask"
            )
            continue
        required_numbers = _revision_semantic_number_tokens(positive_clause)
        if required_numbers:
            present_numbers = set(_revision_number_tokens(target_text))
            missing_numbers = [
                number
                for number in required_numbers
                if not _revision_number_present(
                    number,
                    present_numbers,
                    positive_clause,
                )
            ]
            if missing_numbers:
                target_hint = f" in `{target_id}`" if target_id else ""
                issues.append(
                    "review revision numeric requirement appears unaddressed"
                    f"{target_hint}: `{revision}`; missing number(s): "
                    + ", ".join(missing_numbers[:8])
                )
                continue
        haystack = set(_revision_keywords(target_text))
        covered = [keyword for keyword in keywords if keyword in haystack]
        required = min(len(keywords), min(4, max(2, (len(keywords) + 1) // 2)))
        if len(covered) >= required:
            continue
        missing = [keyword for keyword in keywords if keyword not in haystack]
        target_hint = f" in `{target_id}`" if target_id else ""
        issues.append(
            "review revision appears unaddressed"
            f"{target_hint}: `{revision}`; missing keyword(s): "
            + ", ".join(missing[:8])
        )
    return issues


__all__ = [
    '_phase_plan_revision_items',
    '_revision_positive_clause',
    '_revision_keywords',
    '_normalise_revision_subtask_ref',
    '_normalise_decimal_revision_number',
    '_revision_decimal_aliases',
    '_revision_subtask_aliases',
    '_revision_subtask_ref_numbers',
    '_revision_number_tokens',
    '_revision_number_tokens_without_examples',
    '_revision_semantic_number_tokens',
    '_revision_number_present',
    '_revision_is_meta_test_strategy_instruction',
    '_revision_is_optional_instruction',
    '_revision_is_non_actionable_budget_comment',
    '_revision_is_success_test_quality_instruction',
    '_revision_rename_issue',
    '_expand_revision_range',
    '_revision_target_ids',
    '_plan_text_for_revision_target',
    '_phase_plan_revision_contract_issues',
]
