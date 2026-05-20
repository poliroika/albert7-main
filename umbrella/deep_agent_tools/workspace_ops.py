"""Workspace write, patch, verification, and delegation helpers."""

from typing import Iterable

from umbrella.deep_agent_tools.workspace_common import *
from umbrella.deep_agent_tools.workspace_gmas import (
    _gmas_context_before_write_block,
    _llm_behavior_fallback_contract_block,
    _llm_runtime_contract_block,
)
from umbrella.deep_agent_tools.workspace_read import _workspace_file_was_read

def _workspace_layout_policy_block(rel_path: str) -> dict[str, Any] | None:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts:
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": rel_path,
            "message": "file_path must name a workspace-relative file.",
        }

    name = parts[-1]
    lower_name = name.lower()
    top = parts[0].lower()
    if len(parts) == 1 and _ROOT_DIAGNOSTIC_WRITE_RE.match(name):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": (
                "Ad-hoc diagnostic/test scripts must not be written into the "
                "workspace root."
            ),
            "next_step": (
                "Use `run_workspace_command` to inspect data live (no file), "
                "fold reusable logic into the package under `src/`, or call "
                "`delete_workspace_file` if a leftover probe is no longer "
                "needed. Real tests belong under `tests/`."
            ),
        }
    if len(parts) == 1 and _ROOT_DOC_WRITE_RE.match(name):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": "Non-README architecture/handoff docs belong under `docs/`.",
            "next_step": f"Use `docs/{name}` unless this file is the workspace README.",
        }
    if (
        len(parts) >= 2
        and top == "src"
        and lower_name.startswith("test_")
        and lower_name.endswith(".py")
    ):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": "Pytest test modules belong under `tests/`, not `src/`.",
            "next_step": f"Write this as `tests/{name}` or keep only production code under `src/`.",
        }
    # Diagnostic/probe Python scripts under ``docs/`` or ``src/scripts/``
    # are the production failure mode the user explicitly called out
    # (extract_requirements.py, probe_docx.py, read_requirements.py,
    # check_format.py). They get checked in, the noise sweep flags
    # them, the agent can't delete them, the run gets stuck.
    if len(parts) >= 2 and top in {"docs", "doc"} and lower_name.endswith(".py"):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": (
                "Python files do not belong under `docs/`. `docs/` is for "
                "Markdown/spec documentation only."
            ),
            "next_step": (
                "Run analysis with `run_workspace_command` (no script "
                "checked in) or move reusable code into `src/<pkg>/...`. "
                "If a leftover script is already on disk, remove it with "
                "`delete_workspace_file`."
            ),
        }
    if (
        len(parts) >= 3
        and top == "src"
        and parts[1].lower() == "scripts"
        and _DIAGNOSTIC_SCRIPT_BASENAME_RE.match(name)
    ):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": (
                "Ad-hoc `check_*/debug_*/probe_*/read_*/extract_*` etc. "
                "scripts must not live under `src/scripts/` either. Real "
                "CLI entrypoints can keep their name; one-off probes "
                "should be live `run_workspace_command` invocations, not "
                "checked-in files."
            ),
            "next_step": (
                "If the logic is reusable, give it a non-diagnostic name "
                "and place it under the package (e.g. "
                "`src/<pkg>/io/docx_loader.py`). Otherwise delete it with "
                "`delete_workspace_file`."
            ),
        }
    # Raw-extracted artefacts (``*_raw.txt`` etc.) are never deliverables.
    # Block them everywhere except inside ``.memory/`` (legitimate
    # scratch).
    if top not in {
        ".memory",
        ".umbrella",
        ".umbrella_scratch",
    } and _RAW_ARTIFACT_BASENAME_RE.match(name):
        return {
            "status": "blocked",
            "reason": "workspace_layout_policy",
            "file_path": norm,
            "message": (
                "Raw-extracted artefacts (`*_raw.*` / `*_extracted.*`) are "
                "scratch output, not deliverables."
            ),
            "next_step": (
                "Produce a clean version (`docs/requirements.md` etc.) and "
                "discard the raw blob. If it is already on disk, remove it "
                "with `delete_workspace_file`."
            ),
        }
    return None


def _workspace_python_impl_roots(seed_path: Path) -> set[str]:
    roots: set[str] = set()
    try:
        iterator = seed_path.rglob("*.py")
        for path in iterator:
            try:
                rel = path.relative_to(seed_path)
            except ValueError:
                continue
            parts = [p for p in rel.parts if p and p != "."]
            if not parts:
                continue
            lowered = [p.lower() for p in parts]
            if any(
                part
                in {
                    ".git",
                    ".memory",
                    ".umbrella",
                    ".umbrella_scratch",
                    ".venv",
                    "__pycache__",
                    "node_modules",
                    "venv",
                }
                for part in lowered
            ):
                continue
            top = lowered[0]
            if top in {"tests", "test", "docs", "doc", "frontend"}:
                continue
            name = lowered[-1]
            if len(parts) == 1 and name in _GREENFIELD_PY_ALLOWED_ROOT_FILES:
                continue
            if name.startswith("test_"):
                continue
            roots.add(parts[0])
    except OSError:
        return set()
    return roots


def _src_python_package_roots_from_paths(paths: Iterable[str]) -> set[str]:
    roots: set[str] = set()
    for raw in paths:
        parts = [
            p
            for p in str(raw or "").replace("\\", "/").strip().lstrip("/").split("/")
            if p and p != "."
        ]
        if len(parts) < 3:
            continue
        if parts[0].lower() != "src":
            continue
        if not parts[-1].lower().endswith(".py"):
            continue
        lowered = [p.lower() for p in parts]
        name = lowered[-1]
        if name.startswith("test_") or "tests" in lowered or "test" in lowered:
            continue
        roots.add(parts[1])
    return roots


def _workspace_src_python_package_roots(seed_path: Path) -> set[str]:
    try:
        paths = [
            str(path.relative_to(seed_path)).replace("\\", "/")
            for path in (seed_path / "src").rglob("*.py")
            if path.is_file()
        ]
    except OSError:
        return set()
    return _src_python_package_roots_from_paths(paths)


def _greenfield_python_src_layout_block(
    seed_path: Path, rel_path: str, *, planned_paths: set[str] | None = None
) -> dict[str, Any] | None:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts:
        return None
    name = parts[-1]
    lower_name = name.lower()
    if not lower_name.endswith(".py"):
        return None
    lowered = [p.lower() for p in parts]
    top = lowered[0]
    if top in {"tests", "test"}:
        return None
    if top in {"docs", "doc"}:
        return None
    if top == "frontend":
        return None
    if lower_name.startswith("test_") or "tests" in lowered or "test" in lowered:
        return None
    if len(parts) == 1 and lower_name in _GREENFIELD_PY_ALLOWED_ROOT_FILES:
        return None
    if top in _GREENFIELD_PY_NON_IMPL_TOPS:
        return None

    planned_lower = {
        str(path or "").replace("\\", "/").strip().lstrip("/").lower()
        for path in (planned_paths or set())
    }
    if top == "src":
        existing_src_roots = _workspace_src_python_package_roots(seed_path)
        planned_src_roots = _src_python_package_roots_from_paths(planned_paths or set())
        if len(parts) < 3:
            return {
                "status": "blocked",
                "reason": "greenfield_python_src_layout_policy",
                "file_path": norm,
                "existing_src_package_roots": sorted(existing_src_roots),
                "planned_src_package_roots": sorted(planned_src_roots),
                "message": (
                    "New Python application/library modules must live under a "
                    "package directory inside `src/<package>/...`, not as "
                    "bare `src/*.py` or `src/__init__.py`."
                ),
                "next_step": (
                    "Move this file under one project package, for example "
                    "`src/<package>/game_engine.py` or "
                    "`src/<package>/api/app.py`, and keep tests under `tests/`."
                ),
            }
        src_root = parts[1]
        all_src_roots = set(existing_src_roots) | set(planned_src_roots) | {src_root}
        if len(all_src_roots) > 1:
            return {
                "status": "blocked",
                "reason": "greenfield_python_src_layout_policy",
                "file_path": norm,
                "existing_src_package_roots": sorted(existing_src_roots),
                "planned_src_package_roots": sorted(planned_src_roots),
                "message": (
                    "Greenfield Python code under `src/` must use one "
                    "canonical package root (`src/<package>/...`), not "
                    "parallel roots such as `src/api`, `src/agents`, and "
                    "`src/config`."
                ),
                "next_step": (
                    "Move related modules under one package root, for example "
                    "`src/<package>/api/...`, `src/<package>/agents/...`, and "
                    "`src/<package>/config/...`."
                ),
            }
        return None

    project_markers = {
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "package.json",
    }
    has_project_marker = any((seed_path / marker).exists() for marker in project_markers)
    has_project_marker = has_project_marker or bool(planned_lower & project_markers)
    if not has_project_marker:
        try:
            workspace_toml = seed_path / "workspace.toml"
            has_project_marker = (
                workspace_toml.is_file()
                and "multi_agent_gmas"
                in workspace_toml.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            has_project_marker = False
    if not has_project_marker:
        return None

    existing_roots = _workspace_python_impl_roots(seed_path)
    existing_roots_lower = {root.lower() for root in existing_roots}
    if parts[0].lower() in existing_roots_lower:
        return None
    return {
        "status": "blocked",
        "reason": "greenfield_python_src_layout_policy",
        "file_path": norm,
        "existing_python_roots": sorted(existing_roots),
        "message": (
            "New Python application/library modules must use a canonical "
            "`src/<package>/...` layout instead of creating parallel "
            "top-level packages."
        ),
        "next_step": (
            "Place this code under `src/<package>/...` (for example "
            "`src/<package>/game_engine/...` or `src/<package>/backend/...`) "
            "and keep tests under `tests/`. If this is an existing project "
            "with an established non-src package root, read that root first "
            "and repair within it instead of starting a new greenfield tree."
        ),
    }


def _python_syntax_block(file_path: str, content: str) -> dict[str, Any] | None:
    if not str(file_path or "").endswith(".py"):
        return None
    try:
        tree = ast.parse(content)
    except SyntaxError as syn:
        snippet = ""
        try:
            line_no = int(syn.lineno or 0)
            if line_no:
                lines = content.splitlines()
                snippet = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        except Exception:
            snippet = ""
        return {
            "status": "blocked",
            "reason": "python_syntax_error",
            "file_path": file_path,
            "error": f"{syn.msg} (line {syn.lineno}, col {syn.offset})",
            "offending_line": snippet,
            "next_step": (
                "Re-emit Python source without escaped quotes; JSON encoding is "
                "handled by the transport layer automatically."
            ),
        }
    if text_encoding_block := _python_text_read_encoding_block(file_path, tree):
        return text_encoding_block
    if quoted_block := _quoted_python_source_lines_block(file_path, content, tree):
        return quoted_block
    if order_block := _python_top_level_order_block(file_path, tree):
        return order_block
    return None


def _python_text_read_encoding_block(
    file_path: str, tree: ast.Module
) -> dict[str, Any] | None:
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "read_text":
            continue
        if node.args:
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        offenders.append(int(getattr(node, "lineno", 0) or 0))
    if not offenders:
        return None
    return {
        "status": "blocked",
        "reason": "python_text_read_encoding_required",
        "file_path": file_path,
        "line_numbers": offenders[:8],
        "message": (
            "Python workspace code/tests must pass an explicit text encoding "
            "to Path.read_text(). On Windows, the locale default can decode "
            "UTF-8 Markdown/docs as a legacy codepage and fail with "
            "UnicodeDecodeError."
        ),
        "next_step": (
            "Use `path.read_text(encoding=\"utf-8\")` or "
            "`path.read_text(encoding=\"utf-8\", errors=\"replace\")` for "
            "diagnostic previews. Keep generated docs/text UTF-8-clean and "
            "avoid mojibake or stray non-task-language characters."
        ),
    }


_QUOTED_PYTHON_SOURCE_RE = re.compile(
    r"^\s*(?:[rRuUbBfF]{0,3})?(?P<quote>['\"]).*(?P=quote)\s*$"
)
_QUOTED_SOURCE_CODE_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"from\s+\S+\s+import\b|"
    r"import\s+\S+|"
    r"class\s+\w+|"
    r"(?:async\s+)?def\s+\w+|"
    r"@\w+|"
    r"return\b|"
    r"if\s+.+:|"
    r"for\s+.+:|"
    r"while\s+.+:|"
    r"try:|"
    r"except\b|"
    r"[A-Za-z_]\w*\s*(?::|=)"
    r")"
)


def _literal_quoted_line_value(line: str) -> str | None:
    stripped = line.strip()
    if not _QUOTED_PYTHON_SOURCE_RE.match(stripped):
        return None
    try:
        value = ast.literal_eval(stripped)
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _quoted_python_source_lines_block(
    file_path: str, content: str, tree: ast.Module
) -> dict[str, Any] | None:
    non_empty = [line for line in content.splitlines() if line.strip()]
    if len(non_empty) < 8:
        return None
    quoted_values = [
        value
        for line in non_empty
        if (value := _literal_quoted_line_value(line)) is not None
    ]
    if len(quoted_values) < max(8, int(len(non_empty) * 0.7)):
        return None
    if any(_stmt_bound_names(stmt) for stmt in tree.body):
        return None
    code_markers = [
        value
        for value in quoted_values
        if _QUOTED_SOURCE_CODE_MARKER_RE.search(value)
    ]
    if len(code_markers) < 3:
        return None
    return {
        "status": "blocked",
        "reason": "quoted_source_lines",
        "file_path": file_path,
        "quoted_line_count": len(quoted_values),
        "non_empty_line_count": len(non_empty),
        "message": (
            "This Python file appears to contain source code escaped as one "
            "string literal per line. It would import successfully but define "
            "none of the intended symbols."
        ),
        "next_step": (
            "Re-emit real Python source lines without wrapping each line in "
            "quotes; JSON and patch transport escaping is handled automatically."
        ),
    }


