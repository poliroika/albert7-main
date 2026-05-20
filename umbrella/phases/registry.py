import os
import pathlib
from typing import Iterator

from umbrella.phases.base import PhaseManifest
from umbrella.phases.loader import load_manifest, PhaseManifestError

_DEFAULT_MANIFESTS_DIR = pathlib.Path(__file__).parent / "manifests"


class PhaseRegistry:
    def __init__(self, manifests_dir: pathlib.Path | None = None) -> None:
        self._dir = manifests_dir or pathlib.Path(
            os.environ.get("OUROBOROS_PHASES_DIR", str(_DEFAULT_MANIFESTS_DIR))
        )
        self._cache: dict[str, PhaseManifest] = {}
        self._errors: dict[str, str] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._dir.is_dir():
            return
        for yaml_path in sorted(self._dir.glob("*.yaml")):
            try:
                m = load_manifest(yaml_path)
                if m.id in self._cache:
                    self._errors[str(yaml_path)] = f"Duplicate phase id '{m.id}'"
                    continue
                self._cache[m.id] = m
            except PhaseManifestError as exc:
                self._errors[str(yaml_path)] = str(exc)

    def get(self, phase_id: str) -> PhaseManifest:
        self._ensure_loaded()
        if phase_id not in self._cache:
            raise KeyError(f"Unknown phase '{phase_id}'. Errors: {self._errors}")
        return self._cache[phase_id]

    def all(self) -> list[PhaseManifest]:
        self._ensure_loaded()
        return list(self._cache.values())

    def ids(self) -> list[str]:
        self._ensure_loaded()
        return list(self._cache.keys())

    def errors(self) -> dict[str, str]:
        self._ensure_loaded()
        return dict(self._errors)

    def validate_all(self) -> list[str]:
        self._ensure_loaded()
        return [f"{path}: {msg}" for path, msg in self._errors.items()]

    def __iter__(self) -> Iterator[PhaseManifest]:
        return iter(self.all())


_global_registry: PhaseRegistry | None = None


def get_registry(manifests_dir: pathlib.Path | None = None) -> PhaseRegistry:
    global _global_registry
    if _global_registry is None or manifests_dir is not None:
        _global_registry = PhaseRegistry(manifests_dir)
    return _global_registry


def reset_registry() -> None:
    global _global_registry
    _global_registry = None
