"""Phase tool declaration and workspace-root policy helpers."""

from umbrella.deep_agent_tools.phase_contract_common import *
from umbrella.deep_agent_tools.phase_contract_base import *

def _iter_plan_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            strings.extend(_iter_plan_strings(child))
    elif isinstance(value, list):
        for child in value:
            strings.extend(_iter_plan_strings(child))
    return strings


def _repo_root_from_module() -> pathlib.Path:
    path = pathlib.Path(__file__).resolve()
    for parent in path.parents:
        if (parent / "umbrella" / "phases" / "manifests").is_dir():
            return parent
    return path.parents[3]


def _known_phase_tool_names() -> set[str]:
    names: set[str] = set()
    manifests = _repo_root_from_module() / "umbrella" / "phases" / "manifests"
    try:
        import yaml  # type: ignore

        for path in manifests.glob("*.yaml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for tool_name in data.get("allowed_tools") or []:
                if isinstance(tool_name, str) and tool_name.strip():
                    names.add(tool_name.strip())
    except Exception:
        pass
    try:
        from umbrella.deep_agent_tools import phase_contract_tools as _tools
        names.update(tool.name for tool in _tools.get_tools())
    except Exception:
        pass
    return names


def _is_phase_tool_declaration_site(parent_path: str, key: str) -> bool:
    """Return true for plan fields that describe Umbrella phase tools.

    Domain plans can legitimately describe their own runtime tools, for example
    GMAS game actions under ``gmas_usage.agent_roles[].tools``.  Only the root
    plan and concrete work-item cards use these keys as phase-tool declarations.
    """
    key_lower = str(key).strip().lower()
    if key_lower not in _PLAN_TOOL_DECLARATION_KEYS:
        return False
    return parent_path == "plan" or bool(_PLAN_WORK_ITEM_PATH_RE.search(parent_path))


def _iter_declared_phase_tools(value: Any, path: str = "plan") -> list[tuple[str, str]]:
    declared: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if _is_phase_tool_declaration_site(path, str(key)):
                declared.extend(
                    (child_path, tool_name)
                    for tool_name in _extract_declared_tool_names(child)
                )
            else:
                declared.extend(_iter_declared_phase_tools(child, child_path))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            declared.extend(_iter_declared_phase_tools(child, f"{path}[{idx}]"))
    return declared


def _extract_declared_tool_names(value: Any) -> list[str]:
    raw_items: list[str] = []
    if isinstance(value, str):
        raw_items.extend(re.split(r"[,;\s]+", value))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                raw_items.extend(re.split(r"[,;\s]+", item))
            elif isinstance(item, dict):
                name = item.get("name") or item.get("tool")
                if isinstance(name, str):
                    raw_items.append(name)
    elif isinstance(value, dict):
        for key in ("name", "tool"):
            item = value.get(key)
            if isinstance(item, str):
                raw_items.append(item)

    names: list[str] = []
    for item in raw_items:
        token = item.strip().strip("`'\"[](){}")
        if _PLAN_TOOL_NAME_RE.match(token):
            names.append(token)
    return list(dict.fromkeys(names))


def _extract_plan_paths(plan: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for text in _iter_plan_strings(plan):
        for raw in re.split(r"\s+-\s+", text.strip(), maxsplit=1)[:1]:
            cleaned = raw.strip().strip("`'\"()[]{}")
            if "/" in cleaned and not re.search(r"\s", cleaned):
                paths.add(cleaned.replace("\\", "/").strip("/"))
        for match in _PLAN_PATH_RE.finditer(text.replace("\\", "/")):
            paths.add(match.group(1).strip("/"))
    return {path for path in paths if path and not path.startswith(("http://", "https://"))}


def _path_looks_like_code(path: str) -> bool:
    norm = path.replace("\\", "/").strip("/")
    suffix = pathlib.PurePosixPath(norm).suffix.lower()
    if suffix in _PLAN_CODE_EXTENSIONS:
        return True
    parts = [part for part in norm.split("/") if part]
    return bool(parts and parts[-1] in {"package.json", "vite.config.ts", "tsconfig.json"})


def _workspace_existing_impl_roots(ctx: ToolContext | None) -> set[str]:
    if ctx is None:
        return set()
    workspace_id = _workspace_id(ctx)
    if not workspace_id:
        return set()
    try:
        repo_root = pathlib.Path(umbrella_tools._resolve_umbrella_repo_root(ctx))
        root = umbrella_tools._workspace_root(repo_root, workspace_id, ctx)
    except Exception:
        try:
            root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir) / "workspaces" / workspace_id
        except Exception:
            return set()
    if not root.is_dir():
        return set()

    roots: set[str] = set()
    for child in root.iterdir():
        name = child.name
        lower = name.lower()
        if lower in _PLAN_NON_IMPL_ROOTS or lower.startswith("."):
            continue
        if child.is_file():
            if child.suffix.lower() in _PLAN_CODE_EXTENSIONS or lower in {
                "package.json",
                "vite.config.ts",
                "tsconfig.json",
            }:
                roots.add(name)
            continue
        if child.is_dir():
            try:
                has_code = any(
                    p.is_file()
                    and (
                        p.suffix.lower() in _PLAN_CODE_EXTENSIONS
                        or p.name.lower()
                        in {"package.json", "vite.config.ts", "tsconfig.json"}
                    )
                    for p in child.rglob("*")
                    if "node_modules" not in {part.lower() for part in p.parts}
                    and "__pycache__" not in {part.lower() for part in p.parts}
                )
            except OSError:
                has_code = False
            if has_code:
                roots.add(name)
    return roots


def _workspace_root_for_policy(ctx: ToolContext | None) -> pathlib.Path | None:
    if ctx is None:
        return None
    workspace_id = _workspace_id(ctx)
    if not workspace_id:
        return None
    try:
        repo_root = pathlib.Path(umbrella_tools._resolve_umbrella_repo_root(ctx))
        root = umbrella_tools._workspace_root(repo_root, workspace_id, ctx)
    except Exception:
        try:
            root = pathlib.Path(ctx.host_repo_root or ctx.repo_dir) / "workspaces" / workspace_id
        except Exception:
            return None
    return root if root.is_dir() else None


def _phase_plan_parallel_root_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    existing = _workspace_existing_impl_roots(ctx)
    if not existing:
        return []
    planned_paths = _extract_plan_paths(plan)
    planned_roots = {
        path.split("/", 1)[0]
        for path in planned_paths
        if "/" in path
        and _path_looks_like_code(path)
        and path.split("/", 1)[0].lower() not in _PLAN_NON_IMPL_ROOTS
    }
    new_roots = planned_roots - existing
    if not new_roots:
        return []

    plan_text = "\n".join(_iter_plan_strings(plan))
    mentions_existing = any(root in plan_text for root in existing)
    has_migration_language = bool(_PLAN_MIGRATION_WORD_RE.search(plan_text))
    if mentions_existing and has_migration_language:
        return []

    return [
        (
            "plan introduces new top-level implementation root(s) "
            f"{sorted(new_roots)} while existing implementation root(s) "
            f"{sorted(existing)} are already present; either use the existing "
            "layout or explicitly plan a migration/refactor with cleanup/removal "
            "of obsolete code"
        )
    ]


def _phase_plan_rebuild_existing_issues(
    ctx: ToolContext | None, plan: dict[str, Any]
) -> list[str]:
    existing = _workspace_existing_impl_roots(ctx)
    if not existing:
        return []
    plan_text = "\n".join(_iter_plan_strings(plan))
    if not _PLAN_REBUILD_EXISTING_RE.search(plan_text):
        return []
    mentions_existing = any(root in plan_text for root in existing)
    if mentions_existing and _PLAN_EXISTING_REPAIR_WORD_RE.search(plan_text):
        return []
    return [
        (
            "plan proposes scaffolding/building project structure from scratch "
            f"while existing implementation root(s) {sorted(existing)} are already "
            "present; create a repair/refactor/integration plan against the "
            "current codebase, or explicitly plan migration/removal of obsolete "
            "code before adding replacements"
        )
    ]


__all__ = [
    '_iter_plan_strings',
    '_repo_root_from_module',
    '_known_phase_tool_names',
    '_is_phase_tool_declaration_site',
    '_iter_declared_phase_tools',
    '_extract_declared_tool_names',
    '_extract_plan_paths',
    '_path_looks_like_code',
    '_workspace_existing_impl_roots',
    '_workspace_root_for_policy',
    '_phase_plan_parallel_root_issues',
    '_phase_plan_rebuild_existing_issues',
]
