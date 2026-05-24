"""Research-summary and workspace-evidence validation helpers."""

from umbrella.deep_agent_tools.phase_control_common import *
from umbrella.deep_agent_tools.phase_control_base import *
from umbrella.deep_agent_tools.research_provenance import (
    next_finding_source_hint as _research_summary_next_finding_hint,
    palace_add_source_paths_by_id as _shared_palace_add_source_paths_by_id,
    research_scarcity_handoff_issue as _research_scarcity_handoff_issue,
    research_source_coverage_report as _research_source_coverage_report,
    research_summary_source_claim_issue as _shared_research_summary_source_claim_issue,
)

def _stale_claim_context(text: str) -> bool:
    window = str(text or "")
    return bool(_STALE_CLAIM_CONTEXT_RE.search(window)) and not bool(
        _STALE_CLAIM_NEGATION_RE.search(window)
    )


def _accepted_palace_add_ids_for_task(ctx: ToolContext) -> set[str]:
    return set(_accepted_palace_add_aliases_for_task(ctx))


def _accepted_palace_add_aliases_for_task(ctx: ToolContext) -> dict[str, str]:
    task_id = str(getattr(ctx, "task_id", "") or "")
    return _accepted_research_finding_aliases_for_task_id(ctx, task_id)


def _accepted_research_finding_aliases_for_task_id(
    ctx: ToolContext,
    task_id: str,
) -> dict[str, str]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return {}
    path = pathlib.Path(ctx.drive_root) / "logs" / "tools.jsonl"
    if not path.exists():
        return {}
    accepted: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if str(row.get("task_id") or "") != task_id:
                continue
            if str(row.get("tool") or "") != "palace_add":
                continue
            if not _palace_add_row_counts_as_research_finding(row):
                continue
            preview = _json_obj_from_preview(
                row.get("result_preview") or row.get("result")
            )
            if preview.get("saved") is not True:
                continue
            primary_ids = [
                str(preview.get(key) or "").strip()
                for key in ("id", "memory_id", "artifact_id")
                if str(preview.get(key) or "").strip()
            ]
            aliases = list(primary_ids)
            legacy = preview.get("legacy")
            if isinstance(legacy, dict):
                value = str(legacy.get("id") or "").strip()
                if value:
                    aliases.append(value)
            canonical = primary_ids[0] if primary_ids else (aliases[0] if aliases else "")
            if not canonical:
                continue
            for alias in aliases:
                accepted[alias] = canonical
    except OSError:
        return accepted
    return accepted


def _normalise_research_finding_ids(ctx: ToolContext, findings_ids: list[str]) -> list[str]:
    aliases = _accepted_palace_add_aliases_for_task(ctx)
    normalised: list[str] = []
    seen: set[str] = set()
    for raw in findings_ids:
        value = str(raw or "").strip()
        if not value:
            continue
        canonical = aliases.get(value, value)
        if canonical in seen:
            continue
        seen.add(canonical)
        normalised.append(canonical)
    return normalised


def _tool_rows_after(rows: list[dict[str, Any]], since: float | None) -> list[dict[str, Any]]:
    if since is None:
        return rows
    filtered: list[dict[str, Any]] = []
    for row in rows:
        row_time = _tool_row_time(row)
        if row_time is None or row_time + 0.001 >= since:
            filtered.append(row)
    return filtered


