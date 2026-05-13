"""
Default policy values and loader for umbrella policies.
"""

import logging
from pathlib import Path
from typing import Any, Dict

from umbrella.policies.models import (
    SystemBoundaryPolicy,
)

log = logging.getLogger(__name__)

# Default policy path relative to this file
DEFAULT_POLICY_PATH = Path(__file__).parent / "default_policy.yaml"


def _merge_dataclass(instance: Any, updates: dict[str, Any]) -> Any:
    """Return a new dataclass instance with ``updates`` applied (known fields only)."""
    cls = type(instance)
    kwargs = {}
    for name in cls.__dataclass_fields__:
        if name in updates:
            kwargs[name] = updates[name]
        else:
            kwargs[name] = getattr(instance, name)
    return cls(**kwargs)


def _dict_to_policy(data: dict[str, Any]) -> SystemBoundaryPolicy:
    """Convert loaded YAML/TOML dict to SystemBoundaryPolicy."""
    sb = dict(data.get("system_boundary") or {})

    si_data = sb.pop("self_improvement", None) or {}
    fb_data = sb.pop("framework_boundary", None) or {}
    es_data = sb.pop("edit_surface", None) or {}
    wm_data = sb.pop("workspace_mutation", None) or {}
    esc_data = sb.pop("escalation", None) or {}
    sse_data = sb.pop("sandbox_self_edit", None) or {}

    base = SystemBoundaryPolicy()
    self_improvement = (
        _merge_dataclass(base.self_improvement, si_data)
        if si_data
        else base.self_improvement
    )
    framework_boundary = (
        _merge_dataclass(base.framework_boundary, fb_data)
        if fb_data
        else base.framework_boundary
    )
    edit_surface = (
        _merge_dataclass(base.edit_surface, es_data) if es_data else base.edit_surface
    )
    workspace_mutation = (
        _merge_dataclass(base.workspace_mutation, wm_data)
        if wm_data
        else base.workspace_mutation
    )
    escalation = (
        _merge_dataclass(base.escalation, esc_data) if esc_data else base.escalation
    )
    sandbox_self_edit = (
        _merge_dataclass(base.sandbox_self_edit, sse_data)
        if sse_data
        else base.sandbox_self_edit
    )

    nested_keys = {
        "edit_surface",
        "self_improvement",
        "escalation",
        "workspace_mutation",
        "framework_boundary",
        "sandbox_self_edit",
    }
    top_fields = set(SystemBoundaryPolicy.__dataclass_fields__) - nested_keys
    top_updates = {k: v for k, v in sb.items() if k in top_fields}

    return SystemBoundaryPolicy(
        edit_surface=edit_surface,
        self_improvement=self_improvement,
        escalation=escalation,
        workspace_mutation=workspace_mutation,
        framework_boundary=framework_boundary,
        sandbox_self_edit=sandbox_self_edit,
        **top_updates,
    )


def load_default_policy() -> SystemBoundaryPolicy:
    """Load the default policy from ``default_policy.yaml``, or built-in defaults if missing or invalid."""
    if not DEFAULT_POLICY_PATH.is_file():
        return SystemBoundaryPolicy()
    try:
        return load_policy_from_file(DEFAULT_POLICY_PATH)
    except Exception as exc:
        log.warning("Falling back to built-in policy defaults (%s)", exc)
        return SystemBoundaryPolicy()


def load_policy_from_file(path: Path) -> SystemBoundaryPolicy:
    """Load policy from a configuration file path.

    Supports YAML and TOML formats based on file extension.
    """
    if not path.exists():
        raise FileNotFoundError(f"Policy file not found: {path}")

    content = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix in (".yaml", ".yml"):
        return _parse_yaml_policy(content)
    if suffix == ".toml":
        return _parse_toml_policy(content)
    raise ValueError(f"Unsupported policy file format: {suffix}")


def load_policy(config_path: Path | str | None = None) -> SystemBoundaryPolicy:
    """Load policy from a configuration file path or return defaults."""
    if config_path is None:
        return load_default_policy()

    if isinstance(config_path, str):
        config_path = Path(config_path)

    return load_policy_from_file(config_path)


def _parse_yaml_policy(content: str) -> SystemBoundaryPolicy:
    """Parse policy from YAML content."""
    try:
        import yaml
    except ImportError as exc:
        raise ImportError("PyYAML is required to load YAML policy files") from exc

    data = yaml.safe_load(content)
    if not data:
        return SystemBoundaryPolicy()

    return _dict_to_policy(data)


def _parse_toml_policy(content: str) -> SystemBoundaryPolicy:
    """Parse policy from TOML content."""
    try:
        import tomllib

        data = tomllib.loads(content)
    except ImportError:
        try:
            import toml

            data = toml.loads(content)
        except ImportError as exc:
            raise ImportError(
                "tomli or toml is required to load TOML policy files"
            ) from exc

    if not data:
        return SystemBoundaryPolicy()

    return _dict_to_policy(data)
