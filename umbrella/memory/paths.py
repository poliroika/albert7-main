"""Filesystem layout for per-workspace memory isolation.

Workspace-scoped memory lives under ``workspaces/<id>/.memory/``.
Manager (cross-workspace) memory stays under ``.umbrella/memory/``.
Each workspace gets its own MemPalace Chroma directory under ``.../.memory/palace/``.
"""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from umbrella.memory.store import MemoryStore


def _safe_workspace_segment(workspace_id: str) -> str:
    raw = (workspace_id or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return ""
    if ".." in Path(raw).parts:
        raise ValueError("workspace_id must not contain path traversal")
    return raw


def workspace_memory_root(repo_root: Path, workspace_id: str) -> Path:
    """``repo_root/workspaces/<id>/.memory/`` (resolved)."""
    seg = _safe_workspace_segment(workspace_id)
    if not seg:
        raise ValueError("workspace_id is required for workspace_memory_root")
    return (repo_root / "workspaces" / seg / ".memory").resolve()


def manager_memory_root(repo_root: Path) -> Path:
    """``repo_root/.umbrella/memory/`` — manager lessons, gaps, signals, ideas."""
    return (repo_root / ".umbrella" / "memory").resolve()


def manager_core_root(repo_root: Path) -> Path:
    """``repo_root/.umbrella/memory/core/`` — always-loaded manager core memory."""
    return (manager_memory_root(repo_root) / "core").resolve()


def workspace_core_root(repo_root: Path, workspace_id: str) -> Path:
    """``workspaces/<id>/.memory/core/`` — always-loaded workspace core memory."""
    return (workspace_memory_root(repo_root, workspace_id) / "core").resolve()


def workspace_root(repo_root: Path, workspace_id: str) -> Path:
    """``repo_root/workspaces/<id>/`` — workspace project root (not .memory)."""
    seg = _safe_workspace_segment(workspace_id)
    if not seg:
        raise ValueError("workspace_id is required for workspace_root")
    return (repo_root / "workspaces" / seg).resolve()


def manager_palace_root(repo_root: Path) -> Path:
    """Cross-workspace MemPalace (Chroma) under ``.umbrella/palace/``."""
    return (repo_root / ".umbrella" / "palace").resolve()


def palace_path_for(repo_root: Path, workspace_id: str) -> Path:
    """Per-workspace palace dir, or manager palace when ``workspace_id`` is empty."""
    seg = _safe_workspace_segment(workspace_id)
    if not seg:
        return manager_palace_root(repo_root)
    return (workspace_memory_root(repo_root, seg) / "palace").resolve()


def hierarchical_root_for_palace(palace_dir: Path | str) -> Path:
    """Directory that holds ``ideas.jsonl`` for the given MemPalace data dir."""
    p = Path(palace_dir).resolve()
    if p.name != "palace":
        return p
    parent = p.parent
    if parent.name == ".memory":
        return parent
    # ``.umbrella/palace`` -> ``.umbrella/memory``
    return parent / "memory"


def get_workspace_store(repo_root: Path, workspace_id: str) -> "MemoryStore":
    """Return a :class:`MemoryStore` for manager (``workspace_id == ""``) or a workspace."""
    from umbrella.memory.models import MemoryConfig
    from umbrella.memory.store import MemoryStore

    seg = _safe_workspace_segment(workspace_id)
    if not seg:
        root = manager_memory_root(repo_root)
    else:
        root = workspace_memory_root(repo_root, seg)
    root.mkdir(parents=True, exist_ok=True)
    return MemoryStore(
        MemoryConfig(
            memory_root=root,
            lessons_path=root / "lessons.jsonl",
            gaps_path=root / "gaps.jsonl",
            signals_path=root / "signals.jsonl",
        )
    )


__all__ = [
    "get_workspace_store",
    "hierarchical_root_for_palace",
    "manager_core_root",
    "manager_memory_root",
    "manager_palace_root",
    "palace_path_for",
    "workspace_core_root",
    "workspace_memory_root",
]
