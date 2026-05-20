"""Workspace path, file reference, and plan layout policy checks."""

from umbrella.deep_agent_tools.phase_contract_common import *
from umbrella.deep_agent_tools.phase_contract_base import *
from umbrella.deep_agent_tools.phase_contract_declarations import *
from umbrella.deep_agent_tools.phase_contract_success import *
from umbrella.deep_agent_tools.phase_contract_revisions import *

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


_PLAN_FILE_PATH_ANNOTATION_RE = re.compile(
    r"(?i)(?:^|/)[^\s]+(?:\.[a-z0-9]{1,12}|\b(?:dockerfile|makefile))"
    r"\s+(?:\([^\r\n]+\)|\[[^\r\n]+\]|-\s+[^\r\n]+)\s*$"
)


def _phase_plan_file_path_hygiene_issues(plan: dict[str, Any]) -> list[str]:
    annotated: list[str] = []
    for field_path, _key, raw in _iter_plan_file_field_refs(plan):
        text = str(raw or "").strip().strip("`'\"")
        if _PLAN_FILE_PATH_ANNOTATION_RE.search(text):
            annotated.append(f"{field_path}: {raw}")
    if not annotated:
        return []
    return [
        "phase plan file fields must contain bare workspace-relative paths, "
        "not annotated pseudo-paths such as `file.py (updated)` or "
        "`package.json (deps added)`. Put status notes in `goal`, "
        "`description`, or `notes` instead. Offending path(s): "
        + ", ".join(annotated[:8])
    ]


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
    existing_impl = bool(_workspace_existing_impl_roots(ctx))
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
    existing_impl = bool(_workspace_existing_impl_roots(ctx))
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

    issues: list[str] = []
    docs_python_paths = sorted(
        p
        for p in code_paths
        if pathlib.PurePosixPath(p).suffix.lower() == ".py"
        and pathlib.PurePosixPath(p).parts
        and pathlib.PurePosixPath(p).parts[0].lower() in {"docs", "doc"}
    )
    if docs_python_paths:
        issues.append(
            "Python files do not belong under `docs/` in phase plans; "
            "`docs/` is for durable Markdown/spec documentation. Put pytest "
            "verification under `tests/` or reusable Python code under "
            "`src/<package>/...`. Move or remove "
            f"{docs_python_paths[:8]}"
        )
    if existing_impl:
        return issues

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
    if not complex_greenfield:
        return issues

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
            or any(part in {"test", "tests"} for part in lowered)
        )
        if is_test_path and top not in {"tests", "test"}:
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
        if top in {"tests", "test", "frontend"}:
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
            "greenfield Python pytest/test modules must live under `tests/`; "
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

    if disallowed_docs_python and not docs_python_paths:
        issues.append(
            "Python files do not belong under `docs/` in phase plans; "
            "`docs/` is for durable Markdown/spec documentation. Put pytest "
            "verification under `tests/` or reusable Python code under "
            "`src/<package>/...`. Move or remove "
            f"{disallowed_docs_python[:8]}"
        )

    if disallowed_python_scripts:
        issues.append(
            "greenfield Python verify/check/debug/probe helpers must not be "
            "planned under root `scripts/`; put reusable Python code under "
            "`src/<package>/...`, put pytest verification under `tests/`, or "
            "use a non-Python launch script only when it is a real deliverable. "
            f"Move or remove {disallowed_python_scripts[:8]}"
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
        "vertical slices with one real success_test each"
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
        "success_test per leaf, instead of packing multiple domains or "
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
    issues.extend(_phase_plan_file_path_hygiene_issues(plan))
    issues.extend(_phase_plan_forbidden_file_issues(ctx, plan))
    issues.extend(_phase_plan_file_reference_issues(ctx, plan))
    issues.extend(_phase_plan_greenfield_layout_issues(ctx, plan))
    issues.extend(_phase_plan_missing_leaf_file_field_issues(plan))
    issues.extend(_phase_plan_compactness_issues(plan))
    issues.extend(_phase_plan_broad_leaf_issues(ctx, plan))
    issues.extend(_phase_plan_success_test_issues(plan, ctx=ctx))
    issues.extend(_phase_plan_generic_success_test_issues(plan))
    issues.extend(_phase_plan_revision_contract_issues(ctx, plan))
    return list(dict.fromkeys(issues))


__all__ = [
    '_iter_path_values',
    '_normalise_plan_path',
    '_plan_value_has_workspace_prefix',
    '_phase_plan_workspace_prefix_issues',
    '_iter_plan_file_field_refs',
    '_phase_plan_file_path_hygiene_issues',
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