def _coerce_log_payload(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    try:
        return json.loads(stripped)
    except Exception:
        return value


def _stringify_payload(value: Any) -> str:
    value = _coerce_log_payload(value)
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


_NON_FINDING_PALACE_KINDS = {
    "architecture",
    "scratchpad",
    "progress",
    "research_progress",
    "phase_progress",
    "note",
    "plan",
    "phase_plan",
    "research_summary",
    "todo",
    "status",
}
_NON_FINDING_PALACE_TAGS = _NON_FINDING_PALACE_KINDS - {"architecture"}
_RESEARCH_FINDING_PROGRESS_RE = re.compile(
    r"(?i)\b(?:evidence\s+ledger|current\s+finding\s+attempts?|"
    r"finding\s+attempts?|accepted\s+findings?|research\s+progress|"
    r"status\s+update|scratchpad|todo|"
    r"need\s+to\s+continue\s+researching|continue\s+researching|"
    r"continue\s+gathering\s+evidence|let\s+me\s+explore|"
    r"make\s+at\s+least\s+\d+\s+palace_add\s+calls?)\b|"
    r"\b\d+\s*/\s*\d+\s+(?:palace\s+)?findings?\b"
)
_RESEARCH_FINDING_PLACEHOLDER_RE = re.compile(
    r"(?i)^\s*(?:placeholder|todo|tbd|research\s+in\s+progress|"
    r"research\s+progress|phase\s+interrupted|incomplete\s+coverage|"
    r"pending\s+completion)\b"
)


def _palace_add_row_counts_as_research_finding(row: dict[str, Any]) -> bool:
    args = _coerce_log_payload(row.get("args") or {})
    if not isinstance(args, dict):
        return True
    preview = _json_obj_from_preview(row.get("result_preview") or row.get("result"))
    if preview.get("verified") is False:
        return False
    kind = str(args.get("kind") or "").strip().lower()
    result_kind = str(preview.get("kind") or "").strip().lower()
    tags_text = str(args.get("tags") or "").strip().lower()
    title = str(args.get("title") or "")
    content = _stringify_payload(args.get("content") or "")
    text = "\n".join(part for part in (title, content) if part)
    tag_values = {
        item.strip().lower()
        for item in re.split(r"[,;\s]+", tags_text)
        if item.strip()
    }
    if (
        kind in _NON_FINDING_PALACE_KINDS
        or result_kind in _NON_FINDING_PALACE_KINDS
        or tag_values & _NON_FINDING_PALACE_TAGS
    ):
        return False
    if _RESEARCH_FINDING_PLACEHOLDER_RE.search(text) or _RESEARCH_FINDING_PROGRESS_RE.search(text):
        return False
    if (
        kind == "research_finding"
        or result_kind == "research_finding"
        or "research_finding" in tag_values
    ):
        return True
    # Compatibility: older accepted research rows may not carry a result kind,
    # but explicit architecture/plan/summary rows must not inflate findings_ids.
    return kind in {"", "observation"} and result_kind in {"", "observation"}


def _normalise_research_path(path: str, workspace_id: str) -> str:
    text = str(path or "").replace("\\", "/").strip().strip("`'\"()[]{} ,:;")
    if not text or text.startswith(("http://", "https://")):
        return ""
    marker = f"workspaces/{workspace_id}/"
    if workspace_id and marker in text:
        text = text.split(marker, 1)[1]
    prefix = f"{workspace_id}/"
    if workspace_id and text.startswith(prefix):
        text = text[len(prefix) :]
    if text.startswith("./"):
        text = text[2:]
    if text.startswith("/") or any(ch in text for ch in ("*", "?", "\n", "\r")):
        return ""
    return text.strip("/")


def _workspace_root_from_drive(ctx: ToolContext) -> pathlib.Path | None:
    try:
        drive = pathlib.Path(ctx.drive_root).resolve()
    except Exception:
        return None
    if drive.name == "drive" and drive.parent.name == ".memory":
        return drive.parent.parent
    return None


def _safe_workspace_file(ctx: ToolContext, path: str) -> pathlib.Path | None:
    workspace_root = _workspace_root_from_drive(ctx)
    workspace_id = _workspace_id_from_drive(ctx)
    normalized = _normalise_research_path(path, workspace_id)
    if not workspace_root or not normalized or normalized.startswith(".memory/"):
        return None
    candidate = (workspace_root / normalized).resolve()
    try:
        candidate.relative_to(workspace_root.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    if not _workspace_file_rel_path(ctx, candidate):
        return None
    if candidate.suffix.lower() not in _SOURCE_FILE_EXTENSIONS and candidate.suffix.lower() not in {
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".md",
        ".txt",
    }:
        return None
    return candidate


def _read_current_workspace_file(ctx: ToolContext, path: str) -> str:
    candidate = _safe_workspace_file(ctx, path)
    if not candidate:
        return ""
    try:
        if candidate.stat().st_size > 512_000:
            return ""
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _iter_workspace_source_files(ctx: ToolContext, *, max_files: int = 300):
    workspace_root = _workspace_root_from_drive(ctx)
    if not workspace_root or not workspace_root.exists():
        return
    yielded = 0
    try:
        for path in workspace_root.rglob("*"):
            if yielded >= max_files:
                return
            if not path.is_file() or path.suffix.lower() not in _SOURCE_FILE_EXTENSIONS:
                continue
            try:
                rel = path.relative_to(workspace_root)
            except ValueError:
                continue
            if any(part in _SOURCE_SCAN_SKIP_DIRS for part in rel.parts[:-1]):
                continue
            try:
                if path.stat().st_size > 512_000:
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            yielded += 1
            yield rel.as_posix(), content
    except OSError:
        return


def _referenced_workspace_paths(text: str, workspace_id: str) -> set[str]:
    paths: set[str] = set()
    for match in _RESEARCH_PATH_RE.finditer(str(text or "")):
        path = _normalise_research_path(match.group(1), workspace_id)
        if path and not path.startswith(".memory/"):
            paths.add(path)
    return paths


def _workspace_file_rel_path(ctx: ToolContext, candidate: pathlib.Path) -> str:
    workspace_root = _workspace_root_from_drive(ctx)
    if not workspace_root:
        return ""
    try:
        rel = candidate.resolve().relative_to(workspace_root.resolve())
    except (OSError, ValueError):
        return ""
    if any(part in _SOURCE_SCAN_SKIP_DIRS for part in rel.parts[:-1]):
        return ""
    return rel.as_posix()


def _existing_workspace_reference_matches(ctx: ToolContext, path: str) -> list[str]:
    workspace_root = _workspace_root_from_drive(ctx)
    workspace_id = _workspace_id_from_drive(ctx)
    normalized = _normalise_research_path(path, workspace_id)
    if not workspace_root or not normalized or normalized.startswith(".memory/"):
        return []

    exact = _safe_workspace_file(ctx, normalized)
    if exact:
        rel = _workspace_file_rel_path(ctx, exact)
        return [rel] if rel else []

    if "/" in normalized:
        return []

    suffix = pathlib.PurePosixPath(normalized).suffix.lower()
    if suffix not in _SOURCE_FILE_EXTENSIONS and suffix not in {
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".md",
        ".txt",
    }:
        return []

    matches: list[str] = []
    try:
        for candidate in workspace_root.rglob(normalized):
            if not candidate.is_file():
                continue
            rel = _workspace_file_rel_path(ctx, candidate)
            if rel:
                matches.append(rel)
    except OSError:
        return []
    return sorted(set(matches))


def _unread_existing_workspace_references(
    ctx: ToolContext, *, referenced: set[str], read_paths: set[str]
) -> list[str]:
    unread: set[str] = set()
    for path in referenced:
        matches = _existing_workspace_reference_matches(ctx, path)
        if len(matches) > 1:
            read_matches = [
                match
                for match in matches
                if _research_reference_was_read(match, read_paths)
            ]
            if len(read_matches) == 1:
                continue
            unread.add(f"{path} (ambiguous; use an explicit path)")
            continue
        if not matches:
            continue
        existing = matches[0]
        if not (
            _research_reference_was_read(existing, read_paths)
            or _research_reference_was_read(path, read_paths)
        ):
            unread.add(existing)
    return sorted(unread)


def _workspace_has_reviewable_source_files(ctx: ToolContext) -> bool:
    workspace_root = _workspace_root_from_drive(ctx)
    if not workspace_root:
        return False
    try:
        for candidate in workspace_root.rglob("*"):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(workspace_root)
            if any(part in _SOURCE_SCAN_SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if candidate.suffix.lower() in _SOURCE_FILE_EXTENSIONS:
                return True
    except OSError:
        return False
    return False


def _read_file_paths_for_task(ctx: ToolContext, rows: list[dict[str, Any]]) -> set[str]:
    workspace_id = _workspace_id_from_drive(ctx)
    read_paths: set[str] = set()
    for row in rows:
        tool = str(row.get("tool") or "")
        if tool == "read_file":
            args = _coerce_log_payload(row.get("args") or {})
            if not isinstance(args, dict):
                continue
            for key in ("file_path", "path"):
                path = _normalise_research_path(str(args.get(key) or ""), workspace_id)
                if path:
                    read_paths.add(path)
            continue
        if tool == "read_workspace_charter":
            payload = _coerce_log_payload(row.get("result_preview") or row.get("result") or {})
            if not isinstance(payload, dict):
                continue
            files = payload.get("files")
            if not isinstance(files, dict):
                continue
            for name in files:
                path = _normalise_research_path(str(name or ""), workspace_id)
                if path:
                    read_paths.add(path)
    return read_paths


def _research_path_key(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/").casefold()


def _research_reference_was_read(reference: str, read_paths: set[str]) -> bool:
    ref_key = _research_path_key(reference)
    if not ref_key:
        return True
    read_keys = {_research_path_key(path) for path in read_paths if path}
    if ref_key in read_keys:
        return True

    if "/" in ref_key:
        suffix_matches = [path for path in read_keys if path.endswith(f"/{ref_key}")]
        return len(suffix_matches) == 1

    basename_matches = [
        path
        for path in read_keys
        if pathlib.PurePosixPath(path).name == ref_key
    ]
    return len(basename_matches) == 1


def _read_file_content_by_path(
    ctx: ToolContext, rows: list[dict[str, Any]]
) -> dict[str, str]:
    workspace_id = _workspace_id_from_drive(ctx)
    out: dict[str, str] = {}
    for row in rows:
        if str(row.get("tool") or "") != "read_file":
            continue
        args = _coerce_log_payload(row.get("args") or {})
        if not isinstance(args, dict):
            continue
        path = _normalise_research_path(str(args.get("file_path") or args.get("path") or ""), workspace_id)
        if not path:
            continue
        payload = _json_obj_from_preview(row.get("result_preview") or row.get("result"))
        content = str(payload.get("content") or "")
        if not content:
            content = _read_current_workspace_file(ctx, path)
        if content:
            out[path] = content
    return out


def _workspace_contents_for_references(
    ctx: ToolContext,
    references: set[str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for reference in references:
        content = _read_current_workspace_file(ctx, reference)
        if content:
            workspace_id = _workspace_id_from_drive(ctx)
            path = _normalise_research_path(reference, workspace_id)
            if path:
                out[path] = content
            continue
        ref_key = _research_path_key(reference)
        if not ref_key:
            continue
        matches: list[tuple[str, str]] = []
        for path, candidate_content in _iter_workspace_source_files(ctx):
            path_key = _research_path_key(path)
            if (
                path_key == ref_key
                or path_key.endswith(f"/{ref_key}")
                or pathlib.PurePosixPath(path_key).name == ref_key
            ):
                matches.append((path, candidate_content))
        if len(matches) == 1:
            path, candidate_content = matches[0]
            out[path] = candidate_content
    return out


def _workspace_contents_for_identifier(
    ctx: ToolContext,
    identifier: str,
) -> dict[str, str]:
    needle = str(identifier or "").split(".", 1)[0].strip()
    if not needle:
        return {}
    found: dict[str, str] = {}
    escaped = re.escape(needle)
    mention_re = re.compile(
        rf"(?m)^\s*(?:class|(?:async\s+)?def)\s+{escaped}\b|"
        rf"\b{escaped}\b"
    )
    for path, content in _iter_workspace_source_files(ctx):
        if mention_re.search(content):
            found[path] = content
    return found


def _merge_contents(*contents: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for group in contents:
        merged.update(group)
    return merged


def _candidate_read_paths_for_claim(
    *,
    window: str,
    module: str = "",
    workspace_id: str,
) -> set[str]:
    candidates = set(_referenced_workspace_paths(window, workspace_id))
    module_text = str(module or "").strip()
    if module_text:
        candidates.add(module_text.replace(".", "/") + ".py")
    return {path for path in candidates if path}


def _content_items_for_references(
    contents: dict[str, str], references: set[str]
) -> list[tuple[str, str]]:
    if not references:
        return list(contents.items())
    out: list[tuple[str, str]] = []
    read_keys = {_research_path_key(path): path for path in contents}
    for reference in references:
        ref_key = _research_path_key(reference)
        if ref_key in read_keys:
            path = read_keys[ref_key]
            out.append((path, contents[path]))
            continue
        matches = [
            path
            for path in contents
            if _research_path_key(path).endswith(f"/{ref_key}")
            or pathlib.PurePosixPath(_research_path_key(path)).name == ref_key
        ]
        if len(matches) == 1:
            path = matches[0]
            out.append((path, contents[path]))
    return list(dict(out).items())


def _content_items_for_unqualified_basename(
    contents: dict[str, str], references: set[str]
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for reference in references:
        ref_key = _research_path_key(reference)
        if not ref_key or "/" in ref_key:
            continue
        for path, content in contents.items():
            if pathlib.PurePosixPath(_research_path_key(path)).name == ref_key:
                out.append((path, content))
    return list(dict(out).items())


def _symbol_defined_in_content(symbol: str, content: str) -> bool:
    name = re.escape(str(symbol or "").strip())
    if not name:
        return False
    patterns = (
        rf"(?m)^\s*(?:async\s+)?def\s+{name}\s*\(",
        rf"(?m)^\s*class\s+{name}\b",
        rf"(?m)^\s*(?:export\s+)?(?:const|let|var|function|class)\s+{name}\b",
        rf"(?m)^\s*{name}\s*=",
        rf"['\"]{name}['\"]",
    )
    return any(re.search(pattern, content) for pattern in patterns)


def _class_defined_in_content(symbol: str, content: str) -> bool:
    name = re.escape(str(symbol or "").strip())
    if not name:
        return False
    return bool(re.search(rf"(?m)^\s*class\s+{name}\b", content))


def _signature_has_optional_param(target: str, param: str, content: str) -> bool:
    target_text = str(target or "").strip()
    param_text = str(param or "").strip()
    if not target_text or not param_text:
        return False
    parts = target_text.split(".")
    func_name = parts[-1]
    class_name = parts[-2] if len(parts) >= 2 and parts[-1] == "__init__" else ""
    if len(parts) == 1 and re.search(rf"(?m)^\s*class\s+{re.escape(target_text)}\b", content):
        class_name = target_text
        func_name = "__init__"
    search_area = content
    if class_name:
        class_match = re.search(rf"(?ms)^\s*class\s+{re.escape(class_name)}\b.*", content)
        if class_match:
            search_area = class_match.group(0)
    for match in re.finditer(rf"(?ms)def\s+{re.escape(func_name)}\s*\((?P<params>.*?)\)", search_area):
        params = match.group("params")
        param_match = re.search(
            rf"(?s)(?:^|,)\s*{re.escape(param_text)}\b(?P<tail>.*?)(?:,|$)",
            params,
        )
        if param_match and "=" in param_match.group("tail"):
            return True
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    except Exception:
        return False
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if class_name and isinstance(node, ast.ClassDef) and node.name == class_name:
            functions.extend(
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == func_name
            )
        elif not class_name and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            functions.append(node)
    for fn in functions:
        args = list(fn.args.posonlyargs) + list(fn.args.args)
        defaults = [None] * (len(args) - len(fn.args.defaults)) + list(fn.args.defaults)
        for arg, default in zip(args, defaults):
            if arg.arg == param_text and default is not None:
                return True
        for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
            if arg.arg == param_text and default is not None:
                return True
    return False


def _call_includes_argument(target: str, param: str, content: str) -> bool:
    root = str(target or "").split(".", 1)[0].strip()
    param_text = str(param or "").strip()
    if not root or not param_text:
        return False
    return bool(
        re.search(
            rf"(?s)\b{re.escape(root)}\s*\([^)]*\b{re.escape(param_text)}\b",
            content,
        )
    )


def _content_mentions_target(target: str, content: str) -> bool:
    root = str(target or "").split(".", 1)[0].strip()
    if not root:
        return False
    escaped = re.escape(root)
    return bool(
        re.search(rf"(?m)^\s*class\s+{escaped}\b", content)
        or re.search(rf"(?m)^\s*(?:async\s+)?def\s+{escaped}\s*\(", content)
        or re.search(rf"(?s)\b{escaped}\s*\(", content)
    )


def _param_claim_contradiction_issue(
    *,
    label: str,
    target: str,
    param: str,
    contents: dict[str, str],
) -> str:
    target = str(target or "").strip()
    param = str(param or "").strip()
    if (
        not target
        or not param
        or param.casefold() in _IGNORED_PARAM_CLAIM_WORDS
        or target.casefold().endswith((".py", ".js", ".ts", ".tsx", ".jsx"))
    ):
        return ""
    saw_target = False
    for path, content in contents.items():
        saw_target = saw_target or _content_mentions_target(target, content)
        if _signature_has_optional_param(target, param, content) or _call_includes_argument(
            target,
            param,
            content,
        ):
            return (
                f"ERROR: {label} claims `{target}()` is missing required "
                f"`{param}`, but current `{path}` already shows that "
                "parameter/signature/call path. Re-check the current code "
                "or mark the older failure as stale."
            )
    if not saw_target:
        return (
            f"ERROR: {label} claims `{target}()` is missing required "
            f"`{param}`, but no current source file defining or calling "
            f"`{target.split('.', 1)[0]}` was available for validation. Read "
            "the implicated source before carrying forward runtime/signature "
            "failure claims."
        )
    return ""


def _snake_case_identifier(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.lower()


def _content_passes_dependency_to_target(
    *, target: str, dependency: str, content: str
) -> bool:
    target_name = str(target or "").split(".", 1)[0].strip()
    dependency_name = str(dependency or "").split(".", 1)[0].strip()
    if not target_name or not dependency_name:
        return False
    escaped_target = re.escape(target_name)
    escaped_dependency = re.escape(dependency_name)
    dependency_vars = {
        value
        for value in {
            dependency_name,
            _snake_case_identifier(dependency_name),
        }
        if value
    }
    for assign in re.finditer(
        rf"(?m)\b(?P<var>[A-Za-z_]\w*)\s*=\s*{escaped_dependency}\s*\(",
        content,
    ):
        dependency_vars.add(str(assign.group("var") or ""))
    for call in re.finditer(
        rf"(?s)\b{escaped_target}\s*\((?P<args>[^)]{{0,900}})\)",
        content,
    ):
        args = str(call.group("args") or "")
        if re.search(rf"\b{escaped_dependency}\s*\(", args):
            return True
        for var in dependency_vars:
            if re.search(rf"\b{re.escape(var)}\b", args):
                return True
    return False


def _dependency_pass_claim_contradiction_issue(
    *,
    label: str,
    target: str,
    dependency: str,
    contents: dict[str, str],
) -> str:
    target = str(target or "").strip()
    dependency = str(dependency or "").strip()
    if not target or not dependency:
        return ""
    for path, content in contents.items():
        if _content_passes_dependency_to_target(
            target=target, dependency=dependency, content=content
        ):
            return (
                f"ERROR: {label} claims `{target}` needs a fix to pass "
                f"`{dependency}`, but current `{path}` already passes that "
                "dependency into the call path. Re-check the current runtime "
                "failure before planning constructor/wiring edits."
            )
    return ""


def _parameter_or_dependency_claim_contradiction_issue(
    ctx: ToolContext,
    *,
    source_text: str,
    label: str,
    log_contents: dict[str, str],
) -> str:
    param_patterns = (
        (_NEGATIVE_PARAM_CLAIM_RE, False),
        (_CONSTRUCTOR_PARAM_CLAIM_RE, False),
        (_DIRECT_PARAM_CLAIM_RE, False),
        (_MISSING_PARAM_IN_TARGET_RE, False),
        (_INCLUDE_PARAM_IN_TARGET_RE, False),
        (_FIX_TARGET_WITH_PARAM_RE, False),
        (_HANDLE_PARAM_IN_TARGET_RE, False),
        (_WITHOUT_PARAM_CLAIM_RE, True),
    )
    for pattern, require_class_target in param_patterns:
        for match in pattern.finditer(source_text):
            if _stale_claim_context(
                source_text[max(0, match.start() - 140) : match.end() + 180]
            ):
                continue
            target = str(match.group("target") or "").strip()
            param = str(match.group("param") or "").strip()
            if require_class_target and not target.split(".", 1)[0][:1].isupper():
                continue
            contents = _merge_contents(
                log_contents,
                _workspace_contents_for_identifier(ctx, target),
            )
            issue = _param_claim_contradiction_issue(
                label=label,
                target=target,
                param=param,
                contents=contents,
            )
            if issue:
                return issue

    for match in _PASS_DEPENDENCY_TO_TARGET_RE.finditer(source_text):
        if _stale_claim_context(
            source_text[max(0, match.start() - 140) : match.end() + 180]
        ):
            continue
        target = str(match.group("target") or "").strip()
        dependency = str(match.group("dependency") or "").strip()
        contents = _merge_contents(
            log_contents,
            _workspace_contents_for_identifier(ctx, target),
            _workspace_contents_for_identifier(ctx, dependency),
        )
        issue = _dependency_pass_claim_contradiction_issue(
            label=label,
            target=target,
            dependency=dependency,
            contents=contents,
        )
        if issue:
            return issue
    return ""


def _negative_claim_contradiction_issue(
    ctx: ToolContext,
    *,
    rows: list[dict[str, Any]],
    text: str,
    label: str,
) -> str:
    workspace_id = _workspace_id_from_drive(ctx)
    log_contents = _read_file_content_by_path(ctx, rows)
    source_text = str(text or "")
    for match in _NEGATIVE_FILE_EXISTENCE_CLAIM_RE.finditer(source_text):
        if _stale_claim_context(
            source_text[max(0, match.start() - 140) : match.end() + 180]
        ):
            continue
        path = str(
            match.group("path1")
            or match.group("path2")
            or match.group("path3")
            or ""
        ).strip()
        if not path:
            continue
        matches = _existing_workspace_reference_matches(ctx, path)
        if matches:
            return (
                f"ERROR: {label} claims `{path}` is missing/nonexistent, "
                f"but current workspace contains {matches[0]}. Re-check the "
                "current file tree before carrying forward file-missing "
                "failure claims."
            )
    for match in _POSITIVE_CLASS_CLAIM_RE.finditer(source_text):
        if _stale_claim_context(
            source_text[max(0, match.start() - 140) : match.end() + 180]
        ):
            continue
        symbol = str(match.group("symbol") or "").strip()
        path = _normalise_research_path(str(match.group("path") or ""), workspace_id)
        if not symbol or not path:
            continue
        contents = _merge_contents(
            log_contents,
            _workspace_contents_for_references(ctx, {path}),
        )
        for read_path, content in _content_items_for_references(contents, {path}):
            if not _class_defined_in_content(symbol, content):
                return (
                    f"ERROR: {label} claims `{read_path}` defines `{symbol}` "
                    "as a class, but current source for that file does not "
                    "contain that class definition. Re-check the file before "
                    "saving or carrying forward code facts."
                )
    for match in _NEGATIVE_SYMBOL_IN_FILE_RE.finditer(source_text):
        if _stale_claim_context(
            source_text[max(0, match.start() - 140) : match.end() + 180]
        ):
            continue
        symbol = str(match.group("symbol") or "").strip()
        path = _normalise_research_path(str(match.group("path") or ""), workspace_id)
        if not symbol or not path:
            continue
        reference_contents = _workspace_contents_for_references(ctx, {path})
        path_key = _research_path_key(path)
        contents = _merge_contents(
            log_contents,
            reference_contents,
            _workspace_contents_for_identifier(ctx, symbol)
            if "/" not in path_key and not reference_contents
            else {},
        )
        for read_path, content in _content_items_for_references(contents, {path}):
            if _symbol_defined_in_content(symbol, content):
                return (
                    f"ERROR: {label} claims `{symbol}` is missing/import-broken, "
                    f"but current `{read_path}` contains that symbol. "
                    "Do not summarize stale negative verification memory as fact; "
                    "revise the finding using current file evidence."
                )
        for read_path, content in _content_items_for_unqualified_basename(
            contents, {path}
        ):
            if _symbol_defined_in_content(symbol, content):
                return (
                    f"ERROR: {label} claims `{symbol}` is missing/import-broken "
                    f"from `{path}`, but current `{read_path}` contains that "
                    "symbol. Use an explicit current path and re-check the "
                    "actual import failure before carrying forward a blocker."
                )
    for match in _SYMBOL_EXPECTATION_MISMATCH_RE.finditer(source_text):
        if _stale_claim_context(
            source_text[max(0, match.start() - 140) : match.end() + 180]
        ):
            continue
        symbol = str(match.group("symbol") or "").strip()
        if not symbol:
            continue
        window = source_text[max(0, match.start() - 220) : match.end() + 220]
        candidates = _candidate_read_paths_for_claim(
            window=window,
            workspace_id=workspace_id,
        )
        contents = _merge_contents(
            log_contents,
            _workspace_contents_for_references(ctx, candidates),
            _workspace_contents_for_identifier(ctx, symbol),
        )
        for path, content in _content_items_for_references(contents, candidates):
            if _symbol_defined_in_content(symbol, content):
                return (
                    f"ERROR: {label} claims an import/test mismatch for `{symbol}`, "
                    f"but current `{path}` contains that symbol. Re-check the "
                    "actual import failure before planning an export or symbol fix."
                )
    for match in _NEGATIVE_SYMBOL_CLAIM_RE.finditer(source_text):
        if _stale_claim_context(
            source_text[max(0, match.start() - 140) : match.end() + 180]
        ):
            continue
        symbol = str(
            match.group("import_symbol")
            or match.group("missing_import_symbol")
            or match.group("missing_import_or_impl_symbol")
            or match.group("missing_import_after_symbol")
            or match.group("implement_missing_symbol")
            or match.group("missing_symbol")
            or match.group("not_exported_symbol")
            or match.group("fix_export_symbol")
            or match.group("ensure_export_symbol")
            or match.group("fix_import_error_symbol")
            or match.group("fix_import_symbol_after")
            or match.group("missing_subject_symbol")
            or match.group("unimportable_symbol")
            or ""
        ).strip()
        if not symbol:
            continue
        window = source_text[max(0, match.start() - 180) : match.end() + 180]
        candidates = _candidate_read_paths_for_claim(
            window=window,
            module=str(match.group("module") or ""),
            workspace_id=workspace_id,
        )
        candidate_contents = _workspace_contents_for_references(ctx, candidates)
        contents = _merge_contents(
            log_contents,
            candidate_contents,
            _workspace_contents_for_identifier(ctx, symbol)
            if not candidate_contents
            else {},
        )
        for path, content in _content_items_for_references(contents, candidates):
            if _symbol_defined_in_content(symbol, content):
                return (
                    f"ERROR: {label} claims `{symbol}` is missing/import-broken, "
                    f"but current `{path}` contains that symbol. "
                    "Do not summarize stale negative verification memory as fact; "
                    "revise the finding using current file evidence."
                )
        for path, content in _content_items_for_unqualified_basename(
            contents, candidates
        ):
            if _symbol_defined_in_content(symbol, content):
                return (
                    f"ERROR: {label} claims `{symbol}` is missing/import-broken, "
                    f"but current `{path}` contains that symbol. The file "
                    "reference is ambiguous by basename; re-check the exact "
                    "current import path before carrying forward the blocker."
                )
    issue = _parameter_or_dependency_claim_contradiction_issue(
        ctx,
        source_text=source_text,
        label=label,
        log_contents=log_contents,
    )
    if issue:
        return issue
    return ""


def _palace_add_text_by_id(
    rows: list[dict[str, Any]], finding_ids: set[str]
) -> dict[str, str]:
    chunks: dict[str, str] = {}
    for row in rows:
        if str(row.get("tool") or "") != "palace_add":
            continue
        preview = _json_obj_from_preview(row.get("result_preview") or row.get("result"))
        ids: list[str] = [
            str(preview.get(key) or "").strip()
            for key in ("id", "memory_id", "artifact_id")
            if str(preview.get(key) or "").strip()
        ]
        legacy = preview.get("legacy")
        if isinstance(legacy, dict):
            legacy_id = str(legacy.get("id") or "").strip()
            if legacy_id:
                ids.append(legacy_id)
        matching = [item for item in ids if item in finding_ids]
        if not matching:
            continue
        args = _coerce_log_payload(row.get("args") or {})
        if isinstance(args, dict):
            text = _stringify_payload({k: args.get(k) for k in ("title", "content", "tags")})
            for item in matching:
                chunks[item] = text
    return chunks


def _palace_add_source_paths_by_id(
    rows: list[dict[str, Any]], finding_ids: set[str]
) -> dict[str, str]:
    return _shared_palace_add_source_paths_by_id(rows, finding_ids)


def _research_summary_source_claim_issue(
    rows: list[dict[str, Any]], *, finding_ids: set[str], notes: str
) -> str:
    return _shared_research_summary_source_claim_issue(
        rows,
        finding_ids=finding_ids,
        notes=notes,
    )


def _palace_add_text_for_ids(
    rows: list[dict[str, Any]], finding_ids: set[str]
) -> str:
    chunks = _palace_add_text_by_id(rows, finding_ids)
    return "\n".join(chunks.values())


def _research_summary_unread_path_issue(
    ctx: ToolContext, *, findings_ids: list[str], notes: str
) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "")
    rows = _tool_log_rows_for_task(ctx, task_id)
    workspace_id = _workspace_id_from_drive(ctx)
    read_paths = _read_file_paths_for_task(ctx, rows)
    finding_id_set = {str(item).strip() for item in findings_ids if str(item).strip()}
    evidence_text = "\n".join(
        part
        for part in (
            str(notes or ""),
            _palace_add_text_for_ids(rows, finding_id_set),
        )
        if part
    )
    referenced = _referenced_workspace_paths(evidence_text, workspace_id)
    unread = _unread_existing_workspace_references(
        ctx, referenced=referenced, read_paths=read_paths
    )
    if not unread:
        return ""
    accepted = sorted(_accepted_palace_add_ids_for_task(ctx))
    accepted_hint = ", ".join(accepted[:8]) or "none"
    read_hint = ", ".join(sorted(read_paths)[:8]) or "none"
    return (
        "ERROR: research summary/finding references current workspace file(s) "
        f"not read in this research phase: {', '.join(unread[:8])}. "
        "Recover by calling read_file for each named path, or by resubmitting "
        "with those file names and code facts removed. Do not invent finding "
        f"IDs. Accepted finding ids currently include: {accepted_hint}. "
        f"Files already read in this phase include: {read_hint}."
    )


def _phase_manifest_payload(ctx: ToolContext) -> dict[str, Any]:
    overlays = _context_overlays(ctx)
    payload = overlays.get("phase_manifest") if isinstance(overlays, dict) else None
    return payload if isinstance(payload, dict) else {}


def _research_depth(ctx: ToolContext) -> str:
    overlays = _context_overlays(ctx)
    value = str(overlays.get("research_depth") or "").strip().lower()
    return value if value in {"none", "light", "full"} else "full"


def _research_summary_min_valid_findings(ctx: ToolContext) -> int:
    manifest = _phase_manifest_payload(ctx)
    criteria = (
        manifest.get("exit_criteria")
        if isinstance(manifest.get("exit_criteria"), dict)
        else {}
    )
    required = 1
    for key in ("required_palace_writes", "min_palace_writes"):
        rules = criteria.get(key) if isinstance(criteria, dict) else []
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if str(rule.get("store") or "") != "palace.run":
                continue
            try:
                n = max(1, int(rule.get("n") or 1))
            except (TypeError, ValueError):
                n = 1
            # Research min_palace_writes is a finding floor, not a total log
            # row floor. The summary is the handoff after those findings.
            required = max(required, n)
    return required


def _research_discovery_validation_issue(
    ctx: ToolContext, rows: list[dict[str, Any]]
) -> str:
    if _research_depth(ctx) != "full":
        return ""
    manifest = _phase_manifest_payload(ctx)
    allowed = manifest.get("allowed_tools") if isinstance(manifest, dict) else []
    if not isinstance(allowed, list):
        return ""
    allowed_tools = {str(item) for item in allowed if str(item).strip()}
    called_tools = {
        str(row.get("tool") or "")
        for row in rows
        if _research_discovery_row_counts(row)
    }
    missing: list[str] = []
    if (
        _RESEARCH_GITHUB_DISCOVERY_TOOL in allowed_tools
        and _RESEARCH_GITHUB_DISCOVERY_TOOL not in called_tools
    ):
        missing.append(_RESEARCH_GITHUB_DISCOVERY_TOOL)
    internet_tools = _RESEARCH_INTERNET_DISCOVERY_TOOLS & allowed_tools
    if internet_tools and not (internet_tools & called_tools):
        missing.append(
            "one internet search tool "
            f"({', '.join(sorted(internet_tools))})"
        )
    if (
        _RESEARCH_MCP_DISCOVERY_TOOL in allowed_tools
        and _RESEARCH_MCP_DISCOVERY_TOOL not in called_tools
    ):
        missing.append(_RESEARCH_MCP_DISCOVERY_TOOL)
    if missing:
        return (
            "ERROR: research summary cannot be submitted before discovery "
            f"coverage has a successful or recoverable attempt: missing "
            f"{', '.join(missing)}. Call the available discovery tools with "
            "task-specific queries and record empty/no-result outcomes as "
            "evidence when applicable. TOOL_ARG_ERROR and query-less calls do "
            "not count as discovery coverage."
        )
    return ""


def _research_discovery_row_counts(row: dict[str, Any]) -> bool:
    tool = str(row.get("tool") or "")
    if tool not in (
        _RESEARCH_GITHUB_DISCOVERY_TOOL,
        _RESEARCH_MCP_DISCOVERY_TOOL,
        *_RESEARCH_INTERNET_DISCOVERY_TOOLS,
    ):
        return False
    args = _coerce_log_payload(row.get("args") or {})
    if isinstance(args, dict) and not str(args.get("query") or "").strip():
        return False
    raw = str(row.get("result_preview") or row.get("result") or "")
    if "TOOL_ARG_ERROR" in raw or "TOOL_PREFLIGHT_ERROR" in raw:
        return False
    payload = _json_obj_from_preview(raw)
    status = str(payload.get("status") or "").strip().lower()
    if not status:
        return True
    if (
        status in {"provider_unavailable", "provider_error", "no_results"}
        and tool in _RESEARCH_INTERNET_DISCOVERY_TOOLS
    ):
        return True
    return status not in {"error", "failed", "failure"}


def _unread_existing_workspace_path_issue(
    ctx: ToolContext, *, text: str, label: str
) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "")
    rows = _tool_log_rows_for_task(ctx, task_id)
    workspace_id = _workspace_id_from_drive(ctx)
    read_paths = _read_file_paths_for_task(ctx, rows)
    referenced = _referenced_workspace_paths(text, workspace_id)
    unread = _unread_existing_workspace_references(
        ctx, referenced=referenced, read_paths=read_paths
    )
    if not unread:
        return ""
    return (
        f"ERROR: {label} references current workspace file(s) not read in this "
        f"phase: {', '.join(unread[:8])}. Read the file with read_file before "
        "saving verified/current code facts, or write the finding as an "
        "unverified lead instead."
    )


def _latest_research_summary_payload(ctx: ToolContext) -> dict[str, Any]:
    path = _drive_state(ctx) / "research_summary_latest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _research_review_validation_issue(
    ctx: ToolContext,
    *,
    verdict: str,
    revisions: list[str] | None = None,
    notes: str,
) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "")
    phase = task_id.split(":", 1)[1] if ":" in task_id else ""
    verdict_lc = str(verdict or "").strip().lower()
    if phase != "research_review":
        return ""

    rows = _tool_log_rows_for_task(ctx, task_id)
    revisions_text = "\n".join(
        str(item) for item in (revisions or []) if str(item).strip()
    )
    review_text = "\n".join(
        part for part in (str(notes or ""), revisions_text) if part
    )
    if verdict_lc != "ok":
        contradiction = _negative_claim_contradiction_issue(
            ctx,
            rows=rows,
            text=review_text,
            label=f"research_review {verdict_lc or 'verdict'}",
        )
        if contradiction:
            return (
                contradiction
                + " Research review revisions must not reintroduce "
                "contradicted code blockers; mark old memory as stale or cite "
                "current file evidence."
            )
        if (
            verdict_lc == "revise"
            and _NON_BLOCKING_RESEARCH_REVISE_RE.search(review_text)
            and not _BLOCKING_RESEARCH_REVISE_RE.search(review_text)
        ):
            return (
                "ERROR: research_review revise is reserved for blocking "
                "research defects that make planning unsafe. Prior-art wording, "
                "citation, novelty, or minor summary corrections should be "
                "submitted as verdict=ok notes unless they create a concrete "
                "architecture or charter blocker."
            )
        if (
            verdict_lc == "revise"
            and _IMPLEMENTATION_OWNED_RESEARCH_REVISE_RE.search(review_text)
            and not _BLOCKING_RESEARCH_REVISE_RE.search(review_text)
        ):
            return (
                "ERROR: research_review revise is reserved for blocking "
                "research defects that make planning unsafe. Exact schemas, "
                "code snippets, module lists, endpoint contracts, test commands, "
                "and other implementation-owned details belong in verdict=ok "
                "notes for the plan/execute/verify phases when the research "
                "already contains a viable architecture."
            )
        return ""

    summary_payload = _latest_research_summary_payload(ctx)
    summary_created_at = summary_payload.get("created_at")
    if not isinstance(summary_created_at, (int, float)):
        summary_created_at = None
    rows_since_summary = _tool_rows_after(
        rows,
        float(summary_created_at) if summary_created_at is not None else None,
    )
    read_paths = _read_file_paths_for_task(ctx, rows_since_summary)
    if not _research_reference_was_read(_RESEARCH_SUMMARY_REL_PATH, read_paths):
        return (
            "ERROR: research_review ok requires reading "
            f"{_RESEARCH_SUMMARY_REL_PATH} in this review phase before "
            "accepting the research handoff."
        )
    summary_policy_issue = _latest_research_summary_memory_policy_issue(
        ctx, summary_payload=summary_payload
    )
    if summary_policy_issue:
        return summary_policy_issue

    summary_notes = str(summary_payload.get("notes") or "")
    evidence_text = "\n".join(
        part for part in (summary_notes, str(notes or "")) if part
    )
    workspace_id = _workspace_id_from_drive(ctx)
    referenced = _referenced_workspace_paths(evidence_text, workspace_id)
    unread = _unread_existing_workspace_references(
        ctx, referenced=referenced, read_paths=read_paths
    )
    if unread:
        return (
            "ERROR: research_review ok references current workspace file(s) "
            f"not read in this review phase: {', '.join(unread[:8])}. "
            "Read the current files yourself before accepting or revise the "
            "research handoff."
        )
    contradiction = _negative_claim_contradiction_issue(
        ctx,
        rows=rows_since_summary,
        text=evidence_text,
        label="research_review ok",
    )
    if contradiction:
        return contradiction

    has_code_claims = bool(referenced) or bool(
        _RESEARCH_REVIEW_CODE_CLAIM_RE.search(evidence_text)
    )
    source_reads = [
        path
        for path in read_paths
        if path and not path.startswith(".memory/") and path != _RESEARCH_SUMMARY_REL_PATH
    ]
    if (
        has_code_claims
        and not source_reads
        and _workspace_has_reviewable_source_files(ctx)
    ):
        return (
            "ERROR: research_review ok includes code/runtime claims but no "
            "current workspace source file was read in this review phase. "
            "Read the implicated workspace files and compare them with the "
            "research summary. External framework/API claims should be "
            "validated with discovery/context tools and do not require "
            "root-repo files to be read through workspace read_file."
        )
    return ""


def _research_review_current_finding_revise_issue(
    ctx: ToolContext,
    *,
    verdict: str,
    issues: list[Any],
    notes: str,
) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "")
    phase = task_id.split(":", 1)[1] if ":" in task_id else ""
    if phase != "research_review" or str(verdict or "").strip().lower() != "revise":
        return ""
    issue_codes = {
        str(getattr(item, "code", "") or "").strip()
        for item in issues
    }
    if "insufficient_research_evidence" not in issue_codes:
        return ""
    summary_payload = _latest_research_summary_payload(ctx)
    if str(summary_payload.get("coverage_status") or "").strip() != "source_scarce":
        return ""
    raw_ids = summary_payload.get("findings_ids")
    if not isinstance(raw_ids, list):
        return ""
    finding_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    if not finding_ids:
        return ""
    research_task_id = _research_task_id_for_review(task_id)
    accepted_aliases = _accepted_research_finding_aliases_for_task_id(
        ctx,
        research_task_id,
    )
    current_ids = [item for item in finding_ids if item in accepted_aliases]
    if not current_ids:
        return ""
    detail = str(notes or "").strip()
    return (
        "ERROR: research_review revise cannot demote the latest source_scarce "
        "research handoff as insufficient evidence when the current summary "
        "already cites accepted current-run research finding id(s): "
        f"{', '.join(current_ids[:6])}. Treat source_scarce as a constrained "
        "low-evidence handoff unless you cite a concrete source-policy, "
        "fabrication, or unbacked-label blocker in typed issues. "
        f"Review note: {detail[:300]}"
    )


def _research_task_id_for_review(task_id: str) -> str:
    text = str(task_id or "").strip()
    if ":" not in text:
        return text
    run_id, phase = text.rsplit(":", 1)
    if phase == "research_review":
        return f"{run_id}:research"
    return text


def _latest_research_summary_memory_policy_issue(
    ctx: ToolContext, *, summary_payload: dict[str, Any]
) -> str:
    notes = str(summary_payload.get("notes") or "")
    fallback_issue = _llm_fallback_handoff_issue(
        notes,
        label="latest research summary notes",
    )
    if fallback_issue:
        return fallback_issue
    test_double_issue = _llm_test_double_handoff_issue(
        notes,
        label="latest research summary notes",
    )
    if test_double_issue:
        return test_double_issue
    cached_decision_issue = _llm_cached_decision_handoff_issue(
        notes,
        label="latest research summary notes",
    )
    if cached_decision_issue:
        return cached_decision_issue
    raw_ids = summary_payload.get("findings_ids")
    if not isinstance(raw_ids, list):
        return ""
    finding_ids = {str(item).strip() for item in raw_ids if str(item).strip()}
    if not finding_ids:
        return ""
    task_id = str(getattr(ctx, "task_id", "") or "")
    research_task_id = _research_task_id_for_review(task_id)
    rows = _tool_log_rows_for_task(ctx, research_task_id)
    if not rows:
        return ""
    source_claim_issue = _research_summary_source_claim_issue(
        rows,
        finding_ids=finding_ids,
        notes=notes,
    )
    if source_claim_issue:
        return (
            source_claim_issue
            + " Research review cannot accept ok while latest summary has "
            "source labels or discovery claims not bound to its cited "
            "accepted findings; loop back to research for a corrected "
            "summary/finding handoff."
        )
    for finding_id, finding_text in _palace_add_text_by_id(rows, finding_ids).items():
        fallback_issue = _llm_fallback_handoff_issue(
            finding_text,
            label=f"research finding `{finding_id}` cited by latest summary",
        )
        if fallback_issue:
            return (
                fallback_issue
                + " Research review cannot accept ok while latest summary "
                "cites unsafe hot memory; loop back to research and save/cite "
                "a corrected palace_add finding."
            )
        test_double_issue = _llm_test_double_handoff_issue(
            finding_text,
            label=f"research finding `{finding_id}` cited by latest summary",
        )
        if test_double_issue:
            return (
                test_double_issue
                + " Research review cannot accept ok while latest summary "
                "cites unsafe hot memory; loop back to research and save/cite "
                "a corrected palace_add finding."
            )
        cached_decision_issue = _llm_cached_decision_handoff_issue(
            finding_text,
            label=f"research finding `{finding_id}` cited by latest summary",
        )
        if cached_decision_issue:
            return (
                cached_decision_issue
                + " Research review cannot accept ok while latest summary "
                "cites unsafe hot memory; loop back to research and save/cite "
                "a corrected palace_add finding."
            )
    return ""


def _research_summary_validation_issue(
    ctx: ToolContext,
    *,
    architecture_id: str,
    findings_ids: list[str],
    notes: str,
    coverage_status: str = "",
) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "")
    phase = task_id.split(":", 1)[1] if ":" in task_id else ""
    if phase != "research":
        return ""
    architecture = str(architecture_id or "").strip()
    if not architecture:
        return "ERROR: research summary needs a non-empty architecture_id"
    if (
        "/" in architecture
        or "\\" in architecture
        or not _RESEARCH_ARCHITECTURE_ID_RE.match(architecture)
    ):
        return (
            "ERROR: research summary architecture_id must be a stable "
            "`arch-...` or `architecture-...` architecture slug that you coin "
            "for this handoff, for example `arch-civilization-gmas-web-v1`. "
            "Do not pass palace_add/memory ids such as UUIDs or `drawer_*` "
            "values as architecture_id; those belong only in `findings_ids`."
        )
    if _RESEARCH_ARCHITECTURE_ID_BAD_TOKEN_RE.search(architecture):
        return (
            "ERROR: research summary architecture_id must not include mock, "
            "fake, stub, dry-run, fallback, or placeholder tokens. The "
            "architecture id is an authoritative handoff label, not a proof "
            "shortcut or temporary implementation mode."
        )
    note_text = str(notes or "").strip()
    concrete_findings = [str(item).strip() for item in findings_ids if str(item).strip()]
    rows = _tool_log_rows_for_task(ctx, task_id)
    if not concrete_findings:
        source_hint = _research_summary_next_finding_hint(rows)
        return (
            "ERROR: research summary needs findings_ids from accepted palace_add "
            "result ids; do not submit an empty findings list."
            f"{source_hint}"
        )
    accepted_aliases = _accepted_palace_add_aliases_for_task(ctx)
    accepted = set(accepted_aliases)
    unknown = [item for item in concrete_findings if item not in accepted]
    if unknown:
        known = ", ".join(sorted(accepted)[:6]) or "none"
        source_hint = _research_summary_next_finding_hint(rows)
        return (
            "ERROR: research summary references finding id(s) that were not "
            f"accepted by palace_add as research_finding for this task: "
            f"{', '.join(unknown[:6])}. Use the id or legacy.id returned by a "
            f"concrete research_finding palace_add entry. Known ids: {known}."
            f"{source_hint}"
        )
    min_findings = _research_summary_min_valid_findings(ctx)
    canonical_findings: list[str] = []
    duplicate_aliases: list[str] = []
    seen_canonical: set[str] = set()
    for item in concrete_findings:
        canonical = accepted_aliases.get(item)
        if not canonical:
            continue
        if canonical in seen_canonical:
            duplicate_aliases.append(item)
            continue
        seen_canonical.add(canonical)
        canonical_findings.append(canonical)
    if duplicate_aliases:
        return (
            "ERROR: research summary cites the same palace_add finding more "
            "than once via id/legacy aliases: "
            f"{', '.join(duplicate_aliases[:6])}. Cite each accepted memory "
            "entry once, preferably using the primary `id` returned by "
            "palace_add; do not inflate findings_ids with legacy drawer aliases."
        )
    valid_unique = set(canonical_findings)
    if len(valid_unique) < min_findings:
        scarcity_issue = _research_scarcity_handoff_issue(
            rows,
            accepted_count=len(valid_unique),
            min_findings=min_findings,
            coverage_status=coverage_status,
        )
        if not scarcity_issue:
            pass
        elif str(coverage_status or "").strip():
            return scarcity_issue
        else:
            source_hint = _research_summary_next_finding_hint(rows)
            return (
                "ERROR: research summary needs at least "
                f"{min_findings} accepted palace_add finding id(s) for this phase, "
                "counted as unique memory entries; "
                f"got {len(valid_unique)}. Add another concrete palace_add finding "
                "or include the correct returned id."
                f"{source_hint} {scarcity_issue}"
            )
    skill_issue = _research_summary_skill_coverage_issue(ctx, rows)
    if skill_issue:
        return skill_issue
    gmas_issue = _research_summary_gmas_coverage_issue(ctx, rows)
    if gmas_issue:
        return gmas_issue
    discovery_issue = _research_discovery_validation_issue(ctx, rows)
    if discovery_issue:
        return discovery_issue
    unread_issue = _research_summary_unread_path_issue(
        ctx, findings_ids=concrete_findings, notes=note_text
    )
    if unread_issue:
        return unread_issue
    finding_id_set = {str(item).strip() for item in concrete_findings if str(item).strip()}
    source_claim_issue = _research_summary_source_claim_issue(
        rows, finding_ids=finding_id_set, notes=note_text
    )
    if source_claim_issue:
        return source_claim_issue
    contradiction = _negative_claim_contradiction_issue(
        ctx,
        rows=rows,
        text=note_text,
        label="research summary notes",
    )
    if contradiction:
        return contradiction
    for finding_id, finding_text in _palace_add_text_by_id(rows, finding_id_set).items():
        contradiction = _negative_claim_contradiction_issue(
            ctx,
            rows=rows,
            text=finding_text,
            label=f"research finding `{finding_id}`",
        )
        if contradiction:
            return (
                f"{contradiction} Remove that finding id from findings_ids "
                "or replace it with a corrected palace_add finding."
            )
    return ""


def _research_summary_allowed_skills(ctx: ToolContext) -> list[str]:
    overlays = _context_overlays(ctx)
    manifest = overlays.get("phase_manifest")
    if not isinstance(manifest, dict):
        return []
    raw = manifest.get("allowed_skills") or []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _research_tool_row_succeeded(row: dict[str, Any]) -> bool:
    if row.get("error"):
        return False
    if row.get("exit_code", 0) not in (0, None):
        return False
    raw = str(row.get("result_preview") or row.get("result") or "").strip()
    lower = raw.lower()
    if lower.startswith(("error:", "warning:")):
        return False
    try:
        payload = json.loads(raw)
    except Exception:
        return True
    if not isinstance(payload, dict):
        return True
    status = str(payload.get("status") or "").strip().lower()
    if status in {
        "blocked",
        "budget_exhausted",
        "error",
        "failed",
        "provider_error",
        "rate_limited",
        "tool_error",
        "warning",
    }:
        return False
    if payload.get("error"):
        return False
    if payload.get("passed") is False:
        return False
    return True


def _research_gmas_tool_row_succeeded(row: dict[str, Any]) -> bool:
    if not _research_tool_row_succeeded(row):
        return False
    raw = str(row.get("result_preview") or row.get("result") or "").strip()
    try:
        payload = json.loads(raw)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    has_non_fallback_result = False
    for item in results:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        source_type = str(item.get("source_type") or "").strip().lower()
        if metadata.get("fallback") is True or source_type == "gmas_fallback":
            continue
        has_non_fallback_result = True
        break
    has_card_signal = any(
        payload.get(key)
        for key in (
            "recommended_pattern",
            "key_files",
            "key_symbols",
            "example_usage",
            "doc_references",
            "retrieval_excerpt",
        )
    )
    return bool(has_non_fallback_result or has_card_signal)


def _research_summary_skill_coverage_issue(
    ctx: ToolContext, rows: list[dict[str, Any]]
) -> str:
    allowed = _research_summary_allowed_skills(ctx)
    if not allowed:
        return ""
    loaded = [
        str((row.get("args") or {}).get("slug") or "").strip()
        for row in rows
        if str(row.get("tool") or "") == "load_skill"
        and isinstance(row.get("args"), dict)
        and _research_tool_row_succeeded(row)
    ]
    if loaded:
        return ""
    examples = ", ".join(f"`{item}`" for item in allowed[:4])
    return (
        "ERROR: research summary missing required skill coverage. "
        "This research manifest declares allowed_skills, so load at least one "
        f"task-relevant skill via `load_skill(slug=...)` before submitting. "
        f"Available examples: {examples}."
    )


def _research_summary_requires_gmas_context(ctx: ToolContext) -> bool:
    overlays = _context_overlays(ctx)
    if bool(overlays.get("gmas_prewrite_required")):
        return True
    domains = overlays.get("detected_domains") or []
    if isinstance(domains, list) and any(
        str(item).strip().lower() == "multi_agent_gmas" for item in domains
    ):
        return True
    drive_root = pathlib.Path(getattr(ctx, "drive_root", "") or "")
    workspace_root = drive_root.parent.parent if drive_root.name == "drive" else None
    if workspace_root is None:
        return False
    try:
        text = (workspace_root / "workspace.toml").read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(r"(?im)^\s*multi_agent_gmas\s*=\s*true\s*$", text))


def _research_summary_gmas_coverage_issue(
    ctx: ToolContext, rows: list[dict[str, Any]]
) -> str:
    if not _research_summary_requires_gmas_context(ctx):
        return ""
    gmas_tools = {"get_gmas_context", "search_gmas_knowledge"}
    attempted = any(
        str(row.get("tool") or "") in gmas_tools
        and _research_gmas_tool_row_succeeded(row)
        for row in rows
    )
    if attempted:
        return ""
    return (
        "ERROR: research summary missing GMAS context coverage. This workspace "
        "is marked as multi_agent_gmas/LLM-agent work, so research must call "
        "`get_gmas_context(query=...)` or `search_gmas_knowledge(query=...)` "
        "with a concrete architecture/API query in this same research phase "
        "before handing off to planning."
    )


__all__ = [
    '_stale_claim_context',
    '_accepted_palace_add_ids_for_task',
    '_accepted_palace_add_aliases_for_task',
    '_accepted_research_finding_aliases_for_task_id',
    '_normalise_research_finding_ids',
    '_tool_rows_after',
    '_coerce_log_payload',
    '_stringify_payload',
    '_normalise_research_path',
    '_workspace_root_from_drive',
    '_safe_workspace_file',
    '_read_current_workspace_file',
    '_iter_workspace_source_files',
    '_referenced_workspace_paths',
    '_workspace_file_rel_path',
    '_existing_workspace_reference_matches',
    '_unread_existing_workspace_references',
    '_workspace_has_reviewable_source_files',
    '_read_file_paths_for_task',
    '_research_path_key',
    '_research_reference_was_read',
    '_read_file_content_by_path',
    '_research_task_id_for_review',
    '_latest_research_summary_memory_policy_issue',
    '_workspace_contents_for_references',
    '_workspace_contents_for_identifier',
    '_merge_contents',
    '_candidate_read_paths_for_claim',
    '_content_items_for_references',
    '_content_items_for_unqualified_basename',
    '_symbol_defined_in_content',
    '_class_defined_in_content',
    '_signature_has_optional_param',
    '_call_includes_argument',
    '_content_mentions_target',
    '_param_claim_contradiction_issue',
    '_snake_case_identifier',
    '_content_passes_dependency_to_target',
    '_dependency_pass_claim_contradiction_issue',
    '_parameter_or_dependency_claim_contradiction_issue',
    '_negative_claim_contradiction_issue',
    '_palace_add_text_by_id',
    '_palace_add_source_paths_by_id',
    '_palace_add_text_for_ids',
    '_research_summary_source_claim_issue',
    '_research_summary_unread_path_issue',
    '_phase_manifest_payload',
    '_research_depth',
    '_research_summary_min_valid_findings',
    '_research_source_coverage_report',
    '_research_discovery_validation_issue',
    '_research_discovery_row_counts',
    '_unread_existing_workspace_path_issue',
    '_latest_research_summary_payload',
    '_research_review_validation_issue',
    '_research_review_current_finding_revise_issue',
    '_research_summary_validation_issue',
]
