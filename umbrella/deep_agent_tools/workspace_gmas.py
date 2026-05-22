"""GMAS and context-contract helpers for Umbrella workspace tools."""

from umbrella.deep_agent_tools.workspace_common import *


def search_gmas_knowledge(
    ctx: Any,
    query: str,
    max_results: int = 6,
    max_chars_per_hit: int = 8000,
    limit: int | None = None,
    intent: str = "",
    slug: str = "",
) -> str:
    """Search GMAS docs/examples/code and return rich implementation context."""
    try:
        from umbrella.retrieval.gmas_context import build_gmas_context

        if limit is not None:
            max_results = limit
        repo_root = _resolve_umbrella_repo_root(ctx)
        result = build_gmas_context(
            repo_root,
            query,
            max_results=max(1, min(int(max_results), 12)),
            max_chars_per_hit=max(1000, min(int(max_chars_per_hit), 30000)),
        )
        active = _active_execute_subtask_info(ctx)
        subtask_id = str(active.get("id") or "").strip() if active else ""
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("status", "ok")
            intent_norm = str(intent or "").strip()
            if intent_norm:
                result["intent"] = intent_norm
            slug_norm = str(slug or "").strip()
            if slug_norm:
                result["slug"] = slug_norm
            if subtask_id:
                result["active_subtask_id"] = subtask_id
        _mark_explicit_gmas_context_call(ctx, subtask_id=subtask_id)
        return _json(result)
    except Exception as e:
        log.error("GMAS search failed: %s", e, exc_info=True)
        return f"WARNING: GMAS search error: {e}"


def get_gmas_context(
    ctx: Any,
    query: str,
    max_results: int = 6,
    max_chars_per_hit: int = 12000,
    limit: int | None = None,
    intent: str = "",
    slug: str = "",
) -> str:
    """Alias with a more explicit name for full GMAS context retrieval."""
    return search_gmas_knowledge(
        ctx,
        query=query,
        max_results=max_results,
        max_chars_per_hit=max_chars_per_hit,
        limit=limit,
        intent=intent,
        slug=slug,
    )


def _mark_explicit_gmas_context_call(ctx: Any, *, subtask_id: str = "") -> None:
    try:
        setattr(
            ctx,
            "explicit_gmas_context_calls",
            int(getattr(ctx, "explicit_gmas_context_calls", 0) or 0) + 1,
        )
        if subtask_id:
            current = getattr(ctx, "explicit_gmas_context_subtask_ids", None)
            values = set(current or [])
            values.add(subtask_id)
            setattr(ctx, "explicit_gmas_context_subtask_ids", values)
        view = getattr(ctx, "loop_state_view", None)
        if isinstance(view, dict):
            view["explicit_gmas_context_calls"] = (
                int(view.get("explicit_gmas_context_calls") or 0) + 1
            )
            if subtask_id:
                values = set(view.get("explicit_gmas_context_subtask_ids") or [])
                values.add(subtask_id)
                view["explicit_gmas_context_subtask_ids"] = sorted(values)
    except Exception:
        log.debug("Failed to mark explicit GMAS context call", exc_info=True)


