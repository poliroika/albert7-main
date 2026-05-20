"""Success-test and LLM-contract policy checks for phase plans."""

from umbrella.deep_agent_tools.phase_contract_common import *
from umbrella.deep_agent_tools.phase_contract_base import *
from umbrella.deep_agent_tools.phase_contract_declarations import *
from umbrella.deep_agent_tools.evidence_graph import (
    phase_plan_pytest_target_availability_messages,
)
from umbrella.deep_agent_tools.domain_policy import unsupported_llm_env_alias_issues

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
    portability_issue = _command_portability_issue(raw_text)
    if portability_issue:
        return portability_issue
    if _SUCCESS_TEST_FAILURE_MASK_RE.search(raw_text):
        return (
            "success test masks command failure with `||`, `|| true`, "
            "`|| exit 0`, or another alternate success branch; use one proof "
            "command that fails when the checked behavior is broken"
        )
    localhost_issue = _unmanaged_localhost_success_test_issue(raw_text)
    if localhost_issue:
        return localhost_issue
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
    if _PYTEST_CD_SRC_SUCCESS_TEST_RE.search(raw_text):
        return (
            "success test changes into source root `src` before running pytest; "
            "greenfield tests must live under workspace-level `tests/`, so run "
            "`python -m pytest tests/test_x.py -q` from the workspace root "
            "instead of `cd src && ...`"
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
    shell_segment_issue = _shell_command_segment_issue(raw_text)
    if shell_segment_issue:
        return shell_segment_issue
    if _GENERIC_SUCCESS_TOOL_WITH_ARGS_RE.search(raw_text):
        return (
            "success test uses a generic Umbrella tool name with pseudo-arguments; "
            "use the bare tool only for final gates or write the exact underlying "
            "command such as `python -m pytest ... -q`, `npm test`, or a checked-in "
            "verification script"
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
        r"npm\s+run\s+build|pnpm|yarn|playwright|browser)\b",
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


def _python_inline_docs_content_issue(tree: ast.AST) -> str:
    if not isinstance(tree, ast.Module):
        return ""

    def is_docs_content_path(value: str) -> bool:
        path = str(value or "").strip().replace("\\", "/").lstrip("./").lower()
        if path in {"readme.md", "readme.rst", "readme.txt"}:
            return True
        return path.startswith("docs/") and path.endswith((".md", ".rst", ".txt"))

    has_docs_path = any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and is_docs_content_path(node.value)
        for node in ast.walk(tree)
    )
    if not has_docs_path:
        return ""

    has_file_read = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            has_file_read = True
            break
        if isinstance(func, ast.Attribute) and func.attr in {"read", "read_text"}:
            has_file_read = True
            break
    if not has_file_read:
        return ""

    return (
        "success test `python -c` checks generated documentation/content inline; "
        "put docs/content assertions in a checked-in pytest or verification "
        "script and use that command as success_test"
    )


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
        docs_content_issue = _python_inline_docs_content_issue(tree)
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
    segments = _split_shell_command_segments(str(value or ""))
    for segment in segments:
        stripped = segment.strip()
        if not stripped:
            continue
        if re.match(r"(?i)^(?:echo|printf|write-host)\b", stripped):
            return (
                "success test appends a decorative shell output command "
                "(`echo`/`printf`/`Write-Host`) instead of executable proof; "
                "remove the status banner or put the real behavior in a "
                "checked-in test/verification script"
            )
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


_E2E_PYTEST_TARGET_CONTEXT_RE = re.compile(
    r"(?i)\b(e2e|end[-\s]?to[-\s]?end|localhost|127\.0\.0\.1|"
    r"full[-\s]?game|browser|playwright|selenium|smoke)\b|"
    r"брауз|локалхост|подними"
)
_PYTEST_COMMAND_SEGMENT_RE = re.compile(
    r"(?i)\b(?:python\s+-m\s+pytest|pytest)\b(?P<args>.*)$"
)
_FRONTEND_TEST_CWD_RE = re.compile(r"(?i)(?:^|[;&|]\s*)cd\s+frontend\b")
_JS_TEST_COMMAND_SEGMENT_RE = re.compile(
    r"(?i)\b(?:npm|pnpm|yarn|npx|vitest|jest)\b(?P<args>.*)$"
)
_JS_TEST_FILE_TOKEN_RE = re.compile(
    r"(?:^|/)[^/\s]+\.(?:test|spec)\.(?:[cm]?[jt]sx?)$|"
    r"^[^/\s]+\.(?:test|spec)\.(?:[cm]?[jt]sx?)$",
    re.IGNORECASE,
)
_FRONTEND_BUILD_COMMAND_RE = re.compile(
    r"(?is)(?:^|[;&|]\s*)cd\s+frontend\b[^;&|]*"
    r"(?:&&|;|\|\|)?[^;&|]*\b(?:npm|pnpm|yarn)\s+(?:run\s+)?build\b|"
    r"\b(?:vite|tsc)\s+(?:build\b|--build\b)"
)
_FRONTEND_SOURCE_RE = re.compile(
    r"(?i)^frontend/src/.+\.(?:[cm]?[jt]sx?|css|scss|sass)$"
)
_FRONTEND_SCRIPT_SOURCE_RE = re.compile(
    r"(?i)^frontend/src/.+\.(?:[cm]?[jt]sx?)$"
)
_FRONTEND_VITE_CONFIG_RE = re.compile(r"(?i)^frontend/vite\.config\.[cm]?[jt]s$")
_DIRECT_LOCALHOST_HTTP_RE = re.compile(
    r"(?i)\b(?:curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod)\b"
    r"(?=[^;&|\n]*\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b)"
)
_MANAGED_LOCALHOST_PROOF_RE = re.compile(
    r"(?i)\b(?:http_boot|behavioral_http|run_real_e2e|"
    r"playwright|selenium|python\s+-m\s+pytest|pytest|"
    r"npx\s+playwright|npm\s+(?:run\s+)?(?:e2e|test))\b"
)


def _pytest_file_targets_from_success_test(value: str) -> list[str]:
    targets: list[str] = []
    for segment in _split_shell_command_segments(str(value or "")):
        match = _PYTEST_COMMAND_SEGMENT_RE.search(segment)
        if not match:
            continue
        for token in re.split(r"\s+", match.group("args").strip()):
            cleaned = token.strip().strip("`'\"()[]{}.,;")
            if not cleaned or cleaned.startswith("-"):
                continue
            cleaned = cleaned.split("::", 1)[0].replace("\\", "/").lstrip("./")
            if cleaned.startswith("tests/") and cleaned.endswith(".py"):
                targets.append(cleaned)
    return list(dict.fromkeys(targets))


def _js_test_file_targets_from_success_test(value: str) -> list[str]:
    targets: list[str] = []
    for segment in _split_shell_command_segments(str(value or "")):
        match = _JS_TEST_COMMAND_SEGMENT_RE.search(segment)
        if not match:
            continue
        if not re.search(r"(?i)\b(?:test|vitest|jest)\b", segment):
            continue
        for token in re.split(r"\s+", match.group("args").strip()):
            cleaned = token.strip().strip("`'\"()[]{}.,;").replace("\\", "/")
            if not cleaned or cleaned.startswith("-"):
                continue
            cleaned = cleaned.split("::", 1)[0].lstrip("./")
            if _JS_TEST_FILE_TOKEN_RE.search(cleaned):
                targets.append(cleaned)
    return list(dict.fromkeys(targets))


def _iter_declared_plan_path_strings(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _PLAN_FILE_FIELD_KEYS:
                yield from _iter_declared_plan_path_strings(child)
            else:
                yield from _iter_declared_plan_path_strings(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_declared_plan_path_strings(item)
    elif isinstance(value, str):
        text = value.strip().strip("`'\"").replace("\\", "/").lstrip("./")
        if text:
            yield text


def _phase_plan_e2e_pytest_target_issues(
    plan: dict[str, Any], ctx: ToolContext | None = None
) -> list[str]:
    declared_paths = {
        path for path in _iter_declared_plan_path_strings(plan) if path.startswith("tests/")
    }
    workspace_root = _workspace_root_for_policy(ctx)
    issues: list[str] = []
    for idx, subtask in enumerate(_iter_plan_subtasks(plan), start=1):
        success_text = _plan_item_success_test_text(subtask)
        targets = _pytest_file_targets_from_success_test(success_text)
        if not targets:
            continue
        context = _plan_item_non_success_context_text(subtask)
        if not _E2E_PYTEST_TARGET_CONTEXT_RE.search(
            "\n".join(part for part in (context, success_text) if part)
        ):
            continue
        missing: list[str] = []
        for target in targets:
            if target in declared_paths:
                continue
            if workspace_root is not None and (workspace_root / target).is_file():
                continue
            missing.append(target)
        if not missing:
            continue
        subtask_id = (
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or f"subtask #{idx}"
        )
        issues.append(
            f"subtask `{subtask_id}` success_test targets pytest file(s) "
            f"{missing[:4]} for an e2e/localhost proof, but no plan leaf "
            "declares those tests in files_to_create/files_to_change/"
            "files_affected. Add the checked-in test file to the plan leaf "
            "that owns the proof instead of relying on an implicit test."
        )
    return issues


def _phase_plan_frontend_test_target_issues(
    plan: dict[str, Any], ctx: ToolContext | None = None
) -> list[str]:
    declared_paths = set(_iter_declared_plan_path_strings(plan))
    declared_by_name: dict[str, set[str]] = {}
    for path in declared_paths:
        if re.search(r"(?i)\.(?:test|spec)\.(?:[cm]?[jt]sx?)$", path):
            declared_by_name.setdefault(path.rsplit("/", 1)[-1], set()).add(path)
    workspace_root = _workspace_root_for_policy(ctx)
    issues: list[str] = []
    for idx, subtask in enumerate(_iter_plan_subtasks(plan), start=1):
        success_text = _plan_item_success_test_text(subtask)
        if not _FRONTEND_TEST_CWD_RE.search(success_text):
            continue
        targets = _js_test_file_targets_from_success_test(success_text)
        if not targets:
            continue
        subtask_id = (
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or f"subtask #{idx}"
        )
        for target in targets:
            normalized = target.replace("\\", "/").lstrip("./")
            expected = ""
            if normalized.startswith("frontend/"):
                expected = normalized
            elif "/" in normalized:
                expected = f"frontend/{normalized}"
            if expected and expected in declared_paths:
                continue
            if expected and workspace_root is not None and (workspace_root / expected).is_file():
                continue
            basename = normalized.rsplit("/", 1)[-1]
            matches = declared_by_name.get(basename, set())
            frontend_matches = {path for path in matches if path.startswith("frontend/")}
            if frontend_matches:
                continue
            if "/" not in normalized and workspace_root is not None:
                if list((workspace_root / "frontend").rglob(basename)):
                    continue
            if matches:
                issues.append(
                    f"subtask `{subtask_id}` runs frontend tests from `cd frontend` "
                    f"with target `{target}`, but the plan declares matching test "
                    f"path(s) outside the frontend package: {sorted(matches)[:4]}. "
                    "Declare the test under `frontend/...` or run it from the "
                    "workspace root with a command that matches the declared path."
                )
            else:
                issues.append(
                    f"subtask `{subtask_id}` runs frontend test target `{target}` "
                    "from `cd frontend`, but no plan leaf declares that checked-in "
                    "frontend test file. Add the `frontend/...` test file to "
                    "files_to_create/files_to_change/files_affected or use a "
                    "package script without an implicit file target."
                )
    return issues


def _workspace_file_exists(ctx: ToolContext | None, rel_path: str) -> bool:
    workspace_root = _workspace_root_for_policy(ctx)
    return bool(workspace_root is not None and (workspace_root / rel_path).is_file())


def _workspace_has_frontend_script_source(ctx: ToolContext | None) -> bool:
    workspace_root = _workspace_root_for_policy(ctx)
    if workspace_root is None:
        return False
    src = workspace_root / "frontend" / "src"
    if not src.is_dir():
        return False
    for pattern in ("*.ts", "*.tsx", "*.js", "*.jsx", "*.mts", "*.cts"):
        if list(src.rglob(pattern)):
            return True
    return False


def _workspace_has_vite_config(ctx: ToolContext | None) -> bool:
    workspace_root = _workspace_root_for_policy(ctx)
    if workspace_root is None:
        return False
    return any((workspace_root / "frontend").glob("vite.config.*"))


def _phase_plan_frontend_build_order_issues(
    plan: dict[str, Any], ctx: ToolContext | None = None
) -> list[str]:
    issues: list[str] = []
    cumulative_paths = {
        path
        for path in _iter_declared_plan_path_strings(plan)
        if _workspace_file_exists(ctx, path)
    }
    for idx, subtask in enumerate(_iter_plan_subtasks(plan), start=1):
        declared_now = set(_iter_declared_plan_path_strings(subtask))
        success_text = _plan_item_success_test_text(subtask)
        subtask_id = (
            subtask.get("id")
            or subtask.get("subtask_id")
            or subtask.get("title")
            or f"subtask #{idx}"
        )
        available = cumulative_paths | declared_now
        if _FRONTEND_BUILD_COMMAND_RE.search(success_text):
            missing: list[str] = []
            if not any(
                _FRONTEND_SCRIPT_SOURCE_RE.search(path) for path in available
            ) and not _workspace_has_frontend_script_source(ctx):
                missing.append("frontend/src/<entry>.tsx")
            vite_declared = any(
                _FRONTEND_VITE_CONFIG_RE.search(path) for path in available
            ) or _workspace_has_vite_config(ctx)
            if (
                vite_declared
                and "frontend/index.html" not in available
                and not _workspace_file_exists(ctx, "frontend/index.html")
            ):
                missing.append("frontend/index.html")
            if missing:
                issues.append(
                    f"subtask `{subtask_id}` runs a frontend build success_test "
                    "before the files needed by that build are declared in the "
                    "same or an earlier leaf: "
                    + ", ".join(missing)
                    + ". Move the build success_test to the leaf that owns the "
                    "entrypoint, or declare the entrypoint files on this leaf."
                )
        cumulative_paths.update(declared_now)
    return issues


def _unmanaged_localhost_success_test_issue(value: str) -> str:
    for segment in _split_shell_command_segments(str(value or "")):
        stripped = segment.strip()
        if not _DIRECT_LOCALHOST_HTTP_RE.search(stripped):
            continue
        if _MANAGED_LOCALHOST_PROOF_RE.search(stripped):
            continue
        return (
            "success test probes localhost with a direct HTTP shell command "
            "(`curl`/`Invoke-WebRequest`) without a managed server harness in "
            "that same proof step; use a checked-in pytest/playwright/e2e "
            "harness or Umbrella `http_boot`/`behavioral_http` so the proof "
            "starts and stops services instead of depending on a pre-existing "
            "listener"
        )
    return ""


def _has_background_shell_operator(value: str) -> bool:
    text = str(value or "")
    for idx, ch in enumerate(text):
        if ch != "&":
            continue
        prev_ch = text[idx - 1] if idx > 0 else ""
        next_ch = text[idx + 1] if idx + 1 < len(text) else ""
        if prev_ch == "&" or next_ch == "&":
            continue
        return True
    return False


def _command_portability_issue(value: str) -> str:
    text = str(value or "")
    if _NON_PORTABLE_SHELL_RE.search(text) or _has_background_shell_operator(text):
        return (
            "success test uses non-portable or unmanaged shell/process-control "
            "syntax that is not a reliable Umbrella workspace proof on this "
            "host; use Python/pytest/node/npm, a checked-in verification "
            "script, or a managed HTTP/browser verification gate that starts "
            "and stops services cleanly"
        )
    return ""


def _phase_plan_success_test_issues(
    plan: dict[str, Any], ctx: ToolContext | None = None
) -> list[str]:
    issues: list[str] = []
    for idx, subtask in enumerate(_iter_plan_subtasks(plan), start=1):
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
    issues.extend(
        phase_plan_pytest_target_availability_messages(
            subtasks=_iter_plan_subtasks(plan),
            plan=plan,
            workspace_root=_workspace_root_for_policy(ctx),
            workspace_id=_active_workspace_id(ctx) if ctx is not None else "",
        )
    )
    issues.extend(_phase_plan_e2e_pytest_target_issues(plan, ctx=ctx))
    issues.extend(_phase_plan_frontend_test_target_issues(plan, ctx=ctx))
    issues.extend(_phase_plan_frontend_build_order_issues(plan, ctx=ctx))
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
        if tool in {"run_workspace_verify", "run_unit_tests"}:
            inappropriate.append(subtask_id)
    issues: list[str] = []
    if inappropriate:
        issues.append(
            "bare `run_workspace_verify`/`run_unit_tests` is too generic for "
            "phase-plan subtasks; use a concrete local command or an explicit "
            "HTTP/browser/tool proof, and reserve workspace verify for a final "
            "gate after concrete smoke/e2e commands: "
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
                "needs `id`, `title`, `goal`, files, and `success_test`."
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
                "files_affected, success_test}, ...]`."
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
                    "title, goal, files, and success_test"
                )
            for key, child in value.items():
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for idx, child in enumerate(value):
                walk(child, f"{path}[{idx}]")

    walk(plan)
    return issues


def _phase_plan_llm_fallback_issues(plan: dict[str, Any]) -> list[str]:
    for value in _iter_plan_llm_fallback_contexts(plan):
        matches = list(_PLAN_LLM_FALLBACK_RE.finditer(value))
        first_unprotected = ""
        for match in matches:
            if _plan_llm_fallback_match_is_allowed(value, match):
                continue
            first_unprotected = " ".join(match.group(0).split())
            break
        if first_unprotected:
            return [
                (
                    "plan proposes deterministic/static/heuristic fallback for LLM "
                    "behavior required by the task; use real GMAS/LLM behavior and "
                    "make LLM failure explicit, paused, retried, or surfaced as an "
                    "error instead of silently replacing it with hardcoded logic. "
                    f"Matched text: `{first_unprotected[:220]}`."
                )
            ]
        cached_match = _PLAN_LLM_CACHED_DECISION_RE.search(value)
        if (
            cached_match
            and _PLAN_LLM_CONTEXT_RE.search(value)
            and not _llm_cached_decision_match_is_protective(value)
        ):
            first_unprotected = " ".join(cached_match.group(0).split())
            return [
                (
                    "plan proposes cached decision/action/reasoning reuse for LLM/GMAS/bot "
                    "behavior. Required bot decisions must use the inherited real "
                    "LLM runtime; use bounded retry, pause, or surfaced errors "
                    "instead of cached replacement decisions. Matched text: "
                    f"`{first_unprotected[:220]}`."
                )
            ]
        if _PLAN_LLM_CONTEXT_RE.search(value) and not _plan_string_is_identifier_like(value):
            for generic_match in _PLAN_GENERIC_FALLBACK_RE.finditer(value):
                if _plan_llm_fallback_match_is_allowed(value, generic_match):
                    continue
                return [
                    (
                        "plan describes generic fallback logic for LLM/GMAS/bot "
                        "behavior. Use explicit retry, paused bot turn, surfaced "
                        "runtime/startup error, or configuration requirement instead "
                        "of vague fallback handling. Matched text: "
                        f"`{' '.join(value.split())[:220]}`."
                    )
                ]
    return []


def _plan_llm_fallback_match_is_allowed(value: str, match: re.Match[str]) -> bool:
    """Evaluate fallback protection around the matched claim, not the whole plan."""

    text = str(value or "")
    start = max(0, match.start() - 180)
    end = min(len(text), match.end() + 180)
    window = text[start:end]
    return _llm_fallback_match_is_protective(match.group(0)) or (
        _llm_fallback_match_is_protective(window)
    )


def _plan_llm_test_double_is_protective(text: str) -> bool:
    lowered = str(text or "").lower()
    if re.search(
        r"\b(no|never|not|must\s+not|without|prohibit(?:s|ed)?|"
        r"prohibited|forbid(?:s|den)?|"
        r"disallow(?:s|ed)?|reject(?:s|ed)?|prevent(?:s|ed)?)\b"
        r".{0,100}\b(?:mock|fake|dry[-\s]?run|test\s+double)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:mock|fake|dry[-\s]?run|test\s+double)\b.{0,100}"
        r"\b(?:not\s+allowed|prohibited|forbidden|disallowed|rejected|"
        r"must\s+not|do\s+not|never)\b",
        lowered,
    ):
        return True
    return False


def _iter_plan_non_success_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if str(key).strip().lower() in {
                "success_test",
                "acceptance_command",
                "verification_command",
                "verification_commands",
                "verification",
                "test",
                "anti_patterns",
                "anti_patterns_to_avoid",
                "forbidden_patterns",
                "avoid",
            }:
                continue
            yield from _iter_plan_non_success_strings(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _iter_plan_non_success_strings(child)


def _phase_plan_llm_test_double_issues(plan: dict[str, Any]) -> list[str]:
    for value in _iter_plan_non_success_strings(plan):
        match = _PLAN_LLM_TEST_DOUBLE_RE.search(value)
        if not match:
            continue
        if _plan_llm_test_double_is_protective(value):
            continue
        matched_text = " ".join(match.group(0).split())[:220]
        return [
            (
                "plan proposes mock/fake/dry-run LLM behavior for an LLM/GMAS/"
                "bot/model path. Required LLM behavior must be proved with the "
                "inherited real runtime env, while missing credentials should "
                "fail, skip explicitly, pause, retry, or surface an error. "
                f"Matched text: `{matched_text}`."
            )
        ]
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


def _llm_env_contract_issue_from_text(
    text: str, *, subject: str, require_explicit_contract: bool = True
) -> str:
    if not _LLM_ENV_CONTEXT_RE.search(text):
        return ""
    unsupported_alias = next(_UNSUPPORTED_OUROBOROS_MODEL_ALIAS_RE.finditer(text), None)
    if unsupported_alias:
        return (
            f"{subject} uses unsupported model env alias "
            "`OUROBOROS_LLM_MODEL`. Generated projects should expose "
            "`LLM_MODEL` as their public model setting and may accept "
            "`OUROBOROS_MODEL` only as an inherited Umbrella compatibility "
            "alias."
        )
    invalid_alias_issues = unsupported_llm_env_alias_issues(
        text,
        subject=subject,
        exclude_aliases={"OUROBOROS_LLM_MODEL"},
    )
    if invalid_alias_issues:
        return invalid_alias_issues[0].message
    has_ouroboros_alias = bool(_LLM_ENV_ALIAS_RE.search(text))
    legacy_mentions = _LLM_LEGACY_ENV_RE.findall(text)
    openai_required = bool(_OPENAI_REQUIRED_RE.search(text))
    openai_mentions = bool(_OPENAI_KEY_RE.search(text))
    web_search_only = bool(_WEB_SEARCH_ONLY_CONTEXT_RE.search(text)) and not legacy_mentions
    missing_aliases = _missing_llm_runtime_aliases(text)
    has_any_runtime_alias = len(missing_aliases) < len(_LLM_LEGACY_ENV_ALIASES)
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
            "a standalone generated project. Generated workspace code/tests "
            "must support public aliases `LLM_API_KEY`, `LLM_BASE_URL`, and "
            "`LLM_MODEL`, and may also accept inherited Umbrella aliases "
            "`OUROBOROS_LLM_API_KEY`, `OUROBOROS_LLM_BASE_URL`, and "
            "`OUROBOROS_MODEL`; do not require `OPENAI_API_KEY` as the "
            "universal way to run real LLM/e2e behavior. "
            "`OPENAI_API_KEY` is only one possible provider/web-search "
            "credential, not the universal project LLM contract."
            f"{missing_text}"
        )
    if not require_explicit_contract:
        return ""
    if not (
        _LLM_ENV_OMISSION_REQUIRED_RE.search(text)
        or _LLM_ENV_CONTRACT_REQUIRED_RE.search(text)
    ):
        return ""
    return (
        f"{subject} omits the standalone LLM runtime env contract for "
        "LLM/GMAS/bot work. The plan must explicitly require generated "
        "workspace code/tests to resolve public aliases `LLM_API_KEY`, "
        "`LLM_BASE_URL`, and `LLM_MODEL`, optionally with inherited Umbrella "
        "compatibility aliases, and to fail/skip/pause clearly when real LLM "
        "credentials are absent."
    )


def _phase_plan_llm_env_issues(plan: dict[str, Any]) -> list[str]:
    issue = _llm_env_contract_issue_from_text(
        "\n".join(_iter_plan_strings(plan)),
        subject="plan",
    )
    return [issue] if issue else []


def _iter_plan_llm_fallback_contexts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        direct_strings = []
        for key, child in value.items():
            if not isinstance(child, str):
                continue
            child_text = str(child).strip()
            if not child_text:
                continue
            key_text = str(key).strip().replace("_", " ").replace("-", " ")
            direct_strings.append(f"{key_text}: {child_text}" if key_text else child_text)
        if direct_strings and _should_combine_llm_fallback_context(value):
            yield "\n".join(direct_strings)
        for child in value.values():
            yield from _iter_plan_llm_fallback_contexts(child)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            yield from _iter_plan_llm_fallback_contexts(child)


def _should_combine_llm_fallback_context(value: dict[str, Any]) -> bool:
    keys = {str(key).strip().lower() for key in value}
    if keys & {"subtasks", "steps", "phases", "ordered_subtasks"}:
        return False
    normalized_key_text = "\n".join(
        key.replace("_", " ").replace("-", " ") for key in keys
    )
    if _PLAN_LLM_CONTEXT_RE.search(normalized_key_text):
        return True
    if keys & {
        "risk",
        "risks",
        "mitigation",
        "risk_mitigation",
        "risk_mitigations",
        "risks_and_mitigations",
    }:
        return True
    if keys & {
        "decision_policy",
        "decision_policies",
        "failure_policy",
        "failure_policies",
        "error_handling",
        "acceptance_criteria",
        "llm_policy",
        "policies",
        "runtime_policy",
        "runtime_policies",
    }:
        return True
    return False


def _plan_string_is_identifier_like(value: str) -> bool:
    text = str(value or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9_.:/\\-]{1,160}", text))


def _llm_cached_decision_match_is_protective(text: str) -> bool:
    lowered = str(text or "").lower()
    return bool(
        re.search(
            r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
            r"disallow(?:s|ed)?|prohibit(?:s|ed)?|block(?:s|ed)?|"
            r"reject(?:s|ed)?|prevent(?:s|ed)?)\b.{0,140}\b"
            r"(?:decision|action|response|reasoning)\s+caching\b",
            lowered,
        )
        or re.search(
            r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
            r"disallow(?:s|ed)?|prohibit(?:s|ed)?|block(?:s|ed)?|"
            r"reject(?:s|ed)?|prevent(?:s|ed)?)\b.{0,140}\b"
            r"cached\s+(?:decisions?|actions?|responses?|outputs?|reasoning)\b",
            lowered,
        )
        or re.search(
            r"\b(?:tests?|verification|harness|assertions?)\b.{0,140}"
            r"\bfail(?:s|ed|ing)?\b.{0,160}\b"
            r"(?:decision|action|response|reasoning)\s+caching\b",
            lowered,
        )
    )


def _llm_fallback_match_is_protective(text: str) -> bool:
    lowered = str(text or "").lower()
    if re.search(
        r"\bno\s+(?:llm\s+)?(?:credentials?|api\s+keys?|keys?|env(?:ironment)?"
        r"(?:\s+vars?)?|providers?|configuration|config)\b.{0,140}"
        r"\b(?:fallback|fall[-\s]+back)\b",
        lowered,
    ):
        return False
    if (
        _PLAN_ENV_ALIAS_FALLBACK_RE.search(lowered)
        and not _PLAN_BAD_FALLBACK_REPLACEMENT_RE.search(lowered)
    ):
        return True
    if re.search(
        r"\b(no|never|not|must\s+not|without|forbid(?:s|den)?|"
        r"disallow(?:s|ed)?|prohibit(?:s|ed)?|block(?:s|ed)?|"
        r"refuse(?:s)?\s+to)\b[^.;\n]{0,100}\b(?:fallback|fall[-\s]+back)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:fallback|fall[-\s]+back)\b.{0,120}\b("
        r"forbidden|disallowed|prohibited|blocked|rejected|not\s+allowed"
        r")\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:tests?|verification|harness|check|assertions?)\b.{0,120}"
        r"\bfail(?:s|ed|ing)?\b.{0,160}\b("
        r"fallback|fall[-\s]+back|hardcoded|heuristics?|deterministic|static"
        r")\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(detect|detects|detected|assert|asserts|enforce|enforces|"
        r"prevent|prevents|prove|proves|confirm|confirms|reject|rejects|"
        r"block|blocks|catch|catches|caught)\b.{0,140}\b("
        r"fallback|fall[-\s]+back|hardcoded|heuristics?|deterministic|static"
        r")\b",
        lowered,
    ):
        return True
    return False


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
    '_python_inline_docs_content_issue',
    '_python_inline_complexity_issue',
    '_python_inline_syntax_issue',
    '_python_test_module_invocation_issue',
    '_shell_command_segment_issue',
    '_split_shell_command_segments',
    '_has_background_shell_operator',
    '_command_portability_issue',
    '_phase_plan_success_test_issues',
    '_phase_plan_e2e_pytest_target_issues',
    '_phase_plan_generic_success_test_issues',
    '_phase_plan_structure_issues',
    '_phase_plan_placeholder_issues',
    '_phase_plan_llm_fallback_issues',
    '_plan_llm_test_double_is_protective',
    '_iter_plan_non_success_strings',
    '_phase_plan_llm_test_double_issues',
    '_provider_default_match_is_protective',
    '_phase_plan_llm_provider_default_issues',
    '_phase_plan_empty_test_skeleton_issues',
    '_missing_llm_runtime_aliases',
    '_llm_env_contract_issue_from_text',
    '_phase_plan_llm_env_issues',
    '_iter_plan_llm_fallback_contexts',
    '_should_combine_llm_fallback_context',
    '_plan_string_is_identifier_like',
    '_llm_cached_decision_match_is_protective',
    '_llm_fallback_match_is_protective',
]