def _target_bound_names(target: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            names.update(_target_bound_names(elt))
    elif isinstance(target, ast.Starred):
        names.update(_target_bound_names(target.value))
    return names


def _stmt_bound_names(stmt: ast.stmt) -> set[str]:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {stmt.name}
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        names: set[str] = set()
        for alias in stmt.names:
            if alias.name == "*":
                continue
            names.add(alias.asname or alias.name.split(".", 1)[0])
        return names
    if isinstance(stmt, ast.Assign):
        names: set[str] = set()
        for target in stmt.targets:
            names.update(_target_bound_names(target))
        return names
    if isinstance(stmt, ast.AnnAssign):
        return _target_bound_names(stmt.target)
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return _target_bound_names(stmt.target)
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        names: set[str] = set()
        for item in stmt.items:
            if item.optional_vars is not None:
                names.update(_target_bound_names(item.optional_vars))
        return names
    return set()


class _TopLevelRuntimeLoadVisitor(ast.NodeVisitor):
    """Collect names loaded by code executed while importing a module.

    Function bodies are intentionally skipped: forward references inside a
    function are legal because they run later. Decorators/default values still
    execute at import time and are visited.
    """

    def __init__(self) -> None:
        self.loads: list[tuple[str, int]] = []

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self.loads.append((node.id, int(getattr(node, "lineno", 0) or 0)))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        for deco in node.decorator_list:
            self.visit(deco)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        for deco in node.decorator_list:
            self.visit(deco)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword)
        # Class body execution is dynamic enough that this lightweight guard
        # leaves it to real import/verification checks.

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        self.visit(node.test)
        if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            return
        for child in [*node.body, *node.orelse]:
            self.visit(child)


def _runtime_loads_for_stmt(stmt: ast.stmt) -> list[tuple[str, int]]:
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return []
    visitor = _TopLevelRuntimeLoadVisitor()
    visitor.visit(stmt)
    return visitor.loads


def _python_top_level_order_block(
    file_path: str, tree: ast.Module
) -> dict[str, Any] | None:
    future_defs: dict[str, int] = {}
    for stmt in tree.body:
        line = int(getattr(stmt, "lineno", 0) or 0)
        for name in _stmt_bound_names(stmt):
            future_defs.setdefault(name, line)

    known = set(dir(builtins)) | _PY_MAGIC_GLOBALS
    for stmt in tree.body:
        stmt_line = int(getattr(stmt, "lineno", 0) or 0)
        for name, line in _runtime_loads_for_stmt(stmt):
            if name in known:
                continue
            defined_at = future_defs.get(name)
            if defined_at is not None and defined_at > max(line, stmt_line):
                return {
                    "status": "blocked",
                    "reason": "python_top_level_name_order",
                    "file_path": file_path,
                    "name": name,
                    "used_line": line or stmt_line,
                    "defined_line": defined_at,
                    "message": (
                        f"`{name}` is used by top-level import-time code before "
                        "it is defined. This would raise NameError when the module imports."
                    ),
                    "next_step": (
                        "Move the definition/import above the first top-level use, "
                        "or defer the reference inside a function that runs after setup."
                    ),
                }
        known.update(_stmt_bound_names(stmt))
    return None


def _planned_python_paths(planned_paths: set[str] | None) -> set[str]:
    return {
        str(path).replace("\\", "/").strip("/")
        for path in (planned_paths or set())
        if str(path).replace("\\", "/").strip("/").endswith(".py")
    }


def _module_rel_candidates(module: str) -> list[str]:
    parts = [part for part in module.split(".") if part]
    if not parts:
        return []
    rel = "/".join(parts)
    return [f"{rel}.py", f"{rel}/__init__.py"]


def _module_exists_in_workspace(
    seed_path: Path,
    module: str,
    *,
    planned_paths: set[str],
) -> bool:
    for rel in _module_rel_candidates(module):
        if rel in planned_paths or (seed_path / rel).is_file():
            return True
    return False


