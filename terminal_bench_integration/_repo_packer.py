"""Build a tarball of the umbrella repo for shipping into a TB container.

The tarball is built from an **allow-list** of top-level entries: only
the things `umbrella.app_ouroboros` actually needs at runtime against the
`workspaces/terminal_bench` adapter workspace are included. Everything
else (other workspaces' vendored MCPs with hundreds of MB of demo
assets, host caches, host-OS-specific venvs, git history, build
artefacts) is dropped.

If you add a new workspace and want it shipped into the container,
extend `_INCLUDE_WORKSPACES` below.

The result is a single ``.tar.gz`` written to a caller-chosen path.
"""



import logging
import tarfile
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

# Top-level files that should always ship.
_INCLUDE_TOPLEVEL_FILES: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "README.md",
        ".gitignore",
    }
)

# Top-level directories that should always ship in full (subject to the
# generic exclusions below).
_INCLUDE_TOPLEVEL_DIRS: frozenset[str] = frozenset(
    {
        "umbrella",
        "ouroboros",
        "terminal_bench_integration",
    }
)

# `gmas/` is shipped, but only the runtime-essential parts -- `src/`,
# `pyproject.toml`, `LICENSE`, `README.md`. We deliberately exclude
# `tests/`, `examples/`, `benchmarks/`, `docs/`, `uv.lock`,
# `requirements.txt` (>100 KB) and the multi-MB tutorial assets, since
# nothing about TB tasks needs them.
_INCLUDE_GMAS_TOPLEVEL_FILES: frozenset[str] = frozenset(
    {"pyproject.toml", "LICENSE", "README.md"}
)
_INCLUDE_GMAS_TOPLEVEL_DIRS: frozenset[str] = frozenset({"src"})

# Workspaces under `workspaces/` that we want shipped. Add more here as
# you start running them under TB. The `registry.toml` and any
# top-level `__init__.py` inside `workspaces/` are always shipped.
_INCLUDE_WORKSPACES: frozenset[str] = frozenset(
    {
        "terminal_bench",
    }
)

# Path components anywhere in the relative path that disqualify a file
# even if its top-level entry is on the include list. These are pure
# noise inside a runtime container.
_EXCLUDE_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        ".venv312",
        "venv",
        "venv312",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".umbrella",
        ".memory",
        "node_modules",
        "_runs",
        "instances",
        "snapshots",
        "reports",
        "runs",
        "vendor",
    }
)

# Suffixes that should never be packed.
_EXCLUDE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pyc", ".pyo", ".pyd", ".dll", ".lib", ".exe", ".so~", ".dylib",
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff",
        ".mp4", ".mov", ".webm",
        ".log",
        ".tar", ".tar.gz", ".tgz", ".zip",
    }
)


def _included(rel: Path) -> bool:
    """Decide whether a repo-relative path should be packed.

    Allow-list semantics:

    * Top-level files in :data:`_INCLUDE_TOPLEVEL_FILES`.
    * Anything beneath a directory in :data:`_INCLUDE_TOPLEVEL_DIRS`.
    * `workspaces/registry.toml`, `workspaces/__init__.py`, and
      everything beneath `workspaces/<id>/` for ``id`` in
      :data:`_INCLUDE_WORKSPACES`.

    Then a generic deny pass strips dirs in :data:`_EXCLUDE_DIR_NAMES`
    and suffixes in :data:`_EXCLUDE_SUFFIXES` from the result.
    """
    parts = rel.parts
    if not parts:
        return False

    # Generic deny.
    for part in parts:
        if part in _EXCLUDE_DIR_NAMES:
            return False
        if part.startswith("tmp_"):
            return False
    if rel.suffix.lower() in _EXCLUDE_SUFFIXES:
        return False

    if len(parts) == 1:
        return parts[0] in _INCLUDE_TOPLEVEL_FILES

    head = parts[0]
    if head in _INCLUDE_TOPLEVEL_DIRS:
        return True

    if head == "gmas":
        if len(parts) == 2 and parts[1] in _INCLUDE_GMAS_TOPLEVEL_FILES:
            return True
        if len(parts) >= 2 and parts[1] in _INCLUDE_GMAS_TOPLEVEL_DIRS:
            return True
        return False

    if head == "workspaces":
        if len(parts) == 2 and parts[1] in {"registry.toml", "__init__.py"}:
            return True
        if len(parts) >= 2 and parts[1] in _INCLUDE_WORKSPACES:
            return True
        return False

    return False


def _iter_repo_files(repo_root: Path) -> Iterable[Path]:
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root)
        if not _included(rel):
            continue
        yield path


def build_repo_tarball(repo_root: Path, dest: Path) -> Path:
    """Create a ``.tar.gz`` of ``repo_root`` at ``dest`` and return the path.

    The archive is built deterministically (sorted file order). Members are
    rooted at ``umbrella/`` so extracting the tar inside `/opt/` yields
    ``/opt/umbrella/...`` directly.
    """
    repo_root = repo_root.resolve()
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(_iter_repo_files(repo_root))
    log.info("Packing %d files from %s into %s", len(files), repo_root, dest)

    with tarfile.open(dest, "w:gz") as tar:
        for path in files:
            arcname = "umbrella/" + path.relative_to(repo_root).as_posix()
            tar.add(path, arcname=arcname, recursive=False)

    size_mb = dest.stat().st_size / (1024 * 1024)
    log.info("Built tarball %s (%.1f MB)", dest, size_mb)
    return dest
