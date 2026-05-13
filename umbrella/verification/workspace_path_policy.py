"""Unified workspace path policy for verification, sweep, and promotion."""

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _toml  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover
    import tomli as _toml  # type: ignore[no-redef]

log = logging.getLogger(__name__)


BUILTIN_SKIP_PATH_GLOBS: tuple[str, ...] = (
    ".git/**",
    ".memory/**",
    ".umbrella/**",
    ".umbrella_scratch/**",
    ".venv/**",
    "venv/**",
    "vendor/**",
    "node_modules/**",
    "__pycache__/**",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
)


@dataclass(frozen=True)
class WorkspacePathPolicy:
    workspace_path: Path
    skip_patterns: tuple[str, ...] = BUILTIN_SKIP_PATH_GLOBS
    exclude_patterns: tuple[str, ...] = ()

    @classmethod
    def load(cls, workspace_path: str | Path) -> "WorkspacePathPolicy":
        root = Path(workspace_path)
        skip: list[str] = []
        exclude: list[str] = []
        for rel in ("workspace.toml", "verification.toml"):
            path = root / rel
            if not path.exists():
                continue
            try:
                with path.open("rb") as fh:
                    data: dict[str, Any] = _toml.load(fh)
            except Exception as exc:  # noqa: BLE001
                log.debug("workspace path policy: failed to parse %s: %s", path, exc)
                continue
            verification = data.get("verification") if rel == "workspace.toml" else data
            config = data.get("config") if rel == "workspace.toml" else {}
            if isinstance(verification, dict):
                skip.extend(_coerce_patterns(verification.get("skip_paths")))
            if isinstance(config, dict):
                exclude.extend(_coerce_patterns(config.get("exclude_paths")))
            exclude.extend(_coerce_patterns(data.get("exclude_paths")))
        return cls(
            workspace_path=root,
            skip_patterns=(*BUILTIN_SKIP_PATH_GLOBS, *skip),
            exclude_patterns=tuple(exclude),
        )

    def is_skipped(self, rel: str | Path) -> bool:
        text = normalize_rel(rel)
        return glob_matches_any(text, (*self.skip_patterns, *self.exclude_patterns))

    def is_dependency_or_runtime(self, rel: str | Path) -> bool:
        return self.is_skipped(rel)


def normalize_rel(rel: str | Path) -> str:
    return str(rel).replace("\\", "/").strip("/")


def _coerce_patterns(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item or "").strip()]


def glob_matches_any(rel: str | Path, patterns: tuple[str, ...]) -> bool:
    text = normalize_rel(rel)
    if not text:
        return False
    for pattern in patterns:
        pattern = normalize_rel(pattern)
        if not pattern:
            continue
        if pattern.endswith("/**"):
            prefix = pattern[: -len("/**")]
            if text == prefix or text.startswith(prefix + "/"):
                return True
        if pattern.startswith("**/"):
            tail = pattern[len("**/") :]
            parts = text.split("/")
            for idx in range(len(parts)):
                if fnmatch.fnmatch("/".join(parts[idx:]), tail):
                    return True
            continue
        if fnmatch.fnmatch(text, pattern):
            return True
    return False


__all__ = [
    "BUILTIN_SKIP_PATH_GLOBS",
    "WorkspacePathPolicy",
    "glob_matches_any",
    "normalize_rel",
]