def _module_content_in_workspace(
    seed_path: Path,
    module: str,
    *,
    planned_content_by_path: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    planned_content_by_path = planned_content_by_path or {}
    for rel in _module_rel_candidates(module):
        if rel in planned_content_by_path:
            return rel, str(planned_content_by_path[rel])
        path = seed_path / rel
        if path.is_file():
            try:
                return rel, path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return None
    return None


def _python_module_exported_symbols(
    seed_path: Path,
    module: str,
    *,
    planned_content_by_path: dict[str, str] | None = None,
) -> set[str] | None:
    found = _module_content_in_workspace(
        seed_path, module, planned_content_by_path=planned_content_by_path
    )
    if found is None:
        return None
    _, content = found
    try:
        tree = ast.parse(content or "")
    except SyntaxError:
        return None
    if any(
        isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
        and stmt.name == "__getattr__"
        for stmt in tree.body
    ):
        return None
    symbols: set[str] = set()
    for stmt in tree.body:
        symbols.update(_stmt_bound_names(stmt))
    return symbols


def _module_top_is_local(
    seed_path: Path,
    module: str,
    *,
    planned_paths: set[str],
) -> bool:
    top = module.split(".", 1)[0]
    if not top:
        return False
    if (seed_path / f"{top}.py").is_file() or (seed_path / top).exists():
        return True
    prefix = f"{top}/"
    return any(path == f"{top}.py" or path.startswith(prefix) for path in planned_paths)


def _relative_import_module(file_path: str, node: ast.ImportFrom) -> str:
    parts = [part for part in file_path.replace("\\", "/").split("/") if part]
    if parts and parts[-1].endswith(".py"):
        if parts[-1] == "__init__.py":
            package = parts[:-1]
        else:
            package = parts[:-1]
    else:
        package = parts
    level = int(node.level or 0)
    if level:
        base = package[: max(0, len(package) - level + 1)]
    else:
        base = []
    if node.module:
        base.extend(part for part in node.module.split(".") if part)
    return ".".join(base)


def _python_import_resolution_block(
    seed_path: Path,
    file_path: str,
    content: str,
    *,
    planned_paths: set[str] | None = None,
    planned_content_by_path: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if not str(file_path or "").endswith(".py"):
        return None
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    planned = _planned_python_paths(planned_paths)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            module = (
                _relative_import_module(file_path, node)
                if node.level
                else str(node.module or "")
            )
            if not module:
                continue
            candidates = [module]
            if node.module is None:
                candidates = [
                    ".".join(part for part in [module, alias.name] if part)
                    for alias in node.names
                    if alias.name != "*"
                ]
            for candidate in candidates:
                if not _module_top_is_local(seed_path, candidate, planned_paths=planned):
                    continue
                if _module_exists_in_workspace(seed_path, candidate, planned_paths=planned):
                    continue
                return {
                    "status": "blocked",
                    "reason": "python_missing_local_import",
                    "file_path": file_path,
                    "module": candidate,
                    "line": int(getattr(node, "lineno", 0) or 0),
                    "message": (
                        f"Local import `{candidate}` does not resolve inside the workspace. "
                        "Do not leave package imports broken between patches."
                    ),
                    "next_step": (
                        "Create the imported module in the same patch first, correct the import, "
                        "or remove the package export until the implementation exists."
                    ),
                }
            if node.module is not None:
                module_is_local = _module_top_is_local(
                    seed_path, module, planned_paths=planned
                )
                if module_is_local and _module_exists_in_workspace(
                    seed_path, module, planned_paths=planned
                ):
                    exported = _python_module_exported_symbols(
                        seed_path,
                        module,
                        planned_content_by_path=planned_content_by_path,
                    )
                    if exported is not None:
                        for alias in node.names:
                            if alias.name == "*":
                                continue
                            if alias.name in exported:
                                continue
                            submodule = ".".join(
                                part for part in [module, alias.name] if part
                            )
                            if _module_exists_in_workspace(
                                seed_path, submodule, planned_paths=planned
                            ):
                                continue
                            return {
                                "status": "blocked",
                                "reason": "python_missing_local_import_symbol",
                                "file_path": file_path,
                                "module": module,
                                "imported_name": alias.name,
                                "line": int(getattr(node, "lineno", 0) or 0),
                                "message": (
                                    f"Local import `{alias.name}` is not exported by "
                                    f"`{module}`. Do not leave package imports broken "
                                    "between patches or add duplicate replacement imports."
                                ),
                                "next_step": (
                                    "Fix the import with a real replacement hunk that "
                                    "removes the stale line, create/export the missing "
                                    "symbol in the imported module, or use a paired "
                                    "same-path Delete/Add replacement after reading the "
                                    "file in full."
                                ),
                            }
        elif isinstance(node, ast.Import):
            for alias in node.names:
                candidate = alias.name
                if not _module_top_is_local(seed_path, candidate, planned_paths=planned):
                    continue
                if _module_exists_in_workspace(seed_path, candidate, planned_paths=planned):
                    continue
                return {
                    "status": "blocked",
                    "reason": "python_missing_local_import",
                    "file_path": file_path,
                    "module": candidate,
                    "line": int(getattr(node, "lineno", 0) or 0),
                    "message": (
                        f"Local import `{candidate}` does not resolve inside the workspace. "
                        "Do not leave package imports broken between patches."
                    ),
                    "next_step": (
                        "Create the imported module in the same patch first, correct the import, "
                        "or remove the import until the implementation exists."
                    ),
                }
    return None


def _workspace_line_delta(old_content: str, new_content: str) -> int:
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    return max(0, len(new_lines) - len(old_lines))


_EMPTY_FILE_ALLOWED_BASENAMES = {"__init__.py", ".gitkeep", ".keep"}
_PLACEHOLDER_INTEGRATION_RE = re.compile(
    r"(?is)\b(?:gmas|llm|skill[_-]?compatibility|multi[_-]?agent)\b"
    r".{0,160}\b(?:placeholder|stub|temporary|todo|will\s+be\s+fully\s+integrated)\b|"
    r"\b(?:placeholder|stub|temporary|todo)\b"
    r".{0,160}\b(?:gmas|llm|skill[_-]?compatibility|multi[_-]?agent)\b|"
    r"\b(?:import\s+gmas|from\s+gmas|gmas|llm|multi[_-]?agent)\b"
    r".{0,180}\b(?:satisfy|pass|appease|silence)\b"
    r".{0,120}\b(?:skill|compliance|import[_-]?check|quality|check|requirement)\b|"
    r"\b(?:satisfy|pass|appease|silence)\b"
    r".{0,120}\b(?:skill|compliance|import[_-]?check|quality|check|requirement)\b"
    r".{0,180}\b(?:gmas|llm|multi[_-]?agent)\b"
)


def _empty_workspace_file_block(rel_path: str, content: str) -> dict[str, Any] | None:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    pure = Path(norm)
    suffix = pure.suffix.lower()
    name = pure.name.lower()
    if str(content or "").strip():
        return None
    if name in _EMPTY_FILE_ALLOWED_BASENAMES:
        return None
    if suffix not in _SOURCE_TRUNCATION_EXTENSIONS:
        return None
    return {
        "status": "blocked",
        "reason": "empty_workspace_file",
        "file_path": norm,
        "message": (
            "Non-package source, test, config, and documentation files must not "
            "be created empty."
        ),
        "next_step": (
            "Add the real implementation, test, or documentation content in "
            "this patch. Empty package marker files are only allowed for "
            "`__init__.py` or explicit keep files."
        ),
    }


def _placeholder_integration_bridge_block(
    rel_path: str, content: str
) -> dict[str, Any] | None:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    suffix = Path(norm).suffix.lower()
    parts = {part.lower() for part in norm.split("/") if part}
    if suffix not in _LLM_BEHAVIOR_SOURCE_EXTENSIONS:
        return None
    if "tests" in parts or "test" in parts or "docs" in parts:
        return None
    text = str(content or "")
    if not text or not _PLACEHOLDER_INTEGRATION_RE.search(text):
        return None
    return {
        "status": "blocked",
        "reason": "placeholder_integration_bridge",
        "file_path": norm,
        "message": (
            "Generated workspace code must not satisfy skill or integration "
            "checks with placeholder GMAS/LLM bridge files."
        ),
        "next_step": (
            "Implement the real integration owned by the current subtask, or "
            "leave it to the planned future subtask and close only after the "
            "current subtask's declared success test passes."
        ),
    }


def _top_level_python_symbols(content: str) -> set[str]:
    try:
        tree = ast.parse(content or "")
    except SyntaxError:
        return set()
    symbols: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id.isupper():
                symbols.add(node.target.id)
    return symbols


def _generic_source_markers(content: str) -> set[str]:
    markers: set[str] = set()
    for match in re.finditer(
        r"(?m)^\s*(?:export\s+)?(?:class|function|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
        content or "",
    ):
        markers.add(match.group(1))
    for match in re.finditer(
        r"(?m)^\s*(?:def|class)\s+([A-Za-z_][\w]*)",
        content or "",
    ):
        markers.add(match.group(1))
    return markers


def _source_truncation_block(
    rel_path: str,
    old_content: str,
    new_content: str,
    *,
    allow_large_overwrite: bool = False,
    validation_summary: str = "",
) -> dict[str, Any] | None:
    """Block likely accidental full-file truncation during repair loops."""

    suffix = Path(str(rel_path or "").replace("\\", "/")).suffix.lower()
    if suffix not in _SOURCE_TRUNCATION_EXTENSIONS:
        return None
    old_text = str(old_content or "")
    new_text = str(new_content or "")
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    if len(old_text) < 3000 and len(old_lines) < 80:
        return None
    byte_ratio = (len(new_text) / max(1, len(old_text))) if old_text else 1.0
    line_ratio = (len(new_lines) / max(1, len(old_lines))) if old_lines else 1.0
    if byte_ratio >= 0.45 and line_ratio >= 0.45:
        return None
    if allow_large_overwrite and str(validation_summary or "").strip():
        return None
    if suffix in {".py", ".pyi"}:
        old_symbols = _top_level_python_symbols(old_text)
        new_symbols = _top_level_python_symbols(new_text)
    else:
        old_symbols = _generic_source_markers(old_text)
        new_symbols = _generic_source_markers(new_text)
    symbol_drop = (
        len(old_symbols) >= 3
        and len(new_symbols) <= max(1, len(old_symbols) // 3)
    )
    severe_drop = byte_ratio < 0.20 and line_ratio < 0.25 and len(new_text) < 2000
    if not (symbol_drop or severe_drop):
        return None
    return {
        "status": "blocked",
        "reason": "source_truncation_guard",
        "file_path": str(rel_path or "").replace("\\", "/").strip().lstrip("/"),
        "old_chars": len(old_text),
        "new_chars": len(new_text),
        "old_lines": len(old_lines),
        "new_lines": len(new_lines),
        "old_symbol_count": len(old_symbols),
        "new_symbol_count": len(new_symbols),
        "message": (
            "This full-file write would remove most of an existing source file. "
            "That usually means the model is repairing from a truncated preview "
            "or shell output, not from the authoritative file."
        ),
        "next_step": (
            "Re-read the full current file with read_file/repo_read offsets if "
            "needed, then apply a targeted patch or send the complete replacement. "
            "Only for an intentional large rewrite, call the full-file write again "
            "with allow_large_overwrite=true and a validation_summary explaining "
            "what was preserved/replaced."
        ),
    }


def _record_workspace_diff(
    ctx: Any,
    *,
    file_path: str,
    old_content: str,
    new_content: str,
    added_file: bool = False,
    deleted_file: bool = False,
) -> None:
    try:
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            view = {}
            setattr(ctx, "loop_state_view", view)
        diff = view.setdefault("subtask_diff", {})
        if not isinstance(diff, dict):
            diff = {}
            view["subtask_diff"] = diff
        norm = str(file_path or "").replace("\\", "/").strip().lstrip("/")
        added = (
            len(new_content.splitlines())
            if added_file
            else _workspace_line_delta(old_content, new_content)
        )
        entry = diff.setdefault(
            norm, {"lines_added": 0, "added_file": False, "deleted_file": False}
        )
        if isinstance(entry, dict):
            entry["lines_added"] = int(entry.get("lines_added") or 0) + int(added)
            entry["added_file"] = bool(entry.get("added_file")) or bool(added_file)
            entry["deleted_file"] = bool(entry.get("deleted_file")) or bool(
                deleted_file
            )
    except Exception:
        log.debug("Failed to record workspace diff", exc_info=True)


def _record_subtask_discovery_tool_call(ctx: Any, tool_name: str) -> None:
    try:
        view = getattr(ctx, "loop_state_view", None)
        if not isinstance(view, dict):
            return
        counts = view.setdefault("subtask_discovery_calls_by_tool", {})
        if not isinstance(counts, dict):
            counts = {}
            view["subtask_discovery_calls_by_tool"] = counts
        counts[tool_name] = int(counts.get(tool_name) or 0) + 1
    except Exception:
        log.debug("Failed to record subtask discovery call", exc_info=True)


def _verification_steps_from_toml(text: str) -> list[dict[str, Any]]:
    try:
        data = tomllib.loads(text or "")
    except Exception:
        return []
    verification = data.get("verification") if isinstance(data, dict) else None
    if not isinstance(verification, dict):
        return []
    steps = verification.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _verification_step_name(step: dict[str, Any], index: int) -> str:
    value = (
        step.get("name")
        or step.get("id")
        or step.get("command")
        or step.get("path")
        or index
    )
    return str(value).strip()


def _verification_step_kind(step: dict[str, Any]) -> str:
    return str(step.get("kind") or step.get("type") or "").strip().lower()


def _workspace_toml_verification_guard(
    seed_path: Path,
    rel_path: str,
    new_content: str,
) -> dict[str, Any] | None:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if norm != "workspace.toml":
        return None
    old_path = seed_path / "workspace.toml"
    if not old_path.is_file():
        return None
    try:
        old_content = old_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    old_steps = _verification_steps_from_toml(old_content)
    new_steps = _verification_steps_from_toml(new_content)
    if not old_steps:
        return None
    dropped_count = len(new_steps) < len(old_steps)
    old_by_name = {
        _verification_step_name(step, idx): _verification_step_kind(step)
        for idx, step in enumerate(old_steps)
    }
    new_by_name = {
        _verification_step_name(step, idx): _verification_step_kind(step)
        for idx, step in enumerate(new_steps)
    }
    missing_names = [name for name in old_by_name if name and name not in new_by_name]
    downgraded = [
        name
        for name, old_kind in old_by_name.items()
        if old_kind in _STRONG_VERIFICATION_KINDS
        and new_by_name.get(name) == "file_exists"
    ]
    replacement_strong_count = sum(
        1 for kind in new_by_name.values() if kind in _STRONG_VERIFICATION_KINDS
    )
    old_strong_count = sum(
        1 for kind in old_by_name.values() if kind in _STRONG_VERIFICATION_KINDS
    )
    dropped_strong = bool(missing_names) and replacement_strong_count < old_strong_count
    if dropped_count or downgraded or dropped_strong:
        return {
            "status": "blocked",
            "reason": "verification_self_weakening_blocked",
            "file_path": norm,
            "old_step_count": len(old_steps),
            "new_step_count": len(new_steps),
            "missing_steps": missing_names[:10],
            "downgraded_steps": downgraded[:10],
            "message": (
                "workspace.toml verification cannot be weakened during a run. "
                "Add stronger checks or fix existing checks instead of deleting/downgrading them."
            ),
            "next_step": (
                "Keep prior shell/pytest/smoke verification coverage and let "
                "umbrella.verification.spec_loader augment safety-critical local tests."
            ),
        }
    return None


def update_workspace_seed(
    ctx: Any,
    workspace_id: str,
    file_path: str,
    new_content: str,
    create_backup: bool = True,
    allow_large_overwrite: bool = False,
    validation_summary: str = "",
) -> str:
    try:
        from umbrella.control_plane.workspace_code_update import (
            update_seed_workspace_file,
        )

        coerced: Any = new_content
        if isinstance(coerced, dict) and isinstance(coerced.get("new_content"), str):
            coerced = coerced["new_content"]
        if not isinstance(coerced, str):
            actual_type = type(coerced).__name__
            sample = ""
            try:
                if isinstance(coerced, dict):
                    sample = "keys=" + ",".join(sorted(map(str, coerced.keys()))[:8])
            except Exception:
                sample = ""
            return _json(
                {
                    "status": "blocked",
                    "reason": "new_content_must_be_string",
                    "file_path": file_path,
                    "got_type": actual_type,
                    "sample": sample,
                    "hint": (
                        "`new_content` must be the raw Python/text source as a JSON string, "
                        "NOT an object. The wrapper fields you may have seen in past tool "
                        "results (new_content_len, new_content_sha256, new_content_truncated) "
                        "are OUTPUT-only metadata and must not be sent back as input."
                    ),
                    "next_step": (
                        "Re-emit the call with `new_content` as a single JSON string "
                        "containing the file contents."
                    ),
                }
            )

        new_content = coerced

        repo_root = _resolve_umbrella_repo_root(ctx)
        seed_path = _workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="update_workspace_seed", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not seed_path.exists():
            return f"Workspace not found: {workspace_id}"
        if gmas_block := _gmas_context_before_write_block(ctx, workspace_id, seed_path):
            return _json(gmas_block)
        if phase_order_block := _phase_plan_write_order_block(ctx):
            return _json(phase_order_block)
        file_path = _strip_workspace_prefix(workspace_id, file_path)
        if layout_block := _workspace_layout_policy_block(file_path):
            return _json(layout_block)
        if src_layout_block := _greenfield_python_src_layout_block(seed_path, file_path):
            return _json(src_layout_block)
        if verification_block := _workspace_toml_verification_guard(
            seed_path, file_path, new_content
        ):
            return _json(verification_block)
        if llm_contract_block := _llm_runtime_contract_block(file_path, new_content):
            return _json(llm_contract_block)
        if behavior_fallback_block := _llm_behavior_fallback_contract_block(
            file_path, new_content
        ):
            return _json(behavior_fallback_block)
        target = _workspace_path(seed_path, file_path)
        if syntax_block := _python_syntax_block(file_path, new_content):
            return _json(syntax_block)
        if import_block := _python_import_resolution_block(
            seed_path,
            file_path,
            new_content,
            planned_paths={file_path},
            planned_content_by_path={file_path: new_content},
        ):
            return _json(import_block)
        old_content_for_diff = ""
        added_file_for_diff = not target.exists()
        if target.exists() and target.is_file():
            old_content = target.read_text(encoding="utf-8", errors="replace")
            old_content_for_diff = old_content
            if truncation_block := _source_truncation_block(
                file_path,
                old_content,
                new_content,
                allow_large_overwrite=allow_large_overwrite,
                validation_summary=validation_summary,
            ):
                return _json(truncation_block)
            old_lines = old_content.count("\n") + 1
            new_lines = new_content.count("\n") + 1
            large_file = len(old_content) >= 20000 or old_lines >= 400
            suspicious_shrink = len(new_content) < max(
                12000, int(len(old_content) * 0.75)
            )
            suspicious_line_drop = new_lines < max(200, int(old_lines * 0.75))
            if large_file and (suspicious_shrink or suspicious_line_drop):
                if not allow_large_overwrite or not validation_summary.strip():
                    return _json(
                        {
                            "status": "blocked",
                            "reason": "large_file_overwrite_guard",
                            "file_path": file_path,
                            "old_chars": len(old_content),
                            "new_chars": len(new_content),
                            "old_lines": old_lines,
                            "new_lines": new_lines,
                            "next_step": (
                                "Read the file, make a smaller targeted change, or call again with "
                                "allow_large_overwrite=true and a validation_summary explaining why the "
                                "large replacement is correct."
                            ),
                        }
                    )
        result = update_seed_workspace_file(
            seed_path=seed_path,
            relative_file_path=file_path,
            new_content=new_content,
            create_backup=create_backup,
            backup_dir=repo_root / ".umbrella" / "backups",
        )
        if not result.applied:
            return f"Update failed: {result.error or 'unknown error'}"
        _record_workspace_diff(
            ctx,
            file_path=file_path,
            old_content=old_content_for_diff,
            new_content=new_content,
            added_file=added_file_for_diff,
        )
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="change",
            summary=f"Updated {file_path}",
            details=f"Backup: {result.backup_path or 'none'}",
            severity="info",
            tags="change,seed",
        )
        advisory = _gmas_first_write_advisory(
            ctx, repo_root=repo_root, workspace_id=workspace_id, file_path=file_path
        )
        body = f"Updated {file_path}\nBackup: {result.backup_path or 'none'}"
        if advisory:
            body += "\n\n" + advisory
        return body
    except Exception as e:
        log.error("Seed update failed: %s", e, exc_info=True)
        return f"WARNING: seed update error: {e}"


def _phase_plan_write_order_block(ctx: Any) -> dict[str, Any] | None:
    """Block writes that jump ahead of an explicit non-write subtask gate."""
    try:
        from ouroboros.tools.phase_control import (
            _current_phase_node,
            _first_incomplete_subtask,
            _is_phase_run_context,
            _latest_tool_result_for_task,
            _phase_subtasks,
            _required_tool_from_success_test,
            _read_phase_plan,
            _subtask_success_test_text,
        )

        if not _is_phase_run_context(ctx):
            return None
        plan = _read_phase_plan(ctx)
        if not isinstance(plan, dict):
            return None
        current_phase = _current_phase_node(ctx, plan)
        first = _first_incomplete_subtask(_phase_subtasks(current_phase))
        if not isinstance(first, dict):
            return None
        subtask_id = str(first.get("id") or "").strip()
        required_tool = _required_tool_from_success_test(
            _subtask_success_test_text(first)
        )
        prewrite_tools = {
            "harness_run",
            "web_search",
            "deep_search",
            "github_project_search",
            "mcp_discover",
        }
        if required_tool not in prewrite_tools:
            return None
        if _latest_tool_result_for_task(
            ctx,
            tool_name=required_tool,
            subtask_id=subtask_id,
        ):
            return None
        return {
            "status": "blocked",
            "reason": "phase_subtask_order_before_write",
            "subtask_id": subtask_id,
            "required_tool": required_tool,
            "message": (
                f"The next phase-plan subtask `{subtask_id}` declares "
                f"`{required_tool}` as its success test. Run that tool before "
                "applying workspace writes so execution follows the accepted plan."
            ),
        }
    except Exception:
        log.debug("phase plan write order guard failed open", exc_info=True)
        return None


def _phase_subtask_retry_escalation_block(
    ctx: Any, *, tool_name: str
) -> dict[str, Any] | None:
    """Require watcher review after repeated declared success-test failures."""

    try:
        from ouroboros.tools.phase_control import (
            _phase_subtask_retry_escalation_block as _phase_control_retry_block,
        )

        return _phase_control_retry_block(ctx, tool_name=tool_name)
    except Exception:
        log.debug("phase subtask retry escalation guard failed open", exc_info=True)
        return None


def _active_declared_success_test_edit_block(
    ctx: Any, *, rel_path: str
) -> dict[str, Any] | None:
    """Block attempts to repair implementation failures by editing the active test.

    A real execute run tried to resolve repeated `tests/test_state.py` failures by
    rewriting the declared success-test file instead of repairing the missing
    implementation API. The broader test-weakening guard catches large removals,
    but small edits such as changing `world.get_state_summary()` to `world.to_dict()`
    can preserve test item counts while still weakening the accepted contract.
    Another run hit the same pattern after only one failing success-test run.
    """

    try:
        from umbrella.deep_agent_tools.phase_control_base import (
            _is_phase_run_context,
            _read_phase_plan,
            _subtask_success_test_text,
        )
        from umbrella.deep_agent_tools.phase_control_completion import (
            _current_phase_node,
            _first_incomplete_subtask,
            _phase_subtask_retry_state,
            _phase_subtasks,
            _success_test_command_groups,
        )
        from ouroboros.tools.git import (
            _repo_write_active_success_test_has_latest_failure,
            _repo_write_success_test_targets_rel,
        )

        if not _is_phase_run_context(ctx):
            return None
        plan = _read_phase_plan(ctx)
        if not isinstance(plan, dict):
            return None
        current_phase = _current_phase_node(ctx, plan)
        if not isinstance(current_phase, dict):
            return None
        if str(current_phase.get("id") or "").strip() != "execute":
            return None
        subtask = _first_incomplete_subtask(_phase_subtasks(current_phase))
        if not isinstance(subtask, dict):
            return None
        success_text = _subtask_success_test_text(subtask)
        norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
        if not _repo_write_success_test_targets_rel(success_text, norm):
            return None
        state = _phase_subtask_retry_state(ctx)
        if not state:
            return None
        failed_attempts = int(state.get("failures") or 0)
        if failed_attempts < 1:
            return None
        groups = _success_test_command_groups(success_text)
        if not groups:
            return None
        if not _repo_write_active_success_test_has_latest_failure(
            ctx,
            plan=plan,
            groups=groups,
            rel=norm,
        ):
            return None
        migration_reason = _active_success_test_contract_migration_reason(
            plan=plan,
            subtask=subtask,
            rel_path=norm,
            latest_failure_time=state.get("latest_failure_time"),
        )
        if migration_reason:
            return None
        return {
            "status": "blocked",
            "reason": "active_success_test_edit_after_failure",
            "file_path": norm,
            "subtask_id": str(subtask.get("id") or ""),
            "success_test": success_text,
            "failed_attempts": failed_attempts,
            "message": (
                "The active declared success-test file has already failed for "
                "the current execute subtask. Do not repair the subtask by "
                "changing that test contract."
            ),
            "next_step": (
                "Repair the implementation/source/config that the test is "
                "exercising. If the declared success test itself is genuinely "
                "wrong, do not keep calling `request_watcher_review`. First "
                "call `mutate_phase_plan` with "
                "`patch={\"subtasks\":[{\"id\":\""
                + str(subtask.get("id") or "")
                + "\",\"contract_migration_reason\":\"why the generated "
                "test contract is internally wrong\",\"contract_migration_files\":[\""
                + norm
                + "\"]}]}`. After that accepted plan mutation, edit only the "
                "minimal generated test assertion needed to preserve the "
                "intended behavior and rerun the same success_test."
            ),
        }
    except Exception:
        log.debug("active success-test edit guard failed open", exc_info=True)
        return None


_CONTRACT_MIGRATION_KEYS = {
    "contract_migration_reason",
    "test_contract_migration_reason",
    "success_test_contract_migration_reason",
    "contract_migration",
    "test_contract_migration",
    "success_test_contract_migration",
}


def _migration_patch_reason(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in _CONTRACT_MIGRATION_KEYS:
        value = item.get(key)
        if isinstance(value, dict):
            text = str(value.get("reason") or value.get("summary") or "").strip()
        else:
            text = str(value or "").strip()
        if text:
            return text
    return ""


def _migration_patch_mentions_file(item: Any, rel_path: str) -> bool:
    if not isinstance(item, dict):
        return False
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    raw_files = item.get("contract_migration_files") or item.get("test_contract_migration_files")
    if raw_files is None:
        raw_files = item.get("files") or item.get("file_paths")
    if isinstance(raw_files, str):
        raw_files = [raw_files]
    if not isinstance(raw_files, list):
        return True
    files = {
        str(file_path or "").replace("\\", "/").strip().lstrip("/")
        for file_path in raw_files
        if str(file_path or "").strip()
    }
    return not files or norm in files


def _active_success_test_contract_migration_reason(
    *,
    plan: dict[str, Any],
    subtask: dict[str, Any],
    rel_path: str,
    latest_failure_time: Any,
) -> str:
    subtask_id = str(subtask.get("id") or "").strip()
    if not subtask_id:
        return ""
    try:
        failure_time = float(latest_failure_time or 0)
    except (TypeError, ValueError):
        failure_time = 0.0
    edits = plan.get("edits_log")
    if not isinstance(edits, list):
        return ""
    for edit in reversed(edits):
        if not isinstance(edit, dict):
            continue
        try:
            edit_time = float(edit.get("timestamp") or 0)
        except (TypeError, ValueError):
            edit_time = 0.0
        if failure_time and edit_time and edit_time < failure_time:
            continue
        patch = edit.get("patch")
        if not isinstance(patch, dict):
            continue
        for item in patch.get("subtasks") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() != subtask_id:
                continue
            reason = _migration_patch_reason(item)
            if reason and _migration_patch_mentions_file(item, rel_path):
                return reason
    return ""


def _active_success_test_contract_migration_reason_for_path(
    ctx: Any, rel_path: str
) -> str:
    try:
        from umbrella.deep_agent_tools.phase_control_base import (
            _is_phase_run_context,
            _read_phase_plan,
            _subtask_success_test_text,
        )
        from umbrella.deep_agent_tools.phase_control_completion import (
            _current_phase_node,
            _first_incomplete_subtask,
            _phase_subtask_retry_state,
            _phase_subtasks,
            _success_test_command_groups,
        )
        from ouroboros.tools.git import (
            _repo_write_active_success_test_has_latest_failure,
            _repo_write_success_test_targets_rel,
        )

        if not _is_phase_run_context(ctx):
            return ""
        plan = _read_phase_plan(ctx)
        if not isinstance(plan, dict):
            return ""
        current_phase = _current_phase_node(ctx, plan)
        if not isinstance(current_phase, dict):
            return ""
        if str(current_phase.get("id") or "").strip() != "execute":
            return ""
        subtask = _first_incomplete_subtask(_phase_subtasks(current_phase))
        if not isinstance(subtask, dict):
            return ""
        success_text = _subtask_success_test_text(subtask)
        norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
        if not _repo_write_success_test_targets_rel(success_text, norm):
            return ""
        state = _phase_subtask_retry_state(ctx)
        if not state or int(state.get("failures") or 0) < 1:
            return ""
        groups = _success_test_command_groups(success_text)
        if not groups:
            return ""
        if not _repo_write_active_success_test_has_latest_failure(
            ctx,
            plan=plan,
            groups=groups,
            rel=norm,
        ):
            return ""
        return _active_success_test_contract_migration_reason(
            plan=plan,
            subtask=subtask,
            rel_path=norm,
            latest_failure_time=state.get("latest_failure_time"),
        )
    except Exception:
        log.debug(
            "active success-test contract migration lookup failed open",
            exc_info=True,
        )
        return ""


_PATCH_HUNK_MISMATCH_REPLACEMENT_THRESHOLD = 2


def _tool_result_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("result_preview") or row.get("result") or {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _patch_payload_mentions_path(payload: dict[str, Any], rel_path: str) -> bool:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if not norm:
        return False
    direct_values = (
        payload.get("file_path"),
        payload.get("path"),
        payload.get("existing_file"),
    )
    if any(str(value or "").replace("\\", "/").strip().lstrip("/") == norm for value in direct_values):
        return True
    applied = payload.get("applied")
    if isinstance(applied, list):
        return any(norm in str(item).replace("\\", "/") for item in applied)
    return False


def _patch_hunks_contain_escaped_line_endings(hunks: Any) -> bool:
    """Detect hunks copied from JSON-rendered read previews.

    Real patch lines must be separated by actual newlines. When a model copies
    `read_file` JSON content verbatim it can emit lines ending in the literal
    characters ``\r`` or ``\n``; those can never match the file text.
    """

    if not isinstance(hunks, list):
        return False
    for hunk in hunks:
        if not isinstance(hunk, list):
            continue
        for line in hunk:
            text = str(line or "")
            stripped = text.rstrip()
            if (
                stripped.endswith("\\r")
                or stripped.endswith("\\n")
                or "\\r\\n" in stripped
            ):
                return True
    return False


def _latest_failure_line_for_path(ctx: Any, rel_path: str) -> int | None:
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    drive_root = getattr(ctx, "drive_root", None)
    if not task_id or drive_root is None:
        return None
    logs_path = Path(drive_root) / "logs" / "tools.jsonl"
    try:
        rows = logs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if not norm:
        return None
    path_pattern = re.escape(norm).replace("/", r"[\\/]+")
    line_re = re.compile(rf"{path_pattern}:(?P<line>\d+)")
    for raw in reversed(rows[-300:]):
        try:
            row = json.loads(raw)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("task_id") or "") != task_id:
            continue
        text = json.dumps(row, ensure_ascii=False, default=str)
        match = line_re.search(text)
        if not match:
            continue
        try:
            line_no = int(match.group("line"))
        except (TypeError, ValueError):
            continue
        if line_no > 0:
            return line_no
    return None


def _line_context_payload(
    text: str, line_no: int | None, *, radius: int = 6
) -> list[dict[str, Any]]:
    if not line_no or line_no < 1:
        return []
    lines = str(text or "").splitlines()
    if not lines:
        return []
    start = max(1, int(line_no) - radius)
    end = min(len(lines), int(line_no) + radius)
    context: list[dict[str, Any]] = []
    for idx in range(start, end + 1):
        value = lines[idx - 1]
        if len(value) > 240:
            value = value[:237] + "..."
        context.append({"line": idx, "text": value})
    return context


def _patch_hunk_mismatch_payload(
    ctx: Any,
    *,
    rel_path: str,
    error: str,
    migration_reason: str,
    old_content: str,
    hunks: Any,
) -> dict[str, Any]:
    escaped_line_endings = _patch_hunks_contain_escaped_line_endings(hunks)
    failure_line = _latest_failure_line_for_path(ctx, rel_path)
    read_hint = ""
    if failure_line:
        line_start = max(1, int(failure_line) - 8)
        read_hint = (
            f'read_file(file_path="{rel_path}", line_start={line_start}, '
            "line_count=32)"
        )

    next_parts: list[str] = []
    if migration_reason:
        next_parts.append(
            "This file has an accepted contract migration, so preserve every "
            "existing test item and retry with a tiny exact `*** Update File:` "
            "hunk for only the wrong generated line or smallest contradictory "
            "block. Do not use full Delete/Add replacement unless you can "
            "preserve the whole file verbatim except the migrated assertion."
        )
    else:
        next_parts.append(
            "Re-read the file and retry once with a smaller exact hunk. If it "
            "still mismatches, use one `apply_workspace_patch` with paired "
            f"same-path `*** Delete File: {rel_path}` and "
            f"`*** Add File: {rel_path}` entries for an audited replacement, "
            "or call `request_watcher_review`. Do not create "
            "`.new`/`_corrected` sidecar files or shell rewrites."
        )
    if escaped_line_endings:
        next_parts.append(
            "The attempted hunk appears to contain literal JSON-rendered line "
            "endings such as `\\r` or `\\n`. Patch hunks must use real line "
            "breaks and literal source lines; do not copy JSON `content` text "
            "with escaped line endings into the hunk."
        )
    if read_hint:
        next_parts.append(
            f"Use `{read_hint}` to get the exact local context around the "
            "latest failure line, then paste those displayed source lines as "
            "normal patch lines. When a line-slice read returns "
            "`line_range_complete=true`, that requested slice is complete; "
            "`has_more_lines_after=true` only means the rest of the file "
            "continues below it."
        )

    payload: dict[str, Any] = {
        "status": "blocked",
        "reason": "patch_hunk_mismatch",
        "file_path": rel_path,
        "error": error,
        "escaped_line_endings_detected": escaped_line_endings,
        "next_step": " ".join(next_parts),
    }
    if read_hint:
        payload["read_file_hint"] = read_hint
    context = _line_context_payload(old_content, failure_line)
    if context:
        payload["current_context"] = context
    return payload


def _patch_hunk_mismatch_replacement_required_block(
    ctx: Any, rel_path: str
) -> dict[str, Any] | None:
    """Escalate repeated update-hunk mismatches into a hard replacement contract.

    The patch tool already tells the agent to retry once and then switch to
    paired same-path Delete/Add. Real runs showed the model can keep emitting
    mismatching Update hunks anyway, burning rounds while Umbrella only repeats
    the same advice. The control plane should make that transition explicit.
    """

    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    drive_root = getattr(ctx, "drive_root", None)
    if not task_id or drive_root is None:
        return None
    logs_path = Path(drive_root) / "logs" / "tools.jsonl"
    try:
        lines = logs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    mismatch_count = 0
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if _active_success_test_contract_migration_reason_for_path(ctx, norm):
        return None
    for line in reversed(lines[-300:]):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("task_id") or "") != task_id:
            continue
        if str(row.get("tool") or "") != "apply_workspace_patch":
            continue
        payload = _tool_result_payload(row)
        if not payload or not _patch_payload_mentions_path(payload, norm):
            continue
        if str(payload.get("status") or "") == "applied":
            break
        if (
            str(payload.get("status") or "") == "blocked"
            and str(payload.get("reason") or "") == "patch_hunk_mismatch"
        ):
            mismatch_count += 1
            if mismatch_count >= _PATCH_HUNK_MISMATCH_REPLACEMENT_THRESHOLD:
                return {
                    "status": "blocked",
                    "reason": "patch_hunk_mismatch_replacement_required",
                    "file_path": norm,
                    "recent_mismatches": mismatch_count,
                    "message": (
                        "This task has already hit repeated Update hunk "
                        "mismatches for this file since the last successful "
                        "patch. Stop emitting more `*** Update File:` hunks "
                        "for the same path."
                    ),
                    "required_patch_shape": (
                        "*** Begin Patch\n"
                        f"*** Delete File: {norm}\n"
                        f"*** Add File: {norm}\n"
                        "+<full replacement file content, every line prefixed with +>\n"
                        "*** End Patch"
                    ),
                    "forbidden_next_write": f"*** Update File: {norm}",
                    "next_step": (
                        f"Use `read_file` or `repo_read` to read `{norm}` in "
                        "full if needed; do not use shell for file inspection. "
                        "Then use one "
                        "`apply_workspace_patch` envelope with paired "
                        f"`*** Delete File: {norm}` and "
                        f"`*** Add File: {norm}` entries for an audited "
                        "same-path replacement, or call "
                        "`request_watcher_review` if replacement would weaken "
                        "the contract. The paired replacement still runs the "
                        "normal syntax, layout, LLM-contract, and test "
                        "weakening guards."
                    ),
                }
    return None


_REPLACEMENT_SUFFIX_RE = re.compile(r"\.(?:new|tmp|replacement)$", re.IGNORECASE)
_REPLACEMENT_STEM_RE = re.compile(
    r"^(?P<base>.+)_(?:corrected|fixed|updated|new|replacement)$",
    re.IGNORECASE,
)
_CONTEXTUAL_REPLACEMENT_STEM_RE = re.compile(
    r"^(?P<base>.+)_(?:extra|extras|aux|auxiliary|additional|patch|patched|repair|repaired)$",
    re.IGNORECASE,
)


def _replacement_artifact_candidate(
    rel_path: str, *, include_contextual_names: bool = False
) -> str | None:
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if not norm:
        return None
    path = Path(norm)
    name = path.name
    if _REPLACEMENT_SUFFIX_RE.search(name):
        return norm[: -len(path.suffix)]
    stem_patterns = [_REPLACEMENT_STEM_RE]
    if include_contextual_names:
        stem_patterns.append(_CONTEXTUAL_REPLACEMENT_STEM_RE)
    for pattern in stem_patterns:
        match = pattern.match(path.stem)
        if match:
            candidate_name = f"{match.group('base')}{path.suffix}"
            return str(path.with_name(candidate_name)).replace("\\", "/")
    return None


def _replacement_artifact_block(seed_path: Path, rel_path: str) -> dict[str, Any] | None:
    """Block sidecar files that are really failed same-path rewrites."""

    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if not norm:
        return None
    candidate = _replacement_artifact_candidate(norm)
    if not candidate:
        return None
    try:
        original = _workspace_path(seed_path, candidate)
    except ValueError:
        return None
    if not original.is_file():
        return None
    return {
        "status": "blocked",
        "reason": "replacement_artifact_blocked",
        "file_path": norm,
        "existing_file": candidate,
        "message": (
            "Do not create alternate `.new`, `_corrected`, or similar source/test "
            "files to work around patch mismatches; they leave duplicate contracts "
            "for verification."
        ),
        "next_step": (
            f"Read `{candidate}` in full, then use one `apply_workspace_patch` "
            f"with paired `*** Delete File: {candidate}` and "
            f"`*** Add File: {candidate}` entries to perform an audited same-path "
            "replacement, or call `request_watcher_review` if the replacement "
            "would weaken the contract."
        ),
    }


def _pending_replacement_required_sidecar_block(
    ctx: Any, seed_path: Path, rel_path: str
) -> dict[str, Any] | None:
    """Block contextual sidecars after Umbrella has required same-path replacement."""

    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    candidate = _replacement_artifact_candidate(
        norm, include_contextual_names=True
    )
    if not norm or not candidate or candidate == norm:
        return None
    try:
        original = _workspace_path(seed_path, candidate)
    except ValueError:
        return None
    if not original.is_file():
        return None
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    drive_root = getattr(ctx, "drive_root", None)
    if not task_id or drive_root is None:
        return None
    logs_path = Path(drive_root) / "logs" / "tools.jsonl"
    try:
        lines = logs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    for line in reversed(lines[-300:]):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        if str(row.get("task_id") or "") != task_id:
            continue
        if str(row.get("tool") or "") != "apply_workspace_patch":
            continue
        payload = _tool_result_payload(row)
        if not payload or not _patch_payload_mentions_path(payload, candidate):
            continue
        if str(payload.get("status") or "") == "applied":
            return None
        if (
            str(payload.get("status") or "") == "blocked"
            and str(payload.get("reason") or "")
            == "patch_hunk_mismatch_replacement_required"
        ):
            return {
                "status": "blocked",
                "reason": "replacement_required_sidecar_blocked",
                "file_path": norm,
                "existing_file": candidate,
                "message": (
                    "This task already requires an audited same-path "
                    f"replacement for `{candidate}`. Do not add auxiliary, "
                    "extra, patched, or repair files to route around that "
                    "contract."
                ),
                "required_patch_shape": (
                    "*** Begin Patch\n"
                    f"*** Delete File: {candidate}\n"
                    f"*** Add File: {candidate}\n"
                    "+<full replacement file content, every line prefixed with +>\n"
                    "*** End Patch"
                ),
                "next_step": (
                    f"Use `read_file` or `repo_read` to read `{candidate}` in "
                    "full if needed, then use one paired same-path Delete/Add "
                    "replacement for that file. Call `request_watcher_review` "
                    "instead if the replacement would weaken the contract."
                ),
            }
    return None


def _add_file_literal_hunk_marker_block(
    rel_path: str, content_lines: list[str]
) -> dict[str, Any] | None:
    marker_lines: list[dict[str, Any]] = []
    for line_no, line in enumerate(content_lines or [], start=1):
        text = str(line or "")
        if not text.lstrip().startswith("@@"):
            continue
        marker_lines.append({"line": line_no, "text": text[:120]})
        if len(marker_lines) >= 5:
            break
    if not marker_lines:
        return None
    norm = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    return {
        "status": "blocked",
        "reason": "patch_add_file_literal_hunk_marker",
        "file_path": norm,
        "line_numbers": [item["line"] for item in marker_lines],
        "offending_lines": marker_lines,
        "message": (
            "`*** Add File:` content must not include literal `@@` hunk marker "
            "lines. `@@` belongs to `*** Update File:` hunks, not new-file "
            "content."
        ),
        "next_step": (
            "Re-emit the Add File patch without the `@@` line. Put each new "
            "file content line under `*** Add File:` as `+<content>`, then end "
            "the patch with bare `*** End Patch`. Use `*** Update File:` with "
            "`@@` only for existing files after reading them first."
        ),
    }


def _plan_workspace_patch_replacement_operation(
    ctx: Any,
    *,
    workspace_id: str,
    seed_path: Path,
    delete_op: Any,
    add_op: Any,
    operation_paths: set[str],
    validation_summary: str,
    text_from_add_lines: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Plan a same-path Delete+Add pair as one audited replacement."""

    rel_path = _strip_workspace_prefix(workspace_id, add_op.path)
    if add_hunk_marker_block := _add_file_literal_hunk_marker_block(
        rel_path, add_op.content_lines
    ):
        return None, _json(add_hunk_marker_block)
    if layout_block := _workspace_layout_policy_block(rel_path):
        return None, _json(layout_block)
    if active_test_block := _active_declared_success_test_edit_block(
        ctx, rel_path=rel_path
    ):
        return None, _json(active_test_block)
    if src_layout_block := _greenfield_python_src_layout_block(
        seed_path, rel_path, planned_paths=operation_paths
    ):
        return None, _json(src_layout_block)
    if not _workspace_file_was_read(ctx, workspace_id, rel_path):
        return None, _json(
            {
                "status": "blocked",
                "reason": "read_before_patch_required",
                "file_path": rel_path,
                "next_step": (
                    "Call phase `read_file` for this exact workspace-relative "
                    "path before using paired Delete/Add replacement in "
                    "`apply_workspace_patch`."
                ),
            }
        )
    try:
        target = _workspace_path(seed_path, rel_path)
    except ValueError as exc:
        return None, _json(
            {
                "status": "blocked",
                "reason": "path_traversal",
                "file_path": rel_path,
                "error": str(exc),
            }
        )
    if not target.is_file():
        return None, _json(
            {
                "status": "blocked",
                "reason": "patch_replacement_target_missing",
                "file_path": rel_path,
                "next_step": "Use `*** Add File:` for a new file; paired Delete/Add replacement is for existing files.",
            }
        )
    old_content = target.read_text(encoding="utf-8", errors="replace")
    new_content = text_from_add_lines(add_op.content_lines)

    if verification_block := _workspace_toml_verification_guard(
        seed_path, rel_path, new_content
    ):
        return None, _json(verification_block)
    if llm_contract_block := _llm_runtime_contract_block(rel_path, new_content):
        return None, _json(llm_contract_block)
    if behavior_fallback_block := _llm_behavior_fallback_contract_block(
        rel_path, new_content
    ):
        return None, _json(behavior_fallback_block)
    if syntax_block := _python_syntax_block(rel_path, new_content):
        return None, _json(syntax_block)
    if truncation_block := _source_truncation_block(
        rel_path,
        old_content,
        new_content,
        allow_large_overwrite=bool(validation_summary.strip()),
        validation_summary=validation_summary,
    ):
        return None, _json(truncation_block)
    try:
        from ouroboros.tools.git import _repo_write_test_weakening_block

        if test_block := _repo_write_test_weakening_block(
            ctx=ctx,
            target=target,
            rel=f"workspaces/{workspace_id}/{rel_path}",
            content_text=new_content,
        ):
            return None, test_block
    except Exception:
        log.debug("workspace replacement test guard failed open", exc_info=True)

    return {
        "action": "update",
        "path": rel_path,
        "target": target,
        "old_content": old_content,
        "new_content": new_content,
    }, None


def _plan_workspace_patch_operation(
    ctx: Any,
    *,
    workspace_id: str,
    seed_path: Path,
    op: Any,
    operation_paths: set[str],
    validation_summary: str,
    apply_update_to_text: Any,
    text_from_add_lines: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    rel_path = _strip_workspace_prefix(workspace_id, op.path)
    if op.action == "add" and (
        add_hunk_marker_block := _add_file_literal_hunk_marker_block(
            rel_path, op.content_lines
        )
    ):
        return None, _json(add_hunk_marker_block)
    if op.action != "delete" and (
        layout_block := _workspace_layout_policy_block(rel_path)
    ):
        return None, _json(layout_block)
    if op.action == "add" and (
        replacement_block := _replacement_artifact_block(seed_path, rel_path)
    ):
        return None, _json(replacement_block)
    if op.action == "add" and (
        pending_replacement_block := _pending_replacement_required_sidecar_block(
            ctx, seed_path, rel_path
        )
    ):
        return None, _json(pending_replacement_block)
    if op.action in {"update", "delete"} and (
        active_test_block := _active_declared_success_test_edit_block(
            ctx, rel_path=rel_path
        )
    ):
        return None, _json(active_test_block)
    if op.action != "delete" and (
        src_layout_block := _greenfield_python_src_layout_block(
            seed_path, rel_path, planned_paths=operation_paths
        )
    ):
        return None, _json(src_layout_block)
    if op.action in {"update", "delete"} and not _workspace_file_was_read(
        ctx, workspace_id, rel_path
    ):
        return None, _json(
            {
                "status": "blocked",
                "reason": "read_before_patch_required",
                "file_path": rel_path,
                "next_step": (
                    "Call phase `read_file` (alias for `read_workspace_file`) "
                    "for this exact workspace-relative path before using "
                    "`apply_workspace_patch` to update or delete it. "
                    "`repo_read(\"workspaces/<workspace_id>/<path>\")` also "
                    "counts as a pre-read when the active workspace is known."
                ),
            }
        )

    target = _workspace_path(seed_path, rel_path)
    old_content = ""
    new_content = ""
    if op.action == "update":
        if not target.is_file():
            return None, _json(
                {
                    "status": "blocked",
                    "reason": "patch_target_missing",
                    "file_path": rel_path,
                    "next_step": "Use `*** Add File:` for new files or read/list the workspace to find the right path.",
                }
            )
        if mismatch_block := _patch_hunk_mismatch_replacement_required_block(
            ctx, rel_path
        ):
            return None, _json(mismatch_block)
        old_content = target.read_text(encoding="utf-8", errors="replace")
        try:
            new_content = apply_update_to_text(old_content, op.hunks, rel_path)
        except ValueError as exc:
            migration_reason = _active_success_test_contract_migration_reason_for_path(
                ctx, rel_path
            )
            return None, _json(
                _patch_hunk_mismatch_payload(
                    ctx,
                    rel_path=rel_path,
                    error=str(exc),
                    migration_reason=migration_reason,
                    old_content=old_content,
                    hunks=op.hunks,
                )
            )
    elif op.action == "add":
        if target.exists():
            return None, _json(
                {
                    "status": "blocked",
                    "reason": "patch_add_target_exists",
                    "file_path": rel_path,
                    "next_step": (
                        "Use `*** Update File:` after `read_workspace_file` for "
                        "targeted edits. For full replacement after repeated "
                        "hunk mismatches, use one paired same-path Delete/Add "
                        "patch envelope instead of adding a sidecar file."
                    ),
                }
            )
        new_content = text_from_add_lines(op.content_lines)
    elif op.action == "delete":
        delete_target, rel_norm, delete_block = _delete_validate_path(
            seed_path, workspace_id, rel_path
        )
        if delete_block is not None or delete_target is None:
            return None, _json(
                delete_block
                or {
                    "status": "error",
                    "reason": "unknown_delete_validation_error",
                    "file_path": rel_path,
                }
            )
        rel_path = rel_norm
        target = delete_target
        old_content = target.read_text(encoding="utf-8", errors="replace")
    else:
        return None, _json(
            {
                "status": "blocked",
                "reason": "unsupported_patch_action",
                "action": op.action,
            }
        )

    if op.action != "delete":
        if empty_block := _empty_workspace_file_block(rel_path, new_content):
            return None, _json(empty_block)
        if placeholder_bridge_block := _placeholder_integration_bridge_block(
            rel_path, new_content
        ):
            return None, _json(placeholder_bridge_block)
        if verification_block := _workspace_toml_verification_guard(
            seed_path, rel_path, new_content
        ):
            return None, _json(verification_block)
        if llm_contract_block := _llm_runtime_contract_block(rel_path, new_content):
            return None, _json(llm_contract_block)
        if behavior_fallback_block := _llm_behavior_fallback_contract_block(
            rel_path, new_content
        ):
            return None, _json(behavior_fallback_block)
        if syntax_block := _python_syntax_block(rel_path, new_content):
            return None, _json(syntax_block)
        if op.action == "update" and (
            truncation_block := _source_truncation_block(
                rel_path,
                old_content,
                new_content,
                allow_large_overwrite=bool(validation_summary.strip()),
                validation_summary=validation_summary,
            )
        ):
            return None, _json(truncation_block)
        try:
            from ouroboros.tools.git import _repo_write_test_weakening_block

            if test_block := _repo_write_test_weakening_block(
                ctx=ctx,
                target=target,
                rel=f"workspaces/{workspace_id}/{rel_path}",
                content_text=new_content,
            ):
                return None, test_block
        except Exception:
            log.debug("workspace patch test guard failed open", exc_info=True)

    return {
        "action": op.action,
        "path": rel_path,
        "target": target,
        "old_content": old_content,
        "new_content": new_content,
    }, None


def _plan_workspace_patch_operations(
    ctx: Any,
    *,
    workspace_id: str,
    seed_path: Path,
    operations: list[Any],
    validation_summary: str,
    apply_update_to_text: Any,
    text_from_add_lines: Any,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    operation_paths = {
        _strip_workspace_prefix(workspace_id, op.path)
        for op in operations
        if op.action != "delete"
    }
    planned: list[dict[str, Any]] = []
    index = 0
    while index < len(operations):
        op = operations[index]
        next_op = operations[index + 1] if index + 1 < len(operations) else None
        if (
            op.action == "delete"
            and next_op is not None
            and next_op.action == "add"
            and _strip_workspace_prefix(workspace_id, op.path)
            == _strip_workspace_prefix(workspace_id, next_op.path)
        ):
            item, response = _plan_workspace_patch_replacement_operation(
                ctx,
                workspace_id=workspace_id,
                seed_path=seed_path,
                delete_op=op,
                add_op=next_op,
                operation_paths=operation_paths,
                validation_summary=validation_summary,
                text_from_add_lines=text_from_add_lines,
            )
            if response:
                return None, response
            if item is not None:
                planned.append(item)
            index += 2
            continue
        item, response = _plan_workspace_patch_operation(
            ctx,
            workspace_id=workspace_id,
            seed_path=seed_path,
            op=op,
            operation_paths=operation_paths,
            validation_summary=validation_summary,
            apply_update_to_text=apply_update_to_text,
            text_from_add_lines=text_from_add_lines,
        )
        if response:
            return None, response
        if item is not None:
            planned.append(item)
        index += 1

    planned_paths = {
        str(item["path"]) for item in planned if item["action"] != "delete"
    }
    planned_content_by_path = {
        str(item["path"]): str(item["new_content"])
        for item in planned
        if item["action"] != "delete"
    }
    for item in planned:
        if item["action"] == "delete":
            continue
        if import_block := _python_import_resolution_block(
            seed_path,
            str(item["path"]),
            str(item["new_content"]),
            planned_paths=planned_paths,
            planned_content_by_path=planned_content_by_path,
        ):
            return None, _json(import_block)
    return planned, None


_SUBTASK_WRITE_SCOPE_KEYS = (
    "files_to_create",
    "files_to_change",
    "files_affected",
    "contract_migration_files",
)


def _norm_workspace_rel_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("/")


def _plan_context_subtask_id(ctx: Any) -> str:
    plan_ctx = getattr(ctx, "plan_execution_context", None)
    if isinstance(plan_ctx, dict):
        return str(
            plan_ctx.get("subtask_id")
            or plan_ctx.get("current_subtask_id")
            or plan_ctx.get("active_subtask_id")
            or ""
        ).strip()
    return str(
        getattr(plan_ctx, "subtask_id", "")
        or getattr(plan_ctx, "current_subtask_id", "")
        or getattr(plan_ctx, "active_subtask_id", "")
        or ""
    ).strip()


def _active_execute_subtask_for_write_scope(
    ctx: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], int]:
    drive_root = getattr(ctx, "drive_root", None)
    if not drive_root:
        return None, [], -1
    plan_path = Path(drive_root) / "state" / "phase_plan.json"
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception:
        return None, [], -1
    if not isinstance(payload, dict):
        return None, [], -1
    subtasks: list[dict[str, Any]] = []
    for node in payload.get("nodes") or []:
        if not isinstance(node, dict) or str(node.get("id") or "") != "execute":
            continue
        if str(node.get("status") or "").lower() not in {"running", "pending"}:
            return None, [], -1
        subtasks = [
            item
            for item in (node.get("subtasks") or [])
            if isinstance(item, dict)
        ]
        break
    if not subtasks:
        return None, [], -1

    active_id = _plan_context_subtask_id(ctx)
    if active_id:
        for idx, subtask in enumerate(subtasks):
            if str(subtask.get("id") or "") == active_id:
                return subtask, subtasks, idx

    for idx, subtask in enumerate(subtasks):
        if str(subtask.get("status") or "").lower() != "done":
            return subtask, subtasks, idx
    return None, subtasks, -1


def _subtask_declared_paths(subtask: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for key in _SUBTASK_WRITE_SCOPE_KEYS:
        raw = subtask.get(key)
        if isinstance(raw, str):
            raw_items: Iterable[Any] = [raw]
        elif isinstance(raw, Iterable):
            raw_items = raw
        else:
            raw_items = []
        for item in raw_items:
            norm = _norm_workspace_rel_path(item)
            if norm:
                paths.add(norm)
    return paths


def _future_subtask_path_owners(
    subtasks: list[dict[str, Any]],
    *,
    current_index: int,
) -> dict[str, str]:
    owners: dict[str, str] = {}
    for subtask in subtasks[current_index + 1 :]:
        owner = str(subtask.get("id") or subtask.get("title") or "").strip()
        for path in _subtask_declared_paths(subtask):
            owners.setdefault(path, owner)
    return owners


def _execute_subtask_write_scope_block(
    ctx: Any,
    *,
    planned: list[dict[str, Any]],
) -> dict[str, Any] | None:
    task_id = str(getattr(ctx, "task_id", "") or "")
    if ":execute" not in task_id:
        return None
    active, subtasks, active_index = _active_execute_subtask_for_write_scope(ctx)
    if not active or active_index < 0:
        return None
    allowed = _subtask_declared_paths(active)
    if not allowed:
        return None
    planned_paths = sorted(
        {
            _norm_workspace_rel_path(item.get("path"))
            for item in planned
            if _norm_workspace_rel_path(item.get("path"))
        }
    )
    out_of_scope = [path for path in planned_paths if path not in allowed]
    if not out_of_scope:
        return None
    future_owners = _future_subtask_path_owners(subtasks, current_index=active_index)
    future_hits = {
        path: future_owners[path]
        for path in out_of_scope
        if path in future_owners
    }
    active_id = str(active.get("id") or "").strip()
    return {
        "status": "blocked",
        "reason": "active_subtask_write_scope",
        "active_subtask_id": active_id,
        "allowed_paths": sorted(allowed),
        "blocked_paths": out_of_scope,
        "future_subtask_owners": future_hits,
        "message": (
            "Execute must work one phase-plan leaf at a time. This patch writes "
            "file(s) not declared on the active subtask."
        ),
        "next_step": (
            f"Finish `{active_id}` using only its declared files. If a blocked "
            "file truly belongs to this subtask, first call `mutate_phase_plan` "
            "to add that workspace-relative path to the active subtask's "
            "`files_to_create`, `files_to_change`, or `files_affected`; otherwise "
            "wait until the owning future subtask is active."
        ),
    }


def _apply_workspace_patch_plan(
    ctx: Any,
    *,
    repo_root: Path,
    seed_path: Path,
    workspace_id: str,
    planned: list[dict[str, Any]],
    update_seed_workspace_file: Any,
) -> tuple[list[str], list[str], str | None]:
    applied: list[str] = []
    backups: list[str] = []
    for item in planned:
        rel_path = str(item["path"])
        action = str(item["action"])
        if action == "delete":
            Path(item["target"]).unlink()
            _record_workspace_diff(
                ctx,
                file_path=rel_path,
                old_content=str(item["old_content"]),
                new_content="",
                deleted_file=True,
            )
            applied.append(f"deleted {rel_path}")
            continue
        result = update_seed_workspace_file(
            seed_path=seed_path,
            relative_file_path=rel_path,
            new_content=str(item["new_content"]),
            create_backup=True,
            backup_dir=repo_root / ".umbrella" / "backups",
        )
        if not result.applied:
            return (
                applied,
                backups,
                f"Patch update failed for {rel_path}: {result.error or 'unknown error'}",
            )
        if result.backup_path:
            backups.append(str(result.backup_path))
        _record_workspace_diff(
            ctx,
            file_path=rel_path,
            old_content=str(item["old_content"]),
            new_content=str(item["new_content"]),
            added_file=(action == "add"),
        )
        verb = "added" if action == "add" else "updated"
        applied.append(f"{verb} {rel_path}")
    return applied, backups, None


def apply_workspace_patch(
    ctx: Any,
    workspace_id: str,
    patch: str,
    validation_summary: str = "",
) -> str:
    try:
        from umbrella.control_plane.workspace_code_update import (
            update_seed_workspace_file,
        )
        from ouroboros.workspace_patch import (
            apply_update_to_text,
            parse_workspace_patch,
            text_from_add_lines,
        )

        repo_root = _resolve_umbrella_repo_root(ctx)
        seed_path = _workspace_root(repo_root, workspace_id, ctx)
        if stop_payload := _stop_requested_block(
            ctx, tool_name="apply_workspace_patch", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not seed_path.exists():
            return f"Workspace not found: {workspace_id}"
        if gmas_block := _gmas_context_before_write_block(ctx, workspace_id, seed_path):
            return _json(gmas_block)
        if phase_order_block := _phase_plan_write_order_block(ctx):
            return _json(phase_order_block)
        if retry_block := _phase_subtask_retry_escalation_block(
            ctx, tool_name="apply_workspace_patch"
        ):
            return _json(retry_block)
        try:
            operations = parse_workspace_patch(patch)
        except ValueError as exc:
            patch_text = str(patch or "")
            prefixed_end_marker = bool(
                re.search(r"(?m)^\+\*\*\* End Patch\s*$", patch_text)
            )
            next_step = (
                "Re-emit an OpenAI-style patch envelope from *** Begin Patch "
                "to *** End Patch."
            )
            if prefixed_end_marker:
                next_step = (
                    "The final patch terminator must be a control line, not "
                    "file content: write `*** End Patch` with no leading `+`. "
                    "Only replacement file content lines between `*** Add "
                    "File:` and the terminator should be prefixed with `+`."
                )
            return _json(
                {
                    "status": "blocked",
                    "reason": "patch_parse_error",
                    "error": str(exc),
                    "end_marker_prefixed": prefixed_end_marker,
                    "next_step": next_step,
                }
            )

        planned, response = _plan_workspace_patch_operations(
            ctx,
            workspace_id=workspace_id,
            seed_path=seed_path,
            operations=operations,
            validation_summary=validation_summary,
            apply_update_to_text=apply_update_to_text,
            text_from_add_lines=text_from_add_lines,
        )
        if response:
            return response
        if planned is None:
            return _json(
                {
                    "status": "error",
                    "reason": "workspace_patch_planning_failed",
                }
            )
        if scope_block := _execute_subtask_write_scope_block(ctx, planned=planned):
            return _json(scope_block)

        applied, backups, response = _apply_workspace_patch_plan(
            ctx,
            repo_root=repo_root,
            seed_path=seed_path,
            workspace_id=workspace_id,
            planned=planned,
            update_seed_workspace_file=update_seed_workspace_file,
        )
        if response:
            return response
        record_workspace_event(
            ctx,
            workspace_id=workspace_id,
            event_type="change",
            summary="Applied workspace patch",
            details="; ".join(applied),
            severity="info",
            tags="change,seed,patch",
        )
        body = _json(
            {
                "status": "applied",
                "workspace_id": workspace_id,
                "applied": applied,
                "backups": backups[:5],
                "validation_summary": validation_summary,
            }
        )
        advisory = _gmas_first_write_advisory(
            ctx,
            repo_root=repo_root,
            workspace_id=workspace_id,
            file_path=applied[0] if applied else "",
        )
        return body + (("\n\n" + advisory) if advisory else "")
    except Exception as e:
        log.error("Workspace patch failed: %s", e, exc_info=True)
        return f"WARNING: workspace patch error: {e}"


def _gmas_first_write_advisory(
    ctx: Any,
    *,
    repo_root: Path,
    workspace_id: str,
    file_path: str,
) -> str:
    """Return a one-shot soft advisory when the first ``src/*.py`` write in
    a GMAS-active workspace happens without prior ``get_gmas_context`` /
    ``search_gmas_knowledge`` call inside the current task.

    Intentionally NON-BLOCKING. The hard pre-write gate that used to
    enforce this was removed because the agent learned to call
    ``get_gmas_context(query="placeholder")`` once just to satisfy it
    (cargo-cult behaviour observed in earlier synthetic GMAS gates).
    A soft, one-shot advisory keeps the signal — "you are about to
    write GMAS-relevant code, the in-repo GMAS library is the required
    stack, here is how to look up its API" — without creating a
    forced-ritual loop. Triggers at most once per task.
    """
    norm = str(file_path or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if (
        len(parts) < 2
        or parts[0].lower() != "src"
        or not parts[-1].lower().endswith(".py")
    ):
        return ""
    # Skip __init__ noise and tests (tests live under tests/ anyway,
    # but be defensive).
    if parts[-1].lower() in {"__init__.py"} or parts[-1].lower().startswith("test_"):
        return ""
    # The advisory only makes sense for GMAS-active workspaces. The
    # workspace skill detector caches its verdict in
    # ``workspaces/<id>/.memory/domains.json`` as
    # ``{"domains": ["multi_agent_gmas", ...]}``. Absent / unreadable
    # cache silently skips the advisory.
    try:
        domains_path = (
            repo_root / "workspaces" / workspace_id / ".memory" / "domains.json"
        )
        if not domains_path.is_file():
            return ""
        payload = json.loads(domains_path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    raw = payload.get("domains") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return ""
    domains = {str(v).lower() for v in raw if str(v).strip()}
    if "multi_agent_gmas" not in domains:
        return ""
    # ctx accumulates already-emitted advisories so we don't repeat
    # ourselves across many writes.
    fired = getattr(ctx, "_gmas_advisory_fired_tasks", None)
    if fired is None:
        fired = set()
        try:
            setattr(ctx, "_gmas_advisory_fired_tasks", fired)
        except Exception:
            return ""
    task_id = str(getattr(ctx, "task_id", "") or "")
    if task_id in fired:
        return ""
    # Check the tools log to see whether the agent already called the
    # GMAS retrieval tools in this task. If they did, no advisory.
    try:
        drive_root = getattr(ctx, "drive_root", None)
        if drive_root is not None:
            tools_log = Path(drive_root) / "logs" / "tools.jsonl"
            if tools_log.is_file():
                gmas_tools = {"get_gmas_context", "search_gmas_knowledge"}
                with tools_log.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if task_id and str(event.get("task_id") or "") != task_id:
                            continue
                        if str(event.get("tool") or "") in gmas_tools:
                            fired.add(task_id)
                            return ""
    except Exception:
        log.debug("gmas advisory tools.jsonl scan failed", exc_info=True)
    fired.add(task_id)
    return (
        "[GMAS_FIRST_WRITE_ADVISORY]\n"
        "This is your first `src/*.py` write in a GMAS-active workspace "
        "and you have not called `get_gmas_context` / "
        "`search_gmas_knowledge` yet. The in-repo `gmas/` library is the "
        "required stack for LLM/agent/judge nodes; before the next "
        "write batch consider one call: "
        '`get_gmas_context(query="<the API you need — e.g. defining a '
        'tool agent, wiring a judge, registering a graph node>")`. '
        "This is an advisory, not a block — the current write went "
        "through. The advisory will not repeat in this task."
    )


def _source_repair_delete_block(rel_norm: str, reason: str) -> dict[str, Any] | None:
    """Block using cleanup deletion as a source-code repair strategy."""

    suffix = Path(rel_norm).suffix.lower()
    if suffix not in _DELETE_SOURCE_EXTS:
        return None
    parts = [p for p in str(rel_norm or "").split("/") if p and p != "."]
    if parts and parts[0].lower() in _DELETE_MANAGED_SOURCE_TOP_DIRS:
        managed_source = True
    else:
        managed_source = False
    if not managed_source and not _DELETE_AS_REPAIR_REASON_RE.search(str(reason or "")):
        return None
    return {
        "status": "blocked",
        "reason": "source_repair_delete_blocked",
        "file_path": rel_norm,
        "message": (
            "`delete_workspace_file` is cleanup-only. Do not delete source, "
            "test, config, or frontend files merely because a patch mismatched "
            "or a read preview was truncated."
        ),
        "next_step": (
            "Read the exact current file content, using `read_file` offsets "
            "when the preview is truncated, then repair forward with "
            "`apply_workspace_patch`. If repeated patch mismatches block "
            "progress, call `request_watcher_review` with the failing patch "
            "shape and test output instead of delete/recreate looping."
        ),
    }


def _delete_validate_path(
    workspace_root: Path, workspace_id: str, file_path: str
) -> tuple[Path | None, str, dict[str, Any] | None]:
    """Return ``(resolved_path, rel_norm, blocked_payload)`` for the delete tool.

    Centralises every refusal reason so ``delete_workspace_file`` stays
    short and the AST size-budget test does not regress.
    """
    rel = _strip_workspace_prefix(workspace_id, file_path)
    rel_norm = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel_norm:
        return (
            None,
            "",
            {
                "status": "blocked",
                "reason": "file_path_required",
                "next_step": "Pass a non-empty workspace-relative file_path.",
            },
        )
    parts = [p for p in rel_norm.split("/") if p and p != "."]
    if not parts:
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "file_path_required",
                "file_path": rel_norm,
            },
        )
    if parts[0].lower() in _DELETE_PROTECTED_TOP_DIRS:
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "protected_directory",
                "file_path": rel_norm,
                "next_step": (
                    f"Files under `{parts[0]}/` are runtime substrate and "
                    "cannot be removed with this tool."
                ),
            },
        )
    if parts[-1].lower() in _DELETE_PROTECTED_BASENAMES:
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "protected_file",
                "file_path": rel_norm,
                "next_step": (
                    f"`{parts[-1]}` is required by the workspace contract; "
                    "edit via `apply_workspace_patch`, never delete."
                ),
            },
        )
    try:
        target = _workspace_path(workspace_root, rel_norm)
    except ValueError as exc:
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "path_traversal",
                "file_path": rel_norm,
                "error": str(exc),
            },
        )
    if not target.exists():
        return (
            None,
            rel_norm,
            {
                "status": "not_found",
                "reason": "file_missing",
                "file_path": rel_norm,
            },
        )
    if target.is_dir():
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "is_directory",
                "file_path": rel_norm,
                "next_step": ("delete_workspace_file removes one file at a time."),
            },
        )
    if not target.is_file():
        return (
            None,
            rel_norm,
            {
                "status": "blocked",
                "reason": "not_a_regular_file",
                "file_path": rel_norm,
            },
        )
    return target, rel_norm, None


def delete_workspace_file(
    ctx: Any,
    workspace_id: str,
    file_path: str,
    reason: str = "",
) -> str:
    """Sanctioned single-file delete for workspace cleanup.

    Without this, the agent has no way to remove the ad-hoc diagnostic
    scripts / extracted raw artifacts that the layout policy and final
    sweep flag during remediation: shell ``rm`` / ``del`` /
    ``Remove-Item`` and ``python -c "...unlink()..."`` are blocked on
    purpose, so the observed production failure mode was the agent
    identifying the noise correctly, attempting every shell variant,
    and surrendering with the pollution still on disk. The reason
    field is recommended (audit trail); empty reasons surface a
    warning but do not hard-fail.
    """
    try:
        if stop_payload := _stop_requested_block(
            ctx, tool_name="delete_workspace_file", workspace_id=workspace_id
        ):
            return _json(stop_payload)
        if not workspace_id:
            return _json(
                {
                    "status": "blocked",
                    "reason": "workspace_id_required",
                    "next_step": "Pass the workspace_id of the workspace you are cleaning up.",
                }
            )
        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)
        if not workspace_root.exists():
            return _json(
                {
                    "status": "not_found",
                    "reason": "workspace_missing",
                    "workspace_id": workspace_id,
                }
            )
        target, rel_norm, blocked = _delete_validate_path(
            workspace_root, workspace_id, file_path
        )
        if blocked is not None or target is None:
            return _json(blocked or {"status": "error", "reason": "unknown"})
        if repair_block := _source_repair_delete_block(rel_norm, reason):
            return _json(repair_block)
        try:
            byte_size = target.stat().st_size
        except OSError:
            byte_size = -1
        try:
            target.unlink()
        except OSError as exc:
            log.warning("delete_workspace_file: unlink failed for %s: %s", target, exc)
            return _json(
                {
                    "status": "error",
                    "reason": "unlink_failed",
                    "file_path": rel_norm,
                    "error": str(exc),
                }
            )
        reason_norm = (reason or "").strip()
        warning = (
            ""
            if reason_norm
            else (
                "delete_workspace_file called without a `reason`; future "
                "audits will not know why this file was removed."
            )
        )
        try:
            record_workspace_event(
                ctx,
                workspace_id=workspace_id,
                event_type="delete",
                summary=f"Deleted {rel_norm}",
                details=f"reason: {reason_norm or '(unspecified)'}\nbyte_size: {byte_size}",
                severity="warning",
                tags="cleanup,delete",
            )
        except Exception:
            log.debug("record_workspace_event after delete failed", exc_info=True)
        payload: dict[str, Any] = {
            "status": "deleted",
            "workspace_id": workspace_id,
            "file_path": rel_norm,
            "byte_size": byte_size,
            "reason": reason_norm,
        }
        if warning:
            payload["warning"] = warning
        return _json(payload)
    except Exception as e:
        log.error("delete_workspace_file failed: %s", e, exc_info=True)
        return f"WARNING: delete error: {e}"


def _verification_next_actions(report_dict: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    results = report_dict.get("results")
    if not isinstance(results, list):
        return actions
    for raw in results:
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "").lower()
        optional = bool(raw.get("optional"))
        if optional or status not in {"failed", "error"}:
            continue
        name = str(
            raw.get("name")
            or raw.get("step_name")
            or raw.get("kind")
            or "verification step"
        )
        text = " ".join(
            str(raw.get(key) or "")
            for key in ("summary", "error", "stdout", "stderr", "command")
        ).lower()
        if "test_quality_guard" in name or "test_quality_guard" in text:
            actions.append(
                "Strengthen tests in `tests/`: cover behavior with real assertions; for web projects exercise endpoints with TestClient/requests/httpx."
            )
        elif "final_sweep" in name or "blocking noise" in text:
            actions.append(
                "Clean workspace layout: move root diagnostic scripts to `src/scripts/`, tests to `tests/`, docs to `docs/`, or delete throwaway files."
            )
        elif "no tests ran" in text or "file or directory not found: tests" in text:
            actions.append(
                "Create real pytest files under `tests/` and rerun the exact acceptance command."
            )
        elif "file_exists" in name or "missing required" in text:
            actions.append(
                "Create the missing required files named by the failing file/layout step."
            )
        else:
            actions.append(
                f"Fix required verification step `{name}` using its stderr/summary, then rerun `run_workspace_verify`."
            )
    deduped: list[str] = []
    for action in actions:
        if action not in deduped:
            deduped.append(action)
    return deduped[:5]


def run_workspace_verify(
    ctx: Any, workspace_id: str, timeout_seconds: int = 600
) -> str:
    """Run the workspace's verification spec and return a structured report.

    This is the agent-facing equivalent of the post-loop verification that
    Umbrella runs automatically. Exposing it as a tool lets the agent gate
    its own work mid-loop instead of discovering broken integrations only
    after MAX_ROUNDS — that failure mode is what the JKX run hit.

    The result is also persisted into MemPalace under ``room=verify_runs``
    so that subsequent periodic recall can show the agent what its last
    verify attempt looked like.

    Returns JSON with ``passed``, ``pass_rate``, per-step results, and a
    short rendered summary suitable for the model to read directly.
    """
    try:
        from umbrella.verification.models import VerificationStep, VerificationStepKind
        from umbrella.verification.runner import run_verification
        from umbrella.verification.spec_loader import load_verification_spec
        from ouroboros.memory_hooks import record_verify_outcome

        repo_root = _resolve_umbrella_repo_root(ctx)
        workspace_root = _workspace_root(repo_root, workspace_id, ctx)

        steps = load_verification_spec(workspace_root)
        if not steps:
            _set_workspace_verification_state(
                ctx,
                workspace_id=workspace_id,
                passed=False,
                summary="No verification steps found.",
            )
            return _json(
                {
                    "passed": False,
                    "pass_rate": 0.0,
                    "skipped": True,
                    "reason": (
                        "No verification steps found in workspace.toml or "
                        "verification.toml, and autodetect produced none. "
                        'Add [[verification.steps]] entries (or steps = ["..."]) '
                        "to workspace.toml so this tool can do its job."
                    ),
                    "next_actions": [
                        "Add deterministic verification steps to `workspace.toml` or `verification.toml`, including tests under `tests/` when code is changed."
                    ],
                    "results": [],
                }
            )

        # Local-vs-external verify parity (fixes the "agent sees 6/6 PASS,
        # external harness fails source_policy:mock_scaffold_scan" gap):
        # the external orchestrator always passes ``changed_files`` and
        # therefore appends a synthetic ``source_policy:mock_scaffold_scan``
        # step. When the agent calls this tool directly without an
        # explicit SOURCE_POLICY entry in workspace.toml, ensure we still
        # add one so the local self-gate matches what the harness uses
        # to decide on remediation. Without this, the agent fixes "6/6"
        # locally, declares done, and the harness immediately kicks
        # another remediation cycle for a check the agent never saw.
        steps_with_policy = list(steps)
        if not any(
            getattr(s, "kind", None) == VerificationStepKind.SOURCE_POLICY
            for s in steps_with_policy
        ):
            steps_with_policy.append(
                VerificationStep(
                    kind=VerificationStepKind.SOURCE_POLICY,
                    name="source_policy:mock_scaffold_scan",
                    optional=False,
                )
            )

        report = run_verification(
            workspace_root,
            steps_with_policy,
            workspace_id=workspace_id,
            overall_timeout_seconds=max(60, int(timeout_seconds)),
        )
        report_dict = report.to_dict()
        summary = report.render_summary(limit_chars=4000)
        next_actions = _verification_next_actions(report_dict)
        _set_workspace_verification_state(
            ctx,
            workspace_id=workspace_id,
            passed=bool(report.passed),
            summary=summary,
        )

        failed_required = sum(
            1
            for r in report.results
            if (not r.step.optional) and r.status.value in {"failed", "error"}
        )
        verify_run_id = ""
        try:
            verify_run_id = (
                record_verify_outcome(
                    workspace_id=workspace_id,
                    passed=bool(report.passed),
                    pass_rate=float(report.pass_rate),
                    summary=f"{sum(1 for r in report.results if r.status.value == 'passed')}/{len(report.results)} steps passed",
                    details=summary,
                    repo_root=repo_root,
                    failed_step_count=failed_required,
                )
                or ""
            )
        except Exception:
            log.debug("record_verify_outcome failed", exc_info=True)

        return _json(
            {
                "passed": report_dict["passed"],
                "pass_rate": report_dict["pass_rate"],
                "skipped": False,
                "duration_seconds": report_dict.get("duration_seconds", 0.0),
                "summary": summary,
                "next_actions": next_actions,
                "results": report_dict["results"],
                "verify_run_id": verify_run_id,
                "failed_step_count": failed_required,
            }
        )
    except Exception as e:
        log.error("run_workspace_verify failed: %s", e, exc_info=True)
        try:
            _set_workspace_verification_state(
                ctx,
                workspace_id=workspace_id,
                passed=False,
                summary=f"verify error: {e}",
            )
        except Exception:
            log.debug("failed to record verification error state", exc_info=True)
        return f"WARNING: verify error: {e}"


def run_workspace_task(
    ctx: Any, task_input: str, workspace_id: str = "", max_iterations: int = 5
) -> str:
    """Deprecated compatibility shim; the old Umbrella manager path is disabled."""
    return _json(
        {
            "status": "disabled",
            "reason": "Umbrella manager delegation is not part of the Ouroboros path anymore.",
            "use_instead": [
                "list_workspace_files",
                "read_workspace_file",
                "run_workspace_command",
                "update_workspace_seed",
                "commit_workspace_changes",
                "get_gmas_context",
                "get_umbrella_memory",
                "save_umbrella_memory",
            ],
            "workspace_id": workspace_id,
            "task_preview": task_input[:300],
            "ignored_max_iterations": max_iterations,
        }
    )


def sandbox_self_edit(
    ctx: Any,
    file_path: str,
    new_content: str,
    reason: str,
    surface: str = "ouroboros",
) -> str:
    """Edit agent-owned code (ouroboros/ or umbrella/) to fix a capability gap.

    Use this only when you cannot accomplish the task with existing tools and
    need to patch your own code to unblock yourself.
    """
    try:
        import os
        from umbrella.policies.engine import can_edit_path
        from umbrella.control_plane.sandbox_self_edit import (
            get_active_session,
            record_sandbox_edit,
        )

        repo_root = _resolve_umbrella_repo_root(ctx)

        session_id = os.environ.get("UMBRELLA_SANDBOX_SESSION_ID")
        if not session_id:
            return _json(
                {
                    "status": "blocked",
                    "reason": "no_sandbox_session",
                    "hint": "Sandbox self-edit is only available during a managed task run.",
                }
            )

        session = get_active_session(repo_root)
        if session is None or session.session_id != session_id:
            return _json(
                {
                    "status": "blocked",
                    "reason": "sandbox_session_mismatch",
                }
            )

        decision = can_edit_path(
            Path(file_path),
            actor="ouroboros",
            action="sandbox_self_edit",
            repo_root=repo_root,
        )
        if not decision.allowed:
            return _json(
                {
                    "status": "blocked",
                    "reason": decision.reason,
                    "policy_id": decision.policy_id,
                }
            )

        target = (repo_root / file_path).resolve()
        if not str(target).startswith(str(repo_root.resolve())):
            return "ERROR: path traversal detected"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")

        record_sandbox_edit(session, file_path)

        record_workspace_event(
            ctx,
            workspace_id="_self",
            event_type="sandbox_self_edit",
            summary=f"Sandbox edit: {file_path}",
            details=f"Reason: {reason}\nSurface: {surface}\nSession: {session_id}",
            severity="warning",
            tags="sandbox,self_edit,capability_gap",
        )

        return _json(
            {
                "status": "applied",
                "file_path": file_path,
                "session_id": session_id,
                "rollback_on_task_end": False,
                "edited_files_count": len(session.edited_files),
            }
        )
    except Exception as e:
        log.error("Sandbox self-edit failed: %s", e, exc_info=True)
        return f"WARNING: sandbox self-edit error: {e}"


def delegate_to_ouroboros(
    ctx: Any,
    task_description: str,
    workspace_id: str = "",
    code_updates: dict[str, str] | None = None,
) -> str:
    """Spawn a separate PhaseRunner task (e.g. system self-improvement).

    The new task runs in the background with its own PhasePlan. Use this only
    when the work is genuinely separate from the current run (such as editing
    Umbrella/Ouroboros code itself). For in-run plan changes, use the
    ``mutate_phase_plan`` / ``add_phase`` / ``edit_subtask_card`` tools.
    """
    try:
        import threading
        import uuid as _uuid
        from umbrella.orchestrator.runner import PhaseRunner

        repo_root = _resolve_umbrella_repo_root(ctx)
        ws_id = workspace_id or _current_workspace_id_from_drive(ctx) or "manager"
        run_id = f"delegate_{_uuid.uuid4().hex[:8]}"

        prefix = ""
        if code_updates:
            prefix = (
                "## Suggested code updates from delegating agent\n"
                + "\n".join(f"- `{k}`" for k in (code_updates or {}).keys())
                + "\n\n"
            )
        full_task = prefix + str(task_description or "").strip()

        def _spawn() -> None:
            try:
                runner = PhaseRunner(repo_root=repo_root, workspace_id=ws_id)
                for _ in runner.run(full_task, run_id=run_id):
                    pass
            except Exception:
                log.error("Delegated PhaseRunner crashed", exc_info=True)

        thread = threading.Thread(
            target=_spawn, name=f"DelegatedRun-{run_id}", daemon=True
        )
        thread.start()
        return _json(
            {
                "delegated": True,
                "run_id": run_id,
                "workspace_id": ws_id,
                "note": "Background PhaseRunner started; check Runs page for progress.",
            }
        )
    except Exception as e:
        log.error("Ouroboros delegation failed: %s", e, exc_info=True)
        return f"WARNING: Ouroboros delegation error: {e}"


def web_fetch(ctx: Any, url: str, max_chars: int = 20000) -> str:
    """Fetch a URL (GET) and return cleaned text (HTML stripped, head+tail truncated)."""
    try:
        _record_subtask_discovery_tool_call(ctx, "web_fetch")
        import re as _re
        import httpx

        u = (url or "").strip()
        if not u:
            return _json({"error": "empty url"})
        if not u.lower().startswith(("http://", "https://")):
            return _json({"error": "only http(s) urls are allowed", "url": u})

        cap = max(2000, min(int(max_chars), 200_000))
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        }
        try:
            r = httpx.get(u, headers=headers, timeout=25.0, follow_redirects=True)
        except Exception as net_err:
            return _json({"error": f"network: {net_err}", "url": u})

        ct = (r.headers.get("content-type") or "").lower()
        body = r.text
        if "html" in ct or body.lstrip().startswith("<"):
            body = _re.sub(r"<script[\s\S]*?</script>", " ", body, flags=_re.IGNORECASE)
            body = _re.sub(r"<style[\s\S]*?</style>", " ", body, flags=_re.IGNORECASE)
            body = _re.sub(r"<[^>]+>", " ", body)
            body = _re.sub(r"\s+", " ", body).strip()

        truncated = False
        if len(body) > cap:
            truncated = True
            half = cap // 2
            body = body[:half] + "\n...(truncated)...\n" + body[-half:]
        return _json(
            {
                "url": str(r.url),
                "status": r.status_code,
                "content_type": ct,
                "truncated": truncated,
                "content": body,
            }
        )
    except Exception as e:
        log.error("web_fetch failed: %s", e, exc_info=True)
        return f"WARNING: web_fetch error: {e}"


__all__ = [
    '_workspace_layout_policy_block',
    '_workspace_python_impl_roots',
    '_greenfield_python_src_layout_block',
    '_python_syntax_block',
    '_quoted_python_source_lines_block',
    '_target_bound_names',
    '_stmt_bound_names',
    '_TopLevelRuntimeLoadVisitor',
    '_runtime_loads_for_stmt',
    '_python_top_level_order_block',
    '_planned_python_paths',
    '_module_rel_candidates',
    '_module_exists_in_workspace',
    '_module_top_is_local',
    '_relative_import_module',
    '_python_import_resolution_block',
    '_workspace_line_delta',
    '_empty_workspace_file_block',
    '_placeholder_integration_bridge_block',
    '_top_level_python_symbols',
    '_generic_source_markers',
    '_source_truncation_block',
    '_record_workspace_diff',
    '_record_subtask_discovery_tool_call',
    '_verification_steps_from_toml',
    '_verification_step_name',
    '_verification_step_kind',
    '_workspace_toml_verification_guard',
    'update_workspace_seed',
    '_phase_plan_write_order_block',
    '_phase_subtask_retry_escalation_block',
    '_plan_workspace_patch_operation',
    '_plan_workspace_patch_operations',
    '_apply_workspace_patch_plan',
    'apply_workspace_patch',
    '_gmas_first_write_advisory',
    '_source_repair_delete_block',
    '_delete_validate_path',
    'delete_workspace_file',
    '_verification_next_actions',
    'run_workspace_verify',
    'run_workspace_task',
    'sandbox_self_edit',
    'delegate_to_ouroboros',
    'web_fetch',
]
