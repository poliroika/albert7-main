import json
import pathlib
from typing import Any

import yaml
try:
    import jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False

from umbrella.phases.base import (
    PhaseManifest, PromptFiles, MemoryPolicy, MemoryAlwaysOnRule,
    MemoryHotRule, MemoryWarmSearchRule, MemoryGraphPolicy, WriteRule,
    PermissionPolicy, PermissionRule, ExitCriteria, RequiredPalaceWrite, Budgets,
)

_SCHEMA_PATH = pathlib.Path(__file__).parent / "schema" / "manifest.schema.json"
_schema: dict[str, Any] | None = None


class PhaseManifestError(Exception):
    def __init__(self, path: str, errors: list[str]) -> None:
        self.path = path
        self.errors = errors
        super().__init__(f"Invalid manifest {path}: {'; '.join(errors)}")


def _load_schema() -> dict[str, Any]:
    global _schema
    if _schema is None:
        _schema = json.loads(_SCHEMA_PATH.read_text())
    return _schema


def _validate(data: dict[str, Any], path: str) -> None:
    if not _HAS_JSONSCHEMA:
        return
    schema = _load_schema()
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        raise PhaseManifestError(path, [e.message for e in errors])
    allowed = set(data.get("allowed_tools", []))
    forbidden = set(data.get("forbidden_tools", []))
    overlap = allowed & forbidden
    if overlap:
        raise PhaseManifestError(path, [f"allowed_tools ∩ forbidden_tools overlap: {overlap}"])


def _parse_memory(raw: dict[str, Any]) -> MemoryPolicy:
    always_on = tuple(
        MemoryAlwaysOnRule(store=r["store"], tier=r.get("tier", "always_on"))
        for r in raw.get("always_on", [])
    )
    hot = tuple(
        MemoryHotRule(store=r["store"], tags=tuple(r.get("tags", [])))
        for r in raw.get("hot", [])
    )
    warm_search = tuple(
        MemoryWarmSearchRule(store=r["store"], n=r.get("n", 6), filter=r.get("filter"))
        for r in raw.get("warm_search", [])
    )
    graph_raw = raw.get("graph")
    graph = None
    if graph_raw:
        graph = MemoryGraphPolicy(
            walk_edges=tuple(graph_raw.get("walk_edges", [])),
            hops=graph_raw.get("hops", 1),
        )
    write_rules = {
        k: WriteRule(
            store=v["store"], tier=v["tier"], scope=v["scope"],
            verified=v.get("verified", False),
        )
        for k, v in raw.get("write_rules", {}).items()
    }
    return MemoryPolicy(always_on=always_on, hot=hot, warm_search=warm_search, graph=graph, write_rules=write_rules)


def _parse_permissions(raw: dict[str, Any]) -> PermissionPolicy:
    rules: list[PermissionRule] = []
    for r in raw.get("rules", []):
        if "allow_tools" in r:
            rules.append(PermissionRule(action="allow", tools=tuple(r["allow_tools"])))
        elif "allow_tool" in r:
            args = r.get("args", {})
            rules.append(PermissionRule(
                action="allow", tools=(r["allow_tool"],),
                cmd_re=args.get("cmd_re"), scope_arg=args.get("scope"),
            ))
        elif "deny_tools" in r:
            rules.append(PermissionRule(action="deny", tools=tuple(r["deny_tools"])))
        elif "deny_tool" in r:
            args = r.get("args", {})
            rules.append(PermissionRule(
                action="deny", tools=(r["deny_tool"],),
                cmd_re=args.get("cmd_re"),
            ))
        elif "deny_path" in r:
            rules.append(PermissionRule(
                action="deny", tools=None,
                path_patterns=tuple(r["deny_path"]),
            ))
    return PermissionPolicy(rules=tuple(rules))


def _parse_exit_criteria(raw: dict[str, Any]) -> ExitCriteria:
    return ExitCriteria(
        required_calls=tuple(raw.get("required_calls", [])),
        required_prior_calls=tuple(raw.get("required_prior_calls", [])),
        required_palace_writes=tuple(
            RequiredPalaceWrite(store=r["store"], tag=r.get("tag"), n=r.get("n", 1))
            for r in raw.get("required_palace_writes", [])
        ),
        min_palace_writes=tuple(
            RequiredPalaceWrite(store=r["store"], n=r.get("n", 1))
            for r in raw.get("min_palace_writes", [])
        ),
    )


def load_manifest(path: pathlib.Path | str) -> PhaseManifest:
    path = pathlib.Path(path)
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    _validate(data, str(path))
    prompt_raw = data["prompt_files"]
    prompt_files = PromptFiles(
        system=tuple(prompt_raw["system"]),
        user_overlay=tuple(prompt_raw.get("user_overlay", [])),
        charter_blocks=tuple(prompt_raw.get("charter_blocks", [])),
    )
    budgets_raw = data.get("budgets", {})
    return PhaseManifest(
        id=data["id"],
        version=data["version"],
        description=data["description"],
        prompt_files=prompt_files,
        allowed_tools=frozenset(data.get("allowed_tools", [])),
        forbidden_tools=frozenset(data.get("forbidden_tools", [])),
        allowed_skills=frozenset(data.get("allowed_skills", [])),
        memory=_parse_memory(data.get("memory", {})),
        permissions=_parse_permissions(data.get("permissions", {})),
        exit_criteria=_parse_exit_criteria(data.get("exit_criteria", {})),
        mini_review_after=data.get("mini_review_after"),
        budgets=Budgets(
            max_tokens=budgets_raw.get("max_tokens"),
            max_seconds=budgets_raw.get("max_seconds"),
            max_tool_calls=budgets_raw.get("max_tool_calls"),
        ),
        temp_tools_allowed=data.get("temp_tools_allowed", False),
    )