def _gmas_json_obj_from_preview(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _gmas_tool_row_successful_context(row: dict[str, Any]) -> bool:
    raw_preview = row.get("result_preview") or row.get("result") or {}
    payload = _gmas_json_obj_from_preview(
        raw_preview
    )
    if not payload:
        text = str(raw_preview or "").strip().lower()
        return bool(text and not text.startswith(("error", "warning")))
    status = str(payload.get("status") or "").strip().lower()
    if status and status != "ok":
        return False
    if payload.get("error"):
        return False
    return bool(
        status == "ok"
        or payload.get("recommended_pattern")
        or payload.get("key_files")
        or payload.get("retrieval_excerpt")
    )


def _gmas_tool_row_subtask_id(row: dict[str, Any]) -> str:
    for source in (
        row.get("args") if isinstance(row.get("args"), dict) else {},
        _gmas_json_obj_from_preview(row.get("result_preview") or row.get("result") or {}),
    ):
        if not isinstance(source, dict):
            continue
        for key in ("active_subtask_id", "subtask_id", "current_subtask_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _task_tool_log_has(
    ctx: Any,
    *,
    tool_names: set[str],
    effective_write_only: bool = False,
    require_successful_context: bool = False,
    active_subtask_id: str = "",
) -> bool:
    task_id = str(getattr(ctx, "task_id", "") or "")
    drive_root = getattr(ctx, "drive_root", None)
    if not task_id or not drive_root:
        return False
    tools_log = Path(drive_root) / "logs" / "tools.jsonl"
    if not tools_log.is_file():
        return False
    try:
        if effective_write_only:
            from umbrella.utils.tool_logs import is_effective_write_tool_log_row
        with tools_log.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(event.get("task_id") or "") != task_id:
                    continue
                if str(event.get("tool") or "") not in tool_names:
                    continue
                if effective_write_only and not is_effective_write_tool_log_row(event):
                    continue
                if require_successful_context and not _gmas_tool_row_successful_context(event):
                    continue
                if active_subtask_id:
                    row_subtask_id = _gmas_tool_row_subtask_id(event)
                    if row_subtask_id != active_subtask_id:
                        continue
                return True
    except Exception:
        log.debug("Failed to inspect task tool log", exc_info=True)
    return False


def _workspace_has_gmas_skill(
    ctx: Any, repo_root: Path, workspace_id: str, workspace_root: Path
) -> bool:
    try:
        data = tomllib.loads(
            (workspace_root / "workspace.toml").read_text(encoding="utf-8")
        )
        skills = data.get("skills") if isinstance(data, dict) else None
        if isinstance(skills, dict) and skills.get("multi_agent_gmas") is True:
            return True
    except Exception:
        pass

    drive_root = getattr(ctx, "drive_root", None)
    candidates: list[Path] = []
    if drive_root:
        candidates.append(Path(drive_root) / "state" / "active_skills.json")
    candidates.append(
        workspace_root / ".memory" / "drive" / "state" / "active_skills.json"
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        text = json.dumps(payload, ensure_ascii=False).lower()
        if str(workspace_id).lower() in text and "multi_agent_gmas" in text:
            return True
    return False


_OBSOLETE_OUROBOROS_API_KEY_ALIAS_RE = re.compile(
    r"(?i)\bOUROBOROS_API_KEY\b|\bouroboros_api_key\b"
)
_OBSOLETE_OUROBOROS_BASE_URL_ALIAS_RE = re.compile(
    r"(?i)\bOUROBOROS_BASE_URL\b|\bouroboros_base_url\b"
)
_REQUIRED_OUROBOROS_LLM_API_KEY_ALIAS_RE = re.compile(
    r"(?i)\bOUROBOROS_LLM_API_KEY\b|\bouroboros_llm_api_key\b"
)
_REQUIRED_OUROBOROS_LLM_BASE_URL_ALIAS_RE = re.compile(
    r"(?i)\bOUROBOROS_LLM_BASE_URL\b|\bouroboros_llm_base_url\b"
)
_HOST_LLM_ENV_ALIAS_RE = re.compile(
    r"(?i)\b(?:OUROBOROS_LLM_API_KEY|OUROBOROS_LLM_BASE_URL|OUROBOROS_MODEL)\b"
)
_LLM_RUNTIME_PROTECTIVE_DOC_RE = re.compile(
    r"(?i)\b(?:do\s+not|don't|never|without|no|avoid|reject|refuse|block|"
    r"forbid|forbidden|must\s+not|should\s+not|not\s+require|not\s+use|"
    r"not\s+default|silent\s+fallbacks?|hardcoded|hard-code(?:d)?|"
    r"strikeout|disallowed|unsupported)\b"
)


def _line_window_for_index(text: str, index: int, *, radius: int = 1) -> str:
    lines = text.splitlines()
    if not lines:
        return text
    offset = 0
    line_idx = 0
    for idx, line in enumerate(lines):
        end = offset + len(line) + 1
        if index < end:
            line_idx = idx
            break
        offset = end
    start = max(0, line_idx - radius)
    end = min(len(lines), line_idx + radius + 1)
    return "\n".join(lines[start:end])


def _provider_default_matches(text: str) -> list[re.Match[str]]:
    return [
        match
        for pattern in _LLM_PROVIDER_DEFAULT_PATTERNS
        for match in pattern.finditer(text)
    ]


def _provider_default_match_is_protective_doc(
    text: str, match: re.Match[str]
) -> bool:
    window = _line_window_for_index(text, match.start(), radius=1)
    return bool(_LLM_RUNTIME_PROTECTIVE_DOC_RE.search(window))


def _llm_runtime_contract_block(rel_path: str, content_text: str) -> dict[str, Any] | None:
    """Reject generated LLM code that silently defaults to one provider."""

    path = str(rel_path or "").replace("\\", "/")
    suffix = Path(path).suffix.lower()
    basename = Path(path).name.lower()
    is_env_file = basename == ".env" or basename.startswith(".env.")
    if (
        suffix not in _LLM_RUNTIME_CODE_EXTENSIONS
        and basename not in {"package.json", "pyproject.toml"}
        and not is_env_file
    ):
        return None

    text = str(content_text or "")
    if not text:
        return None

    provider_default_matches = _provider_default_matches(text)
    if suffix in {".md", ".markdown"}:
        provider_default_matches = [
            match
            for match in provider_default_matches
            if not _provider_default_match_is_protective_doc(text, match)
        ]
    provider_defaults = [
        match.group(0).strip("\"'") for match in provider_default_matches
    ]
    narrow_openai_key = (
        "OPENAI_API_KEY" in text
        and "LLM_API_KEY" not in text
    )
    wrong_model_alias = "OUROBOROS_LLM_MODEL" in text
    host_llm_aliases = sorted({match.group(0) for match in _HOST_LLM_ENV_ALIAS_RE.finditer(text)})
    obsolete_api_alias_only = (
        _OBSOLETE_OUROBOROS_API_KEY_ALIAS_RE.search(text) is not None
        and _REQUIRED_OUROBOROS_LLM_API_KEY_ALIAS_RE.search(text) is None
    )
    obsolete_base_url_alias_only = (
        _OBSOLETE_OUROBOROS_BASE_URL_ALIAS_RE.search(text) is not None
        and _REQUIRED_OUROBOROS_LLM_BASE_URL_ALIAS_RE.search(text) is None
    )
    has_llm_runtime_signal = any(
        marker in text
        for marker in (
            "LLM_API_KEY",
            "OUROBOROS_LLM_API_KEY",
            "LLM_BASE_URL",
            "OUROBOROS_LLM_BASE_URL",
            "LLM_MODEL",
            "OUROBOROS_MODEL",
            "create_openai_caller",
            "chat.completions",
            "llm",
            "LLM",
        )
    )
    obsolete_ouroboros_alias_only = (
        has_llm_runtime_signal
        and (obsolete_api_alias_only or obsolete_base_url_alias_only)
    )
    if (
        not provider_defaults
        and not narrow_openai_key
        and not wrong_model_alias
        and not host_llm_aliases
        and not obsolete_ouroboros_alias_only
    ):
        return None
    if (
        provider_defaults
        and not has_llm_runtime_signal
        and not wrong_model_alias
        and not host_llm_aliases
        and not obsolete_ouroboros_alias_only
    ):
        return None

    issues: list[str] = []
    if provider_defaults:
        issues.append(
            "hardcoded provider/model default(s): "
            + ", ".join(sorted(set(provider_defaults))[:8])
        )
    if narrow_openai_key:
        issues.append("OPENAI_API_KEY is used without public LLM_API_KEY")
    if wrong_model_alias:
        issues.append("unsupported model env alias: OUROBOROS_LLM_MODEL")
    if host_llm_aliases:
        issues.append(
            "host/control-plane LLM env alias(es) leaked into generated workspace: "
            + ", ".join(host_llm_aliases)
        )
    if obsolete_api_alias_only and has_llm_runtime_signal:
        issues.append(
            "obsolete API-key alias `OUROBOROS_API_KEY`/`ouroboros_api_key` "
            "is used; generated workspaces should use public `LLM_API_KEY`"
        )
    if obsolete_base_url_alias_only and has_llm_runtime_signal:
        issues.append(
            "obsolete base-url alias `OUROBOROS_BASE_URL`/`ouroboros_base_url` "
            "is used; generated workspaces should use public `LLM_BASE_URL`"
        )

    next_step = (
        "Use a small runtime resolver that reads the project public aliases "
        "LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL. Umbrella maps any host "
        "control-plane launch env into those public aliases before workspace "
        "commands run, so generated code, docs, tests, and env examples should "
        "not mention or require OUROBOROS_* LLM aliases. If any required value "
        "is missing, raise a clear configuration error or skip only the "
        "specific live-LLM test with an explicit reason; do not fallback to "
        "https://api.openai.com/v1, gpt-* defaults, or OPENAI_API_KEY as the "
        "only credential path."
    )
    if wrong_model_alias:
        next_step += (
            " Remove the unsupported model alias from generated code, docs, "
            "tests, and env examples; use LLM_MODEL for the project's public "
            "model setting."
        )

    return {
        "status": "blocked",
        "reason": "llm_runtime_contract",
        "file_path": path,
        "issues": issues,
        "message": (
            "Generated workspace code/tests must use a provider-neutral LLM "
            "runtime contract instead of silently defaulting to OpenAI "
            "credentials, URLs, or model names."
        ),
        "next_step": next_step,
    }


def _llm_behavior_fallback_contract_block(
    rel_path: str, content_text: str
) -> dict[str, Any] | None:
    """Reject generated agent code that replaces LLM decisions with heuristics."""

    path = str(rel_path or "").replace("\\", "/").strip("/")
    suffix = Path(path).suffix.lower()
    name = Path(path).name.lower()
    parts = {part.lower() for part in path.split("/") if part}
    if suffix not in _LLM_BEHAVIOR_SOURCE_EXTENSIONS:
        return None
    if name.startswith("test_") or "tests" in parts or "docs" in parts:
        return None

    text = str(content_text or "")
    if not text or not _LLM_BEHAVIOR_SIGNAL_RE.search(text):
        return None

    for pattern in _LLM_BEHAVIOR_FALLBACK_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        matched = " ".join(match.group(0).split())
        if _llm_behavior_fallback_match_is_protective(matched):
            continue
        return {
            "status": "blocked",
            "reason": "llm_behavior_fallback_contract",
            "file_path": path,
            "issues": [
                (
                    "deterministic/static/heuristic replacement for LLM "
                    f"behavior: {matched[:240]}"
                )
            ],
            "message": (
                "Generated LLM/GMAS/bot code must not silently replace required "
                "model decisions with deterministic, static, keyword, sentiment, "
                "or default fallback decisions."
            ),
            "next_step": (
                "Require a structured LLM/GMAS response, retry or surface a clear "
                "runtime error when parsing fails, and let tests assert that "
                "malformed model output fails loudly instead of choosing a cached "
                "or heuristic action."
            ),
        }
    return None


def _llm_behavior_fallback_match_is_protective(text: str) -> bool:
    raw = str(text or "")
    lower = raw.lower()
    if not re.search(r"\b(?:fallback|fall[-\s]?back)\b", lower):
        return False
    protective = (
        r"\b(?:do\s+not|don't|never|without|no|avoid|reject|refuse|block|"
        r"forbid|raise|error|fail|fails|failure|instead\s+of)\b"
    )
    dangerous = (
        r"\b(?:positive_count|negative_count|positive_words|negative_words|"
        r"sentiment|keyword|heuristics?|deterministic|static|rule[-\s]?based|"
        r"default|accept|reject|decision|action)\b"
    )
    return bool(re.search(protective, lower)) and bool(re.search(dangerous, lower))


def _gmas_context_before_write_block(
    ctx: Any, workspace_id: str, workspace_root: Path
) -> dict[str, Any] | None:
    try:
        repo_root = _resolve_umbrella_repo_root(ctx)
        if not _workspace_has_gmas_skill(ctx, repo_root, workspace_id, workspace_root):
            return None
        active = _active_execute_subtask_info(ctx)
        active_subtask_id = str(active.get("id") or "").strip() if active else ""
        if active is not None and not _subtask_requires_gmas_context(active):
            return None
        explicit_calls = int(getattr(ctx, "explicit_gmas_context_calls", 0) or 0)
        view = getattr(ctx, "loop_state_view", None)
        if isinstance(view, dict):
            explicit_calls = max(
                explicit_calls, int(view.get("explicit_gmas_context_calls") or 0)
            )
        explicit_subtasks = set(
            getattr(ctx, "explicit_gmas_context_subtask_ids", None) or []
        )
        if isinstance(view, dict):
            explicit_subtasks.update(view.get("explicit_gmas_context_subtask_ids") or [])
        if (active_subtask_id and active_subtask_id in explicit_subtasks) or (
            not active_subtask_id and explicit_calls > 0
        ):
            return None
        if _task_tool_log_has(
            ctx,
            tool_names={"get_gmas_context", "search_gmas_knowledge"},
            require_successful_context=True,
            active_subtask_id=active_subtask_id,
        ):
            return None
        if not active_subtask_id and _task_tool_log_has(
            ctx,
            tool_names={
                "apply_workspace_patch",
                "update_workspace_seed",
                "delete_workspace_file",
                "repo_write_commit",
            },
            effective_write_only=True,
        ):
            return None
        return {
            "status": "blocked",
            "reason": "gmas_context_before_first_write",
            "workspace_id": workspace_id,
            "active_subtask_id": active_subtask_id,
            "message": (
                "This workspace has multi_agent_gmas active for the current "
                "LLM/agent subtask. Before writing that subtask, make a "
                "successful GMAS retrieval tool call scoped to it."
            ),
            "next_step": (
                "Call `get_gmas_context(query=...)` or `search_gmas_knowledge(query=...)` "
                "with a query that names the active subtask and the GMAS "
                "APIs/patterns needed for it, then retry this write."
            ),
        }
    except Exception:
        log.debug("GMAS before-write gate failed open", exc_info=True)
        return None


_GMAS_SUBTASK_SURFACE_RE = re.compile(
    r"(?i)\b(?:gmas|llm|multi[-_\s]?agent|agent|agents|bot|bots|"
    r"ai[-_\s]?opponent|model[-_\s]?driven|judge)\b"
)
_GMAS_SETUP_ONLY_RE = re.compile(
    r"(?i)\b(?:project|frontend|backend|python|react|vite|package|dependency|"
    r"dependencies|env|environment|config|configuration|scaffold|setup|"
    r"initialize|initialise|init)\b"
)
_GMAS_IMPLEMENTATION_PATH_RE = re.compile(
    r"(?i)(?:^|/)(?:src|tests?)(?:/|$).*"
    r"\b(?:ai|agent|agents|gmas|llm|bot|bots|judge|decision_router)\b"
)
_GMAS_PROJECT_SHELL_PATH_RE = re.compile(
    r"(?i)(?:"
    r"(?:^|/)__init__\.py$|"
    r"(?:^|/)py\.typed$|"
    r"^frontend/src/(?:main\.[jt]sx?|App\.[jt]sx?|vite-env\.d\.ts|index\.css)$|"
    r"^tests/test_(?:project_)?structure\.py$"
    r")"
)
_GMAS_CONFIG_ONLY_PATH_RE = re.compile(
    r"(?i)("
    r"^(?:pyproject\.toml|uv\.lock|requirements(?:-[^/]+)?\.txt|"
    r"package(?:-lock)?\.json|pnpm-lock\.yaml|yarn\.lock|"
    r"\.?env(?:\.[^/]*)?|README\.md|"
    r"workspace\.toml)$|"
    r"^frontend/(?:package(?:-lock)?\.json|pnpm-lock\.yaml|yarn\.lock|"
    r"vite\.config\.[cm]?[jt]s|tsconfig(?:\.[^/]*)?\.json|"
    r"index\.html)$|"
    r"^docs?/|^\.?github/|^config/|^scripts/(?:setup|install|verify)[^/]*$"
    r")"
)


def _subtask_declared_paths(subtask: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("files_to_create", "files_to_change", "files_affected", "files"):
        raw = subtask.get(key)
        values = raw if isinstance(raw, (list, tuple, set, frozenset)) else [raw]
        for value in values:
            text = str(value or "").strip().strip("`'\"").replace("\\", "/")
            if text.startswith("./"):
                text = text[2:]
            if text:
                paths.append(text)
    return list(dict.fromkeys(paths))


def _subtask_declares_gmas_implementation(paths: list[str]) -> bool:
    return any(
        _GMAS_IMPLEMENTATION_PATH_RE.search(path)
        and not _GMAS_PROJECT_SHELL_PATH_RE.search(path)
        for path in paths
    )


def _subtask_is_setup_only_for_gmas(subtask: dict[str, Any], paths: list[str]) -> bool:
    if not paths:
        return False
    title_text = " ".join(
        str(subtask.get(key) or "")
        for key in ("id", "subtask_id", "title", "name", "goal", "description")
    )
    if not _GMAS_SETUP_ONLY_RE.search(title_text):
        return False
    return all(
        _GMAS_CONFIG_ONLY_PATH_RE.search(path)
        or _GMAS_PROJECT_SHELL_PATH_RE.search(path)
        for path in paths
    )


def _subtask_requires_gmas_context(subtask: dict[str, Any]) -> bool:
    paths = _subtask_declared_paths(subtask)
    if _subtask_declares_gmas_implementation(paths):
        return True
    if _subtask_is_setup_only_for_gmas(subtask, paths):
        return False
    parts: list[str] = []
    for key in (
        "id",
        "subtask_id",
        "title",
        "name",
        "goal",
        "description",
        "proof",
        "files_to_create",
        "files_to_change",
        "files_affected",
    ):
        parts.append(str(subtask.get(key) or ""))
    text = "\n".join(parts)
    if not _GMAS_SUBTASK_SURFACE_RE.search(text):
        return False
    if paths and all(
        _GMAS_CONFIG_ONLY_PATH_RE.search(path)
        or _GMAS_PROJECT_SHELL_PATH_RE.search(path)
        for path in paths
    ):
        return False
    return True


def _active_execute_subtask_info(ctx: Any) -> dict[str, Any] | None:
    drive_root = getattr(ctx, "drive_root", None)
    if not drive_root:
        return None
    plan_path = Path(drive_root) / "state" / "phase_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(plan, dict):
        return None
    for node in plan.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("id") or "") != "execute":
            continue
        if str(node.get("status") or "").lower() not in {"running", "pending"}:
            continue
        for subtask in node.get("subtasks") or []:
            if not isinstance(subtask, dict):
                continue
            if str(subtask.get("status") or "").lower() == "done":
                continue
            return subtask
    return None


__all__ = [
    'search_gmas_knowledge',
    'get_gmas_context',
    '_mark_explicit_gmas_context_call',
    '_task_tool_log_has',
    '_workspace_has_gmas_skill',
    '_llm_runtime_contract_block',
    '_llm_behavior_fallback_contract_block',
    '_llm_behavior_fallback_match_is_protective',
    '_gmas_context_before_write_block',
]
